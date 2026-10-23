import os
import sys
import json
import ccxt
import requests
import pandas as pd
from datetime import datetime

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
RISK_PERCENTAGE = 0.03  # Set to 3% as requested
JOURNAL_FILE = "trade_journal.json"


def send_telegram_alert(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[TELEGRAM] (Skipped) {message}")
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    # Removed parse_mode to prevent Markdown crashes
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code != 200:
            print(f"[TELEGRAM ERROR] {r.text}")
    except Exception as e:
        print(f"[TELEGRAM EXCEPTION] {e}")


def log_trade_to_memory(data):
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
    try:
        try:
            exchange.set_margin_mode('cross', symbol)
        except Exception:
            pass
        exchange.set_leverage(LEVERAGE, symbol, {'lever': str(LEVERAGE), 'mgnMode': 'cross'})
    except Exception as e:
        print(f"[SETUP NOTICE] {symbol}: Leverage config note -> {e}")


def has_active_position_or_order(exchange, symbol):
    try:
        positions = exchange.fetch_positions([symbol])
        for pos in positions:
            if float(pos.get('contracts', 0)) > 0:
                return True
                
        open_orders = exchange.fetch_open_orders(symbol)
        if len(open_orders) > 0:
            return True
    except Exception as e:
        print(f"[GUARD WARNING] Could not verify positions for {symbol}: {e}")
    return False


def run_market_strategy(exchange):
    print(f"--- Running Scan at {datetime.now()} ---")
    
    for symbol in SYMBOLS:
        try:
            setup_market_leverage(exchange, symbol)
            
            if has_active_position_or_order(exchange, symbol):
                print(f"[GUARD] Active position/orders for {symbol}. Skipping.")
                continue
            
            ohlcv_4h = exchange.fetch_ohlcv(symbol, '4h', limit=50)
            ohlcv_15m = exchange.fetch_ohlcv(symbol, '15m', limit=50)
            
            df_4h = pd.DataFrame(ohlcv_4h, columns=['t', 'o', 'h', 'l', 'c', 'v'])
            df_15m = pd.DataFrame(ohlcv_15m, columns=['t', 'o', 'h', 'l', 'c', 'v'])
            
            # 4H Macro Trend
            df_4h['ema20'] = df_4h['c'].ewm(span=20, adjust=False).mean()
            df_4h['ema50'] = df_4h['c'].ewm(span=50, adjust=False).mean()
            
            is_bullish = df_4h['ema20'].iloc[-1] > df_4h['ema50'].iloc[-1]
            is_bearish = df_4h['ema20'].iloc[-1] < df_4h['ema50'].iloc[-1]
            
            # 15m Liquidity Sweep
            current_price = df_15m['c'].iloc[-1]
            recent_low = df_15m['l'].iloc[-20:-1].min()
            recent_high = df_15m['h'].iloc[-20:-1].max()
            
            swept_low = df_15m['l'].iloc[-1] < recent_low and current_price > recent_low
            swept_high = df_15m['h'].iloc[-1] > recent_high and current_price < recent_high
            
            signal = None
            if is_bullish and swept_low:
                signal = "LONG"
                entry = current_price
                sl = recent_low * 0.995
                tp = entry + (entry - sl) * 2.5
                side = 'buy'
            elif is_bearish and swept_high:
                signal = "SHORT"
                entry = current_price
                sl = recent_high * 1.005
                tp = entry - (sl - entry) * 2.5
                side = 'sell'
            else:
                print(f"[HOLD] {symbol}: No clean sweep setup.")
                continue

            # --- POSITION SIZING & CONTRACT MATH ---
            balance = exchange.fetch_balance()['USDT']['free']
            risk_amount = balance * RISK_PERCENTAGE
            price_risk = abs(entry - sl)
            
            market = exchange.market(symbol)
            contract_size = market['contractSize'] # THIS IS THE FIX
            
            # Calculate exact contracts for the exchange
            raw_contracts = (risk_amount / price_risk) / contract_size
            amount = float(exchange.amount_to_precision(symbol, raw_contracts))
            
            if amount > 0:
                print(f"Executing {side.upper()} for {amount} contracts...")
                
                # MARKET ORDER WITH SL/TP
                order = exchange.create_order(
                    symbol=symbol, 
                    type='market', # THIS IS THE FIX (Market, not Limit)
                    side=side, 
                    amount=amount,
                    params={'tpTriggerPx': tp, 'slTriggerPx': sl}
                )
                
                msg = (
                    f"✅ {signal} EXECUTED\n"
                    f"Asset: {symbol}\n"
                    f"Size: {amount} contracts\n"
                    f"Entry: {entry:.4f}\n"
                    f"SL: {sl:.4f}\n"
                    f"TP: {tp:.4f}"
                )
                send_telegram_alert(msg)
                log_trade_to_memory({"symbol": symbol, "side": side, "entry": entry, "status": "executed"})
                
        except Exception as e:
            err_msg = f"⚠️ Bot Error on {symbol}:\n{str(e)}"
            print(err_msg)
            send_telegram_alert(err_msg)


if __name__ == "__main__":
    print("[INIT] Starting structural blueprint scan...")
    if not API_KEY or not API_SECRET:
        print(f"[CRITICAL] Missing API credentials!")
        sys.exit(1)
        
    bot_exchange = initialize_exchange()
    run_market_strategy(bot_exchange)
    print("[COMPLETE] Scan finished safely.")
