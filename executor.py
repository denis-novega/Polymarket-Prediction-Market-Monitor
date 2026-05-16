# executor.py — FOK/IOC исполнение (LONG сразу; SHORT добавим после split)
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import LimitOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY
from settings import HOST, CHAIN_ID, SIG_TYPE, FUNDER, MAX_USD_PER_TRADE
import time

client = ClobClient(HOST, signature_type=SIG_TYPE, funder=FUNDER, chain_id=CHAIN_ID)
client.set_api_creds(client.create_or_derive_api_creds())

def buy_yes_fok(token_id: str, qty: float, px: float):
    lo = LimitOrderArgs(token_id=token_id, side=BUY, size=qty, price=px, order_type=OrderType.FOK)
    signed = client.create_limit_order(lo)
    return client.post_order(signed, OrderType.FOK)

def exec_long_bundle(market, unit: float, max_total_cost: float):
    spent = 0.0
    results = []
    for o in market["outs"]:
        # ставим лимит по лучшему аску, чтобы FOK не проскользнул
        book = client.get_order_book({"token_id": o["token_id"]})
        ask = float(book["asks"][0]["price"])
        # safety: не тратить больше лимита
        if spent + ask*unit > max_total_cost:
            return {"ok": False, "reason": "budget"}
        r = buy_yes_fok(o["token_id"], unit, ask)
        if r.get("status") != "FILLED":
            # откат/хедж — для MVP просто выходим (портфель неполный)
            return {"ok": False, "reason": "leg_failed", "resp": r}
        spent += ask*unit
        results.append(r)
        time.sleep(0.05)  # маленькая задержка
    return {"ok": True, "spent": spent, "fills": results}
