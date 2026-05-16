#!/usr/bin/env python3
# polymarket_smoke.py — Gamma + CLOB smoke, с фильтрацией по дате и объёму

from __future__ import annotations
import asyncio, json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE  = "https://clob.polymarket.com"
UA = {"User-Agent": "Hydra-Polymarket-Smoke/Final/1.0"}

def now_utc() -> datetime: return datetime.now(timezone.utc)
def iso(dt: datetime) -> str: return dt.isoformat()
def log(m: str): print(m, flush=True)
def warn(m: str): print(f"[WARN] {m}", flush=True)
def err(m: str): print(f"[ERR]  {m}", flush=True)

def parse_array(raw) -> List[str]:
    if raw is None: return []
    if isinstance(raw, list): return [str(x) for x in raw]
    if isinstance(raw, str):
        s=raw.strip()
        if s.startswith("[") and s.endswith("]"):
            try:
                arr=json.loads(s)
                if isinstance(arr,list): return [str(x) for x in arr]
            except: pass
        return [s]
    return []

def parse_prices(raw) -> List[float]:
    return [float(x) for x in parse_array(raw) if _is_number(x)]

def _is_number(s: Any) -> bool:
    try: float(s); return True
    except: return False

def vwap_cost_from_asks(asks: List[Dict[str,str]], shares: float) -> Optional[float]:
    need, total = shares, 0.0
    for lvl in asks:
        try:
            p = float(lvl.get("price")); q = float(lvl.get("size"))
        except:
            continue
        if q <= 0: continue
        take=min(q, need)
        total += p * take
        need -= take
        if need <= 1e-12: return total
    return None

async def gamma_get_markets(*, end_max: Optional[str], limit: int = 1000) -> List[Dict[str,Any]]:
    params = {"active":"true", "limit": limit}
    if end_max: params["end_date_max"] = end_max
    async with httpx.AsyncClient(headers=UA,timeout=20) as c:
        log(f"[HTTP] GET {GAMMA_BASE}/markets params={params}")
        r = await c.get(f"{GAMMA_BASE}/markets", params=params)
        r.raise_for_status()
        return r.json() or []

async def clob_get_book(c: httpx.AsyncClient, token_id: str) -> Optional[Dict[str, Any]]:
    try:
        r = await c.get(f"{CLOB_BASE}/book", params={"token_id": token_id})
        if r.status_code != 200: return None
        return r.json() or {}
    except:
        return None

@dataclass
class MarketLite:
    slug: str
    end: datetime
    prices: List[float]
    clob_ids: List[str]

@dataclass
class Result:
    slug: str
    end: datetime
    gamma_price_sum: float
    gamma_edge: float
    vwap_sum: Optional[float]
    edge_exec: Optional[float]

def normalize(m:Dict[str,Any]) -> Optional[MarketLite]:
    slug = m.get("slug") or m.get("question") or m.get("id", "")
    end = m.get("end_date") or m.get("endDate")
    if not end: return None
    end_dt = datetime.fromisoformat(end.replace("Z","+00:00"))
    prices = parse_prices(m.get("outcomePrices"))
    clob_ids = parse_array(m.get("clob_token_ids") or m.get("clobTokenIds"))
    if len(prices) < 2 or len(clob_ids) < 2:
        return None
    return MarketLite(slug=slug, end=end_dt, prices=prices, clob_ids=clob_ids)

async def evaluate(markets: List[MarketLite], *, shares:float, fee_bps: float) -> List[Result]:
    sem = asyncio.Semaphore(20)
    fee = fee_bps/10000.0
    results: List[Result] = []
    async with httpx.AsyncClient(headers=UA,timeout=20) as c:
        async def eval_one(m: MarketLite) -> Result:
            gamma_sum = sum(m.prices)
            g_edge = 1.0 - gamma_sum
            # fetch per token
            tasks=[]
            for tid in m.clob_ids[:len(m.prices)]:
                tasks.append(fetch_cost(c, sem, tid, shares))
            costs = await asyncio.gather(*tasks)
            if any(x is None for x in costs):
                return Result(m.slug, m.end, gamma_sum, g_edge, None, None)
            vwap_sum=sum(costs)
            edge_exec = 1.0 - vwap_sum - fee
            return Result(m.slug, m.end, gamma_sum, g_edge, vwap_sum, edge_exec)
        results = await asyncio.gather(*[eval_one(m) for m in markets])
    return results

async def fetch_cost(client, sem, tid, shares):
    async with sem:
        b = await clob_get_book(client, tid)
    if not b: return None
    return vwap_cost_from_asks(b.get("asks", []), shares)

async def main():
    import argparse
    p=argparse.ArgumentParser("Final smoke")
    p.add_argument("--days", type=int, default=2, help="До какого момента — дни вперёд")
    p.add_argument("--limit", type=int, default=1000)
    p.add_argument("--shares", type=float, default=0.5)
    p.add_argument("--fee-bps", type=float, default=0.0)
    p.add_argument("--top", type=int, default=10)
    args = p.parse_args()

    end_max = iso(now_utc() + timedelta(days=args.days))
    raw = await gamma_get_markets(end_max=end_max, limit=args.limit)
    ml = [normalize(m) for m in raw]
    lm = [m for m in ml if m and m.end > now_utc()]
    log(f"[Discovery] {len(raw)} markets → {len(lm)} valid upcoming with prices & tokens")

    res = await evaluate(lm, shares=args.shares, fee_bps=args.fee_bps)
    res_ok=[r for r in res if r.edge_exec is not None]
    res_sorted=sorted(res_ok, key=lambda x: x.edge_exec, reverse=True)[:args.top]
    print("[Top Opportunities]")
    for r in res_sorted:
        print(f" {r.slug}\n  end={r.end}\n  Γ sum={r.gamma_price_sum:.6f} Γ edge={r.gamma_edge*100:.2f}%\n"
              f"  VWAP sum={r.vwap_sum:.6f} Exec edge={r.edge_exec*100:.2f}%\n")
    return

if __name__ == "__main__":
    import asyncio
    raise SystemExit(asyncio.run(main()))
