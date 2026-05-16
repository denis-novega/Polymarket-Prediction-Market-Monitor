# settings.py
import os
from dotenv import load_dotenv
load_dotenv()

HOST = os.getenv("POLY_HOST", "https://clob.polymarket.com")
CHAIN_ID = int(os.getenv("POLY_CHAIN_ID", "137"))
SIG_TYPE = int(os.getenv("POLY_SIGNATURE_TYPE", "2"))  # MetaMask/browser
FUNDER   = os.getenv("POLY_FUNDER")  # твой адрес
EPSILON  = float(os.getenv("ARB_EPSILON", "0.01"))     # 1% порог
MAX_USD_PER_TRADE = float(os.getenv("MAX_USD_PER_TRADE", "1000"))
MAX_TIME_HOURS = int(os.getenv("MAX_TIME_HOURS", "24"))
