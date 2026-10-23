import os
import requests
import ccxt

# ==========================================
# 1. CONFIGURATION & SECRETS
# ==========================================
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

def send_telegram_alert(message):
    """Sends notification messages straight to your Telegram bot."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TELEGRAM] Credentials missing.")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown"
        }
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")

# Initialize OKX connection
exchange = ccxt.okx({
    'apiKey': os.getenv('OKX_API_KEY'),
    'secret': os.getenv('OKX_SECRET_KEY'),
    'password': os.getenv('OKX_PASSWORD'),
    'options': {'defaultType': 'swap'}
})

# ENABLE DEMO / SANDBOX MODE
exchange.set_sandbox_mode(True)

SYMBOLS = ['BTC/USDT:USDT', 'SOL/USDT:USDT']
RISK_PERCENTAGE = 0.05  # 5% Risk per trade model
LEVERAGE = 3

def initialize_market(symbol):
    try:
        exchange.set_margin_mode('isolated', symbol)
        exchange.set_leverage(LEVERAGE, symbol)
        print(f"[SETUP] {symbol} configured for Isolated Margin @ {LEVERAGE}x.")
    except Exception as e:
        print(f"[SETUP NOTICE] {symbol}: {e}")

# ==========================================
# 2. THE BLUEPRINT STRATEGY ENGINE
# ==========================================
def execute_strategy():
    print("--- Running Market Structure & Liquidity Sweep Scan ---")
    
    for symbol in SYMBOLS:
        try:
            # ==========================================
            # STEP A: 4H MACRO TREND BIAS (The Foundation)
            # ==========================================
            ohlcv_4h = exchange.fetch_ohlcv(symbol, timeframe='4h', limit=10)
            closes_4h = [c[4] for c in ohlcv_4h]
            
            # Check if 4H is making higher highs / higher lows (Bullish Macro Bias)
            is_macro_bullish = closes_4h[-1] > closes_4h[-3] and closes_4h[-3] > closes_4h[-5]
            is_macro_bearish = closes_4h[-1] < closes_4h[-3] and closes_4h[-3] < closes_4h[-5]
            
            if not is_macro_bullish and not is_macro_bearish:
                print(f"[HOLD] {symbol}: 4H macro trend is consolidating. Sitting on hands.")
                continue

            # ==========================================
            # STEP B: 15m EXECUTION & LIQUIDITY SWEEP
# ==========================================
            ohlcv_15m = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=30)
            timestamps = [c[0] for c in ohlcv_15m]
            opens = [c[1] for c in ohlcv_15m]
            highs = [c[2] for c in ohlcv_15m]
            lows = [c[3] for c in ohlcv_15m]
            closes = [c[4] for c in ohlcv_15m]
            
            current_price = closes[-1]
            
            if is_macro_bullish:
                # Look for a Long Setup: Liquidity sweep below a recent low, followed by a bullish shift
                recent_low = min(lows[-10:-2]) # Lowest point of recent range
                swept_liquidity = lows[-2] < recent_low # Previous candle dipped below the low
                bullish_shift = closes[-1] > opens[-1] and closes[-1] > highs[-2] # Strong close back up
                
                if swept_liquidity and bullish_shift:
                    print(f"🚨 *BULLISH LIQUIDITY SWEEP DETECTED* on {symbol}")
                    
                    # ==========================================
                    # STEP C: RISK MANAGEMENT & EXPLICIT SL / TP
                    # ==========================================
                    entry_price = current_price
                    # Stop-Loss placed precisely below the swept low (Structural Protection)
                    stop_loss = recent_low * 0.995 
                    
                    risk_per_unit = entry_price - stop_loss
                    if risk_per_unit <= 0:
                        continue
                        
                    # 1:3 Risk-to-Reward Ratio Target (Strict Discipline)
                    take_profit = entry_price + (risk_per_unit * 3.0)
                    
                    # ==========================================
                    # STEP D: POSITION SIZING & OKX EXECUTION
                    # ==========================================
                    balance = exchange.fetch_balance()['USDT']['free']
                    risk_amount = balance * RISK_PERCENTAGE
                    raw_size = risk_amount / risk_per_unit
                    
                    amount = float(exchange.amount_to_precision(symbol, raw_size))
                    
                    if amount > 0:
                        order = exchange.create_order(
                            symbol=symbol,
                            type='limit',
                            side='buy',
                            amount=amount,
                            price=entry_price,
                            params={
                                'tpTriggerPx': take_profit,
                                'slTriggerPx': stop_loss
                            }
                        )
                        
                        signal_report = (
                            f"🟢 *OKX DEMO LONG SIGNAL EXECUTED*\n\n"
                            f"*Asset:* `{symbol}`\n"
                            f"*Strategy:* 4H Trend + 15m Sweep\n"
                            f"*Entry Price:* `{entry_price}`\n"
                            f"*Stop-Loss:* `{stop_loss:.2f}` *(Structural)*\n"
                            f"*Take-Profit:* `{take_profit:.2f}` *(1:3 R:R)*\n"
                            f"*Risk Allocated:* `5%`\n"
                            f"*Position Size:* `{amount}`\n"
                            f"*Order ID:* `{order['id']}`"
                        )
                        print(signal_report)
                        send_telegram_alert(signal_report)
                else:
                    print(f"[WAIT] {symbol}: Waiting for clean 15m liquidity sweep in bullish 4H trend.")
                    
            elif is_macro_bearish:
                # Look for a Short Setup: Liquidity sweep above a recent high, followed by a bearish shift
                recent_high = max(highs[-10:-2])
                swept_liquidity = highs[-2] > recent_high
                bearish_shift = closes[-1] < opens[-1] and closes[-1] < lows[-2]
                
                if swept_liquidity and bearish_shift:
                    print(f"🚨 *BEARISH LIQUIDITY SWEEP DETECTED* on {symbol}")
                    
                    entry_price = current_price
                    stop_loss = recent_high * 1.005  # Structural Stop-Loss above high
                    
                    risk_per_unit = stop_loss - entry_price
                    if risk_per_unit <= 0:
                        continue
                        
                    take_profit = entry_price - (risk_per_unit * 3.0) # 1:3 R:R Target
                    
                    balance = exchange.fetch_balance()['USDT']['free']
                    risk_amount = balance * RISK_PERCENTAGE
                    raw_size = risk_amount / risk_per_unit
                    
                    amount = float(exchange.amount_to_precision(symbol, raw_size))
                    
                    if amount > 0:
                        order = exchange.create_order(
                            symbol=symbol,
                            type='limit',
                            side='sell',
                            amount=amount,
                            price=entry_price,
                            params={
                                'tpTriggerPx': take_profit,
                                'slTriggerPx': stop_loss
                            }
                        )
                        
                        signal_report = (
                            f"🔴 *OKX DEMO SHORT SIGNAL EXECUTED*\n\n"
                            f"*Asset:* `{symbol}`\n"
                            f"*Strategy:* 4H Trend + 15m Sweep\n"
                            f"*Entry Price:* `{entry_price}`\n"
                            f"*Stop-Loss:* `{stop_loss:.2f}` *(Structural)*\n"
                            f"*Take-Profit:* `{take_profit:.2f}` *(1:3 R:R)*\n"
                            f"*Risk Allocated:* `5%`\n"
                            f"*Position Size:* `{amount}`\n"
                            f"*Order ID:* `{order['id']}`"
                        )
                        print(signal_report)
                        send_telegram_alert(signal_report)
                else:
                    print(f"[WAIT] {symbol}: Waiting for clean 15m liquidity sweep in bearish 4H trend.")
                    
        except Exception as e:
            err_msg = f"⚠️ *Execution Error on {symbol}:* `{e}`"
            print(err_msg)
            send_telegram_alert(err_msg)

if __name__ == "__main__":
    print("\n[INIT] Starting structural blueprint scan...")
    for sym in SYMBOLS:
        initialize_market(sym)
    execute_strategy()
    print("[COMPLETE] Scan finished safely.")
