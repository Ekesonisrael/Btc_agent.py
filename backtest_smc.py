import ccxt
import pandas as pd
import numpy as np
import requests
import time
from datetime import datetime

# ==========================================
# CONFIGURATION
# ==========================================
SYMBOLS = {
    'BTC/USDT:USDT': 'BTC',
    'SOL/USDT:USDT': 'SOL'
}
RISK_REWARD = 2.5
STARTING_BALANCE = 10000.0

exchange = ccxt.okx({'enableRateLimit': True, 'options': {'defaultType': 'swap'}})

def fetch_okx(symbol, timeframe, limit=1000):
    """Fetches historical data from OKX."""
    print(f"Fetching {timeframe} for {symbol}...")
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
        df['time'] = pd.to_datetime(df['time'], unit='ms')
        return df
    except Exception as e:
        print(f"Error fetching {symbol}: {e}")
        return pd.DataFrame()

def run_backtest(symbol):
    # 1. Fetch 4H and 15M Data
    # Note: For a real test, we would paginate to get years of data. 
    # OKX limits to 1000 bars per request. 1000 bars of 15m is ~10 days. 
    # For this script, we will pull the max available in one go for the demonstration.
    df_4h = fetch_okx(symbol, '4h', limit=1000)
    df_15m = fetch_okx(symbol, '15m', limit=1000)
    
    if df_4h.empty or df_15m.empty:
        return

    # 2. Calculate 4H Trend
    df_4h['ema20'] = df_4h['close'].ewm(span=20, adjust=False).mean()
    df_4h['ema50'] = df_4h['close'].ewm(span=50, adjust=False).mean()
    df_4h['trend'] = np.where(df_4h['ema20'] > df_4h['ema50'], 1, -1)

    # 3. Merge 4H trend into 15M data (forward fill)
    df_15m = pd.merge_asof(df_15m, df_4h[['time', 'trend']], on='time', direction='backward')

    # 4. Find 15M Sweeps
    df_15m['recent_low'] = df_15m['low'].rolling(window=20).min().shift(1)
    df_15m['recent_high'] = df_15m['high'].rolling(window=20).max().shift(1)

    # Sweep Logic
    df_15m['swept_low'] = (df_15m['low'] < df_15m['recent_low']) & (df_15m['close'] > df_15m['recent_low'])
    df_15m['swept_high'] = (df_15m['high'] > df_15m['recent_high']) & (df_15m['close'] < df_15m['recent_high'])

    # 5. Simulate Trades
    trades = []
    in_trade = False

    for i in range(1, len(df_15m) - 1):
        if in_trade:
            continue

        row = df_15m.iloc[i]
        next_row = df_15m.iloc[i+1] # Enter on next candle
        
        # Long Setup
        if row['trend'] == 1 and row['swept_low']:
            entry = next_row['open']
            sl = row['recent_low'] * 0.9995
            risk = entry - sl
            tp = entry + (risk * RISK_REWARD)

            # Check if TP or SL was hit in the subsequent candles
            for j in range(i+1, len(df_15m)):
                candle = df_15m.iloc[j]
                if candle['low'] <= sl:
                    trades.append({'result': 'LOSS', 'r': -1.0})
                    in_trade = True
                    break
                elif candle['high'] >= tp:
                    trades.append({'result': 'WIN', 'r': RISK_REWARD})
                    in_trade = True
                    break

        # Short Setup
        elif row['trend'] == -1 and row['swept_high']:
            entry = next_row['open']
            sl = row['recent_high'] * 1.0005
            risk = sl - entry
            tp = entry - (risk * RISK_REWARD)

            for j in range(i+1, len(df_15m)):
                candle = df_15m.iloc[j]
                if candle['high'] >= sl:
                    trades.append({'result': 'LOSS', 'r': -1.0})
                    in_trade = True
                    break
                elif candle['low'] <= tp:
                    trades.append({'result': 'WIN', 'r': RISK_REWARD})
                    in_trade = True
                    break

    # 6. Report
    if not trades:
        print(f"\n{symbol}: No trades found in this data window.")
        return

    df_trades = pd.DataFrame(trades)
    wins = len(df_trades[df_trades['result'] == 'WIN'])
    losses = len(df_trades[df_trades['result'] == 'LOSS'])
    total = len(df_trades)
    win_rate = (wins / total) * 100 if total > 0 else 0
    total_r = df_trades['r'].sum()
    
    print(f"\n=== {symbol} RESULTS ===")
    print(f"Total Trades: {total}")
    print(f"Wins: {wins} | Losses: {losses}")
    print(f"Win Rate: {win_rate:.2f}%")
    print(f"Total R: {total_r:+.2f}R")
    print(f"Average R per trade: {(total_r/total):+.2f}R")

if __name__ == "__main__":
    for sym in SYMBOLS:
        run_backtest(sym)
