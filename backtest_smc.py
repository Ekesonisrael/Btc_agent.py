import ccxt
import pandas as pd
import numpy as np
import time

# ==========================================
# CONFIGURATION
# ==========================================
SYMBOLS = ['BTC/USDT:USDT', 'SOL/USDT:USDT']
RISK_REWARD = 2.5
BARS_15M = 5000 # Approx 50 days of 15m data
BARS_4H = 1000  # Approx 160 days of 4h data

exchange = ccxt.okx({'enableRateLimit': True, 'options': {'defaultType': 'swap'}})

def fetch_history(symbol, timeframe, limit):
    print(f"Fetching {timeframe} for {symbol}...")
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
            print(f"Error: {e}")
            break
            
    df = pd.DataFrame(all_ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
    df['time'] = pd.to_datetime(df['time'], unit='ms')
    return df.drop_duplicates('time').sort_values('time').reset_index(drop=True)

def run_backtest(symbol):
    df_4h = fetch_history(symbol, '4h', BARS_4H)
    df_15m = fetch_history(symbol, '15m', BARS_15M)
    
    if df_4h.empty or df_15m.empty:
        print(f"Data missing for {symbol}")
        return

    # Calculate 4H Trend
    df_4h['ema20'] = df_4h['close'].ewm(span=20, adjust=False).mean()
    df_4h['ema50'] = df_4h['close'].ewm(span=50, adjust=False).mean()
    df_4h['trend'] = np.where(df_4h['ema20'] > df_4h['ema50'], 1, -1)

    # Merge Trend into 15M data
    df_15m = pd.merge_asof(df_15m, df_4h[['time', 'trend']], on='time', direction='backward')
    df_15m['recent_low'] = df_15m['low'].rolling(window=20).min().shift(1)
    df_15m['recent_high'] = df_15m['high'].rolling(window=20).max().shift(1)

    # Sweep Logic
    df_15m['swept_low'] = (df_15m['low'] < df_15m['recent_low']) & (df_15m['close'] > df_15m['recent_low'])
    df_15m['swept_high'] = (df_15m['high'] > df_15m['recent_high']) & (df_15m['close'] < df_15m['recent_high'])

    trades = []
    i = 20 # Skip the warmup period for indicators

    # ✅ FIXED LOOP
    while i < len(df_15m) - 1:
        row = df_15m.iloc[i]
        next_row = df_15m.iloc[i+1]
        
        trade_entered = False
        
        # Long Setup
        if row['trend'] == 1 and row['swept_low']:
            entry = next_row['open']
            sl = row['recent_low'] * 0.9995
            risk = entry - sl
            tp = entry + (risk * RISK_REWARD)
            trade_entered = True
            
        # Short Setup
        elif row['trend'] == -1 and row['swept_high']:
            entry = next_row['open']
            sl = row['recent_high'] * 1.0005
            risk = sl - entry
            tp = entry - (risk * RISK_REWARD)
            trade_entered = True

        if trade_entered:
            # Look forward to see if we hit TP or SL
            exit_idx = None
            for j in range(i+1, len(df_15m)):
                candle = df_15m.iloc[j]
                
                # LONG exit check
                if row['trend'] == 1 and row['swept_low']:
                    if candle['low'] <= sl:
                        trades.append('LOSS')
                        exit_idx = j
                        break
                    elif candle['high'] >= tp:
                        trades.append('WIN')
                        exit_idx = j
                        break
                        
                # SHORT exit check
                elif row['trend'] == -1 and row['swept_high']:
                    if candle['high'] >= sl:
                        trades.append('LOSS')
                        exit_idx = j
                        break
                    elif candle['low'] <= tp:
                        trades.append('WIN')
                        exit_idx = j
                        break
                        
            if exit_idx:
                i = exit_idx + 1 # Jump to the candle after the trade closed
            else:
                i += 1 # If trade never closed (ran out of data), just move forward
        else:
            i += 1

    # Report
    if not trades:
        print(f"\n{symbol}: No trades found.")
        return

    total = len(trades)
    wins = trades.count('WIN')
    losses = trades.count('LOSS')
    win_rate = (wins / total) * 100
    total_r = (wins * RISK_REWARD) - (losses * 1.0)
    
    print(f"\n=== {symbol} RESULTS ===")
    print(f"Total Trades: {total}")
    print(f"Wins: {wins} | Losses: {losses}")
    print(f"Win Rate: {win_rate:.2f}%")
    print(f"Total R: {total_r:+.2f}R")
    print(f"Average R per trade: {(total_r/total):+.2f}R")

if __name__ == "__main__":
    for sym in SYMBOLS:
        run_backtest(sym)
