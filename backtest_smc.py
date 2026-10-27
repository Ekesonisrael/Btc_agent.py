import ccxt
import pandas as pd
import numpy as np
import time

# ==========================================
# CONFIGURATION
# ==========================================
SYMBOLS = ['BTC/USDT:USDT', 'SOL/USDT:USDT']
RISK_REWARD = 2.5
BARS_1H = 3000  # Approx 4 months of 1H data
BARS_4H = 1000  # Approx 160 days of 4h data
FEE_RATE = 0.0005 # 0.05% per side (OKX Taker)
SLIPPAGE = 0.0005 # 0.05% per side

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
    df_1h = fetch_history(symbol, '1h', BARS_1H) # ✅ CHANGED TO 1H
    
    if df_4h.empty or df_1h.empty: return

    # 4H Trend
    df_4h['ema20'] = df_4h['close'].ewm(span=20, adjust=False).mean()
    df_4h['ema50'] = df_4h['close'].ewm(span=50, adjust=False).mean()
    df_4h['trend'] = np.where(df_4h['ema20'] > df_4h['ema50'], 1, -1)

    # Merge
    df_1h = pd.merge_asof(df_1h, df_4h[['time', 'trend']], on='time', direction='backward')
    df_1h['recent_low'] = df_1h['low'].rolling(window=20).min().shift(1)
    df_1h['recent_high'] = df_1h['high'].rolling(window=20).max().shift(1)

    # Sweep Logic
    df_1h['swept_low'] = (df_1h['low'] < df_1h['recent_low']) & (df_1h['close'] > df_1h['recent_low'])
    df_1h['swept_high'] = (df_1h['high'] > df_1h['recent_high']) & (df_1h['close'] < df_1h['recent_high'])

    trades = []
    i = 20 

    while i < len(df_1h) - 1:
        row = df_1h.iloc[i]
        next_row = df_1h.iloc[i+1]
        
        trade_entered = False
        side = None
        
        if row['trend'] == 1 and row['swept_low']:
            entry = next_row['open']
            sl = row['recent_low'] * 0.9995
            risk = entry - sl
            tp = entry + (risk * RISK_REWARD)
            trade_entered = True
            side = 'LONG'
            
        elif row['trend'] == -1 and row['swept_high']:
            entry = next_row['open']
            sl = row['recent_high'] * 1.0005
            risk = sl - entry
            tp = entry - (risk * RISK_REWARD)
            trade_entered = True
            side = 'SHORT'

        if trade_entered:
            exit_idx = None
            result = None
            
            # Calculate Trade Cost (Fees + Slippage) in price terms
            cost = (entry * (FEE_RATE + SLIPPAGE)) * 2
            cost_r = cost / risk

            for j in range(i+1, len(df_1h)):
                candle = df_1h.iloc[j]
                
                if side == 'LONG':
                    if candle['low'] <= sl:
                        result = 'LOSS'
                        exit_idx = j
                        break
                    elif candle['high'] >= tp:
                        result = 'WIN'
                        exit_idx = j
                        break
                        
                elif side == 'SHORT':
                    if candle['high'] >= sl:
                        result = 'LOSS'
                        exit_idx = j
                        break
                    elif candle['low'] <= tp:
                        result = 'WIN'
                        exit_idx = j
                        break
                        
            if exit_idx:
                if result == 'WIN':
                    net_r = RISK_REWARD - cost_r
                else:
                    net_r = -1.0 - cost_r
                    
                trades.append({'result': result, 'r': net_r})
                i = exit_idx + 1 
            else:
                i += 1 
        else:
            i += 1

    if not trades: return

    total = len(trades)
    wins = sum(1 for t in trades if t['result'] == 'WIN')
    losses = sum(1 for t in trades if t['result'] == 'LOSS')
    win_rate = (wins / total) * 100
    total_r = sum(t['r'] for t in trades)
    
    print(f"\n=== {symbol} RESULTS (1H Chart, After Fees) ===")
    print(f"Total Trades: {total}")
    print(f"Wins: {wins} | Losses: {losses}")
    print(f"Win Rate: {win_rate:.2f}%")
    print(f"Total R: {total_r:+.2f}R")
    print(f"Average R per trade: {(total_r/total):+.2f}R")

if __name__ == "__main__":
    for sym in SYMBOLS:
        run_backtest(sym)
