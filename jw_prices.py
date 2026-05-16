#!/usr/bin/env python3
# jw_prices.py — простой «прайс‑граббер» Polymarket (по мотивам Whittaker)
# Выводит текущие цены по исходам: best bid/ask и VWAP(asks) на shares

from __future__ import annotations
import argparse, json, asyncio
from typing import Any, Dict, List, Optional

import httpx

GAMMA = "https://gamma-api.polymarket.com"
CLOB  = "https://clob.polymarket.com"
UA    = {"User-Agent": "JW-PriceGrabber/1.0"}

def parse_array(raw) -> List[str]:
    if raw is None: return []
    if isinstance(raw, list): return [str(x) for x in raw]
    if isinstance(raw, str):
        s = raw.strip()
        if s.startswith("[") and s.endswith("]"):
            try:
                arr = json.loads(s);  # outcomes/outcomePrices/clobTokenIds часто приходят строкой JSON
                if isinstance(arr, list): return [str(x) for x in arr]
            except Exception:
                pass
        return [s]
    return []

async def gamma_active_markets(limit:int, contains:Optional[str], start_iso:Optional[str], end_iso:Optional[str]) -> List[Dict[str,Any]]:
    params: Dict[str,Any] = {"active":"true", "limit": limit}
    if start_iso: params["end_date_min"] = start_iso
    if end_iso:   params["end_date_max"] = end_iso
    async with httpx.AsyncClient(headers=UA, timeout=30) as c:
        r = await c.get(f"{GAMMA}/markets", params=params)
        r.raise_for_status()
        items = r.json() or []
    if contains:
        contains = contains.lower()
        items = [m for m in items if contains in (m.get("slug","")+m.get("question","")).lower()]
    return items

def best_of_book(book: Dict[str,Any]) -> Dict[str, Optional[float]]:
    bid = ask = None
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    if bids:
        try: bid = float(bids[0].get("price"))
        except: pass
    if asks:
        try: ask = float(asks[0].get("price"))
        except: pass
    return {"bid": bid, "ask": ask}

def vwap_from_asks(asks: List[Dict[str,str]], shares: float) -> Optional[float]:
    need, total = shares, 0.0
    for lvl in asks:
        try:
            p = float(lvl.get("price")); q = float(lvl.get("size"))
        except: 
            continue
        if q <= 0: continue
        take = min(q, need)
        total += p * take
        need  -= take
        if need <= 1e-12: return total
    return None

async def get_book(client: httpx.AsyncClient, token_id: str) -> Optional[Dict[str,Any]]:
    try:
        r = await client.get(f"{CLOB}/book", params={"token_id": token_id})
        if r.status_code != 200: return None
        return r.json() or {}
    except httpx.HTTPError:
        return None

async def main():
    ap = argparse.ArgumentParser("Polymarket price fetcher (best bid/ask + VWAP)")
    ap.add_argument("--limit", type=int, default=200, help="сколько рынков тянуть с Gamma")
    ap.add_argument("--contains", type=str, default=None, help="фильтр по подстроке в slug/question (например: tennis, president)")
    ap.add_argument("--since", type=str, default=None, help="фильтр Gamma: end_date_min (ISO)")
    ap.add_argument("--until", type=str, default=None, help="фильтр Gamma: end_date_max (ISO)")
    ap.add_argument("--shares", type=float, default=0.0, help="если >0, посчитает VWAP(asks) на столько шейров")
    ap.add_argument("--max-markets", type=int, default=50, help="сколько рынков выводить максимум (после фильтров)")
    args = ap.parse_args()

    markets = await gamma_active_markets(args.limit, args.contains, args.since, args.until)
    print(f"[INFO] fetched active markets: {len(markets)} (filter={args.contains or '—'}, window={args.since}→{args.until})")
    if not markets:
        return

    shown = 0
    async with httpx.AsyncClient(headers=UA, timeout=20) as c:
        for m in markets:
            if shown >= args.max_markets: break
            slug = m.get("slug") or m.get("question") or m.get("id")
            end  = m.get("end_date") or m.get("endDate") or ""
            outs = parse_array(m.get("outcomes"))
            clob = parse_array(m.get("clob_token_ids") or m.get("clobTokenIds"))
            if len(outs) != len(clob) or len(outs) == 0:
                continue

            print(f"\n[MARKET] {slug}  end={end}  outcomes={len(outs)}")
            for name, tid in zip(outs, clob):
                book = await get_book(c, tid)
                if not book:
                    print(f"  - {name:20s}  token={tid[:10]}…  book=N/A")
                    continue
                ba = best_of_book(book)
                line = f"  - {name:20s}  token={tid[:10]}…  bid={ba['bid']!s:<6}  ask={ba['ask']!s:<6}"
                if args.shares > 0:
                    vwap = vwap_from_asks(book.get('asks') or [], args.shares)
                    vw = f"{vwap:.6f}" if vwap is not None else "None"
                    line += f"  VWAP(asks,{args.shares})={vw}"
                print(line)

            shown += 1

if __name__ == "__main__":
    asyncio.run(main())
