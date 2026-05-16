#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
from typing import List, Dict, Any, Optional

# --- SDK: py-clob-client
try:
    from py_clob_client.client import ClobClient  # pip install py-clob-client
    CLOB = ClobClient(host="https://clob.polymarket.com")
except Exception as e:
    CLOB = None
    print("[!] py-clob-client not available or failed to init:", e)

# --- HTTP для фолбэка на Gamma (только если нет get_markets() у SDK)
try:
    from curl_cffi import requests as http  # pip install curl_cffi
    _HAS_CFFI = True
except Exception:
    import requests as http
    _HAS_CFFI = False

GAMMA_BASE = "https://gamma-api.polymarket.com"
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
}

def _get(url: str, params: dict | None = None):
    if _HAS_CFFI:
        with http.Session(impersonate="chrome120") as s:
            r = s.get(url, params=params, headers=DEFAULT_HEADERS, timeout=20)
            r.raise_for_status()
            return r.json()
    else:
        r = http.get(url, params=params, headers=DEFAULT_HEADERS, timeout=20)
        r.raise_for_status()
        return r.json()

def iso_parse(s: Optional[str]) -> Optional[dt.datetime]:
    if not s: return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    except Exception:
        return None

def fetch_markets_via_sdk(limit: int = 1000, offset: int = 0) -> List[Dict[str, Any]]:
    """
    Пытаемся вытащить рынки через py_clob_client, если в твоей версии есть метод get_markets().
    Если метода нет/кидает ошибку — пробрасываем исключение, чтобы вызвать фолбэк.
    """
    if CLOB is None:
        raise RuntimeError("CLOB client not initialized")

    get_markets = getattr(CLOB, "get_markets", None)
    if not callable(get_markets):
        raise AttributeError("ClobClient.get_markets() is not available in this SDK version")

    # большинство реализаций принимают limit/offset или params
    try:
        markets = get_markets(limit=limit, offset=offset)
    except TypeError:
        # на случай другой сигнатуры
        markets = get_markets(params={"limit": limit, "offset": offset})
    return markets or []

def fetch_markets_via_gamma(limit: int = 2000) -> List[Dict[str, Any]]:
    """
    Фолбэк на Gamma /markets с фильтрами, чтобы получить ТЕКУЩИЕ рынки.
    ВАЖНО: вызывать ровно /markets (без /markets/markets).
    """
    out: List[Dict[str, Any]] = []
    offset = 0
    page_size = 1000 if limit >= 1000 else limit

    while True:
        params = {
            "active": "true",     # только активные
            "closed": "false",    # не закрытые
            "limit": min(1000, page_size),
            "offset": offset,
        }
        page = _get(f"{GAMMA_BASE}/markets", params)
        if not isinstance(page, list) or not page:
            break
        out.extend(page)
        # если вернулась неполная страница — дальше нечего листать
        if len(page) < params["limit"]:
            break
        offset += params["limit"]
        if len(out) >= limit:
            break
    return out[:limit]

def normalize_market(m: Dict[str, Any]) -> Dict[str, Any]:
    """
    Приводим разные поля к единому виду: id/conditionId, slug, endDate (UTC).
    Работает и для SDK-ответов, и для Gamma.
    """
    cid = str(m.get("conditionId") or m.get("condition_id") or m.get("id") or "")
    slug = m.get("slug") or m.get("question") or ""
    end = m.get("endDate") or m.get("end_date")
    end_dt = iso_parse(end)
    active = bool(m.get("active", True))
    closed = bool(m.get("closed", False))
    return {
        "conditionId": cid,
        "slug": slug,
        "endDate": end_dt.isoformat() if end_dt else None,
        "active": active,
        "closed": closed,
    }

def main():
    ap = argparse.ArgumentParser(description="List Polymarket markets (minimal, no prices)")
    ap.add_argument("--limit", type=int, default=50, help="How many markets to show")
    ap.add_argument("--window-min", type=int, default=None, help="Only show markets ending in >= this many minutes")
    ap.add_argument("--window-max", type=int, default=None, help="Only show markets ending in <= this many minutes")
    args = ap.parse_args()

    # 1) Пытаемся через SDK
    markets_raw: List[Dict[str, Any]] = []
    sdk_ok = False
    if CLOB is not None:
        try:
            markets_raw = fetch_markets_via_sdk(limit=max(args.limit, 200))
            sdk_ok = True
            print("[i] fetched markets via py_clob_client.get_markets()")
        except Exception as e:
            print(f"[i] SDK get_markets() not available/failed: {e}; falling back to Gamma API")

    # 2) Фолбэк на Gamma
    if not sdk_ok:
        markets_raw = fetch_markets_via_gamma(limit=max(args.limit, 200))
        print("[i] fetched markets via Gamma API")

    # Нормализация и простая фильтрация по окну
    now = dt.datetime.now(dt.timezone.utc)
    markets = [normalize_market(m) for m in markets_raw]

    def mins_to_end(m) -> Optional[int]:
        if not m.get("endDate"): return None
        try:
            end = dt.datetime.fromisoformat(m["endDate"])
        except Exception:
            return None
        return int((end - now).total_seconds() / 60)

    # фильтр по окну
    filtered = []
    for m in markets:
        if not m.get("endDate"):
            continue
        mins = mins_to_end(m)
        if mins is None:
            continue
        if args.window_min is not None and mins < args.window_min:
            continue
        if args.window_max is not None and mins > args.window_max:
            continue
        filtered.append((mins, m))

    # сортируем по времени до конца
    filtered.sort(key=lambda x: x[0])

    # печатаем первые N
    print(f"\nFound {len(filtered)} markets in window; showing up to {args.limit}:\n")
    for i, (mins, m) in enumerate(filtered[:args.limit], start=1):
        print(f"[{i:02d}] {m['slug'][:80]}")
        print(f"     conditionId: {m['conditionId']}")
        print(f"     ends in:     {mins} min  (UTC end={m['endDate']})")
        print(f"     active/closed: {m['active']}/{m['closed']}\n")

if __name__ == "__main__":
    main()
