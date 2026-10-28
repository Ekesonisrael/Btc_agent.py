#!/usr/bin/env python3
import requests
import pandas as pd
import numpy as np
import os

# ==========================================
# CONFIGURATION
# ==========================================
SYMBOL = "BTC-USDT-SWAP"
LEVERAGE = 3
SL_BUFFER = 0.003  # 0.3% buffer distance
RR_TARGET = 2.0    # 1:2 R:R target

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram_alert(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[ALERT] {message}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"Telegram alert failed: {e}")

# ==========================================
# RECENT DATA FETCH (OKX API)
# ==========================================
def fetch_recent_data(inst, limit=100):
    url = f"https://www.okx.com/api/v5/market/history-candles?instId={inst}&bar=15m&limit={limit}"
    r = requests.get(url, timeout=10).json()
    if r["code"] != "0" or not r["data"]:
        return None
    
    df = pd.DataFrame(r["data"], columns=["ts", "o", "h", "l", "c", "v", "vc", "vq", "confirm"])
    df = df.iloc[::-1].reset_index(drop=True)
    for col in ["o", "h", "l", "c"]:
        df[col] = df[col].astype(float)
    df['ts'] = pd.to_datetime(df['ts'].astype("int64"), unit="ms")
    df['ema_50'] = df['c'].ewm(span=50, adjust=False).mean()
    return df

# ==========================================
# LIVE SCANNER EXECUTION
# ==========================================
def run_live_scanner():
    df = fetch_recent_data(SYMBOL)
    if df is None or len(df) < 50:
        print("Failed to fetch candle data or insufficient history.")
        return

    # Evaluate the most recently completed candle structure
    i = len(df) - 3
    sweep_candle = df.iloc[i]
    confirm_candle = df.iloc[i+1]
    ema = sweep_candle['ema_50']
    
    is_bullish_trend = sweep_candle['c'] >= ema
    is_bearish_trend = sweep_candle['c'] < ema
    
    prior_low = df['l'].iloc[i-15:i].min()
    prior_high = df['h'].iloc[i-15:i].max()
    
    # --- LONG SETUP ---
    is_long_sweep = sweep_candle['l'] < prior_low and sweep_candle['c'] > prior_low
    is_long_disp = confirm_candle['c'] > confirm_candle['o'] and confirm_candle['c'] > sweep_candle['o']
    
    if is_bullish_trend and is_long_sweep and is_long_disp:
        entry_price = df.iloc[i+2]['o']
        sl = sweep_candle['l'] * (1.0 - SL_BUFFER)
        risk_distance = entry_price - sl
        if risk_distance > 0:
            tp = entry_price + (risk_distance * RR_TARGET)
            msg = (
                f"🚨 *BTC SMC LIVE SIGNAL (LONG)* 🚨\n\n"
                f"• *Entry:* `${entry_price:,.2f}`\n"
                f"• *Stop Loss:* `${sl:,.2f}`\n"
                f"• *Take Profit:* `${tp:,.2f}`\n"
                f"• *Mode:* `3x Isolated Margin`"
            )
            print(msg)
            send_telegram_alert(msg)
            return

    # --- SHORT SETUP ---
    is_short_sweep = sweep_candle['h'] > prior_high and sweep_candle['c'] < prior_high
    is_short_disp = confirm_candle['c'] < confirm_candle['o'] and confirm_candle['c'] < sweep_candle['o']
    
    if is_bearish_trend and is_short_sweep and is_short_disp:
        entry_price = df.iloc[i+2]['o']
        sl = sweep_candle['h'] * (1.0 + SL_BUFFER)
        risk_distance = sl - entry_price
        if risk_distance > 0:
            tp = entry_price - (risk_distance * RR_TARGET)
            msg = (
                f"🚨 *BTC SMC LIVE SIGNAL (SHORT)* 🚨\n\n"
                f"• *Entry:* `${entry_price:,.2f}`\n"
                f"• *Stop Loss:* `${sl:,.2f}`\n"
                f"• *Take Profit:* `${tp:,.2f}`\n"
                f"• *Mode:* `3x Isolated Margin`"
            )
            print(msg)
            send_telegram_alert(msg)
            return

    print(f"[{SYMBOL}] No active signal on the latest completed 15m bar.")

if __name__ == "__main__":
    run_live_scanner()
    
