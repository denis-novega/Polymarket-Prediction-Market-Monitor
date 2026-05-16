# main.py — сканер + исполнение LONG
from settings import EPSILON, MAX_USD_PER_TRADE
from detector import list_negrisk_markets, check_long_rebalance
from executor import exec_long_bundle

def run_once():
    candidates = list_negrisk_markets()
    for m in candidates:
        # пробуем unit=1 (1 share каждого исхода); дальше можно масштабировать
        res = check_long_rebalance(m, unit=1.0)
        if not res: 
            continue
        if res["edge"] > EPSILON:
            # пример: тратим не более MAX_USD_PER_TRADE
            out = exec_long_bundle(m, unit=1.0, max_total_cost=MAX_USD_PER_TRADE)
            print(m["name"], "edge=", round(res["edge"]*100,2), "%", out)

if __name__ == "__main__":
    run_once()
