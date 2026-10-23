import os
import requests
import ccxt

# ==========================================
# 1. CONFIGURATION & SECRETS
# ==========================================
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

def send_telegram_alert(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")

exchange = ccxt.okx({
    'apiKey': os.getenv('OKX_API_KEY'),
    'secret': os.getenv('OKX_SECRET_KEY'),
    'password': os.getenv('OKX_PASSWORD'),
    'options': {'defaultType': 'swap'}
})

exchange.set_sandbox_mode(True)  # Keep on demo for testing

SYMBOLS = ['BTC/USDT:USDT', 'SOL/USDT:USDT']
RISK_PERCENTAGE = 0.05  # 5% Risk per trade
LEVERAGE = 3

def initialize_market(symbol):
    try:
        exchange.set_margin_mode('isolated', symbol)
        exchange.set_leverage(LEVERAGE, symbol)
    except Exception as e:
        print(f"[SETUP NOTICE] {symbol}: {e}")

# ==========================================
# 2. MULTI-TIMEFRAME STRATEGY ENGINE
# ==========================================
def execute_strategy():
    print("--- Running Multi-Timeframe Market Structure Scan ---")
    
    for symbol in SYMBOLS:
        try:
            # A. FETCH HIGHER TIMEFRAME (4H) FOR MACRO TREND BIAS
            ohlcv_4h = exchange.fetch_ohlcv(symbol, timeframe='4h', limit=30)
            closes_4h = [candle[4] for candle in ohlcv_4h]
            # Simple macro filter: Is price above the 20 EMA on the 4H chart?
            ema_4h = sum(closes_4h[-20:]) / 20
            is_macro_bullish = closes_4h[-1] > ema_4h
            
            if not is_macro_bullish:
                print(f"[HOLD] {symbol}: 4H macro trend is bearish or flat. No longs taken.")
                continue

            # B. FETCH EXECUTION TIMEFRAME (15m) FOR STRUCTURE & ENTRY
            ohlcv_15m = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
            lows_15m = [candle[3] for candle in ohlcv_15m]
            closes_15m = [candle[4] for candle in ohlcv_15m]
            
            current_price = closes_15m[-1]
            
            # Structural Pullback Condition: Price tests recent structural support (lowest low of last 5 candles)
            recent_support = min(lows_15m[-6:-1])
            is_pullback_to_support = current_price <= (recent_support * 1.002) # Within 0.2% of support
            
            if is_macro_bullish and is_pullback_to_support:
                print(f"🚨 *STRATEGY SETUP MATCHED* on {symbol} at {current_price}")
                
                # C. TRUE RISK MANAGEMENT & STRUCTURAL SL/TP
                entry_price = current_price
                # Stop loss placed safely below the structural swing low
                stop_loss = recent_support * 0.995 
                
                risk_per_unit = entry_price - stop_loss
                if risk_per_unit <= 0:
                    continue
                
                # 1:2.5 Risk-to-Reward Ratio Target
                take_profit = entry_price + (risk_per_unit * 2.5)
                
                # D. POSITION SIZING (5% Risk Model)
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
                    
                    success_msg = (
                        f"✅ *Multi-Timeframe Long Executed (5% Risk)*\n\n"
                        f"*Asset:* `{symbol}`\n"
                        f"*Size:* `{amount}`\n"
                        f"*Entry:* `{entry_price}`\n"
                        f"*Stop Loss (Structure):* `{stop_loss:.2f}`\n"
                        f"*Take Profit (1:2.5 R:R):* `{take_profit:.2f}`\n"
                        f"*Order ID:* `{order['id']}`"
                    )
                    print(success_msg)
                    send_telegram_alert(success_msg)
            else:
                print(f"[HOLD] {symbol}: Waiting for structural pullback to support.")
                
        except Exception as e:
            err_msg = f"⚠️ *Error on {symbol}:* `{e}`"
            print(err_msg)
            send_telegram_alert(err_msg)

if __name__ == "__main__":
    print("\n[INIT] Starting structured scan pass...")
    for sym in SYMBOLS:
        initialize_market(sym)
    execute_strategy()
    print("[COMPLETE] Scan finished.")
