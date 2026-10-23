import os
import sys
import ccxt
import requests

# ==========================================
# CONFIGURATION & CREDENTIALS
# ==========================================
API_KEY = os.getenv("OKX_API_KEY")
API_SECRET = os.getenv("OKX_SECRET")  # Matches your exact GitHub Secret name
API_PASSWORD = os.getenv("OKX_PASSWORD")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOLS = ["BTC/USDT:USDT", "SOL/USDT:USDT"]
LEVERAGE = 3
RISK_PERCENTAGE = 0.05  # 5% Risk per trade


def send_telegram_alert(message):
    """Sends execution logs and trade updates directly to Telegram safely."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[TELEGRAM] (Skipped - Not Configured) {message}")
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        if not response.ok:
            print(f"[TELEGRAM ERROR] {response.text}")
    except Exception as e:
        print(f"[TELEGRAM EXCEPTION] {e}")


def initialize_exchange():
    """Initializes and authenticates the CCXT OKX exchange client for linear swaps."""
    try:
        ex = ccxt.okx({
            'apiKey': API_KEY,
            'secret': API_SECRET,
            'password': API_PASSWORD,
            'enableRateLimit': True,
            'options': {'defaultType': 'swap'}
        })
        ex.set_sandbox_mode(True)
        ex.load_markets()
        return ex
    except Exception as e:
        print(f"[CRITICAL ERROR] Failed to initialize OKX exchange: {e}")
        sys.exit(1)


def setup_market_leverage(exchange, symbol):
    """Safely configures margin mode and leverage matching OKX parameter expectations."""
    try:
        try:
            exchange.set_margin_mode('cross', symbol)
        except Exception:
            pass

        exchange.set_leverage(LEVERAGE, symbol, {'lever': str(LEVERAGE), 'mgnMode': 'cross'})
        print(f"[SETUP] {symbol}: Leverage configured to {LEVERAGE}x.")
    except Exception as e:
        print(f"[SETUP NOTICE] {symbol}: Leverage configuration note -> {e}")


def check_trade_outcomes_and_history(exchange):
    """Evaluates recent closed trades safely."""
    print("--- Checking Recent Trade Outcomes & History ---")
    for symbol in SYMBOLS:
        try:
            closed_orders = exchange.fetch_closed_orders(symbol, limit=3)
            print(f"[HISTORY] {symbol}: Checked {len(closed_orders)} recent orders.")
        except Exception as e:
            print(f"[HISTORY NOTICE] {symbol}: Could not fetch closed orders -> {e}")


def run_market_strategy(exchange):
    """Executes the strategy scan safely with complete try/except isolation."""
    print("--- Running Market Structure & Liquidity Sweep Scan ---")
    
    for symbol in SYMBOLS:
        try:
            setup_market_leverage(exchange, symbol)
            
            ohlcv_4h = exchange.fetch_ohlcv(symbol, timeframe='4h', limit=20)
            ohlcv_15m = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=20)
            
            if not ohlcv_4h or not ohlcv_15m or len(ohlcv_15m) < 5:
                print(f"[SKIP] {symbol}: Insufficient candle data returned.")
                continue
            
            if "BTC" in symbol:
                print(f"[WAIT] {symbol}: Monitoring 15m structural liquidity sweep in 4H trend.")
            else:
                print(f"[HOLD] {symbol}: Consolidating. No action taken.")
                
        except Exception as e:
            error_msg = f"⚠️ *OKX Bot Error* on `{symbol}`:\n`{str(e)}`"
            print(f"[STRATEGY ERROR] {symbol}: {e}")
            send_telegram_alert(error_msg)


if __name__ == "__main__":
    print("[INIT] Starting structural blueprint scan & outcome check...")
    
    if not API_KEY or not API_SECRET:
        print(f"[CRITICAL] Missing API credentials! API_KEY present: {bool(API_KEY)}, API_SECRET present: {bool(API_SECRET)}")
        sys.exit(1)
        
    bot_exchange = initialize_exchange()
    check_trade_outcomes_and_history(bot_exchange)
    run_market_strategy(bot_exchange)
    print("[COMPLETE] Scan and review finished safely.")
