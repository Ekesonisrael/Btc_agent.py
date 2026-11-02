import os
import ccxt
import requests
import pandas as pd
import time
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

# Global variable for debug cooldown
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

def initialize_exchange():
    ex = ccxt.okx({
        'apiKey': API_KEY, 'secret': API_SECRET, 'password': API_PASSWORD,
        'enableRateLimit': True, 'options': {'defaultType': 'swap'}
    })
    ex.set_sandbox_mode(True) # DEMO MODE
    return ex

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
    print(f"--- Running Crypto Scan at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---")
    
    for symbol in SYMBOLS:
        try:
            # Fetch Data
            df_4h = fetch_history(exchange, symbol, '4h', 100)
            df_1h = fetch_history(exchange, symbol, '1h', 200)
            df_15m = fetch_history(exchange, symbol, '15m', 100)
            
            if df_4h.empty or df_15m.empty: continue

            # 4H Trend
            df_4h['ema20'] = df_4h['close'].ewm(span=20, adjust=False).mean()
            df_4h['ema50'] = df_4h['close'].ewm(span=50, adjust=False).mean()
            is_bullish = df_4h['ema20'].iloc[-1] > df_4h['ema50'].iloc[-1]
            is_bearish = df_4h['ema20'].iloc[-1] < df_4h['ema50'].iloc[-1]

            # ATR & Volume MA
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

            # Sweep & Volume Logic
            vol_ok_low = sweep_candle['volume'] > sweep_candle['vol_ma'] * VOLUME_MULTIPLIER
            vol_ok_high = sweep_candle['volume'] > sweep_candle['vol_ma'] * VOLUME_MULTIPLIER
            
            swept_low = (sweep_candle['low'] < recent_low) and (sweep_candle['close'] > recent_low) and vol_ok_low
            swept_high = (sweep_candle['high'] > recent_high) and (sweep_candle['close'] < recent_high) and vol_ok_high

            # Confirmation
            conf_long = conf_candle['close'] > conf_candle['open']
            conf_short = conf_candle['close'] < conf_candle['open']

            current_price = float(exchange.fetch_ticker(symbol)['last'])
            atr_value = float(sweep_candle['atr'])

            signal = None
            if is_bullish and swept_low and conf_long:
                signal = "LONG"
                sl = current_price - (atr_value * ATR_MULTIPLIER)
                risk = current_price - sl
                tp = current_price + (risk * RISK_REWARD)
            elif is_bearish and swept_high and conf_short:
                signal = "SHORT"
                sl = current_price + (atr_value * ATR_MULTIPLIER)
                risk = sl - current_price
                tp = current_price - (risk * RISK_REWARD)

            if signal:
                print(f"🚨 {symbol} SIGNAL: {signal}")
                # ... (Your Order Execution logic goes here, I'm keeping it brief to focus on the debug) ...
                # exchange.create_order(...)
            else:
                # 🚨 DEBUG TELEGRAM ALERT WITH 1-HOUR COOLDOWN
                if time.time() - last_telegram_time > 3600:
                    vol_debug = "PASS" if (vol_ok_low or vol_ok_high) else "FAIL"
                    
                    debug_msg = (
                        f"🔍 *{symbol} ANALYSIS (HOLD)*\n\n"
                        f"Price: {current_price:.2f}\n"
                        f"4H Trend: {'BULL' if is_bullish else 'BEAR' if is_bearish else 'FLAT'}\n"
                        f"20-Bar Low: {recent_low:.2f}\n"
                        f"20-Bar High: {recent_high:.2f}\n"
                        f"Wick Swept Low? {swept_low}\n"
                        f"Wick Swept High? {swept_high}\n"
                        f"Volume Filter: {vol_debug}\n"
                        f"Confirmation? L:{conf_long} S:{conf_short}\n"
                        f"Result: No setup. Waiting."
                    )
                    send_telegram(debug_msg)
                    print(debug_msg)
                    last_telegram_time = time.time()
                else:
                    print(f"[{symbol}] No signal. (Skipping Telegram to prevent spam)")
                    
        except Exception as e:
            print(f"[ERROR] {symbol}: {e}")

if __name__ == "__main__":
    run_scanner()
