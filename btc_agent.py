import os
import ccxt
import pandas as pd
import requests
import time
from google import genai
from datetime import datetime

# ==========================================
# CONFIGURATION (BTC ONLY)
# ==========================================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOL = 'BTC/USDT:USDT'
JOURNAL_FILE = 'btc_journal.csv'

# Risk Settings
ACCOUNT_BALANCE = 1000.0  # Change this to your real balance
RISK_PERCENT = 0.02       # Risk 2% per trade
ATR_MULTIPLIER = 1.5      
RR_RATIO = 2.0            

# Setup AI & Exchange
client = genai.Client(api_key=GEMINI_API_KEY)
exchange = ccxt.okx({'options': {'defaultType': 'swap'}})

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
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
    print(f"--- AI Agent Scanning BTC at {datetime.now()} ---")
    
    # Ensure journal exists (Fix for git pathspec error)
    if not os.path.isfile(JOURNAL_FILE):
        with open(JOURNAL_FILE, 'w') as f:
            f.write("Date,Symbol,Signal,Entry,Stop_Loss,Take_Profit,Size,Reason\n")
    
    ohlcv = exchange.fetch_ohlcv(SYMBOL, '1h', limit=100)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df = calculate_indicators(df)
    
    last = df.iloc[-2] 
    current_price = last['close']
    ema20 = last['EMA_20']
    ema50 = last['EMA_50']
    rsi = last['RSI']
    atr = last['ATR']
    
    prompt = f"""
    You are a quantitative crypto futures trader analyzing BTC.
    Current Price: ${current_price:.2f}
    20 EMA: ${ema20:.2f}
    50 EMA: ${ema50:.2f}
    RSI (14): {rsi:.2f}
    ATR (14): {atr:.2f}
    
    Rules:
    - If trend is UP (Price > 20EMA > 50EMA) and RSI < 70, look for LONG.
    - If trend is DOWN (Price < 20EMA < 50EMA) and RSI > 30, look for SHORT.
    - If the market is choppy or RSI is overextended, say WAIT.
    
    Output EXACTLY this format and nothing else:
    SIGNAL | REASON
    Example: LONG | Price broke above EMA20 with strong RSI momentum
    """
    
    # Retry Logic for 503 High Demand errors
    ai_output = ""
    for attempt in range(3):
        try:
            response = client.models.generate_content(model='gemini-2.0-flash', contents=prompt)
            ai_output = response.text.strip()
            break # Success, exit loop
        except Exception as e:
            print(f"AI Attempt {attempt+1} failed: {e}")
            if attempt < 2:
                print("Waiting 5 seconds to retry...")
                time.sleep(5)
            else:
                print("All AI attempts failed. Skipping this hour.")
                return

    print(f"AI Output: {ai_output}")
    
    if "|" not in ai_output:
        print("AI failed to format correctly. Skipping.")
        return
        
    signal, reason = ai_output.split("|", 1)
    signal = signal.strip().upper()
    reason = reason.strip()
    
    if signal in ["LONG", "SHORT"]:
        if signal == "LONG":
            stop_loss = current_price - (ATR_MULTIPLIER * atr)
            risk_per_coin = current_price - stop_loss
            take_profit = current_price + (risk_per_coin * RR_RATIO)
        else:
            stop_loss = current_price + (ATR_MULTIPLIER * atr)
            risk_per_coin = stop_loss - current_price
            take_profit = current_price - (risk_per_coin * RR_RATIO)
            
        risk_amount = ACCOUNT_BALANCE * RISK_PERCENT
        position_size = risk_amount / risk_per_coin
        
        with open(JOURNAL_FILE, mode='a') as f:
            f.write(f"{datetime.now()},{SYMBOL},{signal},{current_price:.2f},{stop_loss:.2f},{take_profit:.2f},{position_size:.4f},{reason}\n")
            
        msg = (
            f"🚨 *BTC AI TRADE ALERT* 🚨\n\n"
            f"*Signal:* {signal}\n"
            f"*Entry:* ${current_price:.2f}\n"
            f"*Stop Loss:* ${stop_loss:.2f}\n"
            f"*Take Profit:* ${take_profit:.2f}\n"
            f"*Size:* {position_size:.4f} BTC\n\n"
            f"🧠 *AI Reason:* {reason}"
        )
        send_telegram(msg)
        print("Trade Alert Sent to Telegram!")
        
    else:
        print("AI decided to WAIT. No trade.")
        
if __name__ == "__main__":
    run_agent()
