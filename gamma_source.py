# gamma_source.py
import time, datetime as dt
from typing import Dict, List, Tuple, Optional
import requests
from dateutil import parser as dtparser

GAMMA_URL = "https://gamma-api.polymarket.com/markets"

def _parse_iso(s: Optional[str]) -> Optional[dt.datetime]:
    if not s: return None
    try:
        return dtparser.parse(s).astimezone(dt.timezone.utc)
    except Exception:
        return None

def fetch_gamma_markets(limit:int=250, max_pages:int=60, sleep:float=0.12) -> Tuple[List[Dict], Dict[str,int]]:
    """
    Возвращает список рынков из Gamma (массив в корне).
    Берём только страницы; без фильтров по времени здесь (сделаем позже).
    """
    out: List[Dict] = []
    stats = {"pages": 0, "count": 0}
    offset = 0
    headers = {"Accept":"application/json","User-Agent":"pm-arb-sim/1.0"}
    for _ in range(max_pages):
        params = {
            "limit": limit, "offset": offset,
            "active": "true", "closed": "false", "archived": "false",
            "order": "liquidity"
        }
        r = requests.get(GAMMA_URL, params=params, headers=headers, timeout=20)
        r.raise_for_status()
        items = r.json()
        if not isinstance(items, list) or not items:
            break
        out.extend(items)
        stats["pages"] += 1
        stats["count"] += len(items)
        if len(items) < limit:
            break
        offset += limit
        time.sleep(sleep)
    return out, stats

def gamma_to_deadline_map(items: List[Dict]) -> Dict[str, float]:
    """
    Строит карту: condition_id (строкой, lower) -> hours_left.
    """
    m: Dict[str, float] = {}
    now = dt.datetime.now(dt.timezone.utc)
    for it in items:
        cond = it.get("conditionId") or it.get("condition_id")
        if not cond:
            continue
        iso = it.get("end_date") or it.get("endDate") or it.get("closes_at")
        t = _parse_iso(iso)
        if not t:
            continue
        h = max(0.0, (t - now).total_seconds()/3600.0)
        m[str(cond).lower()] = h
    return m
