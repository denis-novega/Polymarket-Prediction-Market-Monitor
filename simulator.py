# simulator.py
import os, csv, time, datetime as dt
from typing import List

from dotenv import load_dotenv
from py_clob_client.client import ClobClient

from settings import HOST, EPSILON, MAX_TIME_HOURS
from clob_source import fetch_all_simplified_markets
from gamma_source import fetch_gamma_markets, gamma_to_deadline_map
from books import get_book, vwap_buy_cost, vwap_sell_revenue

load_dotenv()

SCAN_EVERY_SEC = int(os.getenv("SCAN_EVERY_SEC", "30"))
CSV_PATH       = os.getenv("CSV_PATH", "sim_signals.csv")
ASSUME_CAPITAL = float(os.getenv("SIM_CAPITAL_USD", "1000"))
BUNDLE_UNIT    = float(os.getenv("BUNDLE_UNIT", "1.0"))

client = ClobClient(HOST)

def ensure_csv_header(path: str):
    if not os.path.exists(path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([
                "ts_iso","market","n_tokens","hours_left",
                "type","edge_pct","sum_buy","sum_sell","unit","est_pnl_usd_on_$1000"
            ])

def sum_yes_buy(token_ids: List[str], unit: float):
    total = 0.0
    for tid in token_ids:
        book = get_book(tid)
        cost, need = vwap_buy_cost(book, unit)
        if need > 1e-12:
            return None
        total += cost
    return total

def sum_yes_sell(token_ids: List[str], unit: float):
    total = 0.0
    for tid in token_ids:
        book = get_book(tid)
        rev, left = vwap_sell_revenue(book, unit)
        if left > 1e-12:
            return None
        total += rev
    return total

def simulate_once():
    ensure_csv_header(CSV_PATH)
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()

    # 1) CLOB simplified (пагинация)
    simp, s_stats = fetch_all_simplified_markets(client, max_pages=200)
    # фильтруем живые рынки на стороне CLOB
    simp = [
        m for m in simp
        if m.get("active") and not m.get("closed") and m.get("accepting_orders") is True
        and isinstance(m.get("tokens"), list) and len(m["tokens"]) >= 2
    ]

    # 2) Gamma мета + дедлайны
    gamma_items, g_stats = fetch_gamma_markets(limit=250, max_pages=60)
    cond_to_hours = gamma_to_deadline_map(gamma_items)

    # 3) join по condition_id
    mkts = []
    for m in simp:
        cond = str(m["condition_id"]).lower()
        h = cond_to_hours.get(cond)
        if h is None:
            continue
        # фильтр по времени: только 0 < h ≤ MAX_TIME_HOURS
        if not (0.0 < h <= float(MAX_TIME_HOURS)):
            continue
        token_ids = [t["token_id"] for t in m["tokens"] if "token_id" in t]
        if len(token_ids) < 2:
            continue
        mkts.append({
            "question": m.get("question") or m.get("title") or str(m["condition_id"]),
            "hours_left": h,
            "token_ids": token_ids
        })

    print(f"[{now_iso}] simplified_pages={s_stats['pages']} simp_count={len(simp)} | gamma_pages={g_stats['pages']} gamma_count={g_stats['count']} | after_join_time(0<h≤{MAX_TIME_HOURS}h): {len(mkts)}")
    if not mkts:
        return

    # 4) считаем ΣYES/edge, логируем сигналы (покажем в консоли первые 12 рынков)
    shown = 0
    for m in mkts:
        if shown < 12:
            print(f"  → outcomes: {len(m['token_ids'])} | hours_left: {m['hours_left']:.2f}")
            shown += 1

        s_buy  = sum_yes_buy(m["token_ids"], BUNDLE_UNIT)
        s_sell = sum_yes_sell(m["token_ids"], BUNDLE_UNIT)

        if s_buy is not None:
            edge_long = 1.0 - (s_buy / BUNDLE_UNIT)
            if edge_long > float(EPSILON):
                est_pnl = edge_long * ASSUME_CAPITAL
                print(f"     >>> LONG сигнал | ΣBUY={s_buy:.6f} | edge={edge_long*100:.2f}% | ~${est_pnl:.2f}")
                with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow([
                        now_iso, m["question"], len(m["token_ids"]), f"{m['hours_left']:.2f}",
                        "LONG", round(edge_long*100,3), round(s_buy,6), "", BUNDLE_UNIT, round(est_pnl,2)
                    ])

        if s_sell is not None:
            edge_short = (s_sell / BUNDLE_UNIT) - 1.0
            if edge_short > float(EPSILON):
                est_pnl = edge_short * ASSUME_CAPITAL
                print(f"     >>> SHORT сигнал | ΣSELL={s_sell:.6f} | edge={edge_short*100:.2f}% | ~${est_pnl:.2f}")
                with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow([
                        now_iso, m["question"], len(m["token_ids"]), f"{m['hours_left']:.2f}",
                        "SHORT", round(edge_short*100,3), "", round(s_sell,6), BUNDLE_UNIT, round(est_pnl,2)
                    ])

def main_loop():
    print(f"[simulator] every {SCAN_EVERY_SEC}s | ε={EPSILON} | unit={BUNDLE_UNIT}")
    while True:
        try:
            simulate_once()
        except Exception as e:
            print("[ERROR]", e)
        time.sleep(SCAN_EVERY_SEC)

if __name__ == "__main__":
    main_loop()

