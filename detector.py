# detector.py — NegRisk детектор
from settings import MAX_TIME_HOURS, EPSILON
from py_clob_client.client import ClobClient
from settings import HOST
from books import get_book, vwap_buy_cost, vwap_sell_revenue

client = ClobClient(HOST)

def list_negrisk_markets():
    mkts = client.get_simplified_markets()["data"]
    out = []
    for m in mkts:
        if not m.get("isNegRisk"): 
            continue
        # time_to_end в часах (берём ближайший deadline из объекта рынка)
        hours_left = m.get("hours_to_end") or m.get("timeLeftHours")  # поле зависит от API версии
        if hours_left is None or hours_left > MAX_TIME_HOURS:
            continue
        # outcomes: массив { token_id, outcome, yesPrice, ... }
        outs = m.get("outcomes", [])
        if len(outs) < 2: 
            continue
        out.append({"market_id": m["id"], "name": m["question"], "hours_left": hours_left, "outs": outs})
    return out

def check_long_rebalance(market, unit=1.0):
    # покупаем unit каждой YES: Σ VWAP_ask < unit*(1 - EPSILON)?
    total_cost, min_left = 0.0, 0.0
    for o in market["outs"]:
        book = get_book(o["token_id"])
        cost, need = vwap_buy_cost(book, unit)
        if need > 0: 
            return None  # не хватает объёма
        total_cost += cost
    edge = 1.0 - total_cost/unit
    return {"type":"LONG", "edge":edge, "cost":total_cost}

def check_short_rebalance(market, unit=1.0):
    # продаём unit каждой YES: Σ VWAP_bid > unit*(1 + EPSILON)?
    total_rev, min_left = 0.0, 0.0
    for o in market["outs"]:
        book = get_book(o["token_id"])
        rev, left = vwap_sell_revenue(book, unit)
        if left > 0: 
            return None  # не хватает объёма
        total_rev += rev
    edge = total_rev/unit - 1.0
    return {"type":"SHORT", "edge":edge, "revenue":total_rev}
