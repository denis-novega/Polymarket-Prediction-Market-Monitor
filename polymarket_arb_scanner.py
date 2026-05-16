#!/usr/bin/env python3
"""
Polymarket Arbitrage Scanner — standalone CLI (fixed HTTP + batching)

What it does
------------
• Pulls markets metadata from the Gamma Markets API.
• Pulls binary market token pairs (YES/NO) from the CLOB "simplified-markets" endpoint.
• Pulls top-of-book prices (best bid/ask) and, when needed, full order books.
• Detects intra-market Dutch-book arbitrage on binary markets:
    - LONG arb (buy YES+NO when ask_yes + ask_no < 1 - margin)
    - SHORT arb (mint full set via CTF then sell YES+NO to bids when bid_yes + bid_no > 1 + margin)
• Depth-aware sizing to estimate how many pairs you can fill at or better than the threshold.

This version includes fixes for:
• 422 from Gamma (/markets): remove unsupported query params, avoid /markets/markets.
• 413/400 from CLOB (/prices): numeric token_id filtering + adaptive batching + headers.
• CamelCase fields from Gamma (conditionId, endDate) + robust float parsing of liquidity/volume.

Requirements
-----------
Python 3.10+

pip install requests

Usage examples
--------------
# One-off scan with defaults (focus on markets ending within next 120 minutes)
python polymarket_arb_scanner.py --window-min 0 --window-max 120 --min-edge-bps 20 --min-trade 1000

# Continuous scan every 10 seconds, show top 10 opps
python polymarket_arb_scanner.py --loop 10 --top 10

Notes
-----
• This scanner only flags opportunities. It does not place orders.
• Prices are in USDC units (1.0 == $1). Edge is reported in basis points and cents per pair.
• Respect rate limits.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import time
from typing import Dict, List, Tuple, Optional
from py_clob_client.client import ClobClient

CLOB_CLIENT = ClobClient(host="https://clob.polymarket.com")

# ---- HTTP client: prefer curl_cffi (Cloudflare-friendly), fallback to requests
try:
    from curl_cffi import requests as http  # pip install curl_cffi
    _HAS_CFFI = True
except Exception:
    import requests as http  # fallback
    _HAS_CFFI = False

# ---- Polymarket CLOB client (official)
try:
    from py_clob_client.client import ClobClient, BookParams  # pip install py-clob-client
    CLOB_CLIENT = ClobClient(host="https://clob.polymarket.com")  # no 'network' arg in your version
except Exception:
    ClobClient = None  # type: ignore
    BookParams = None  # type: ignore
    CLOB_CLIENT = None  # will be unused if you don't call SDK paths

# ---------- Configuration ----------
GAMMA_BASE = "https://gamma-api.polymarket.com"   # DO NOT append /markets here
CLOB_BASE = "https://clob.polymarket.com"

DEFAULT_HEADERS = {
    # Cloudflare-friendly browser-ish headers
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://polymarket.com",
    "Referer": "https://polymarket.com/",
}

# ---- Thin HTTP wrappers using the chosen client
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

def _post(url: str, json_body: dict):
    if _HAS_CFFI:
        with http.Session(impersonate="chrome120") as s:
            r = s.post(url, json=json_body, headers=DEFAULT_HEADERS, timeout=20)
            r.raise_for_status()
            return r.json()
    else:
        r = http.post(url, json=json_body, headers=DEFAULT_HEADERS, timeout=20)
        r.raise_for_status()
        return r.json()

# ---------- Data Models ----------
@dataclasses.dataclass
class Token:
    token_id: str  # ERC1155 id used by CLOB (decimal string)
    symbol: str    # "YES" or "NO" if known (fallback: index 0/1)

@dataclasses.dataclass
class Market:
    condition_id: str
    slug: Optional[str]
    active: bool
    closed: bool
    end_date: Optional[dt.datetime]
    liquidity: Optional[float]
    volume: Optional[float]
    tokens: Tuple[Token, Token]  # (YES, NO)

@dataclasses.dataclass
class TopOfBook:
    ask: Optional[float]
    bid: Optional[float]

@dataclasses.dataclass
class BookLevel:
    price: float
    size: float

@dataclasses.dataclass
class OrderBook:
    asks: List[BookLevel]
    bids: List[BookLevel]

@dataclasses.dataclass
class Opportunity:
    kind: str  # "LONG" or "SHORT"
    market: Market
    edge_cents: float
    edge_bps: float
    sum_price: float
    size_pairs: float  # estimated max pairs (YES+NO) at/better than threshold
    notional: float    # in USDC
    mins_to_end: Optional[int]


# ---------- Gamma + CLOB fetchers ----------

def fetch_gamma_markets(limit: int = 5000) -> List[dict]:
    """Paginate Gamma /markets. No active/closed filters (avoid 422)."""
    out: List[dict] = []
    offset = 0
    while True:
        params = {
            "limit": min(1000, limit),
            "offset": offset,
        }
        page = _get(f"{GAMMA_BASE}/markets", params)
        if not isinstance(page, list) or not page:
            break
        out.extend(page)
        if len(page) < params["limit"]:
            break
        offset += params["limit"]
        if len(out) >= limit:
            break
    return out


def fetch_simplified_markets() -> List[dict]:
    """Paginate CLOB /simplified-markets (returns YES/NO token pairs)."""
    out: List[dict] = []
    cursor = ""
    while True:
        data = _get(f"{CLOB_BASE}/simplified-markets", {"next_cursor": cursor} if cursor else None)
        if not isinstance(data, dict) or "data" not in data:
            break
        out.extend(data["data"])  # each has condition_id, tokens[2], active, closed
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return out

# ---------- Mapping & Merging ----------

def normalize_iso8601(s: Optional[str]) -> Optional[dt.datetime]:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    except Exception:
        return None


def build_market_index() -> Dict[str, Market]:
    """Join Gamma /markets with CLOB /simplified-markets by conditionId."""
    gamma = fetch_gamma_markets()
    simp = fetch_simplified_markets()

    # Index Gamma by conditionId/id
    gamma_by_cid: Dict[str, dict] = {}
    for g in gamma:
        cid = str(g.get("conditionId") or g.get("condition_id") or g.get("id") or "")
        if cid:
            gamma_by_cid[cid] = g

    def _to_float(x):
        try:
            return float(x)
        except Exception:
            return 0.0

    merged: Dict[str, Market] = {}
    for sm in simp:
        cid = str(sm.get("condition_id") or sm.get("conditionId") or "")
        if not cid:
            continue
        g = gamma_by_cid.get(cid, {})
        end_date = normalize_iso8601(g.get("endDate") or g.get("end_date"))
        slug = g.get("slug")
        liquidity = _to_float(g.get("liquidity"))
        volume = _to_float(g.get("volume"))

        # Identify YES/NO tokens
        tokens_raw = sm.get("tokens", [])
        t0 = tokens_raw[0] if len(tokens_raw) > 0 else {}
        t1 = tokens_raw[1] if len(tokens_raw) > 1 else {}

        def _sym(tok: dict, fallback: str) -> str:
            sym = tok.get("symbol") or tok.get("outcome") or tok.get("type")
            if isinstance(sym, str) and sym.upper() in {"YES", "NO"}:
                return sym.upper()
            return fallback

        tok0 = Token(token_id=str(t0.get("token_id") or t0.get("id") or ""), symbol=_sym(t0, "YES"))
        tok1 = Token(token_id=str(t1.get("token_id") or t1.get("id") or ""), symbol=_sym(t1, "NO"))

        # Ensure (YES, NO) ordering if labels available
        yes, no = (tok0, tok1)
        if tok0.symbol == "NO" and tok1.symbol == "YES":
            yes, no = tok1, tok0

        m = Market(
            condition_id=cid,
            slug=slug,
            active=bool(sm.get("active", True)),
            closed=bool(sm.get("closed", False)),
            end_date=end_date,
            liquidity=liquidity,
            volume=volume,
            tokens=(yes, no),
        )
        merged[cid] = m

    return merged

# ---------- Pricing ----------

def get_top_prices(token_ids: List[str]) -> Dict[str, TopOfBook]:
    """
    Получаем best bid/ask ТОЛЬКО через официальный SDK (без REST /prices).
    Делаем батчами через get_order_books; если метод/батч недоступен — фолбэк на поштучный get_order_book.
    """
    out: Dict[str, TopOfBook] = {}

    # валидные числовые id + дедуп
    ids = [tid for tid in dict.fromkeys(token_ids) if isinstance(tid, str) and tid.isdigit()]
    if not ids:
        return out

    # --- Попытка: батчевый вызов (если у твоей версии SDK есть get_order_books)
    try:
        # пробуем небольшими партиями, чтобы не уткнуться в лимиты
        BATCH = 60
        for i in range(0, len(ids), BATCH):
            chunk = ids[i:i+BATCH]
            books = CLOB_CLIENT.get_order_books(params=[{"token_id": tid} for tid in chunk])  # SDK: список книг
            # ожидается: каждый элемент содержит token_id/bids/asks
            for book in books:
                tid = str(book.get("token_id") or book.get("tokenId") or "")
                bids = book.get("bids") or []
                asks = book.get("asks") or []
                best_bid = float(bids[0]["price"]) if bids else None
                best_ask = float(asks[0]["price"]) if asks else None
                if tid:
                    out[tid] = TopOfBook(ask=best_ask, bid=best_bid)
        return out
    except Exception:
        # Если у твоей версии SDK нет get_order_books или вернулся 400 — падаем в поштучный режим.
        pass

    # --- Фолбэк: поштучные книги
    for tid in ids:
        try:
            book = CLOB_CLIENT.get_order_book(tid)
            bids = book.get("bids") or []
            asks = book.get("asks") or []
            best_bid = float(bids[0]["price"]) if bids else None
            best_ask = float(asks[0]["price"]) if asks else None
            out[tid] = TopOfBook(ask=best_ask, bid=best_bid)
        except Exception:
            # просто пропускаем «битые» id
            continue

    return out


def get_order_book(token_id: str) -> OrderBook:
    """
    Полная книга для оценки глубины — через SDK (без REST /book).
    """
    book = CLOB_CLIENT.get_order_book(token_id)
    asks = [BookLevel(price=float(x["price"]), size=float(x["size"])) for x in (book.get("asks") or [])]
    bids = [BookLevel(price=float(x["price"]), size=float(x["size"])) for x in (book.get("bids") or [])]
    asks.sort(key=lambda x: x.price)
    bids.sort(key=lambda x: -x.price)
    return OrderBook(asks=asks, bids=bids)

# ---------- Depth fill helpers ----------

def depth_capacity_long(ask_yes: List[BookLevel], ask_no: List[BookLevel], max_pair_price: float, max_pairs: float | None = None) -> Tuple[float, float]:
    i = j = 0
    filled_pairs = 0.0
    total_cost = 0.0
    while i < len(ask_yes) and j < len(ask_no):
        py = ask_yes[i].price
        pn = ask_no[j].price
        pair_price = py + pn
        if pair_price > max_pair_price + 1e-9:
            break
        lot = min(ask_yes[i].size, ask_no[j].size)
        if max_pairs is not None:
            lot = min(lot, max_pairs - filled_pairs)
        if lot <= 0:
            break
        filled_pairs += lot
        total_cost += lot * pair_price
        ask_yes[i].size -= lot
        ask_no[j].size -= lot
        if ask_yes[i].size <= 1e-12:
            i += 1
        if ask_no[j].size <= 1e-12:
            j += 1
        if max_pairs is not None and filled_pairs >= max_pairs - 1e-12:
            break
    avg = (total_cost / filled_pairs) if filled_pairs > 0 else 0.0
    return filled_pairs, avg


def depth_capacity_short(bid_yes: List[BookLevel], bid_no: List[BookLevel], min_pair_price: float, max_pairs: float | None = None) -> Tuple[float, float]:
    i = j = 0
    filled_pairs = 0.0
    total_rev = 0.0
    while i < len(bid_yes) and j < len(bid_no):
        py = bid_yes[i].price
        pn = bid_no[j].price
        pair_price = py + pn
        if pair_price < min_pair_price - 1e-9:
            break
        lot = min(bid_yes[i].size, bid_no[j].size)
        if max_pairs is not None:
            lot = min(lot, max_pairs - filled_pairs)
        if lot <= 0:
            break
        filled_pairs += lot
        total_rev += lot * pair_price
        bid_yes[i].size -= lot
        bid_no[j].size -= lot
        if bid_yes[i].size <= 1e-12:
            i += 1
        if bid_no[j].size <= 1e-12:
            j += 1
        if max_pairs is not None and filled_pairs >= max_pairs - 1e-12:
            break
    avg = (total_rev / filled_pairs) if filled_pairs > 0 else 0.0
    return filled_pairs, avg

# ---------- Scanner ----------

def scan_once(
    min_edge_bps: float = 20.0,
    min_liquidity: float = 0.0,
    min_trade: float = 1000.0,
    window_min: int = 0,
    window_max: int = 120,
    use_depth: bool = True,
) -> List[Opportunity]:
    now = dt.datetime.now(dt.timezone.utc)
    markets = build_market_index()

    # Filter by time window and liquidity
    filtered: List[Market] = []
    for m in markets.values():
        if not m.active or m.closed:
            continue
        if m.liquidity is not None and m.liquidity < min_liquidity:
            continue
        mins_to_end: Optional[int] = None
        if m.end_date:
            delta = (m.end_date - now).total_seconds() / 60.0
            mins_to_end = int(delta)
            if delta < window_min or delta > window_max:
                continue
        filtered.append(m)

    # Gather numeric token ids only
    token_ids: List[str] = []
    for m in filtered:
        for tok in (m.tokens[0], m.tokens[1]):
            tid = (tok.token_id or "").strip()
            if tid and tid.isdigit():
                token_ids.append(tid)
    token_ids = list(dict.fromkeys(token_ids))

    # Bulk top-of-book
    top = get_top_prices(token_ids)

    opps: List[Opportunity] = []
    margin = min_edge_bps / 10000.0

    for m in filtered:
        yes, no = m.tokens
        p_yes = top.get(yes.token_id, TopOfBook(None, None)) if yes.token_id.isdigit() else TopOfBook(None, None)
        p_no  = top.get(no.token_id,  TopOfBook(None, None))  if no.token_id.isdigit()  else TopOfBook(None, None)

        # LONG: buy both at asks
        if p_yes.ask is not None and p_no.ask is not None:
            sum_asks = float(p_yes.ask) + float(p_no.ask)
            threshold = 1.0 - margin
            if sum_asks < threshold:
                size_pairs = 0.0
                avg_pair = sum_asks
                if use_depth:
                    ob_yes = get_order_book(yes.token_id)
                    ob_no = get_order_book(no.token_id)
                    target_pairs = max(min_trade / max(sum_asks, 1e-9), 1.0)
                    size_pairs, avg_pair = depth_capacity_long(ob_yes.asks.copy(), ob_no.asks.copy(), threshold, target_pairs)
                notional = size_pairs * avg_pair
                edge_cents = (1.0 - avg_pair) * 100.0
                edge_bps = (1.0 - avg_pair) * 10000.0
                mins_to_end = None
                if m.end_date:
                    mins_to_end = int((m.end_date - now).total_seconds() / 60.0)
                if size_pairs > 0 and notional >= min_trade:
                    opps.append(
                        Opportunity(
                            kind="LONG",
                            market=m,
                            edge_cents=edge_cents,
                            edge_bps=edge_bps,
                            sum_price=avg_pair,
                            size_pairs=size_pairs,
                            notional=notional,
                            mins_to_end=mins_to_end,
                        )
                    )

        # SHORT: sell both to bids
        if p_yes.bid is not None and p_no.bid is not None:
            sum_bids = float(p_yes.bid) + float(p_no.bid)
            threshold = 1.0 + margin
            if sum_bids > threshold:
                size_pairs = 0.0
                avg_pair = sum_bids
                if use_depth:
                    ob_yes = get_order_book(yes.token_id)
                    ob_no = get_order_book(no.token_id)
                    target_pairs = max(min_trade / max(sum_bids, 1e-9), 1.0)
                    size_pairs, avg_pair = depth_capacity_short(ob_yes.bids.copy(), ob_no.bids.copy(), threshold, target_pairs)
                notional = size_pairs * avg_pair
                edge_cents = (avg_pair - 1.0) * 100.0
                edge_bps = (avg_pair - 1.0) * 10000.0
                mins_to_end = None
                if m.end_date:
                    mins_to_end = int((m.end_date - now).total_seconds() / 60.0)
                if size_pairs > 0 and notional >= min_trade:
                    opps.append(
                        Opportunity(
                            kind="SHORT",
                            market=m,
                            edge_cents=edge_cents,
                            edge_bps=edge_bps,
                            sum_price=avg_pair,
                            size_pairs=size_pairs,
                            notional=notional,
                            mins_to_end=mins_to_end,
                        )
                    )

    # Sort by edge then notional
    opps.sort(key=lambda o: (o.edge_bps, o.notional), reverse=True)
    return opps

# ---------- CLI ----------

def fmt_opp(o: Opportunity, idx: int) -> str:
    m = o.market
    mins = f"{o.mins_to_end}m" if o.mins_to_end is not None else "?m"
    slug = (m.slug or m.condition_id)[:96]
    return (
        f"[{idx}] {o.kind:<5} edge={o.edge_cents:.2f}¢ ({o.edge_bps:.1f} bps) "
        f"sum={o.sum_price:.4f} pairs≈{o.size_pairs:.0f} notional≈${o.notional:,.0f} "
        f"end~{mins} | {slug}"
    )


def main():
    ap = argparse.ArgumentParser(description="Polymarket arbitrage scanner (intra-market)")
    ap.add_argument("--min-edge-bps", type=float, default=20.0,
                    help="Minimum edge in bps (default 20 bps = 0.20%)")
    ap.add_argument("--min-liquidity", type=float, default=0.0,
                    help="Filter markets with Gamma liquidity >= this value")
    ap.add_argument("--min-trade", type=float, default=1000.0,
                    help="Minimum notional per opportunity to report")
    ap.add_argument("--window-min", type=int, default=0,
                    help="Min minutes until end (inclusive)")
    ap.add_argument("--window-max", type=int, default=120,
                    help="Max minutes until end (inclusive)")
    ap.add_argument("--loop", type=int, default=0,
                    help="Loop interval seconds (0=one-shot)")
    ap.add_argument("--top", type=int, default=10,
                    help="Max opportunities to print")
    ap.add_argument("--no-depth", action="store_true",
                    help="Use only top-of-book (faster, less accurate)")
    args = ap.parse_args()

    use_depth = not args.no_depth

    def run_once_print():
        try:
            opps = scan_once(
                min_edge_bps=args.min_edge_bps,
                min_liquidity=args.min_liquidity,
                min_trade=args.min_trade,
                window_min=args.window_min,
                window_max=args.window_max,
                use_depth=use_depth,
            )
        except Exception as e:
            print(f"[!] scan failed: {e}")
            return

        if not opps:
            print("No opportunities found.")
            return
        for i, o in enumerate(opps[: max(1, args.top)], start=1):
            print(fmt_opp(o, i))

    if args.loop > 0:
        while True:
            print("\n=== Polymarket arb scan", dt.datetime.utcnow().isoformat(), "UTC ===")
            run_once_print()
            time.sleep(max(1, args.loop))
    else:
        run_once_print()


if __name__ == "__main__":
    main()
