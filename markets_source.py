# markets_source.py
import time, datetime as dt
from typing import Dict, List, Optional, Tuple, Any

import requests
from dateutil import parser as dtparser

GAMMA_URL = "https://gamma-api.polymarket.com/markets"

def _parse_iso(s: Optional[str]) -> Optional[dt.datetime]:
    if not s:
        return None
    try:
        return dtparser.parse(s).astimezone(dt.timezone.utc)
    except Exception:
        return None

def _hours_left_from(obj: Dict) -> Optional[float]:
    for k in ("closes_at", "end_date", "end_date_iso", "endDate"):
        t = _parse_iso(obj.get(k))
        if t:
            return max(0.0, (t - dt.datetime.now(dt.timezone.utc)).total_seconds() / 3600.0)
    for k in ("hours_to_end", "timeLeftHours"):
        v = obj.get(k)
        try:
            if v is None:
                continue
            return float(v)
        except Exception:
            pass
    return None

def _tokens(obj: Dict) -> List[Dict]:
    raw = obj.get("tokens") or obj.get("outcomes") or []
    toks: List[Dict] = []
    if isinstance(raw, list):
        for t in raw:
            if not isinstance(t, dict):
                continue
            tid = t.get("token_id") or t.get("id") or t.get("tokenId")
            if tid:
                toks.append({"token_id": str(tid), **t})
    return toks

def _is_active_gamma(m: Dict) -> bool:
    if m.get("resolved") is True:
        return False
    if m.get("closed") is True:
        return False
    if m.get("archived") is True:
        return False
    if m.get("active") is False:
        return False
    if m.get("accepting_orders") is False:
        return False
    return True

def fetch_active_markets(limit: int = 250, max_pages: int = 40, sleep_sec: float = 0.12) -> Tuple[List[Dict], dict]:
    """
    Возвращает только активные рынки (не закрыты/не архив), с >=2 токенами и h>0.
    Источник: Gamma HTTP API (пагинация). Формат: список в корне.
    """
    out: List[Dict] = []
    stats = {"pages": 0, "raw": 0, "skipped_inactive": 0, "skipped_fewtokens": 0, "skipped_noh": 0}
    offset = 0
    headers = {
        "Accept": "application/json",
        "User-Agent": "pm-arb-sim/1.0",
    }

    for _ in range(max_pages):
        params = {
            "limit": limit,
            "offset": offset,
            "active": "true",
            "closed": "false",
            "archived": "false",
            "order": "liquidity",
        }
        r = requests.get(GAMMA_URL, params=params, headers=headers, timeout=20)
        r.raise_for_status()
        data = r.json()

        # У тебя в корне сразу список
        if not isinstance(data, list) or not data:
            break

        stats["pages"] += 1
        stats["raw"] += len(data)

        for m in data:
            if not isinstance(m, dict):
                continue
            if not _is_active_gamma(m):
                stats["skipped_inactive"] += 1
                continue
            toks = _tokens(m)
            if len(toks) < 2:
                stats["skipped_fewtokens"] += 1
                continue
            h = _hours_left_from(m)
            if h is None or h <= 0:
                stats["skipped_noh"] += 1
                continue
            out.append({
                "id": m.get("id") or m.get("condition_id"),
                "question": m.get("question") or m.get("title") or str(m.get("id") or m.get("condition_id")),
                "hours_left": float(h),
                "tokens": toks,
            })

        if len(data) < limit:
            break
        offset += limit
        time.sleep(sleep_sec)

    return out, stats
