# books.py
from py_clob_client.client import ClobClient
from settings import HOST

client_ro = ClobClient(HOST)

def get_book(token_id: str):
    """
    Безопасно получаем книгу. Возвращаем структуру с 'bids' и 'asks' (массивы),
    даже если книга пустая.
    """
    try:
        book = client_ro.get_order_book({"token_id": token_id})
        return {
            "bids": book.get("bids", []) or [],
            "asks": book.get("asks", []) or [],
        }
    except Exception:
        return {"bids": [], "asks": []}

def vwap_buy_cost(book, qty: float):
    """
    Сколько USDC нужно, чтобы КУПИТЬ qty токенов (идём по аскам).
    Возвращаем (стоимость, недостающий_объем).
    """
    need, cost = qty, 0.0
    for lvl in book["asks"]:
        try:
            size = float(lvl.get("size", 0))
            price = float(lvl.get("price", 0))
        except Exception:
            continue
        if size <= 0 or price <= 0:
            continue
        take = min(need, size)
        cost += take * price
        need -= take
        if need <= 1e-12:
            break
    return cost, max(0.0, need)

def vwap_sell_revenue(book, qty: float):
    """
    Сколько USDC получим, если ПРОДАТЬ qty токенов (идём по бидам).
    Возвращаем (выручка, недопроданный_объем).
    """
    left, rev = qty, 0.0
    for lvl in book["bids"]:
        try:
            size = float(lvl.get("size", 0))
            price = float(lvl.get("price", 0))
        except Exception:
            continue
        if size <= 0 or price <= 0:
            continue
        take = min(left, size)
        rev += take * price
        left -= take
        if left <= 1e-12:
            break
    return rev, max(0.0, left)
