# clob_source.py
from typing import Dict, List, Tuple
from py_clob_client.client import ClobClient

def fetch_all_simplified_markets(client: ClobClient, max_pages: int = 200) -> Tuple[List[Dict], Dict[str,int]]:
    """
    Полностью выгружает simplified-markets через next_cursor.
    Возвращает (список рынков, stats).
    Элемент рынка содержит: condition_id, active, closed, accepting_orders, tokens[...].
    """
    out: List[Dict] = []
    stats = {"pages": 0, "count": 0}
    cursor = ""  # пустая строка = начало
    for _ in range(max_pages):
        resp = client.get_simplified_markets(next_cursor=cursor)
        data = resp.get("data") if isinstance(resp, dict) else None
        if not isinstance(data, list) or not data:
            break
        out.extend(data)
        stats["pages"] += 1
        stats["count"] += len(data)
        cursor = resp.get("next_cursor", "LTE=")
        if cursor in (None, "LTE="):
            break
    return out, stats
