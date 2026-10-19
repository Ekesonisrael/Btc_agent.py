import os
import ccxt
import pandas as pd
import numpy as np
import requests
import csv
from datetime import datetime, timezone

# ==========================================
# CONFIGURATION (MULTI-ASSET DEMO)
# ==========================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

OKX_API_KEY = os.getenv("OKX_API_KEY")
OKX_SECRET = os.getenv("OKX_SECRET")
OKX_PASSWORD = os.getenv("OKX_PASSWORD")

SYMBOLS = [
    'BTC/USDT:USDT', 
    'SOL/USDT:USDT', 
    'TRX/USDT:USDT', 
    'DOGE/USDT:USDT', 
    'PEPE/USDT:USDT'
]

JOURNAL_FILE = 'multi_journal.csv'

TARGET_VOL = 0.40       # Target 40% annualized volatility per asset
MAX_LEVERAGE = 3.0      # Cap leverage at 3x per asset
ATR_MULTIPLIER = 3.0    # Wide stop loss for daily timeframes
LOOKBACK = 50           # 50-day Donchian breakout

# Setup Exchange
exchange = ccxt.okx({
    'apiKey': OKX_API_KEY,
    'secret': OKX_SECRET,
    'password': OKX_PASSWORD,
    'options': {'defaultType': 'swap'}
})
exchange.set_sandbox_mode(True) 

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram Error: {e}")

def run_agent():
    now = datetime.now(timezone.utc)
    print(f"--- Multi-Agent Scanning at {now} ---")
    
    if not os.path.isfile(JOURNAL_FILE):
        with open(JOURNAL_FILE, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(["Date", "Symbol", "Signal", "Entry", "Stop_Loss", "Contracts", "Reason"])

    try:
        balance = exchange.fetch_balance()
        account_equity = float(balance['USDT']['total'])
        if account_equity <= 0: 
            account_equity = 10000.0
        print(f"Demo Account Equity: ${account_equity:.2f}")
    except Exception as e:
        print(f"Balance Error: {e}")
        return

    for symbol in SYMBOLS:
        print(f"\n--- Analyzing {symbol} ---")
        
        try:
            # 1. Set Leverage
            exchange.set_leverage(MAX_LEVERAGE, symbol)
            
            # 2. Check for Open Positions
            positions = exchange.fetch_positions([symbol])
            has_position = False
            for pos in positions:
                if float(pos['contracts']) > 0:
                    print(f"  Position already open ({pos['side']}). Skipping.")
                    has_position = True
                    break
            if has_position:
                continue

            # 3. Fetch Data (Daily timeframe for macro trend)
            ohlcv = exchange.fetch_ohlcv(symbol, '1d', limit=100)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            
            # 4. Calculate Indicators
            df['hh_50'] = df['high'].rolling(LOOKBACK).max().shift(1)
            df['ll_50'] = df['low'].rolling(LOOKBACK).min().shift(1)
            
            # ATR
            high_low = df['high'] - df['low']
            high_close = (df['high'] - df['close'].shift()).abs()
            low_close = (df['low'] - df['close'].shift()).abs()
            tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
            df['atr'] = tr.rolling(14).mean()
            
            # Volatility (30-day annualized)
            df['log_ret'] = np.log(df['close'] / df['close'].shift(1))
            df['vol'] = df['log_ret'].rolling(30).std() * np.sqrt(365)

            last = df.iloc[-2] # Last closed daily candle
            
            if pd.isna(last['hh_50']) or pd.isna(last['vol']) or last['vol'] <= 0:
                print("  Not enough data or zero volatility. Skipping.")
                continue

            # 5. Determine Signal
            signal = "WAIT"
            if last['close'] > last['hh_50']:
                signal = "LONG"
            elif last['close'] < last['ll_50']:
                signal = "SHORT"
                
            print(f"  Price: {last['close']:.4f} | 50d High: {last['hh_50']:.4f} | 50d Low: {last['ll_50']:.4f} | Signal: {signal}")

            # 6. Execution
            if signal in ["LONG", "SHORT"]:
                live_price = exchange.fetch_ticker(symbol)['last']
                atr = last['atr']
                realized_vol = last['vol']
                
                if signal == "LONG":
                    side = 'buy'
                    stop_loss = live_price - (ATR_MULTIPLIER * atr)
                else:
                    side = 'sell'
                    stop_loss = live_price + (ATR_MULTIPLIER * atr)

                # 7. Volatility Sizing
                # Allocate 1/5th of the portfolio to each asset, then scale by Vol Target
                asset_allocation = account_equity / len(SYMBOLS)
                vol_scalar = TARGET_VOL / realized_vol
                position_value = asset_allocation * vol_scalar
                
                # Cap position value by MAX_LEVERAGE
                position_value = min(position_value, asset_allocation * MAX_LEVERAGE)
                
                # Calculate Contracts (accounting for contract size)
                market = exchange.market(symbol)
                contract_size = market['contractSize']
                
                contracts = position_value / live_price / contract_size
                contracts = float(exchange.amount_to_precision(symbol, contracts))

                if contracts <= 0:
                    print("  Position size too small. Skipping.")
                    continue

                print(f"  Placing {side} for {contracts} contracts (Value: ${position_value:.2f}, VolScalar: {vol_scalar:.2f})...")
                
                # Place Order
                params = {'stopLoss': {'triggerPrice': round(stop_loss, 6)}}
                order = exchange.create_order(symbol, 'market', side, contracts, params=params)
                print(f"  Order Successful! ID: {order['id']}")
                
                # Log to CSV
                with open(JOURNAL_FILE, mode='a', newline='') as f:
                    writer = csv.writer(f)
                    writer.writerow([now, symbol, signal, live_price, round(stop_loss,6), contracts, f"VolScalar:{vol_scalar:.2f}"])
                    
                msg = (
                    f"🚨 *MULTI-ASSET TRADE* 🚨\n\n"
                    f"*Symbol:* {symbol}\n"
                    f"*Signal:* {signal}\n"
                    f"*Entry:* {live_price:.4f}\n"
                    f"*Stop:* {stop_loss:.4f}\n"
                    f"*Size:* {contracts} contracts\n"
                    f"*Volatility:* {realized_vol*100:.1f}%"
                )
                send_telegram(msg)
                
        except Exception as e:
            print(f"  Error on {symbol}: {e}")
            continue

if __name__ == "__main__":
    run_agent()
