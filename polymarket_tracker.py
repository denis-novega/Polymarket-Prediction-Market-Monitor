#!/usr/bin/env python3
# polymarket_tracker.py — автотрекер рассинхронов в одном мульти-событии
# Условия "гарантированного" окна: Σ VWAP(ask YES) < 1  => edge_buy = 1 - ΣVWAP - fee > 0

from __future__ import annotations
import asyncio, json, csv, time, random
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import httpx

GAMMA = "https://gamma-api.polymarket.com"
CLOB  = "https://clob.polymarket.com"
UA = {"User-Agent": "Hydra-Polymarket-Tracker/1.0"}

# ---------- utils ----------
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
        need  -= take
        if need <= 1e-12:
            return total
    return None

async def clob_get_book_retry(c: httpx.AsyncClient, token_id: str,
                              *, max_retries: int = 4, base_delay: float = 0.25) -> Optional[Dict[str, Any]]:
    attempt = 0
    while True:
        try:
            r = await c.get(f"{CLOB}/book", params={"token_id": token_id})
            if r.status_code == 200:
                return r.json() or {}
            if r.status_code in (429, 500, 502, 503, 504):
                attempt += 1
                if attempt > max_retries:
                    return None
                delay = base_delay * (2 ** (attempt - 1)) * (1 + random.random()*0.3)
                await asyncio.sleep(delay)
                continue
            return None
        except httpx.HTTPError:
            attempt += 1
            if attempt > max_retries:
                return None
            delay = base_delay * (2 ** (attempt - 1)) * (1 + random.random()*0.3)
            await asyncio.sleep(delay)

# ---------- discovery ----------
async def gamma_get_event_markets(event_slug: str, *, limit: int = 1000, timeout: float = 20.0) -> List[Dict[str,Any]]:
    """
    Простой способ: берём все активные рынки и фильтруем по events[0].slug == event_slug.
    (Gamma не всегда даёт удобный фильтр по событию, поэтому фильтруем на клиенте.)
    """
    params = {"active":"true", "limit": limit}
    async with httpx.AsyncClient(headers=UA, timeout=timeout) as c:
        r = await c.get(f"{GAMMA}/markets", params=params)
        r.raise_for_status()
        items = r.json() or []
    out = []
    for m in items:
        evs = m.get("events") or []
        if isinstance(evs, list) and evs:
            eslug = evs[0].get("slug") or evs[0].get("ticker") or ""
            if eslug == event_slug:
                out.append(m)
    return out

@dataclass
class Outcome:
    name: str
    token_id: str

@dataclass
class EventBundle:
    event_slug: str
    market_slug: str
    end: str
    outcomes: List[Outcome]  # обязательно 3+ для мульти

def build_event_bundle(markets: List[Dict[str,Any]], *, prefer_market_with_most: bool = True) -> Optional[EventBundle]:
    """
    Некоторые события представлены одним мульти-рынком (Кто выиграет?), другие — серией.
    Для трекера выберем 1 рынок с максимальным числом исходов (обычно «главный» мульти).
    """
    best = None
    best_cnt = 0
    for m in markets:
        outcomes = parse_array(m.get("outcomes"))
        clobs    = parse_array(m.get("clob_token_ids") or m.get("clobTokenIds"))
        if not outcomes or not clobs or len(outcomes) != len(clobs) or len(outcomes) < 3:
            continue
        if len(outcomes) > best_cnt:
            best = m; best_cnt = len(outcomes)
    if not best:
        return None
    evs = best.get("events") or []
    eslug = (evs[0].get("slug") if (isinstance(evs,list) and evs) else "") or ""
    market_slug = best.get("slug") or ""
    end = best.get("end_date") or best.get("endDate") or ""
    outcomes = parse_array(best.get("outcomes"))
    clobs    = parse_array(best.get("clob_token_ids") or best.get("clobTokenIds"))
    outs = [Outcome(name=o, token_id=t) for o,t in zip(outcomes, clobs)]
    return EventBundle(event_slug=eslug, market_slug=market_slug, end=end, outcomes=outs)

# ---------- tracker core ----------
async def compute_sigma_vwap(bundle: EventBundle, *, shares: float, fee_bps: float,
                             timeout: float, concurrency: int, max_outcomes: int) -> Tuple[Optional[float], List[Tuple[str,float]]]:
    fee = fee_bps / 10000.0
    outcomes = bundle.outcomes[:max_outcomes] if len(bundle.outcomes) > max_outcomes else bundle.outcomes
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(headers=UA, timeout=timeout) as c:
        async def leg_cost(out: Outcome) -> Optional[Tuple[str,float]]:
            async with sem:
                book = await clob_get_book_retry(c, out.token_id)
            if not book: return None
            asks = book.get("asks", [])
            if not asks: return None
            cost = vwap_cost_from_asks(asks, shares)
            if cost is None: return None
            return (out.name, cost)
        results = await asyncio.gather(*[leg_cost(o) for o in outcomes])
    if any(r is None for r in results):
        return (None, [])
    pairs = [(name, cost) for (name, cost) in results if name is not None]
    sigma = sum(cost for (_, cost) in pairs)
    sigma_with_fee = sigma + fee
    edge_buy = 1.0 - sigma_with_fee
    # Вернём edge_buy (после fee) и список вкладов
    return (edge_buy, pairs)

# ---------- CLI ----------
async def main():
    import argparse
    p = argparse.ArgumentParser("Polymarket multi-event tracker (ΣVWAP asks for all YES)")
    p.add_argument("--event-slug", required=True, help="Слаг события (events[0].slug), например: 2025-us-open-winner-m")
    p.add_argument("--shares", type=float, default=0.2, help="Шейров на исход")
    p.add_argument("--fee-bps", type=float, default=0.0, help="Комиссия (bps)")
    p.add_argument("--interval", type=float, default=5.0, help="Интервал обновления, сек")
    p.add_argument("--min-edge", type=float, default=0.005, help="Порог алерта: edge_buy >= min_edge (напр. 0.005 = 0.5%)")
    p.add_argument("--timeout", type=float, default=20.0)
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--max-outcomes", type=int, default=200, help="Безопасный лимит исходов для подсчёта")
    p.add_argument("--csv", type=str, default=None, help="Куда писать лог (CSV)")
    args = p.parse_args()

    # 1) discovery: рынки события
    mkts = await gamma_get_event_markets(args.event_slug, limit=1000, timeout=args.timeout)
    if not mkts:
        print(f"[ERR] не нашёл активных рынков для события: {args.event_slug}")
        return 2
    bundle = build_event_bundle(mkts)
    if not bundle:
        print(f"[ERR] событие найдено, но нет подходящего мульти-рынка (или outcomes/tokens не совпали).")
        return 3

    print(f"[TRACK] event={bundle.event_slug} market={bundle.market_slug} end={bundle.end} outcomes={len(bundle.outcomes)}")
    print(f"[TRACK] shares/leg={args.shares}  min_edge={args.min_edge*100:.2f}%  interval={args.interval}s")

    # 2) подготовка CSV
    writer = None
    fcsv = None
    if args.csv:
        fcsv = open(args.csv, "w", newline="", encoding="utf-8")
        writer = csv.writer(fcsv)
        writer.writerow(["ts","edge_buy","sigma_vwap_with_fee","fee_bps","shares","outcomes_used"])

    # 3) основной цикл
    try:
        while True:
            t0 = time.time()
            edge_buy, pairs = await compute_sigma_vwap(bundle, shares=args.shares, fee_bps=args.fee_bps,
                                                       timeout=args.timeout, concurrency=args.concurrency,
                                                       max_outcomes=args.max_outcomes)
            if edge_buy is None:
                print("[WARN] недостаточно глубины или книги недоступны (часть исходов без ask).")
            else:
                sigma_with_fee = 1.0 - edge_buy
                # топ-5 вкладов по стоимости
                top5 = sorted(pairs, key=lambda x: x[1], reverse=True)[:5]
                top_str = ", ".join([f"{name}:{cost:.4f}" for name,cost in top5])
                msg = (f"[EDGE] edge_buy={edge_buy*100:.2f}%  ΣVWAP+fee={sigma_with_fee:.6f}  "
                       f"legs={len(pairs)}  top={top_str}")
                print(msg)
                if writer:
                    writer.writerow([int(t0), f"{edge_buy:.6f}", f"{sigma_with_fee:.6f}",
                                     args.fee_bps, args.shares, len(pairs)])
                # алерт
                if edge_buy >= args.min_edge:
                    print(">>> [ALERT] BUY-ALL WINDOW! edge >= threshold — исполняемый арб доступен.")
            # ждём до следующей итерации
            dt = time.time() - t0
            await asyncio.sleep(max(0.0, args.interval - dt))
    finally:
        if fcsv:
            fcsv.close()

if __name__ == "__main__":
    import asyncio
    raise SystemExit(asyncio.run(main()))
