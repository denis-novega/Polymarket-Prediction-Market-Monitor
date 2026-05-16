#!/usr/bin/env python3
# polymarket_arb_sweep.py — сканер всех активных рынков: исполняемый арб через CLOB (GET /book)

from __future__ import annotations
import asyncio, json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE  = "https://clob.polymarket.com"
UA = {"User-Agent": "Hydra-Polymarket-ArbSweep/1.0"}

# ---------- helpers ----------
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

def vwap_cost_from_asks(asks: List[Dict[str,str]], shares: float) -> Optional[float]:
    need, total = shares, 0.0
    for lvl in asks:
        try:
            p = float(lvl.get("price")); q = float(lvl.get("size"))
        except Exception:
            continue
        if q <= 0: continue
        take = min(q, need)
        total += p * take
        need -= take
        if need <= 1e-12:
            return total
    return None

def market_end(m: Dict[str, Any]) -> str:
    return (m.get("end_date") or m.get("endDate") or m.get("endDateIso") or "")[:25]

def event_end(m: Dict[str, Any]) -> str:
    evs = m.get("events") or []
    if isinstance(evs, list) and evs:
        return (evs[0].get("endDate") or evs[0].get("end_date") or "")[:25]
    return ""

# ---------- HTTP ----------
async def gamma_list_active(limit: int = 1000) -> List[Dict[str, Any]]:
    """
    Запрашиваем активные рынки одной «страницей». Если вернётся ровно лимит,
    можно повторить с иным лимитом или добавить разбиение по end_date окнам.
    На практике 500–1000 покрывает актуальную «витрину».
    """
    params = {"active": "true", "limit": limit}
    async with httpx.AsyncClient(headers=UA, timeout=30) as c:
        r = await c.get(f"{GAMMA_BASE}/markets", params=params)
        r.raise_for_status()
        return r.json() or []

async def clob_get_book(c: httpx.AsyncClient, token_id: str) -> Optional[Dict[str, Any]]:
    try:
        r = await c.get(f"{CLOB_BASE}/book", params={"token_id": token_id})
        if r.status_code != 200:
            return None
        return r.json() or {}
    except httpx.HTTPError:
        return None

# ---------- models ----------
@dataclass
class MarketLite:
    slug: str
    m_end: str
    e_end: str
    token_ids: List[str]
    outcomes_cnt: int

@dataclass
class ExecArb:
    slug: str
    m_end: str
    e_end: str
    outcomes_cnt: int
    shares: float
    sum_vwap: Optional[float]
    edge_exec: Optional[float]

# ---------- core ----------
def normalize(m: Dict[str, Any]) -> Optional[MarketLite]:
    slug = m.get("slug") or m.get("question") or m.get("id") or ""
    clobs = parse_array(m.get("clob_token_ids") or m.get("clobTokenIds"))
    if len(clobs) < 2:
        return None
    outs = parse_array(m.get("outcomes"))
    oc = len(outs) if outs else (len(clobs) if len(clobs) >= 2 else 2)
    return MarketLite(slug=slug, m_end=market_end(m), e_end=event_end(m),
                      token_ids=clobs, outcomes_cnt=oc)

async def compute_exec_for_market(m: MarketLite, *, shares: float, fee_bps: float,
                                  client: httpx.AsyncClient, sem: asyncio.Semaphore,
                                  max_outcomes: int) -> ExecArb:
    fee = fee_bps / 10000.0
    tids = m.token_ids[:max_outcomes] if m.outcomes_cnt > max_outcomes else m.token_ids
    if len(tids) < 2:
        return ExecArb(m.slug, m.m_end, m.e_end, m.outcomes_cnt, shares, None, None)

    async def cost_for_token(tid: str) -> Optional[float]:
        async with sem:
            book = await clob_get_book(client, tid)
        if not book:
            return None
        return vwap_cost_from_asks(book.get("asks", []), shares)

    costs = await asyncio.gather(*[cost_for_token(t) for t in tids])
    if any(c is None for c in costs):
        return ExecArb(m.slug, m.m_end, m.e_end, m.outcomes_cnt, shares, None, None)
    sum_vwap = float(sum(costs))
    edge_exec = 1.0 - sum_vwap - fee
    return ExecArb(m.slug, m.m_end, m.e_end, m.outcomes_cnt, shares, sum_vwap, edge_exec)

async def sweep_active(*, shares: float, fee_bps: float, limit: int,
                       concurrency: int, max_outcomes: int, top: int) -> List[ExecArb]:
    raw = await gamma_list_active(limit=limit)
    markets = [normalize(m) for m in raw]
    markets = [m for m in markets if m is not None]
    print(f"[INFO] active markets fetched={len(raw)} normalized_with_tokens={len(markets)}")

    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(headers=UA, timeout=30) as client:
        tasks = [compute_exec_for_market(m, shares=shares, fee_bps=fee_bps,
                                         client=client, sem=sem, max_outcomes=max_outcomes)
                 for m in markets]
        results = await asyncio.gather(*tasks)

    ok = [r for r in results if r.sum_vwap is not None]
    ok.sort(key=lambda r: (r.edge_exec if r.edge_exec is not None else -999), reverse=True)
    return ok[:top]

# ---------- CLI ----------
async def main():
    import argparse
    p = argparse.ArgumentParser("Arb sweep over ALL active markets (CLOB GET /book, no date filters)")
    p.add_argument("--shares", type=float, default=0.5, help="Шейров на исход для VWAP")
    p.add_argument("--fee-bps", type=float, default=0.0, help="Комиссия (bps), вычитается из edge")
    p.add_argument("--limit", type=int, default=1000, help="Сколько рынков запросить у Gamma (active=true)")
    p.add_argument("--concurrency", type=int, default=24, help="Параллельных запросов к /book")
    p.add_argument("--max-outcomes", type=int, default=20, help="Ограничение исходов на рынок при VWAP")
    p.add_argument("--top", type=int, default=30, help="Сколько лучших арбов показать")
    args = p.parse_args()

    best = await sweep_active(shares=args.shares, fee_bps=args.fee_bps, limit=args.limit,
                              concurrency=args.concurrency, max_outcomes=args.max_outcomes, top=args.top)

    if not best:
        print("[RESULT] No executable opportunities found right now.")
        return 0

    print("[RESULT] Top executable opportunities across ALL active markets:")
    for r in best:
        print(f"  * {r.slug}\n"
              f"      market_end={r.m_end}  event_end={r.e_end}\n"
              f"      outcomes≈{r.outcomes_cnt}  shares/leg={r.shares}\n"
              f"      ΣVWAP={r.sum_vwap:.6f}  edge_exec={(r.edge_exec or 0)*100:.3f}%\n")
    return 0

if __name__ == "__main__":
    import asyncio
    raise SystemExit(asyncio.run(main()))
