import os
import sys
import json
import ccxt
import requests
import pandas as pd

# ==========================================
# CONFIGURATION & SECRETS
# ==========================================
API_KEY = os.getenv("OKX_API_KEY")
API_SECRET = os.getenv("OKX_SECRET")
API_PASSWORD = os.getenv("OKX_PASSWORD")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOLS = ["BTC/USDT:USDT", "SOL/USDT:USDT"]
LEVERAGE = 3
RISK_PERCENTAGE = 0.03  # Set to 3% risk for demo testing
JOURNAL_FILE = "trade_journal.json"


def send_telegram_alert(message):
    """Sends execution logs and trade updates directly to Telegram safely."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[TELEGRAM] (Skipped) {message}")
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code != 200:
            print(f"[TELEGRAM ERROR] {r.text}")
    except Exception as e:
        print(f"[TELEGRAM EXCEPTION] {e}")


def log_trade_to_memory(data):
    """Saves execution logs locally for repository journaling."""
    try:
        history = []
        if os.path.exists(JOURNAL_FILE):
            with open(JOURNAL_FILE, "r") as f:
                history = json.load(f)
        history.append(data)
        with open(JOURNAL_FILE, "w") as f:
            json.dump(history, f, indent=4)
    except Exception as e:
        print(f"[JOURNAL ERROR] {e}")


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
        print(f"[CRITICAL ERROR] Failed to initialize: {e}")
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
        print(f"[SETUP NOTICE] {symbol}: {e}")


def has_active_position_or_order(exchange, symbol):
    """PREVENTS DOUBLE TRADES: Checks if a position or open order already exists."""
    try:
        positions = exchange.fetch_positions([symbol])
        for pos in positions:
            if float(pos.get('contracts', 0)) > 0:
                print(f"[GUARD] Active position already open for {symbol}. Skipping.")
                return True
                
        open_orders = exchange.fetch_open_orders(symbol)
        if len(open_orders) > 0:
            print(f"[GUARD] Active pending orders exist for {symbol}. Skipping.")
            return True
    except Exception as e:
        print(f"[GUARD WARNING] {symbol}: {e}")
    return False


def run_market_strategy(exchange):
    """Executes the 4H macro trend & 15m structural liquidity sweep strategy."""
    print("--- Running Market Structure & Liquidity Sweep Scan ---")
    
    for symbol in SYMBOLS:
        try:
            setup_market_leverage(exchange, symbol)
            
            if has_active_position_or_order(exchange, symbol):
                continue
            
            ohlcv_4h = exchange.fetch_ohlcv(symbol, '4h', limit=50)
            ohlcv_15m = exchange.fetch_ohlcv(symbol, '15m', limit=50)
            
            if not ohlcv_4h or not ohlcv_15m or len(ohlcv_4h) < 30 or len(ohlcv_15m) < 30:
                print(f"[SKIP] {symbol}: Insufficient candle data fetched.")
                continue
            
            df_4h = pd.DataFrame(ohlcv_4h, columns=['t', 'o', 'h', 'l', 'c', 'v'])
            df_15m = pd.DataFrame(ohlcv_15m, columns=['t', 'o', 'h', 'l', 'c', 'v'])
            
            df_4h['ema20'] = df_4h['c'].ewm(span=20, adjust=False).mean()
            df_4h['ema50'] = df_4h['c'].ewm(span=50, adjust=False).mean()
            
            is_bullish = df_4h['ema20'].iloc[-1] > df_4h['ema50'].iloc[-1]
            is_bearish = df_4h['ema20'].iloc[-1] < df_4h['ema50'].iloc[-1]
            
            current_price = df_15m['c'].iloc[-1]
            recent_low = df_15m['l'].iloc[-20:-1].min()
            recent_high = df_15m['h'].iloc[-20:-1].max()
            
            swept_low = df_15m['l'].iloc[-1] < recent_low and current_price > recent_low
            swept_high = df_15m['h'].iloc[-1] > recent_high and current_price < recent_high
            
            if is_bullish and swept_low:
                entry = current_price
                sl = recent_low * 0.995
                tp = entry + (entry - sl) * 2.5
                side = 'buy'
            elif is_bearish and swept_high:
                entry = current_price
                sl = recent_high * 1.005
                tp = entry - (sl - entry) * 2.5
                side = 'sell'
            else:
                print(f"[HOLD] {symbol}: No clean setup. Monitoring structural sweep.")
                continue

            # ---- POSITION SIZING & CONTRACT SIZE FIX ----
            balance = exchange.fetch_balance()['USDT']['free']
            risk_amount = balance * RISK_PERCENTAGE
            price_risk = abs(entry - sl)
            
            market = exchange.market(symbol)
            contract_size = market['contractSize']
            
            raw_contracts = (risk_amount / price_risk) / contract_size
            amount = float(exchange.amount_to_precision(symbol, raw_contracts))
            
            if amount > 0:
                print(f"Executing {side} market order for {amount} contracts...")
                order = exchange.create_order(
                    symbol=symbol,
                    type='market',
                    side=side,
                    amount=amount,
                    params={
                        'tpTriggerPx': tp,
                        'slTriggerPx': sl
                    }
                )
                
                success_msg = (
                    f"✅ *{side.upper()} Market Order Executed (Demo - 3% Risk)*\n\n"
                    f"*Asset:* `{symbol}`\n"
                    f"*Contracts:* `{amount}`\n"
                    f"*Est Entry:* `{entry:.4f}`\n"
                    f"*Stop Loss:* `{sl:.4f}`\n"
                    f"*Take Profit:* `{tp:.4f}`\n"
                    f"*Order ID:* `{order['id']}`"
                )
                print(success_msg)
                send_telegram_alert(success_msg)
                log_trade_to_memory({
                    "symbol": symbol,
                    "side": side,
                    "entry": entry,
                    "contracts": amount,
                    "status": "executed"
                })
                
        except Exception as e:
            err_msg = f"⚠️ *Bot Error* on `{symbol}`:\n`{str(e)}`"
            print(err_msg)
            send_telegram_alert(err_msg)


if __name__ == "__main__":
    print("[INIT] Starting structural blueprint scan & position guard check...")
    
    if not API_KEY or not API_SECRET:
        print(f"[CRITICAL] Missing API credentials! API_KEY: {bool(API_KEY)}, API_SECRET: {bool(API_SECRET)}")
        sys.exit(1)
        
    bot_exchange = initialize_exchange()
    run_market_strategy(bot_exchange)
    print("[COMPLETE] Scan and risk guard execution finished safely.")
