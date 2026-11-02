import os
import ccxt
import requests
import pandas as pd
import time
import json
from datetime import datetime

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
RISK_REWARD = 2.0
ATR_MULTIPLIER = 1.5
VOLUME_MULTIPLIER = 1.5
JOURNAL_FILE = "trade_journal.json"

last_telegram_time = 0

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[TELEGRAM SKIPPED] {message}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"[TELEGRAM EXCEPTION] {e}")

def load_journal():
    if os.path.exists(JOURNAL_FILE):
        try:
            with open(JOURNAL_FILE, "r") as f: return json.load(f)
        except: return []
    return []

def save_journal(journal):
    with open(JOURNAL_FILE, "w") as f: json.dump(journal, f, indent=4)

def initialize_exchange():
    ex = ccxt.okx({
        'apiKey': API_KEY, 'secret': API_SECRET, 'password': API_PASSWORD,
        'enableRateLimit': True, 'options': {'defaultType': 'swap'}
    })
    ex.set_sandbox_mode(True) # DEMO MODE
    return ex

def check_closed_trades(exchange, journal):
    updated = False
    for trade in journal:
        if trade.get("status") == "OPEN":
            symbol = trade["symbol"]
            try:
                # Check positions
                positions = exchange.fetch_positions([symbol])
                has_position = any(float(p.get('contracts', 0)) > 0 for p in positions)
                
                if not has_position:
                    # 🚨 FIX: Wait 3 seconds and check again to prevent OKX API glitches
                    print(f"[{symbol}] Position appears closed. Double-checking in 3s...")
                    time.sleep(3)
                    positions_check = exchange.fetch_positions([symbol])
                    has_position_check = any(float(p.get('contracts', 0)) > 0 for p in positions_check)
                    
                    if not has_position_check:
                        send_telegram(f"🔔 *TRADE CLOSED: {symbol}*\nCheck OKX app for final PnL.\nStrategy: SMC Sweep")
                        print(f"Trade confirmed closed for {symbol}")
                        trade["status"] = "CLOSED"
                        updated = True
            except Exception as e:
                print(f"[RESOLVER ERROR] {symbol}: {e}")
    return updated

def fetch_history(exchange, symbol, timeframe, limit):
    all_ohlcv = []
    since = exchange.milliseconds() - (limit * exchange.parse_timeframe(timeframe) * 1000)
    while True:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=100)
            if not ohlcv: break
            all_ohlcv.extend(ohlcv)
            since = ohlcv[-1][0] + 1
            if len(all_ohlcv) >= limit: break
            time.sleep(0.1)
        except Exception as e: 
            print(f"Fetch error: {e}")
            break
    df = pd.DataFrame(all_ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
    df['time'] = pd.to_datetime(df['time'], unit='ms')
    return df.drop_duplicates('time').sort_values('time').reset_index(drop=True)

def run_scanner():
    global last_telegram_time
    exchange = initialize_exchange()
    journal = load_journal()
    
    if check_closed_trades(exchange, journal):
        save_journal(journal)

    print(f"--- Running Crypto Scan at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---")
    
    for symbol in SYMBOLS:
        if any(t['symbol'] == symbol and t['status'] == "OPEN" for t in journal):
            print(f"[{symbol}] Trade already open. Skipping.")
            continue

        try:
            df_4h = fetch_history(exchange, symbol, '4h', 100)
            df_15m = fetch_history(exchange, symbol, '15m', 100)
            
            if df_4h.empty or df_15m.empty: continue

            df_4h['ema20'] = df_4h['close'].ewm(span=20, adjust=False).mean()
            df_4h['ema50'] = df_4h['close'].ewm(span=50, adjust=False).mean()
            is_bullish = df_4h['ema20'].iloc[-1] > df_4h['ema50'].iloc[-1]
            is_bearish = df_4h['ema20'].iloc[-1] < df_4h['ema50'].iloc[-1]

            high_low = df_15m['high'] - df_15m['low']
            high_close = (df_15m['high'] - df_15m['close'].shift()).abs()
            low_close = (df_15m['low'] - df_15m['close'].shift()).abs()
            tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
            df_15m['atr'] = tr.rolling(window=14).mean()
            df_15m['vol_ma'] = df_15m['volume'].rolling(window=20).mean()

            sweep_candle = df_15m.iloc[-2]
            conf_candle = df_15m.iloc[-1]
            
            recent_low = df_15m['low'].iloc[-20:-2].min()
            recent_high = df_15m['high'].iloc[-20:-2].max()

            vol_ok_low = sweep_candle['volume'] > sweep_candle['vol_ma'] * VOLUME_MULTIPLIER
            vol_ok_high = sweep_candle['volume'] > sweep_candle['vol_ma'] * VOLUME_MULTIPLIER
            
            swept_low = (sweep_candle['low'] < recent_low) and (sweep_candle['close'] > recent_low) and vol_ok_low
            swept_high = (sweep_candle['high'] > recent_high) and (sweep_candle['close'] < recent_high) and vol_ok_high

            conf_long = conf_candle['close'] > conf_candle['open']
            conf_short = conf_candle['close'] < conf_candle['open']

            current_price = float(exchange.fetch_ticker(symbol)['last'])
            atr_value = float(sweep_candle['atr'])

            signal = None
            if is_bullish and swept_low and conf_long:
                signal = "LONG"
                side = 'buy'
                sl = current_price - (atr_value * ATR_MULTIPLIER)
                risk = current_price - sl
                tp = current_price + (risk * RISK_REWARD)
            elif is_bearish and swept_high and conf_short:
                signal = "SHORT"
                side = 'sell'
                sl = current_price + (atr_value * ATR_MULTIPLIER)
                risk = sl - current_price
                tp = current_price - (risk * RISK_REWARD)

            if signal:
                print(f"🚨 {symbol} SIGNAL: {signal}")
                
                try:
                    exchange.set_leverage(LEVERAGE, symbol, {'mgnMode': 'cross'})
                    balance = exchange.fetch_balance()['USDT']['free']
                    risk_amount = balance * RISK_PERCENTAGE
                    price_risk = abs(current_price - sl)
                    
                    market = exchange.market(symbol)
                    contract_size = market['contractSize']
                    
                    raw_contracts = (risk_amount / price_risk) / contract_size
                    max_position_value = (balance * LEVERAGE) * 0.90
                    max_contracts_margin = max_position_value / current_price / contract_size
                    
                    final_contracts = min(raw_contracts, max_contracts_margin)
                    amount = float(exchange.amount_to_precision(symbol, final_contracts))
                    
                    if amount > 0:
                        params = {
                            'stopLoss': {'triggerPrice': float(exchange.price_to_precision(symbol, sl))},
                            'takeProfit': {'triggerPrice': float(exchange.price_to_precision(symbol, tp))}
                        }
                        
                        exchange.create_order(symbol, 'market', side, amount, params=params)
                        
                        journal.append({
                            "symbol": symbol, "side": side, "entry": float(current_price),
                            "sl": float(sl), "tp": float(tp), "amount": float(amount),
                            "status": "OPEN", "time": datetime.now().isoformat()
                        })
                        
                        send_telegram(
                            f"✅ *{signal} EXECUTED*\n"
                            f"Asset: {symbol}\n"
                            f"Size: {amount} contracts\n"
                            f"Entry: {current_price:.4f}\n"
                            f"SL (ATR): {sl:.4f}\n"
                            f"TP: {tp:.4f}"
                        )
                except Exception as e:
                    print(f"Order Failed: {e}")
                    send_telegram(f"⚠️ Order Failed on {symbol}:\n{str(e)}")
            else:
                if time.time() - last_telegram_time > 3600:
                    vol_debug = "PASS" if (vol_ok_low or vol_ok_high) else "FAIL"
                    trend_str = 'BULLISH' if is_bullish else 'BEARISH' if is_bearish else 'FLAT'
                    
                    debug_msg = (
                        f"🔍 *{symbol} ANALYSIS (HOLD)*\n\n"
                        f"Price: {current_price:.2f}\n"
                        f"4H Trend: {trend_str}\n"
                        f"20-Bar Low: {recent_low:.2f}\n"
                        f"20-Bar High: {recent_high:.2f}\n"
                        f"Wick Swept Low? {swept_low}\n"
                        f"Wick Swept High? {swept_high}\n"
                        f"Volume Filter: {vol_debug}\n"
                        f"Result: No setup. Waiting."
                    )
                    send_telegram(debug_msg)
                    print(debug_msg)
                    last_telegram_time = time.time()
                else:
                    print(f"[{symbol}] No signal. (Skipping Telegram to prevent spam)")
                    
        except Exception as e:
            print(f"[ERROR] {symbol}: {e}")

    save_journal(journal)

if __name__ == "__main__":
    run_scanner()
