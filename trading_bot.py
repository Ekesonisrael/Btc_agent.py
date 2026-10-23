import os
import ccxt

# ==========================================
# 1. EXCHANGE CONFIGURATION & SECRETS
# ==========================================
# Credentials are pulled securely from GitHub Environment Variables
exchange = ccxt.okx({
    'apiKey': os.getenv('OKX_API_KEY'),
    'secret': os.getenv('OKX_SECRET_KEY'),
    'password': os.getenv('OKX_API_PASSWORD'),
    'options': {'defaultType': 'swap'}  # Linear Perpetual Swaps
})

# SAFETY FIRST: Set to True for testing, change to False when going live with real funds
exchange.set_sandbox_mode(True)

SYMBOLS = ['BTC/USDT:USDT', 'SOL/USDT:USDT']
RISK_PERCENTAGE = 0.01  # Risk exactly 1% of free USDT balance per trade
LEVERAGE = 3            # Locked low leverage

# ==========================================
# 2. MATHEMATICAL INDICATOR ENGINE (EMA)
# ==========================================
def calculate_ema(prices, period):
    """Calculates Exponential Moving Average."""
    multiplier = 2 / (period + 1)
    ema = [sum(prices[:period]) / period]
    for price in prices[period:]:
        ema.append((price - ema[-1]) * multiplier + ema[-1])
    return ema

# ==========================================
# 3. ENVIRONMENT & RISK SETUP
# ==========================================
def initialize_market(symbol):
    try:
        exchange.set_leverage(LEVERAGE, symbol)
        exchange.set_margin_mode('isolated', symbol)
        print(f"[SETUP] {symbol} configured: Isolated Margin @ {LEVERAGE}x Leverage.")
    except Exception as e:
        print(f"[ERROR] Failed to configure {symbol}: {e}")

# ==========================================
# 4. CORE STRATEGY & EXECUTION (SINGLE PASS)
# ==========================================
def execute_strategy():
    print("--- Scanning Market Structure (15m Timeframe) ---")
    
    for symbol in SYMBOLS:
        try:
            # Fetch last 50 candles on the 15-minute timeframe
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
            if len(ohlcv) < 30:
                continue
                
            closes = [candle[4] for candle in ohlcv]
            
            # Compute Fast (9) and Slow (21) EMAs
            ema_fast = calculate_ema(closes, 9)
            ema_slow = calculate_ema(closes, 21)
            
            current_price = closes[-1]
            prev_price = closes[-2]
            
            # Strategy Rules
            is_bullish_trend = ema_fast[-1] > ema_slow[-1]
            did_bounce = prev_price <= ema_fast[-1] and current_price > ema_fast[-1]
            
            if is_bullish_trend and did_bounce:
                print(f"[SIGNAL DETECTED] Long opportunity on {symbol} at price: {current_price}")
                
                # Risk Management Calculations
                entry_price = current_price
                stop_loss = entry_price * 0.985    # 1.5% Risk cutoff
                take_profit = entry_price * 1.03   # 3.0% Target (1:2 Risk-to-Reward)
                
                # Fetch available free balance
                balance = exchange.fetch_balance()['USDT']['free']
                risk_amount = balance * RISK_PERCENTAGE
                price_risk_per_unit = entry_price - stop_loss
                
                if price_risk_per_unit > 0:
                    raw_size = risk_amount / price_risk_per_unit
                    market = exchange.market(symbol)
                    amount = float(f"{raw_size:.{market['precision']['amount']}f}")
                    
                    if amount > 0:
                        print(f"Executing Buy Order -> Size: {amount} | SL: {stop_loss:.2f} | TP: {take_profit:.2f}")
                        
                        # Send Order to OKX with attached triggers
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
                        print(f"[SUCCESS] Order Placed! ID: {order['id']}\n")
            else:
                print(f"[HOLD] {symbol}: Flat or pulling back. Current Price: {current_price}")
                
        except Exception as e:
            print(f"[ERROR] Execution fault on {symbol}: {e}")

# ==========================================
# 5. SINGLE-RUN ENTRYPOINT
# ==========================================
if __name__ == "__main__":
    print("\n[INIT] Starting scheduled market scan pass...")
    for sym in SYMBOLS:
        initialize_market(sym)
        
    # Run one evaluation pass across all configured symbols, then exit cleanly
    execute_strategy()
    print("[COMPLETE] Scan finished. Exiting process safely.")
          
