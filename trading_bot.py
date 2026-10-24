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
RISK_PERCENTAGE = 0.03  # 3% Risk per trade
JOURNAL_FILE = "trade_journal.json"

def send_telegram_alert(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[TELEGRAM] (Skipped) {message}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"[TELEGRAM EXCEPTION] {e}")

def load_journal():
    if os.path.exists(JOURNAL_FILE):
        try:
            with open(JOURNAL_FILE, "r") as f:
                return json.load(f)
        except:
            return []
    return []

def save_journal(journal):
    with open(JOURNAL_FILE, "w") as f:
        json.dump(journal, f, indent=4)

def initialize_exchange():
    try:
        ex = ccxt.okx({
            'apiKey': API_KEY, 'secret': API_SECRET, 'password': API_PASSWORD,
            'enableRateLimit': True, 'options': {'defaultType': 'swap'}
        })
        ex.set_sandbox_mode(True)
        ex.load_markets()
        return ex
    except Exception as e:
        print(f"[CRITICAL ERROR] Failed to initialize: {e}")
        sys.exit(1)

def setup_market_leverage(exchange, symbol):
    try:
        exchange.set_leverage(LEVERAGE, symbol, {'mgnMode': 'cross'})
        print(f"[SETUP] {symbol}: Leverage configured to {LEVERAGE}x Cross.")
    except Exception as e:
        print(f"[SETUP NOTICE] {symbol}: {e}")

def check_closed_trades(exchange, journal):
    updated = False
    for trade in journal:
        if trade.get("status") == "OPEN":
            symbol = trade["symbol"]
            try:
                positions = exchange.fetch_positions([symbol])
                has_position = any(float(p.get('contracts', 0)) > 0 for p in positions)
                
                if not has_position:
                    since_time = int(datetime.fromisoformat(trade["time"]).timestamp() * 1000) - 60000
                    my_trades = exchange.fetch_my_trades(symbol, since=since_time)
                    
                    exit_price = None
                    for t in my_trades:
                        if t['side'] != trade['side']:
                            exit_price = t['price']
                            break
                    
                    if exit_price is None:
                        exit_price = float(trade['entry'])
                        
                    entry = float(trade['entry'])
                    amount = float(trade['amount'])
                    
                    if trade['side'] == 'buy':
                        pnl = (exit_price - entry) * amount
                    else:
                        pnl = (entry - exit_price) * amount
                        
                    trade["status"] = "CLOSED"
                    trade["exit"] = exit_price
                    trade["pnl"] = round(pnl, 2)
                    updated = True
                    
                    result = "PROFIT 🟢" if pnl > 0 else "LOSS 🔴"
                    msg = (
                        f"🔔 TRADE CLOSED: {symbol}\n"
                        f"Result: {result}\n"
                        f"Entry: {entry:.4f}\n"
                        f"Exit: {exit_price:.4f}\n"
                        f"PnL: {pnl:.2f} USDT"
                    )
                    send_telegram_alert(msg)
            except Exception as e:
                print(f"[ERROR] Checking closed trade for {symbol}: {e}")
    return updated

def run_market_strategy(exchange, journal):
    print(f"--- Running Scan at {datetime.now()} ---")
    
    for symbol in SYMBOLS:
        try:
            setup_market_leverage(exchange, symbol)
            
            has_open_trade = any(t['symbol'] == symbol and t['status'] == "OPEN" for t in journal)
            if has_open_trade:
                print(f"[GUARD] Journal shows an open trade for {symbol}. Skipping.")
                continue

            ohlcv_4h = exchange.fetch_ohlcv(symbol, '4h', limit=50)
            ohlcv_15m = exchange.fetch_ohlcv(symbol, '15m', limit=50)
            
            df_4h = pd.DataFrame(ohlcv_4h, columns=['t', 'o', 'h', 'l', 'c', 'v'])
            df_15m = pd.DataFrame(ohlcv_15m, columns=['t', 'o', 'h', 'l', 'c', 'v'])
            
            df_4h['ema20'] = df_4h['c'].ewm(span=20, adjust=False).mean()
            df_4h['ema50'] = df_4h['c'].ewm(span=50, adjust=False).mean()
            
            is_bullish = df_4h['ema20'].iloc[-1] > df_4h['ema50'].iloc[-1]
            is_bearish = df_4h['ema20'].iloc[-1] < df_4h['ema50'].iloc[-1]
            
            current_price = float(df_15m['c'].iloc[-1])
            recent_low = float(df_15m['l'].iloc[-20:-1].min())
            recent_high = float(df_15m['h'].iloc[-20:-1].max())
            
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

            balance = exchange.fetch_balance()['USDT']['free']
            risk_amount = balance * RISK_PERCENTAGE
            price_risk = abs(entry - sl)
            
            market = exchange.market(symbol)
            contract_size = market['contractSize']
            
            raw_contracts = (risk_amount / price_risk) / contract_size
            amount = float(exchange.amount_to_precision(symbol, raw_contracts))
            
            if amount > 0:
                print(f"Executing {side.upper()} for {amount} contracts...")
                
                # ✅ THE FIX: Explicit OKX V5 parameters for TP/SL execution
                params = {
                    'slTriggerPx': str(round(sl, 4)),
                    'slOrdPx': '-1',  # -1 means Market execution when SL is hit
                    'tpTriggerPx': str(round(tp, 4)),
                    'tpOrdPx': '-1'   # -1 means Market execution when TP is hit
                }
                
                order = exchange.create_order(
                    symbol=symbol, 
                    type='market', 
                    side=side, 
                    amount=amount, 
                    params=params
                )
                
                journal.append({
                    "symbol": symbol,
                    "side": side,
                    "entry": float(entry),
                    "sl": float(sl),
                    "tp": float(tp),
                    "amount": float(amount),
                    "status": "OPEN",
                    "time": datetime.now().isoformat()
                })
                
                msg = (
                    f"✅ {signal} EXECUTED\n"
                    f"Asset: {symbol}\n"
                    f"Size: {float(amount)} contracts\n"
                    f"Entry: {float(entry):.4f}\n"
                    f"SL: {float(sl):.4f}\n"
                    f"TP: {float(tp):.4f}"
                )
                send_telegram_alert(msg)
                
        except Exception as e:
            err_msg = f"⚠️ Execution Error on {symbol}:\n{str(e)}"
            print(err_msg)
            send_telegram_alert(err_msg)

if __name__ == "__main__":
    print("[INIT] Starting structural blueprint scan...")
    bot_exchange = initialize_exchange()
    
    journal = load_journal()
    
    closed_something = check_closed_trades(bot_exchange, journal)
    if closed_something:
        save_journal(journal)
        
    run_market_strategy(bot_exchange, journal)
    save_journal(journal) 
    print("[COMPLETE] Scan finished safely.")
