import os
import json
import time
import ccxt
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone

# ==========================================
# CONFIGURATION & SECRETS (OKX)
# ==========================================
API_KEY = os.getenv("OKX_API_KEY")
API_SECRET = os.getenv("OKX_SECRET")
API_PASSWORD = os.getenv("OKX_PASSWORD")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SANDBOX = os.getenv("OKX_SANDBOX", "true").lower() == "true"
POS_MODE = os.getenv("OKX_POS_MODE", "net").lower()

# ✅ ONLY SOLANA
SYMBOLS = ["SOL/USDT:USDT"]

LEVERAGE = 3
RISK_PERCENTAGE = 0.015      # 1.5% risk per trade
JOURNAL_FILE = "trade_journal.json"

# ==========================================
# HELPER FUNCTIONS
# ==========================================
def now_utc():
    return datetime.now(timezone.utc)

def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r") as f: return json.load(f)
        except: pass
    return default

def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f: json.dump(data, f, indent=4)
    os.replace(tmp, path)

def send_telegram(message, markdown=True):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    if markdown: payload["parse_mode"] = "Markdown"
    try:
        r = requests.post(url, json=payload, timeout=10)
        if not r.ok and markdown: send_telegram(message, markdown=False)
    except: pass

def initialize_exchange():
    ex = ccxt.okx({
        "apiKey": API_KEY, "secret": API_SECRET, "password": API_PASSWORD,
        "enableRateLimit": True, "options": {"defaultType": "swap"},
    })
    ex.set_sandbox_mode(SANDBOX)
    ex.load_markets()
    return ex

def fetch_ohlcv(exchange, symbol, timeframe, limit):
    try:
        return exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    except Exception as e:
        print(f"Fetch error {symbol} {timeframe}: {e}")
        return []

# ==========================================
# STRATEGY ENGINE: SOL 4H TREND
# ==========================================
def run_sol_strategy(exchange, journal):
    symbol = "SOL/USDT:USDT"
    print(f"--- Scanning {symbol} (4H Trend) ---", flush=True)
    
    # 1. MANAGE OPEN TRADES (Trailing Stop Logic)
    open_trades = [t for t in journal if t['symbol'] == symbol and t['status'] == "OPEN"]
    for trade in open_trades:
        try:
            current_price = float(exchange.fetch_ticker(symbol)['last'])
            
            # We need the current 4H ATR to calculate the trail
            raw_4h = fetch_ohlcv(exchange, symbol, "4h", 50)
            if not raw_4h: continue
            df = pd.DataFrame(raw_4h, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
            df['tr'] = pd.concat([df['high'] - df['low'], (df['high'] - df['close'].shift()).abs(), (df['low'] - df['close'].shift()).abs()], axis=1).max(axis=1)
            df['atr'] = df['tr'].ewm(alpha=1/14, adjust=False).mean()
            current_atr = df['atr'].iloc[-1]
            
            if trade['side'] == 'buy':
                # Trail the stop up based on Current Price - (3 * ATR)
                trail_sl = current_price - (current_atr * 3.0)
                if trail_sl > trade['sl']:
                    trade['sl'] = trail_sl
                    save_json(JOURNAL_FILE, journal)
                    print(f"[{symbol}] Trailing Stop updated to {trail_sl:.4f}", flush=True)
                
                # Check if hit
                if current_price <= trade['sl']:
                    close_trade(exchange, journal, trade, current_price, "Trailing Stop Hit")
                    return
            else:
                # Trail the stop down based on Current Price + (3 * ATR)
                trail_sl = current_price + (current_atr * 3.0)
                if trail_sl < trade['sl']:
                    trade['sl'] = trail_sl
                    save_json(JOURNAL_FILE, journal)
                    print(f"[{symbol}] Trailing Stop updated to {trail_sl:.4f}", flush=True)
                
                # Check if hit
                if current_price >= trade['sl']:
                    close_trade(exchange, journal, trade, current_price, "Trailing Stop Hit")
                    return
        except Exception as e:
            print(f"Error managing SOL trade: {e}", flush=True)

    # 2. CHECK FOR NEW ENTRIES
    if open_trades: 
        print(f"[SKIP] {symbol}: Trade already open.", flush=True)
        return

    raw_4h = fetch_ohlcv(exchange, symbol, "4h", 100)
    if not raw_4h: return

    df = pd.DataFrame(raw_4h, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
    df = df.iloc[:-1].reset_index(drop=True) # Drop the currently forming 4H candle

    # Indicators
    df['tr'] = pd.concat([df['high'] - df['low'], (df['high'] - df['close'].shift()).abs(), (df['low'] - df['close'].shift()).abs()], axis=1).max(axis=1)
    df['atr'] = df['tr'].ewm(alpha=1/14, adjust=False).mean()
    df['ema200'] = df['close'].ewm(span=200, adjust=False).mean()
    df['vol_ma'] = df['volume'].rolling(20).mean()
    df['don_high'] = df['high'].rolling(20).max().shift(1)
    df['don_low'] = df['low'].rolling(20).min().shift(1)
    
    up = df['high'].diff()
    down = -df['low'].diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr_s = df['tr'].ewm(alpha=1/14, adjust=False).mean()
    pdi = 100 * pd.Series(plus_dm).ewm(alpha=1/14, adjust=False).mean() / tr_s
    mdi = 100 * pd.Series(minus_dm).ewm(alpha=1/14, adjust=False).mean() / tr_s
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    df['adx'] = dx.ewm(alpha=1/14, adjust=False).mean()

    last = df.iloc[-1]
    
    is_trending = last['adx'] > 20
    vol_ok = last['volume'] > last['vol_ma'] * 1.5
    
    side = None
    if is_trending and vol_ok and last['close'] > last['ema200'] and last['close'] > last['don_high']:
        side = 'buy'
    elif is_trending and vol_ok and last['close'] < last['ema200'] and last['close'] < last['don_low']:
        side = 'sell'

    if side:
        entry = float(exchange.fetch_ticker(symbol)['last'])
        atr = last['atr']
        
        if side == 'buy':
            sl = entry - (atr * 2.0) # Initial 2.0 ATR stop
            tp = 0 # No fixed TP, trailing stop handles it
        else:
            sl = entry + (atr * 2.0)
            tp = 0 

        execute_trade(exchange, journal, symbol, side, entry, sl, tp, atr)

# ==========================================
# EXECUTION & MANAGEMENT
# ==========================================
def execute_trade(exchange, journal, symbol, side, entry, sl, tp, atr):
    try:
        # ✅ ISOLATED MARGIN
        exchange.set_leverage(LEVERAGE, symbol, {'mgnMode': 'isolated'})
        
        balance = exchange.fetch_balance()['USDT']['free']
        risk_amount = balance * RISK_PERCENTAGE
        
        price_risk = abs(entry - sl)
        market = exchange.market(symbol)
        contract_size = float(market['contractSize'])
        
        raw_contracts = (risk_amount / price_risk) / contract_size
        max_position_value = (balance * LEVERAGE) * 0.90
        max_contracts_margin = max_position_value / entry / contract_size
        amount = float(exchange.amount_to_precision(symbol, min(raw_contracts, max_contracts_margin)))

        if amount <= 0: return

        params = {'tdMode': 'isolated'}
        if sl > 0:
            params['stopLoss'] = {'triggerPrice': float(exchange.price_to_precision(symbol, sl))}

        order = exchange.create_order(symbol, 'market', side, amount, params=params)
        
        trade_data = {
            "symbol": symbol, "side": side, "entry": entry, "sl": sl, "tp": 0,
            "amount": amount, "status": "OPEN", "atr": atr,
            "time": now_utc().isoformat()
        }
        
        journal.append(trade_data)
        save_json(JOURNAL_FILE, journal)
        
        send_telegram(f"✅ *SOL 4H TREND EXECUTED*\nAsset: {symbol}\nSide: {side.upper()}\nEntry: {entry:.4f}\nInitial SL: {sl:.4f}")
        
    except Exception as e:
        print(f"Order Failed: {e}")
        send_telegram(f"⚠️ Order Failed {symbol}: {e}", markdown=False)

def close_trade(exchange, journal, trade, exit_price, reason):
    try:
        side = 'sell' if trade['side'] == 'buy' else 'buy'
        exchange.create_order(trade['symbol'], 'market', side, trade['amount'], params={'reduceOnly': True})
        
        trade['status'] = 'CLOSED'
        trade['exit'] = exit_price
        trade['close_reason'] = reason
        trade['closed_at'] = now_utc().isoformat()
        save_json(JOURNAL_FILE, journal)
        
        send_telegram(f"🛑 *TRADE CLOSED* ({reason})\nAsset: {trade['symbol']}\nEntry: {trade['entry']:.4f}\nExit: {exit_price:.4f}")
    except Exception as e:
        print(f"Close error: {e}")

# ==========================================
# MAIN
# ==========================================
if __name__ == "__main__":
    exchange = initialize_exchange()
    journal = load_json(JOURNAL_FILE, [])
    
    print(f"--- Master Bot Run at {now_utc().strftime('%Y-%m-%d %H:%M:%S')} UTC ---", flush=True)
    
    run_sol_strategy(exchange, journal)
