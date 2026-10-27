import os
import sys
import json
import ccxt
import requests
import pandas as pd
import numpy as np # Added for ATR calculation
from datetime import datetime, timezone, timedelta

# ==========================================
# CONFIGURATION & SECRETS (OKX)
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

# ✅ NEW WINNING PARAMETERS
RISK_REWARD = 2.0
ATR_MULTIPLIER = 1.5
VOLUME_MULTIPLIER = 1.5

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
        ex.set_sandbox_mode(True) # DEMO MODE (Set to False when ready for real money)
        ex.load_markets()
        return ex
    except Exception as e:
        print(f"[CRITICAL ERROR] {e}")
        sys.exit(1)

def setup_market_leverage(exchange, symbol):
    try:
        exchange.set_leverage(LEVERAGE, symbol, {'mgnMode': 'cross'})
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
                    
                    exit_price = float(trade['entry'])
                    for t in my_trades:
                        if t['side'] != trade['side']:
                            exit_price = t['price']
                            break
                            
                    entry = float(trade['entry'])
                    amount = float(trade['amount'])
                    
                    if trade['side'] == 'buy':
                        pnl = (exit_price - entry) * amount
                        pct_move = ((exit_price - entry) / entry) * 100
                    else:
                        pnl = (entry - exit_price) * amount
                        pct_move = ((entry - exit_price) / entry) * 100
                        
                    result_emoji = "🟢 WIN" if pnl > 0 else "🔴 LOSS"
                    
                    report = (
                        f"🔔 *TRADE CLOSED: {symbol}*\n\n"
                        f"Result: {result_emoji}\n"
                        f"Side: {'LONG' if trade['side'] == 'buy' else 'SHORT'}\n"
                        f"Entry: ${entry:.4f}\n"
                        f"Exit: ${exit_price:.4f}\n"
                        f"Move: {pct_move:+.2f}%\n"
                        f"PnL: ${pnl:+.2f} USDT\n\n"
                        f"🧠 Strategy: SMC Sweep (ATR Stops)"
                    )
                    send_telegram_alert(report)
                    print(report)
                    
                    trade["status"] = "CLOSED"
                    trade["exit"] = exit_price
                    trade["pnl"] = round(pnl, 2)
                    updated = True
            except Exception as e:
                print(f"[RESOLVER ERROR] {symbol}: {e}")
    return updated

def run_market_strategy(exchange, journal):
    print(f"--- Running Scan at {datetime.now()} ---")
    
    for symbol in SYMBOLS:
        try:
            setup_market_leverage(exchange, symbol)
            
            if any(t['symbol'] == symbol and t['status'] == "OPEN" for t in journal):
                continue

            # Fetch 4H for Trend, 15M for Entry
            ohlcv_4h = exchange.fetch_ohlcv(symbol, '4h', limit=50)
            ohlcv_15m = exchange.fetch_ohlcv(symbol, '15m', limit=50)
            
            df_4h = pd.DataFrame(ohlcv_4h, columns=['t', 'o', 'h', 'l', 'c', 'v'])
            df_15m = pd.DataFrame(ohlcv_15m, columns=['t', 'o', 'h', 'l', 'c', 'v'])
            
            # 4H Trend
            df_4h['ema20'] = df_4h['c'].ewm(span=20, adjust=False).mean()
            df_4h['ema50'] = df_4h['c'].ewm(span=50, adjust=False).mean()
            is_bullish = df_4h['ema20'].iloc[-1] > df_4h['ema50'].iloc[-1]
            is_bearish = df_4h['ema20'].iloc[-1] < df_4h['ema50'].iloc[-1]
            
            # ✅ NEW: Calculate ATR and Volume MA on 15M
            high_low = df_15m['h'] - df_15m['l']
            high_close = (df_15m['h'] - df_15m['c'].shift()).abs()
            low_close = (df_15m['l'] - df_15m['c'].shift()).abs()
            tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
            df_15m['atr'] = tr.rolling(window=14).mean()
            df_15m['vol_ma'] = df_15m['v'].rolling(window=20).mean()
            
            # ✅ NEW: We evaluate the PREVIOUS closed candle (-2) for the sweep
            # and the LAST closed candle (-1) for confirmation.
            sweep_candle = df_15m.iloc[-2]
            conf_candle = df_15m.iloc[-1]
            
            recent_low = float(df_15m['l'].iloc[-20:-2].min())
            recent_high = float(df_15m['h'].iloc[-20:-2].max())
            
            # Sweep Logic + Volume Filter
            swept_low = (sweep_candle['l'] < recent_low) and (sweep_candle['c'] > recent_low) and (sweep_candle['v'] > sweep_candle['vol_ma'] * VOLUME_MULTIPLIER)
            swept_high = (sweep_candle['h'] > recent_high) and (sweep_candle['c'] < recent_high) and (sweep_candle['v'] > sweep_candle['vol_ma'] * VOLUME_MULTIPLIER)
            
            # Confirmation (Green candle for Long, Red candle for Short)
            confirm_long = conf_candle['c'] > conf_candle['o']
            confirm_short = conf_candle['c'] < conf_candle['o']
            
            if (is_bullish and swept_low and confirm_long) or (is_bearish and swept_high and confirm_short):
                live_price = float(exchange.fetch_ticker(symbol)['last'])
                atr_value = float(sweep_candle['atr'])
                buffer = live_price * 0.001 

                if is_bullish and swept_low:
                    signal = "LONG"
                    side = 'buy'
                    entry = live_price
                    # ✅ NEW: ATR Stop Loss
                    sl = entry - (atr_value * ATR_MULTIPLIER)
                    risk = entry - sl
                    tp = entry + (risk * RISK_REWARD) + buffer
                else:
                    signal = "SHORT"
                    side = 'sell'
                    entry = live_price
                    # ✅ NEW: ATR Stop Loss
                    sl = entry + (atr_value * ATR_MULTIPLIER)
                    risk = sl - entry
                    tp = entry - (risk * RISK_REWARD) - buffer

                # Get current balance
                balance = exchange.fetch_balance()['USDT']['free']
                risk_amount = balance * RISK_PERCENTAGE
                price_risk = abs(entry - sl)
                
                market = exchange.market(symbol)
                contract_size = market['contractSize']
                
                raw_contracts = (risk_amount / price_risk) / contract_size
                
                max_position_value = (balance * LEVERAGE) * 0.90
                max_contracts_margin = max_position_value / entry / contract_size
                
                final_contracts = min(raw_contracts, max_contracts_margin)
                amount = float(exchange.amount_to_precision(symbol, final_contracts))
                
                if amount > 0:
                    print(f"Executing {side.upper()} for {amount} contracts...")
                    
                    # ✅ NEW: Added trailing stop (callbackRate) to mimic break-even
                    # OKX uses a percentage callback rate. 1% is roughly our 1R trigger.
                    params = {
                        'stopLoss': {
                            'triggerPrice': float(exchange.price_to_precision(symbol, sl))
                        },
                        'takeProfit': {
                            'triggerPrice': float(exchange.price_to_precision(symbol, tp))
                        },
                        'trailingPercent': 1.0 # Trails price by 1% after it moves in your favor
                    }
                    
                    exchange.create_order(symbol, 'market', side, amount, params=params)
                    
                    journal.append({
                        "symbol": symbol, "side": side, "entry": float(entry),
                        "sl": float(sl), "tp": float(tp), "amount": float(amount),
                        "status": "OPEN", "time": datetime.now().isoformat()
                    })
                    
                    send_telegram_alert(
                        f"✅ {signal} EXECUTED\n"
                        f"Asset: {symbol}\n"
                        f"Size: {amount} contracts\n"
                        f"Entry: {entry:.4f}\n"
                        f"SL (ATR): {sl:.4f}\n"
                        f"TP: {tp:.4f}"
                    )
            else:
                print(f"[HOLD] {symbol}: No clean sweep + confirmation setup.")
                
        except Exception as e:
            print(f"[ORDER ERROR] {symbol}: {e}")
            send_telegram_alert(f"⚠️ Order Failed on {symbol}:\n{str(e)}")

if __name__ == "__main__":
    print("[INIT] Starting OKX Scan...")
    bot_exchange = initialize_exchange()
    journal = load_journal()
    
    if check_closed_trades(bot_exchange, journal):
        save_journal(journal)
        
    run_market_strategy(bot_exchange, journal)
    save_journal(journal) 
    
    print("[COMPLETE] Scan finished.")
