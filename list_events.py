#!/usr/bin/env python3
# list_events.py — надёжный хелпер по событиям Polymarket (Gamma)
# - группирует активные рынки по событию (пытается вытащить slug из разных мест)
# - если явного события нет, умеет Fallback-группировку по ticker или questionID
# - считает уникальные исходы в событии и фильтрует по порогу (мульти)
# - показывает только актуалку (окно дат, опционально only-upcoming)
# - может вывести рынки выбранного события и сохранить агрегат в JSON
#
# Примеры:
#   python list_events.py --only-upcoming --min-outcomes 8 --max-rows 40
#   python list_events.py --since "2025-08-20T00:00:00Z" --until "2025-08-27T00:00:00Z" --min-outcomes 16 --only-upcoming
#   python list_events.py --only-upcoming --show-markets 2025-us-open-winner-m
#   python list_events.py --only-upcoming --dump-one
#   python list_events.py --only-upcoming --fallback-group questionID

from __future__ import annotations
import argparse, json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

import httpx

GAMMA = "https://gamma-api.polymarket.com"
UA = {"User-Agent": "Hydra-Polymarket-EventHelper/1.2"}

# ---------- utils ----------
def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def iso(dt: datetime) -> str:
    return dt.isoformat()

def parse_array(raw) -> List[str]:
    if raw is None: return []
    if isinstance(raw, list): return [str(x) for x in raw]
    if isinstance(raw, str):
        s = raw.strip()
        if s.startswith("[") and s.endswith("]"):
            try:
                arr = json.loads(s)
                if isinstance(arr, list): return [str(x) for x in arr]
            except Exception:
                pass
        return [s]
    return []

def market_end(m: Dict[str, Any]) -> str:
    return m.get("end_date") or m.get("endDate") or m.get("endDateIso") or ""

def event_slug_of(m: Dict[str, Any]) -> str:
    """Попытаться достать slug события из разных мест."""
    evs = m.get("events")
    if isinstance(evs, list) and evs:
        return evs[0].get("slug") or evs[0].get("ticker") or ""
    ev = m.get("event")
    if isinstance(ev, dict):
        return ev.get("slug") or ev.get("ticker") or ""
    # иногда бывает просто строковое поле:
    for k in ("eventSlug", "event_slug", "collectionSlug", "collection_slug"):
        v = m.get(k)
        if isinstance(v, str) and v:
            return v
    return ""

def event_end_of(m: Dict[str, Any]) -> str:
    evs = m.get("events")
    if isinstance(evs, list) and evs:
        return evs[0].get("endDate") or evs[0].get("end_date") or ""
    ev = m.get("event")
    if isinstance(ev, dict):
        return ev.get("endDate") or ev.get("end_date") or ""
    return ""

# ---------- http ----------
async def gamma_fetch_markets(*, end_date_min: Optional[str], end_date_max: Optional[str],
                              limit: int = 1000, active: bool = True,
                              timeout: float = 25.0) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {"active": "true" if active else "false", "limit": limit}
    if end_date_min: params["end_date_min"] = end_date_min
    if end_date_max: params["end_date_max"] = end_date_max
    async with httpx.AsyncClient(headers=UA, timeout=timeout) as c:
        r = await c.get(f"{GAMMA}/markets", params=params)
        r.raise_for_status()
        return r.json() or []

# ---------- model ----------
@dataclass
class EventAgg:
    event_slug: str
    event_end: str = ""
    markets: List[Dict[str, Any]] = field(default_factory=list)
    unique_outcomes: Set[str] = field(default_factory=set)
    total_markets: int = 0
    max_outcomes_in_market: int = 0

    def add_market(self, m: Dict[str, Any]):
        self.markets.append(m)
        outs = parse_array(m.get("outcomes"))
        for o in outs:
            self.unique_outcomes.add(o)
        self.total_markets += 1
        self.max_outcomes_in_market = max(self.max_outcomes_in_market, len(outs))
        if not self.event_end:
            self.event_end = event_end_of(m)

    @property
    def unique_outcomes_count(self) -> int:
        return len(self.unique_outcomes)

# ---------- core ----------
async def list_events(end_min: Optional[str], end_max: Optional[str], limit: int,
                      min_outcomes: int, max_rows: int, show_markets_for: Optional[str],
                      save_json: Optional[str], only_upcoming: bool,
                      dump_one: bool, fallback_group: str) -> None:
    markets = await gamma_fetch_markets(end_date_min=end_min, end_date_max=end_max,
                                        limit=limit, active=True)

    if dump_one:
        if markets:
            first = markets[0]
            print("[DUMP] First market keys:", ", ".join(sorted(first.keys())))
            preview = {
                "id": first.get("id"),
                "slug": first.get("slug"),
                "question": first.get("question"),
                "outcomes": first.get("outcomes"),
                "outcomePrices": first.get("outcomePrices"),
                "events": first.get("events"),
                "event": first.get("event"),
                "ticker": first.get("ticker"),
                "questionID": first.get("questionID"),
                "groupItemTitle": first.get("groupItemTitle"),
            }
            print("[DUMP] First market preview:\n",
                  json.dumps(preview, ensure_ascii=False, indent=2)[:2000])
        else:
            print("[DUMP] empty markets payload")

    # группировка по событию (с фоллбеком)
    agg: Dict[str, EventAgg] = {}
    missing_event = 0
    for m in markets:
        eslug = event_slug_of(m)
        if not eslug:
            missing_event += 1
            if fallback_group == "ticker":
                eslug = m.get("ticker") or ""
                if not eslug:
                    # грубый хак: укоротить market slug до «категории», если похоже
                    mslug = (m.get("slug") or "")
                    if "win-the" in mslug:
                        eslug = mslug.split("win-the", 1)[0].rstrip("-")
            elif fallback_group == "questionID":
                eslug = m.get("questionID") or ""
            # если всё равно пусто — пропускаем
            if not eslug:
                continue

        if eslug not in agg:
            agg[eslug] = EventAgg(event_slug=eslug)
        agg[eslug].add_market(m)

    # фильтрация по числу уникальных исходов (мульти)
    events = [v for v in agg.values() if v.unique_outcomes_count >= min_outcomes]

    # оставить только «актуальные» события: есть хотя бы один рынок end >= now
    if only_upcoming:
        now_iso = utcnow().isoformat()
        events = [e for e in events if any((market_end(m) or "") >= now_iso for m in e.markets)]

    # сортировка: по числу исходов (desc), затем по ближайшему market_end
    def soonest_end(e: EventAgg) -> str:
        ends = sorted([(market_end(m) or "") for m in e.markets])
        return ends[0] if ends else ""
    events.sort(key=lambda e: (e.unique_outcomes_count, soonest_end(e)), reverse=True)

    print(f"[INFO] window: {end_min} → {end_max}  active=true  markets={len(markets)}  "
          f"events={len(events)}  missing_event_fields={missing_event} (grouped by {fallback_group})")
    print("event_slug | unique_outcomes | total_markets | max_outcomes_in_market | event_end")

    for i, e in enumerate(events[:max_rows]):
        print(f"{e.event_slug} | {e.unique_outcomes_count} | {e.total_markets} | "
              f"{e.max_outcomes_in_market} | {e.event_end}")

    # детально показать рынки выбранного события
    if show_markets_for:
        target = agg.get(show_markets_for)
        if not target:
            print(f"\n[WARN] событие '{show_markets_for}' не найдено в этой выборке.")
            return
        print(f"\n[DETAIL] markets for event={show_markets_for} (count={len(target.markets)}):")
        print("market_slug | market_end | outcomes | outcomePrices | clob_tokens")
        for m in sorted(target.markets, key=lambda x: market_end(x) or ""):
            slug = m.get("slug") or m.get("question") or m.get("id")
            outnames = "|".join(parse_array(m.get("outcomes")))
            prices   = "|".join(parse_array(m.get("outcomePrices")))
            clobs    = len(parse_array(m.get("clob_token_ids") or m.get("clobTokenIds")))
            print(f"{slug} | {market_end(m)} | {outnames} | {prices} | {clobs}")

# ---------- cli ----------
async def main():
    ap = argparse.ArgumentParser("Polymarket event lister (robust grouping + multi filter)")
    # окно дат
    ap.add_argument("--since", type=str, default=None, help="ISO начало окна (напр. 2025-08-20T00:00:00Z)")
    ap.add_argument("--until", type=str, default=None, help="ISO конец окна (напр. 2025-08-27T00:00:00Z)")
    ap.add_argument("--since-days", type=int, default=-1, help="Если ISO не задали: дни от сейчас для начала (по умолчанию: вчера)")
    ap.add_argument("--until-days", type=int, default=14, help="Если ISO не задали: дни от сейчас для конца (по умолчанию: +14)")
    ap.add_argument("--limit", type=int, default=1000, help="max рынков с Gamma")
    # фильтры
    ap.add_argument("--min-outcomes", type=int, default=8, help="минимум уникальных исходов (мульти)")
    ap.add_argument("--only-upcoming", action="store_true", help="оставить события, где есть рынки с end >= now")
    # поведение
    ap.add_argument("--dump-one", action="store_true", help="вывести ключи и превью первого рынка (диагностика схемы)")
    ap.add_argument("--fallback-group", choices=["ticker","questionID","none"], default="ticker",
                    help="как группировать, если явного event нет (по умолчанию: ticker)")
    # вывод
    ap.add_argument("--max-rows", type=int, default=50, help="сколько событий печатать")
    ap.add_argument("--show-markets", type=str, default=None, help="показать рынки внутри выбранного event_slug")
    ap.add_argument("--save-json", type=str, default=None, help="сохранить агрегат по событиям в JSON")
    args = ap.parse_args()

    if args.since and args.until:
        end_min, end_max = args.since, args.until
    else:
        now = utcnow()
        end_min = iso(now + timedelta(days=args.since_days))
        end_max = iso(now + timedelta(days=args.until_days))

    await list_events(end_min=end_min, end_max=end_max, limit=args.limit,
                      min_outcomes=args.min_outcomes, max_rows=args.max_rows,
                      show_markets_for=args.show_markets, save_json=args.save_json,
                      only_upcoming=args.only_upcoming, dump_one=args.dump_one,
                      fallback_group=args.fallback_group)

if __name__ == "__main__":
    import asyncio
    raise SystemExit(asyncio.run(main()))

