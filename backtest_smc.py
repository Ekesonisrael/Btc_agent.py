import ccxt
import pandas as pd
import numpy as np
import time

# ==========================================
# CONFIGURATION
# ==========================================
SYMBOLS = ['BTC/USDT:USDT', 'SOL/USDT:USDT']
RISK_REWARD = 1.5  # ✅ FIX 1: Lowered from 2.5 to 1.5 to increase win rate
BARS_1H = 3000  
BARS_4H = 1000  
FEE_RATE = 0.0005 
SLIPPAGE = 0.0005 
ATR_MULTIPLIER = 1.5 
VOLUME_MULTIPLIER = 1.5 # ✅ FIX 2: Sweep volume must be 1.5x average

exchange = ccxt.okx({'enableRateLimit': True, 'options': {'defaultType': 'swap'}})

def fetch_history(symbol, timeframe, limit):
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
        except Exception: break
    df = pd.DataFrame(all_ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
    df['time'] = pd.to_datetime(df['time'], unit='ms')
    return df.drop_duplicates('time').sort_values('time').reset_index(drop=True)

def run_backtest(symbol):
    print(f"Fetching data for {symbol}...")
    df_4h = fetch_history(symbol, '4h', BARS_4H)
    df_1h = fetch_history(symbol, '1h', BARS_1H)
    if df_4h.empty or df_1h.empty: return

    # 4H Trend
    df_4h['ema20'] = df_4h['close'].ewm(span=20, adjust=False).mean()
    df_4h['ema50'] = df_4h['close'].ewm(span=50, adjust=False).mean()
    df_4h['trend'] = np.where(df_4h['ema20'] > df_4h['ema50'], 1, -1)

    # Calculate ATR on 1H
    high_low = df_1h['high'] - df_1h['low']
    high_close = (df_1h['high'] - df_1h['close'].shift()).abs()
    low_close = (df_1h['low'] - df_1h['close'].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df_1h['atr'] = tr.rolling(window=14).mean()

    # ✅ FIX 2: Add Volume Moving Average
    df_1h['vol_ma'] = df_1h['volume'].rolling(window=20).mean()

    # Merge
    df_1h = pd.merge_asof(df_1h, df_4h[['time', 'trend']], on='time', direction='backward')
    df_1h['recent_low'] = df_1h['low'].rolling(window=20).min().shift(1)
    df_1h['recent_high'] = df_1h['high'].rolling(window=20).max().shift(1)

    # ✅ FIX 2: Add Volume Filter to Sweep Logic
    df_1h['swept_low'] = (df_1h['low'] < df_1h['recent_low']) & (df_1h['close'] > df_1h['recent_low']) & (df_1h['volume'] > df_1h['vol_ma'] * VOLUME_MULTIPLIER)
    df_1h['swept_high'] = (df_1h['high'] > df_1h['recent_high']) & (df_1h['close'] < df_1h['recent_high']) & (df_1h['volume'] > df_1h['vol_ma'] * VOLUME_MULTIPLIER)

    trades = []
    i = 20 

    # Ensure we have enough candles left for entry and confirmation
    while i < len(df_1h) - 2:
        row = df_1h.iloc[i]           # The Sweep Candle
        conf_row = df_1h.iloc[i+1]    # The Confirmation Candle
        entry_row = df_1h.iloc[i+2]   # The Entry Candle
        
        trade_entered = False
        side = None
        
        # ✅ FIX 3: Require Confirmation Candle (Green for Long, Red for Short)
        if row['trend'] == 1 and row['swept_low'] and conf_row['close'] > conf_row['open']:
            entry = entry_row['open']
            sl = entry - (row['atr'] * ATR_MULTIPLIER) 
            risk = entry - sl
            tp = entry + (risk * RISK_REWARD)
            trade_entered = True
            side = 'LONG'
            
        elif row['trend'] == -1 and row['swept_high'] and conf_row['close'] < conf_row['open']:
            entry = entry_row['open']
            sl = entry + (row['atr'] * ATR_MULTIPLIER) 
            risk = sl - entry
            tp = entry - (risk * RISK_REWARD)
            trade_entered = True
            side = 'SHORT'

        if trade_entered:
            exit_idx = None
            result = None
            
            cost = (entry * (FEE_RATE + SLIPPAGE)) * 2
            cost_r = cost / risk

            # Start checking for exit from the entry candle (i+2)
            for j in range(i+2, len(df_1h)):
                candle = df_1h.iloc[j]
                
                if side == 'LONG':
                    if candle['low'] <= sl: result, exit_idx = 'LOSS', j; break
                    elif candle['high'] >= tp: result, exit_idx = 'WIN', j; break
                elif side == 'SHORT':
                    if candle['high'] >= sl: result, exit_idx = 'LOSS', j; break
                    elif candle['low'] <= tp: result, exit_idx = 'WIN', j; break
                        
            if exit_idx:
                net_r = (RISK_REWARD if result == 'WIN' else -1.0) - cost_r
                trades.append({'result': result, 'r': net_r})
                i = exit_idx + 1 
            else: 
                i += 1 
        else: 
            i += 1

    if not trades: 
        print(f"No trades found for {symbol} with current filters.")
        return

    total = len(trades)
    wins = sum(1 for t in trades if t['result'] == 'WIN')
    total_r = sum(t['r'] for t in trades)
    
    print(f"\n=== {symbol} RESULTS (New Logic) ===")
    print(f"Total Trades: {total} | Wins: {wins} | Win Rate: {(wins/total)*100:.2f}%")
    print(f"Total R: {total_r:+.2f}R | Avg R: {(total_r/total):+.2f}R")

if __name__ == "__main__":
    for sym in SYMBOLS:
        run_backtest(sym)
