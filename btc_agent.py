import os
import ccxt
import pandas as pd
import requests
from groq import Groq
from datetime import datetime, timezone

# ==========================================
# CONFIGURATION (BTC ONLY - OKX DEMO)
# ==========================================
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

OKX_API_KEY = os.getenv("OKX_API_KEY")
OKX_SECRET = os.getenv("OKX_SECRET")
OKX_PASSWORD = os.getenv("OKX_PASSWORD")

SYMBOL = 'BTC/USDT:USDT'
JOURNAL_FILE = 'btc_journal.csv'

# Risk Settings
ACCOUNT_BALANCE = 10000.0  # Demo balance is usually 10,000 USDT
RISK_PERCENT = 0.02        # Risk 2% per trade
ATR_MULTIPLIER = 1.5       
RR_RATIO = 2.0             

# Setup AI & Exchange (DEMO MODE)
client = Groq(api_key=GROQ_API_KEY)
exchange = ccxt.okx({
    'apiKey': OKX_API_KEY,
    'secret': OKX_SECRET,
    'password': OKX_PASSWORD,
    'options': {'defaultType': 'swap'}
})
exchange.set_sandbox_mode(True) # Routes all orders to Demo environment

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
    now = datetime.now(timezone.utc)
    print(f"--- AI Agent Scanning BTC at {now} ---")
    
    if not os.path.isfile(JOURNAL_FILE):
        with open(JOURNAL_FILE, 'w') as f:
            f.write("Date,Symbol,Signal,Entry,Stop_Loss,Take_Profit,Size,Reason\n")
    
    # Fetch Data
    try:
        ohlcv = exchange.fetch_ohlcv(SYMBOL, '1h', limit=100)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df = calculate_indicators(df)
    except Exception as e:
        print(f"Data Fetch Error: {e}")
        return
    
    last = df.iloc[-2] 
    current_price = last['close']
    ema20 = last['EMA_20']
    ema50 = last['EMA_50']
    rsi = last['RSI']
    atr = last['ATR']
    
    # Memory Loop
    memory = ""
    if os.path.isfile(JOURNAL_FILE):
        with open(JOURNAL_FILE, 'r') as f:
            lines = f.readlines()
            if len(lines) > 1:
                history = lines[0] + "".join(lines[-3:])
                memory = f"\nHere are your last 3 trades:\n{history}\n"
    
    prompt = f"""
    You are a quantitative crypto futures trader analyzing BTC.
    Current Price: ${current_price:.2f}
    20 EMA: ${ema20:.2f}
    50 EMA: ${ema50:.2f}
    RSI (14): {rsi:.2f}
    ATR (14): {atr:.2f}
    {memory}
    
    Rules:
    - If trend is UP (Price > 20EMA > 50EMA) and RSI < 70, look for LONG.
    - If trend is DOWN (Price < 20EMA < 50EMA) and RSI > 30, look for SHORT.
    - If the market is choppy or RSI is overextended, say WAIT.
    - IMPORTANT: Look at your past trades. If you lost money on a LONG recently, be more cautious.
    
    Output EXACTLY this format and nothing else:
    SIGNAL | REASON
    """
    
    try:
        response = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0
        )
        ai_output = response.choices[0].message.content.strip()
        print(f"AI Output: {ai_output}")
    except Exception as e:
        print(f"AI API Error: {e}")
        return

    if "|" not in ai_output:
        print("AI failed to format correctly. Skipping.")
        return
        
    signal, reason = ai_output.split("|", 1)
    signal = signal.strip().upper()
    reason = reason.strip()
    
    # Daily Pulse
    if now.hour in [0, 6, 12, 18] and now.minute < 15:
        pulse_msg = f"🤖 *BTC AI PULSE*\nPrice: ${current_price:.2f}\nDecision: {signal}\nReason: {reason}"
        send_telegram(pulse_msg)
        print("Daily Pulse sent.")

    # Execution
    if signal in ["LONG", "SHORT"]:
        if signal == "LONG":
            stop_loss = current_price - (ATR_MULTIPLIER * atr)
            risk_per_coin = current_price - stop_loss
            take_profit = current_price + (risk_per_coin * RR_RATIO)
            side = 'buy'
        else:
            stop_loss = current_price + (ATR_MULTIPLIER * atr)
            risk_per_coin = stop_loss - current_price
            take_profit = current_price - (risk_per_coin * RR_RATIO)
            side = 'sell'

        risk_amount = ACCOUNT_BALANCE * RISK_PERCENT
        position_size = risk_amount / risk_per_coin
        
        try:
            # Format amount to OKX contract precision
            position_size = float(exchange.amount_to_precision(SYMBOL, position_size))
            
            # Place Market Order with embedded SL/TP
            print(f"Placing {side} order for {position_size} BTC...")
            params = {
                'stopLoss': {'triggerPrice': round(stop_loss, 2)},
                'takeProfit': {'triggerPrice': round(take_profit, 2)}
            }
            
            order = exchange.create_order(
                symbol=SYMBOL,
                type='market',
                side=side,
                amount=position_size,
                params=params
            )
            
            print(f"Order Successful! ID: {order['id']}")
            
            with open(JOURNAL_FILE, mode='a') as f:
                f.write(f"{now},{SYMBOL},{signal},{current_price:.2f},{stop_loss:.2f},{take_profit:.2f},{position_size:.4f},{reason}\n")
                
            msg = (
                f"🚨 *DEMO TRADE EXECUTED* 🚨\n\n"
                f"*Signal:* {signal}\n"
                f"*Entry:* ${current_price:.2f}\n"
                f"*Stop Loss:* ${stop_loss:.2f}\n"
                f"*Take Profit:* ${take_profit:.2f}\n"
                f"*Size:* {position_size} contracts\n\n"
                f"🧠 *AI Reason:* {reason}"
            )
            send_telegram(msg)
            
        except Exception as e:
            print(f"Order Execution Failed: {e}")
            send_telegram(f"⚠️ AI Signal: {signal}, but Order Failed: {e}")
            
    else:
        print("AI decided to WAIT. No trade.")
        
if __name__ == "__main__":
    run_agent()
