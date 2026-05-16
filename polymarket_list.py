#!/usr/bin/env python3
# polymarket_list.py — актуальные рынки с выбором источника даты окончания

from __future__ import annotations
import argparse, json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

GAMMA_BASE = "https://gamma-api.polymarket.com"
UA = {"User-Agent": "Hydra-Polymarket-List/1.2"}

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def iso(dt: datetime) -> str:
    return dt.isoformat()

def parse_array(raw) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str):
        s = raw.strip()
        if s.startswith("[") and s.endswith("]"):
            try:
                arr = json.loads(s)
                if isinstance(arr, list):
                    return [str(x) for x in arr]
            except Exception:
                pass
        return [s]
    return []

def get_market_end(m: Dict[str, Any]) -> Optional[str]:
    return m.get("end_date") or m.get("endDate") or m.get("endDateIso")

def get_event_end(m: Dict[str, Any]) -> Optional[str]:
    evs = m.get("events") or []
    if isinstance(evs, list) and evs:
        return evs[0].get("endDate") or evs[0].get("end_date")
    return None

def pick_chosen_end(m: Dict[str, Any], source: str) -> Optional[str]:
    me = get_market_end(m)
    ee = get_event_end(m)
    if source == "market":
        return me
    if source == "event":
        return ee
    # both → выберем event, если есть, иначе market
    return ee or me

async def fetch_markets(*, end_date_min: Optional[str], end_date_max: Optional[str],
                        limit: int = 1000, active: bool = True) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {
        "active": "true" if active else "false",
        "limit": limit,
    }
    if end_date_min:
        params["end_date_min"] = end_date_min
    if end_date_max:
        params["end_date_max"] = end_date_max

    async with httpx.AsyncClient(headers=UA, timeout=30) as client:
        r = await client.get(f"{GAMMA_BASE}/markets", params=params)
        r.raise_for_status()
        return r.json() or []

def row_from_market(m: Dict[str, Any], end_source: str) -> Dict[str, Any]:
    slug = m.get("slug") or m.get("question") or m.get("id")
    market_end = get_market_end(m) or ""
    event_end = get_event_end(m) or ""
    chosen_end = pick_chosen_end(m, end_source) or ""
    outcomes = parse_array(m.get("outcomes"))
    prices = parse_array(m.get("outcomePrices"))
    clobs = parse_array(m.get("clob_token_ids") or m.get("clobTokenIds"))
    return {
        "slug": slug,
        "market_end": market_end,
        "event_end": event_end,
        "chosen_end": chosen_end,
        "outcomes": "|".join(outcomes) if outcomes else "",
        "outcomePrices": "|".join(prices) if prices else "",
        "clob_tokens": len(clobs),
        "active": str(m.get("active", "")),
    }

def print_table(rows: List[Dict[str, Any]], max_rows: int):
    print("slug | market_end | event_end | chosen_end | outcomes | outcomePrices | clob_tokens | active")
    for i, r in enumerate(rows):
        if i >= max_rows:
            print(f"... ({len(rows)-max_rows} more)")
            break
        print(f"{r['slug']} | {r['market_end']} | {r['event_end']} | {r['chosen_end']} | "
              f"{r['outcomes']} | {r['outcomePrices']} | {r['clob_tokens']} | {r['active']}")

async def main():
    ap = argparse.ArgumentParser("List Polymarket markets with both market_end and event_end")
    ap.add_argument("--since", type=str, default=None, help="ISO начало окна (напр. 2025-08-20T00:00:00Z)")
    ap.add_argument("--until", type=str, default=None, help="ISO конец окна (напр. 2025-08-21T00:00:00Z)")
    ap.add_argument("--since-days", type=int, default=-1, help="Если ISO не задали: дни от сейчас для начала (по умолчанию: вчера)")
    ap.add_argument("--until-days", type=int, default=7, help="Если ISO не задали: дни от сейчас для конца (по умолчанию: +7)")
    ap.add_argument("--end-source", type=str, choices=["market","event","both"], default="market",
                    help="Какая дата считается датой окончания при сортировке/восприятии")
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--max-rows", type=int, default=200)
    ap.add_argument("--csv", type=str, default=None)
    args = ap.parse_args()

    # Построим окно
    if args.since and args.until:
        end_min = args.since
        end_max = args.until
    else:
        now = utcnow()
        start_dt = now + timedelta(days=args.since_days)
        end_dt = now + timedelta(days=args.until_days)
        end_min = iso(start_dt)
        end_max = iso(end_dt)

    markets = await fetch_markets(end_date_min=end_min, end_date_max=end_max, limit=args.limit, active=True)
    rows = [row_from_market(m, args.end_source) for m in markets]

    # Сортировка по выбранной дате
    rows.sort(key=lambda r: r["chosen_end"] or "")

    print(f"[INFO] window: {end_min} → {end_max}   (end-source={args.end_source})")
    print(f"[INFO] fetched: {len(rows)} markets")
    print_table(rows, args.max_rows)

    if args.csv:
        import csv
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=[
                "slug","market_end","event_end","chosen_end",
                "outcomes","outcomePrices","clob_tokens","active"
            ])
            w.writeheader()
            w.writerows(rows)
        print(f"[INFO] saved CSV -> {args.csv}")

if __name__ == "__main__":
    import asyncio
    raise SystemExit(asyncio.run(main()))
