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
        print("[TELEGRAM] Credentials missing, skipping notification.")
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
        print(f"[TELEGRAM ERROR] Failed to send alert: {e}")

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
RISK_PERCENTAGE = 0.05  # 5% Risk per trade
LEVERAGE = 3

# ==========================================
# 2. MATHEMATICAL INDICATOR ENGINE (EMA)
# ==========================================
def calculate_ema(prices, period):
    multiplier = 2 / (period + 1)
    ema = [sum(prices[:period]) / period]
    for price in prices[period:]:
        ema.append((price - ema[-1]) * multiplier + ema[-1])
    return ema

# ==========================================
# 3. ENVIRONMENT & RISK SETUP (Fixed CCXT Leverage Format)
# ==========================================
def initialize_market(symbol):
    try:
        # Pass leverage correctly inside the params dictionary for OKX swap
        exchange.set_margin_mode('isolated', symbol)
        exchange.set_leverage(LEVERAGE, symbol, {'mgnMode': 'isolated'})
        print(f"[SETUP] {symbol} configured successfully.")
    except Exception as e:
        error_msg = f"❌ *Setup Error* on `{symbol}`:\n`{e}`"
        print(error_msg)
        send_telegram_alert(error_msg)

# ==========================================
# 4. CORE STRATEGY & EXECUTION
# ==========================================
def execute_strategy():
    print("--- Scanning Market Structure (15m Timeframe) ---")
    
    for symbol in SYMBOLS:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
            if len(ohlcv) < 30:
                continue
                
            closes = [candle[4] for candle in ohlcv]
            ema_fast = calculate_ema(closes, 9)
            ema_slow = calculate_ema(closes, 21)
            
            current_price = closes[-1]
            prev_price = closes[-2]
            
            is_bullish_trend = ema_fast[-1] > ema_slow[-1]
            did_bounce = prev_price <= ema_fast[-1] and current_price > ema_fast[-1]
            
            if is_bullish_trend and did_bounce:
                signal_msg = f"🚨 *OKX DEMO SIGNAL DETECTED*\n\n*Asset:* `{symbol}`\n*Action:* `BUY (LONG)`\n*Price:* `{current_price}`"
                print(signal_msg)
                send_telegram_alert(signal_msg)
                
                entry_price = current_price
                stop_loss = entry_price * 0.985
                take_profit = entry_price * 1.03
                
                balance = exchange.fetch_balance()['USDT']['free']
                risk_amount = balance * RISK_PERCENTAGE
                price_risk_per_unit = entry_price - stop_loss
                
                if price_risk_per_unit > 0:
                    raw_size = risk_amount / price_risk_per_unit
                    market = exchange.market(symbol)
                    
                    # Safe string formatting for precision
                    precision_decimals = market['precision']['amount']
                    amount = float(f"{raw_size:.{precision_decimals}f}")
                    
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
                        
                        success_msg = (
                            f"✅ *Order Executed Successfully (Demo - 5% Risk)*\n\n"
                            f"*Asset:* `{symbol}`\n"
                            f"*Size:* `{amount}`\n"
                            f"*Entry:* `{entry_price}`\n"
                            f"*Stop Loss:* `{stop_loss:.2f}`\n"
                            f"*Take Profit:* `{take_profit:.2f}`\n"
                            f"*Order ID:* `{order['id']}`"
                        )
                        print(success_msg)
                        send_telegram_alert(success_msg)
            else:
                print(f"[HOLD] {symbol}: Flat or pulling back at {current_price}")
                
        except Exception as e:
            err_msg = f"⚠️ *Execution Error* on `{symbol}`:\n`{e}`"
            print(err_msg)
            send_telegram_alert(err_msg)

# ==========================================
# 5. ENTRYPOINT
# ==========================================
if __name__ == "__main__":
    print("\n[INIT] Starting scheduled market scan pass...")
    for sym in SYMBOLS:
        initialize_market(sym)
        
    execute_strategy()
    print("[COMPLETE] Scan finished. Exiting process safely.")
