#!/usr/bin/env python3
"""
Polymarket Arbitrage Scanner (Hydra Edition)
===========================================

A single-file, async Python scanner that:
  • Pulls events/markets from Polymarket Gamma API
  • Pulls order books from Polymarket CLOB API
  • Detects Dutch-book arbitrage:
      - Binary (SMP): YES+NO < 1
      - Multi-market (GMP / event-level): sum(YES across markets in the same event) < 1
  • Accounts for depth (VWAP fill) per outcome up to N shares (default 1)
  • Filters by end time window (e.g., last hour) and liquidity/volume
  • Outputs ranked opportunities with edge and expected fill quality

Notes
-----
• This file is self-contained. No DB required. You can dump results to CSV optionally.
• Requires: Python 3.10+ (WSL/micromamba OK)
• Install deps:
    pip install httpx==0.27.0 anyio==4.4.0 python-dateutil==2.9.0.post0 tenacity==8.5.0
• Optional (pretty table):
    pip install rich==13.7.1

API references used while implementing (check docs for latest):
• Gamma Markets API (events/markets): https://gamma-api.polymarket.com  
• CLOB REST endpoint: https://clob.polymarket.com
  - GET /book?token_id=...  (single)
  - POST /books            (batch)

Security/Fees
-------------
• CLOB fee schedule is subject to change. Default here assumes 0 bps trading fees
  (as currently shown in docs). You can override via CLI flag --fee-bps.
• This scanner only *detects* opportunities. Executing trades requires keys and the
  authenticated CLOB client; handle jurisdiction/TOS and risk.

Usage
-----
    python polymarket_scanner.py --last-hour --min-edge 0.02 --min-liquidity 2000 \
        --per-outcome-shares 1 --limit-events 200 --csv out.csv

Common examples
---------------
# scan last 60 minutes window, print top 20 opportunities
    python polymarket_scanner.py --last-hour --top 20

# scan broader window (next 24h) with higher min volume
    python polymarket_scanner.py --hours 24 --min-volume 10000 --min-edge 0.01

"""
from __future__ import annotations

import asyncio
import math
import os
import sys
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timezone, timedelta

import anyio
import httpx
from dateutil import parser as dtparser
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# Optional pretty console
try:
    from rich.console import Console
    from rich.table import Table
    from rich import box
    RICH = True
    console = Console()
except Exception:
    RICH = False
    console = None

# -----------------------
# Config / Defaults
# -----------------------
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

DEFAULT_HEADERS = {
    "User-Agent": "Hydra-Polymarket-Scanner/1.0 (+https://github.com)"
}

@dataclass
class ScannerConfig:
    hours: int = 1                    # time window forward from now (ignored if last_hour=True)
    last_hour: bool = True            # prioritize T-60m→T window
    min_edge: float = 0.01            # required net dutch-book edge (e.g., 0.01 = 1%)
    min_liquidity: float = 0.0        # Gamma liquidity filter (USDC)
    min_volume: float = 0.0           # Gamma volume filter (USDC)
    per_outcome_shares: float = 1.0   # how many shares to attempt to buy per outcome when computing VWAP
    top: int = 30                     # max rows printed
    fee_bps: float = 0.0              # trading fees in basis points (both sides), default 0
    limit_events: int = 200           # max events fetched
    limit_markets: int = 1000         # max markets fetched
    timeout: float = 15.0             # HTTP timeout seconds
    concurrency: int = 16             # how many parallel HTTP calls for books
    csv_path: Optional[str] = None
    debug: bool = False

# -----------------------
# HTTP utils
# -----------------------
class GammaClient:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, min=0.5, max=3),
           retry=retry_if_exception_type(httpx.HTTPError))
    async def get_events(self, *, active: bool = True, end_min: Optional[str] = None,
                         end_max: Optional[str] = None, limit: int = 200,
                         min_liquidity: float = 0.0, min_volume: float = 0.0) -> List[Dict[str, Any]]:
        params = {"active": str(active).lower(), "limit": limit}
        if end_min:
            params["end_date_min"] = end_min
        if end_max:
            params["end_date_max"] = end_max
        if min_liquidity > 0:
            params["liquidity_min"] = min_liquidity
        if min_volume > 0:
            params["volume_min"] = min_volume
        r = await self.client.get(f"{GAMMA_BASE}/events", params=params)
        r.raise_for_status()
        return r.json() or []

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, min=0.5, max=3),
           retry=retry_if_exception_type(httpx.HTTPError))
    async def get_markets(self, *, active: bool = True, end_min: Optional[str] = None,
                          end_max: Optional[str] = None, limit: int = 1000,
                          min_liquidity: float = 0.0, min_volume: float = 0.0) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"active": str(active).lower(), "limit": limit}
        # Only include filters if provided; sending empty keys yields 422 from Gamma
        if end_min:
            params["end_date_min"] = end_min
        if end_max:
            params["end_date_max"] = end_max
        if min_liquidity > 0:
            params["liquidity_num_min"] = min_liquidity
        if min_volume > 0:
            params["volume_num_min"] = min_volume
        # Remove any accidentally None values just in case
        params = {k: v for k, v in params.items() if v is not None and v != ""}
        r = await self.client.get(f"{GAMMA_BASE}/markets", params=params)
        r.raise_for_status()
        return r.json() or []

class ClobClient:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, min=0.5, max=3),
           retry=retry_if_exception_type(httpx.HTTPError))
    async def get_books(self, token_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """Batch fetch order books for token_ids. Returns dict token_id -> book."""
        # API: POST /books with body {"params": [{"token_id": "..."}, ...]}
        payload = {"params": [{"token_id": tid} for tid in token_ids]}
        r = await self.client.post(f"{CLOB_BASE}/books", json=payload)
        r.raise_for_status()
        data = r.json() or []
        out: Dict[str, Dict[str, Any]] = {}
        for book in data:
            tid = book.get("asset_id") or book.get("token_id")
            if tid:
                out[tid] = book
        return out

# -----------------------
# Core computations
# -----------------------

def _parse_iso(ts: str) -> datetime:
    return dtparser.isoparse(ts)

@dataclass
class MarketLite:
    condition_id: str
    event_id: Optional[int]
    event_slug: Optional[str]
    slug: Optional[str]
    end: datetime
    outcomes: List[str]
    clob_token_ids: List[str]  # [NO, YES] or general; we will map by outcomes
    volume_num: float
    liquidity_num: float

@dataclass
class Opportunity:
    kind: str  # "SMP"(binary) or "GMP"(multi)
    event_id: Optional[int]
    event_slug: Optional[str]
    market_slugs: List[str]
    ends_in_min: float
    shares_per_outcome: float
    cost_sum: float
    edge: float  # 1 - cost_sum
    details: Dict[str, Any]


def normalize_market(m: Dict[str, Any]) -> MarketLite:
    # Gamma fields can be camelCase or snake_case depending on route/version
    cond = m.get("condition_id") or m.get("conditionId") or m.get("conditionid") or m.get("id")
    outcomes = m.get("outcomes") or []
    # token ids can be clob_token_ids or clobTokenIds
    clobs = m.get("clob_token_ids") or m.get("clobTokenIds") or []
    # event mapping: try nested events list, else None
    ev_id = None
    ev_slug = None
    if isinstance(m.get("events"), list) and m["events"]:
        ev = m["events"][0]
        ev_id = ev.get("id")
        ev_slug = ev.get("slug") or ev.get("name")
    end_raw = m.get("end_date") or m.get("endDate") or m.get("end_date_iso") or m.get("endDateIso")
    end_dt = _parse_iso(end_raw) if end_raw else datetime.now(timezone.utc) + timedelta(days=365)

    vol = float(m.get("volume_num") or m.get("volumeNum") or m.get("volume") or 0)
    liq = float(m.get("liquidity_num") or m.get("liquidityNum") or m.get("liquidity") or 0)

    return MarketLite(
        condition_id=str(cond),
        event_id=ev_id,
        event_slug=ev_slug,
        slug=m.get("slug"),
        end=end_dt,
        outcomes=[str(x) for x in outcomes],
        clob_token_ids=[str(x) for x in clobs],
        volume_num=vol,
        liquidity_num=liq,
    )


def vwap_cost_to_buy(asks: List[Dict[str, str]], shares: float) -> Optional[float]:
    """Compute VWAP cost to buy `shares` from ask levels. Returns total USDC or None if insufficient depth."""
    remaining = shares
    total_cost = 0.0
    for lvl in asks:
        try:
            price = float(lvl["price"])  # in USDC per share (0..1)
            size = float(lvl["size"])    # shares available at that price
        except Exception:
            continue
        if size <= 0:
            continue
        take = min(size, remaining)
        total_cost += price * take
        remaining -= take
        if remaining <= 1e-12:
            return total_cost
    return None  # not enough depth


def pick_yes_token(m: MarketLite) -> Optional[str]:
    """Return token id corresponding to outcome 'Yes' (case-insensitive). Fallback to index 1 if two outcomes."""
    # Try to find explicit Yes mapping
    idx = None
    for i, name in enumerate(m.outcomes):
        if str(name).strip().lower() == "yes":
            idx = i
            break
    if idx is None:
        # Heuristic: if binary [No, Yes] common ordering, take index 1
        if len(m.clob_token_ids) == 2:
            idx = 1
        else:
            return None
    if idx >= len(m.clob_token_ids):
        return None
    return m.clob_token_ids[idx]


def pick_no_token(m: MarketLite) -> Optional[str]:
    idx = None
    for i, name in enumerate(m.outcomes):
        if str(name).strip().lower() == "no":
            idx = i
            break
    if idx is None:
        if len(m.clob_token_ids) == 2:
            idx = 0
        else:
            return None
    if idx >= len(m.clob_token_ids):
        return None
    return m.clob_token_ids[idx]


async def detect_opportunities(cfg: ScannerConfig) -> List[Opportunity]:
    now = datetime.now(timezone.utc)
    end_min = now.isoformat()
    end_max = (now + timedelta(hours=1)).isoformat() if cfg.last_hour else (now + timedelta(hours=cfg.hours)).isoformat()

    async with httpx.AsyncClient(timeout=cfg.timeout, headers=DEFAULT_HEADERS) as client:
        gamma = GammaClient(client)
        clob = ClobClient(client)

        # 1) Fetch recent/soon-to-end markets; we group by event later
        markets_raw = await gamma.get_markets(
            active=True,
            end_min=end_min,
            end_max=end_max,
            limit=cfg.limit_markets,
            min_liquidity=cfg.min_liquidity,
            min_volume=cfg.min_volume,
        )
        markets = [normalize_market(m) for m in markets_raw]
        # Keep only markets that have clob tokens
        markets = [m for m in markets if m.clob_token_ids]

        # 2) Group by event (GMP) and also keep singletons (SMP)
        groups: Dict[Optional[int], List[MarketLite]] = {}
        for m in markets:
            groups.setdefault(m.event_id or -1, []).append(m)

        # 3) Precompute the token ids we need for books
        token_ids: List[str] = []
        for ev_id, ms in groups.items():
            if len(ms) == 1:
                # SMP: need both sides
                no_t = pick_no_token(ms[0])
                yes_t = pick_yes_token(ms[0])
                for t in (no_t, yes_t):
                    if t:
                        token_ids.append(t)
            else:
                # GMP: need YES token for each market in the event
                for m in ms:
                    t = pick_yes_token(m)
                    if t:
                        token_ids.append(t)
        # dedupe
        token_ids = sorted(set([t for t in token_ids if t]))

        # 4) Fetch all required books in batches to respect concurrency
        books: Dict[str, Dict[str, Any]] = {}
        CHUNK = 80  # tune if needed
        for i in range(0, len(token_ids), CHUNK):
            chunk = token_ids[i:i+CHUNK]
            try:
                books.update(await clob.get_books(chunk))
            except httpx.HTTPStatusError as e:
                if console:
                    console.print(f"[red]books batch failed {e} for chunk {i}..{i+CHUNK}")

        # 5) Compute opportunities
        opps: List[Opportunity] = []
        fee = cfg.fee_bps / 10000.0
        for ev_id, ms in groups.items():
            # Skip if we cannot compute books for required tokens
            if len(ms) == 1:
                m = ms[0]
                yes_t, no_t = pick_yes_token(m), pick_no_token(m)
                if not (yes_t and no_t and yes_t in books and no_t in books):
                    continue
                yes_cost = vwap_cost_to_buy(books[yes_t].get("asks", []), cfg.per_outcome_shares)
                no_cost  = vwap_cost_to_buy(books[no_t].get("asks", []), cfg.per_outcome_shares)
                if yes_cost is None or no_cost is None:
                    continue
                cost_sum = yes_cost + no_cost
                # conservative fee: apply on the winning leg proceeds (≈1 per share)
                net_edge = 1.0 - cost_sum - fee
                if net_edge >= cfg.min_edge:
                    ends_in = max((m.end - now).total_seconds()/60.0, 0.0)
                    opps.append(Opportunity(
                        kind="SMP",
                        event_id=m.event_id,
                        event_slug=m.event_slug,
                        market_slugs=[m.slug or m.condition_id],
                        ends_in_min=ends_in,
                        shares_per_outcome=cfg.per_outcome_shares,
                        cost_sum=cost_sum,
                        edge=net_edge,
                        details={
                            "condition_id": m.condition_id,
                            "yes_token": yes_t,
                            "no_token": no_t,
                            "yes_best_ask": (books[yes_t].get("asks", [])[:1] or [None])[0],
                            "no_best_ask":  (books[no_t].get("asks", [])[:1] or [None])[0],
                        }
                    ))
            else:
                # GMP: buy YES across every market in event
                cost_sum = 0.0
                tokens_needed: List[str] = []
                market_slugs: List[str] = []
                ok = True
                for m in ms:
                    t = pick_yes_token(m)
                    if not (t and t in books):
                        ok = False
                        break
                    c = vwap_cost_to_buy(books[t].get("asks", []), cfg.per_outcome_shares)
                    if c is None:
                        ok = False
                        break
                    cost_sum += c
                    tokens_needed.append(t)
                    market_slugs.append(m.slug or m.condition_id)
                if not ok:
                    continue
                # Dutch-book edge for the event
                net_edge = 1.0 - cost_sum - fee
                # require at least 2 markets (already true) and edge threshold
                if net_edge >= cfg.min_edge:
                    ends = sorted(ms, key=lambda x: x.end)[0].end
                    ends_in = max((ends - now).total_seconds()/60.0, 0.0)
                    opps.append(Opportunity(
                        kind="GMP",
                        event_id=ms[0].event_id,
                        event_slug=ms[0].event_slug,
                        market_slugs=market_slugs,
                        ends_in_min=ends_in,
                        shares_per_outcome=cfg.per_outcome_shares,
                        cost_sum=cost_sum,
                        edge=net_edge,
                        details={
                            "yes_tokens": tokens_needed,
                        }
                    ))
        # sort by edge desc then sooner end
        opps.sort(key=lambda o: (-o.edge, o.ends_in_min))
        return opps


# -----------------------
# CLI & Rendering
# -----------------------
# -----------------------
# CLI & Rendering
# -----------------------
import argparse

def human_pct(x: float) -> str:
    return f"{x*100:.2f}%"

def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Polymarket Arbitrage Scanner (Hydra)")
    p.add_argument("--hours", type=int, default=1, help="Forward window in hours (ignored if --last-hour)")
    p.add_argument("--last-hour", action="store_true", help="Scan last 60 minutes to end time (T-60→T)")
    p.add_argument("--no-last-hour", action="store_true", help="Disable last-hour mode; use --hours instead")
    p.add_argument("--min-edge", type=float, default=0.01, help="Minimum net edge (e.g. 0.01 = 1%)")
    p.add_argument("--min-liquidity", type=float, default=0.0, help="Gamma liquidity_min filter (USDC)")
    p.add_argument("--min-volume", type=float, default=0.0, help="Gamma volume_min filter (USDC)")
    p.add_argument("--per-outcome-shares", type=float, default=1.0, help="Shares to attempt per outcome (depth check)")
    p.add_argument("--limit-events", type=int, default=200, help="Max events fetched (hint only; markets drive grouping)")
    p.add_argument("--limit-markets", type=int, default=1000, help="Max markets fetched")
    p.add_argument("--fee-bps", type=float, default=0.0, help="Trading fee in bps; conservative edge adjustment")
    p.add_argument("--top", type=int, default=30, help="Top N opportunities to print")
    p.add_argument("--csv", type=str, default=None, help="Optional CSV output path")
    p.add_argument("--debug", action="store_true", help="Verbose diagnostics")

    args = p.parse_args(argv)

    cfg = ScannerConfig(
        hours=args.hours,
        last_hour=True if args.last_hour and not args.no_last_hour else (False if args.no_last_hour else True),
        min_edge=args.min_edge,
        min_liquidity=args.min_liquidity,
        min_volume=args.min_volume,
        per_outcome_shares=args.per_outcome_shares,
        top=args.top,
        fee_bps=args.fee_bps,
        limit_events=args.limit_events,
        limit_markets=args.limit_markets,
        csv_path=args.csv,
        debug=args.debug,
    )

    opps = asyncio.run(detect_opportunities(cfg))

    if cfg.debug:
        print(f"[DEBUG] found {len(opps)} opportunities before top filter")

    if not opps:
        print("No opportunities meeting criteria.")
        return 0

    # Render
    rows = []
    for o in opps[: cfg.top]:
        rows.append({
            "Type": o.kind,
            "Event": o.event_slug or str(o.event_id),
            "Ends in": f"{o.ends_in_min:.1f}m",
            "#Mkts": len(o.market_slugs),
            "Shares/leg": o.shares_per_outcome,
            "Cost": f"{o.cost_sum:.4f}",
            "Edge": human_pct(o.edge),
            "Markets": ", ".join(o.market_slugs[:3]) + ("…" if len(o.market_slugs) > 3 else ""),
        })

    if RICH:
        t = Table(title="Polymarket Arbitrage — Hydra", box=box.MINIMAL_DOUBLE_HEAD)
        for col in ["Type", "Event", "Ends in", "#Mkts", "Shares/leg", "Cost", "Edge", "Markets"]:
            t.add_column(col)
        for r in rows:
            t.add_row(r["Type"], str(r["Event"]), r["Ends in"], str(r["#Mkts"]),
                      str(r["Shares/leg"]), r["Cost"], r["Edge"], r["Markets"])
        console.print(t)
    else:
        # Fallback plain text
        print("Type | Event | EndsIn | #Mkts | Shares/leg | Cost | Edge | Markets")
        for r in rows:
            print(f"{r['Type']} | {r['Event']} | {r['Ends in']} | {r['#Mkts']} | {r['Shares/leg']} | {r['Cost']} | {r['Edge']} | {r['Markets']}")

    # CSV output if requested
    if cfg.csv_path:
        try:
            import csv
            with open(cfg.csv_path, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                w.writeheader()
                for r in rows:
                    w.writerow(r)
            print(f"Saved CSV -> {cfg.csv_path}")
        except Exception as e:
            print(f"CSV save failed: {e}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
