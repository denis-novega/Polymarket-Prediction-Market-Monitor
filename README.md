# Polymarket Prediction Market Monitor

A depth-aware monitoring toolkit for Polymarket prediction markets. The project scans event markets, reads CLOB order books, computes VWAP across outcome bundles, and flags price imbalances where the combined cost or revenue of related outcomes deviates from fair settlement value.

The goal is not a traditional cross-exchange arbitrage bot. It focuses on prediction-market microstructure: mutually exclusive outcomes, event-level bundles, order-book depth, liquidity constraints, and execution risk.

## Features

* Fetches active Polymarket markets from Gamma API and token/order-book data from the CLOB API.
* Computes VWAP from available order-book depth instead of relying only on top-of-book quotes.
* Detects long bundle opportunities, where buying all relevant YES outcomes costs less than the settlement value.
* Detects short/rebalance opportunities, where selling a complete outcome bundle implies an overvalued market.
* Supports event-level multi-outcome tracking, binary-market scanning, CSV logging, and simulation.
* Includes a basic constrained execution module for fill-or-kill style order placement.
* Provides safety controls for minimum edge, per-trade budget, time-to-expiry, fees, and maximum outcome count.

## Repository structure

```text
books.py                    Order-book access and VWAP helpers
clob\_source.py              CLOB simplified-market pagination
gamma\_source.py             Gamma market metadata fetcher
markets\_source.py           Active market discovery helpers
detector.py                 Long/short imbalance detection logic
simulator.py                Strategy simulation and signal logging
executor.py                 Basic constrained execution functions
main.py                     Minimal loop tying detection and execution together
polymarket\_scanner.py       Async scanner for binary and event-level opportunities
polymarket\_arb\_scanner.py   Standalone binary market scanner
polymarket\_arb\_sweep.py     Active-market VWAP sweep
polymarket\_tracker.py       Event-specific live tracker
polymarket\_smoke.py         Smoke test for market/order-book connectivity
polymarket\_list.py          Market listing helper
pm\_markets\_list.py          Market list helper using SDK/Gamma fallback
list\_events.py              Event aggregation/listing helper
jw\_prices.py                Lightweight price/VWAP grabber
settings.py                 Environment-based configuration
```

## Installation

```bash
git clone https://github.com/denis-novega/polymarket-prediction-market-monitor.git
cd polymarket-prediction-market-monitor
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## Example usage

Run a smoke test:

```bash
python polymarket\_smoke.py --days 2 --shares 0.5 --top 10
```

Scan near-expiry markets for depth-aware opportunities:

```bash
python polymarket\_scanner.py --last-hour --min-edge 0.01 --per-outcome-shares 1 --top 30
```

Track a specific multi-outcome event by slug:

```bash
python polymarket\_tracker.py --event-slug <event-slug> --shares 0.2 --interval 5 --min-edge 0.005 --csv signals.csv
```

Run the simulation loop:

```bash
python simulator.py
```

Use execution only after reviewing the code, configuring risk limits, and testing with very small sizes.

## Configuration

Create a local `.env` file from `.env.example`:

```dotenv
POLY\_HOST=https://clob.polymarket.com
POLY\_CHAIN\_ID=137
POLY\_SIGNATURE\_TYPE=2
POLY\_FUNDER=0x0000000000000000000000000000000000000000
ARB\_EPSILON=0.01
MAX\_USD\_PER\_TRADE=1000
MAX\_TIME\_HOURS=24
SCAN\_EVERY\_SEC=30
CSV\_PATH=sim\_signals.csv
SIM\_CAPITAL\_USD=1000
BUNDLE\_UNIT=1.0
```

## How it works

For each candidate market or event bundle, the scanner retrieves token-level order books and walks available price levels to estimate executable VWAP. It then compares the aggregate cost or revenue of all relevant outcomes against the bundle settlement value. A signal is emitted only when the computed edge exceeds the configured threshold after optional fee and sizing constraints.

This makes the monitor more conservative than a top-of-book scanner: a displayed imbalance is filtered by actual depth available at the requested size.

