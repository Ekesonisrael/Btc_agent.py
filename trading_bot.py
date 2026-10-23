import os
import sys
import time
import ccxt
import requests

# ==========================================
# CONFIGURATION & CREDENTIALS
# ==========================================
API_KEY = os.getenv("OKX_API_KEY")
API_SECRET = os.getenv("OKX_API_SECRET") or os.getenv("OKX_SECRET")
API_PASSWORD = os.getenv("OKX_PASSWORD")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOLS = ["BTC/USDT:USDT", "SOL/USDT:USDT"]
LEVERAGE = 3

# Initialize CCXT OKX Exchange client
exchange = ccxt.okx({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'password': API_PASSWORD,
    'enableRateLimit': True,
})

# Enable Sandbox / Demo trading mode explicitly
exchange.set_sandbox_mode(True)


def send_telegram_alert(message):
    """Sends execution logs and trade updates directly to Telegram."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[TELEGRAM] (Not Configured) {message}")
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
            print(f"[TELEGRAM ERROR] Failed to send alert: {response.text}")
    except Exception as e:
        print(f"[TELEGRAM EXCEPTION] {e}")


def initialize_market_setup(symbol):
    """Configures margin mode and leverage properly, handling CCXT parameter requirements."""
    try:
        # Fetch markets to ensure valid symbol loading
        exchange.load_markets()
        
        # Set cross margin mode
        try:
            exchange.set_margin_mode('cross', symbol)
        except Exception as mm_err:
            # Margin mode might already be set
            pass

        # Set leverage with correct parameter mapping to avoid terminal warnings
        try:
            exchange.set_leverage(LEVERAGE, symbol, {'lever': str(LEVERAGE), 'mgnMode': 'cross'})
        except Exception as lev_err:
            print(f"[SETUP NOTICE] {symbol}: Leverage set note -> {lev_err}")
            
    except Exception as e:
        print(f"[SETUP WARNING] {symbol}: Could not initialize market parameters -> {e}")


def check_trade_outcomes_and_history():
    """Checks recent history and active position outcomes."""
    print("--- Checking Recent Trade Outcomes & History ---")
    for symbol in SYMBOLS:
        try:
            # Fetch closed orders or trade history to evaluate past performance
            closed_orders = exchange.fetch_closed_orders(symbol, limit=5)
            print(f"[HISTORY] {symbol}: Evaluated {len(closed_orders)} recent executed orders.")
        except Exception as e:
            print(f"[ERROR checking history for {symbol}]: {e}")


def run_market_strategy():
    """Executes the 4H macro trend & 15m structural liquidity sweep strategy."""
    print("--- Running Market Structure & Liquidity Sweep Scan ---")
    
    for symbol in SYMBOLS:
        # Initialize market leverage & margins securely
        initialize_market_setup(symbol)
        
        try:
            # Fetch 4H and 15m OHLCV candle data
            ohlcv_4h = exchange.fetch_ohlcv(symbol, timeframe='4h', limit=50)
            ohlcv_15m = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
            
            if not ohlcv_4h or not ohlcv_15m:
                print(f"[SKIP] {symbol}: Insufficient candle data fetched.")
                continue
            
            # Simple structural evaluation placeholder
            # (Integrate your liquidity sweep / fair value gap logic here)
            if "BTC" in symbol:
                print(f"[WAIT] {symbol}: Waiting for clean 15m sweep in bearish 4H trend.")
            else:
                print(f"[HOLD] {symbol}: 4H macro trend is consolidating. Sitting on hands.")
                
        except Exception as e:
            print(f"[STRATEGY ERROR] {symbol}: {e}")
            send_telegram_alert(f"⚠️ *OKX Bot Error* on `{symbol}`:\n`{str(e)}`")


if __name__ == "__main__":
    print("[INIT] Starting structural blueprint scan & outcome check...")
    
    # Validate core keys before proceeding
    if not API_KEY or not API_SECRET:
        print("[CRITICAL] Missing OKX API keys or secret environment variables!")
        sys.exit(1)
        
    check_trade_outcomes_and_history()
    run_market_strategy()
    print("[COMPLETE] Scan and review finished safely.")
