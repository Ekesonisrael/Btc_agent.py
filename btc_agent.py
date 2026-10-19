import os
import ccxt
import pandas as pd
import requests
import csv
from datetime import datetime, timezone

# ==========================================
# CONFIGURATION (BTC ONLY - OKX DEMO)
# ==========================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

OKX_API_KEY = os.getenv("OKX_API_KEY")
OKX_SECRET = os.getenv("OKX_SECRET")
OKX_PASSWORD = os.getenv("OKX_PASSWORD")

SYMBOL = 'BTC/USDT:USDT'
JOURNAL_FILE = 'btc_journal.csv'

RISK_PERCENT = 0.02        # Risk 2% of balance
ATR_MULTIPLIER = 1.5       
RR_RATIO = 2.0             
LEVERAGE = 3

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

def calculate_indicators(df):
    df['EMA_20'] = df['close'].ewm(span=20, adjust=False).mean()
    df['EMA_50'] = df['close'].ewm(span=50, adjust=False).mean()
    
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))
    
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift()).abs()
    low_close = (df['low'] - df['close'].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['ATR'] = tr.rolling(window=14).mean()
    
    return df

def run_agent():
    now = datetime.now(timezone.utc)
    print(f"--- Agent Scanning BTC at {now} ---")
    
    # 1. Set Leverage
    try:
        exchange.set_leverage(LEVERAGE, SYMBOL)
    except Exception as e:
        print(f"Leverage setting failed (might already be set): {e}")

    # 2. Check for Open Positions
    try:
        positions = exchange.fetch_positions([SYMBOL])
        for pos in positions:
            if float(pos['contracts']) > 0:
                print(f"Position already open ({pos['side']} {pos['contracts']} contracts). Skipping.")
                return
    except Exception as e:
        print(f"Error checking positions: {e}")
        return

    # 3. Fetch Data
    try:
        ohlcv = exchange.fetch_ohlcv(SYMBOL, '1h', limit=100)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df = calculate_indicators(df)
    except Exception as e:
        print(f"Data Fetch Error: {e}")
        return
    
    last = df.iloc[-2] 
    ema20 = last['EMA_20']
    ema50 = last['EMA_50']
    rsi = last['RSI']
    atr = last['ATR']

    # 4. The Strategy (Pure Math)
    signal = "WAIT"
    reason = "No setup."

    if last['close'] > ema20 > ema50 and rsi < 70:
        signal = "LONG"
        reason = f"Price > 20EMA > 50EMA. RSI {rsi:.1f}"
    elif last['close'] < ema20 < ema50 and rsi > 30:
        signal = "SHORT"
        reason = f"Price < 20EMA < 50EMA. RSI {rsi:.1f}"

    print(f"Signal: {signal} | Reason: {reason}")

    # 5. Execute Trade
    if signal in ["LONG", "SHORT"]:
        live_price = exchange.fetch_ticker(SYMBOL)['last']
        
        if signal == "LONG":
            stop_loss = live_price - (ATR_MULTIPLIER * atr)
            risk_per_coin = live_price - stop_loss
            take_profit = live_price + (risk_per_coin * RR_RATIO)
            side = 'buy'
        else:
            stop_loss = live_price + (ATR_MULTIPLIER * atr)
            risk_per_coin = stop_loss - live_price
            take_profit = live_price - (risk_per_coin * RR_RATIO)
            side = 'sell'

        # Fetch real balance
        try:
            balance = exchange.fetch_balance()
            account_balance = float(balance['USDT']['free'])
        except:
            account_balance = 10000.0

        risk_amount = account_balance * RISK_PERCENT
        
        # 6. THE CONTRACT SIZE FIX
        market = exchange.market(SYMBOL)
        contract_size = market['contractSize'] 
        
        base_amount = risk_amount / risk_per_coin
        contracts_to_trade = base_amount / contract_size
        
        # Format to precision
        contracts_to_trade = float(exchange.amount_to_precision(SYMBOL, contracts_to_trade))

        if contracts_to_trade <= 0:
            print("Position size too small. Skipping.")
            return

        try:
            print(f"Placing {side} for {contracts_to_trade} contracts...")
            params = {
                'stopLoss': {'triggerPrice': round(stop_loss, 2)},
                'takeProfit': {'triggerPrice': round(take_profit, 2)}
            }
            
            order = exchange.create_order(
                symbol=SYMBOL, type='market', side=side, amount=contracts_to_trade, params=params
            )
            print(f"Order Successful! ID: {order['id']}")
            
            # 7. CSV FIX
            file_exists = os.path.isfile(JOURNAL_FILE)
            with open(JOURNAL_FILE, mode='a', newline='') as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow(["Date", "Symbol", "Signal", "Entry", "Stop_Loss", "Take_Profit", "Contracts", "Reason", "Outcome"])
                writer.writerow([now, SYMBOL, signal, live_price, round(stop_loss,2), round(take_profit,2), contracts_to_trade, reason, "PENDING"])
                
            send_telegram(f"🚨 DEMO TRADE EXECUTED\nSignal: {signal}\nEntry: ${live_price:.2f}\nStop: ${stop_loss:.2f}\nTarget: ${take_profit:.2f}\nSize: {contracts_to_trade} contracts\nReason: {reason}")
            
        except Exception as e:
            print(f"Order Failed: {e}")
            send_telegram(f"⚠️ Signal: {signal}, but Order Failed: {e}")
            
    else:
        print("No trade.")

if __name__ == "__main__":
    run_agent()
