import os
import sys
import json
import ccxt
import requests

# ==========================================
# CONFIGURATION & SECRETS
# ==========================================
API_KEY = os.getenv("OKX_API_KEY")
API_SECRET = os.getenv("OKX_SECRET")
API_PASSWORD = os.getenv("OKX_PASSWORD")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOLS = ["BTC/USDT:USDT", "SOL/USDT:USDT"]
LEVERAGE = 3
RISK_PERCENTAGE = 0.05  # 5% Risk per trade
JOURNAL_FILE = "trade_journal.json"


def send_telegram_alert(message):
    """Sends execution logs and trade updates directly to Telegram safely."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[TELEGRAM] (Skipped) {message}")
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"[TELEGRAM EXCEPTION] {e}")


def log_trade_to_memory(data):
    """Provides memory/learning capability by saving execution logs locally."""
    try:
        history = []
        if os.path.exists(JOURNAL_FILE):
            with open(JOURNAL_FILE, "r") as f:
                history = json.load(f)
        history.append(data)
        with open(JOURNAL_FILE, "w") as f:
            json.dump(history, f, indent=4)
    except Exception as e:
        print(f"[JOURNAL ERROR] Could not save trade memory: {e}")


def initialize_exchange():
    """Initializes and authenticates the CCXT OKX exchange client for linear swaps."""
    try:
        ex = ccxt.okx({
            'apiKey': API_KEY,
            'secret': API_SECRET,
            'password': API_PASSWORD,
            'enableRateLimit': True,
            'options': {'defaultType': 'swap'}
        })
        ex.set_sandbox_mode(True)
        ex.load_markets()
        return ex
    except Exception as e:
        print(f"[CRITICAL ERROR] Failed to initialize OKX exchange: {e}")
        sys.exit(1)


def setup_market_leverage(exchange, symbol):
    """Safely configures margin mode and leverage matching OKX parameter expectations."""
    try:
        try:
            exchange.set_margin_mode('cross', symbol)
        except Exception:
            pass
        exchange.set_leverage(LEVERAGE, symbol, {'lever': str(LEVERAGE), 'mgnMode': 'cross'})
        print(f"[SETUP] {symbol}: Leverage configured to {LEVERAGE}x.")
    except Exception as e:
        print(f"[SETUP NOTICE] {symbol}: Leverage configuration note -> {e}")


def has_active_position_or_order(exchange, symbol):
    """PREVENTS DOUBLE TRADES: Checks if a position or open order already exists."""
    try:
        positions = exchange.fetch_positions([symbol])
        for pos in positions:
            if float(pos.get('contracts', 0)) > 0:
                print(f"[GUARD] Active position already open for {symbol}. Skipping.")
                return True
                
        open_orders = exchange.fetch_open_orders(symbol)
        if len(open_orders) > 0:
            print(f"[GUARD] Active pending orders exist for {symbol}. Skipping.")
            return True
    except Exception as e:
        print(f"[GUARD WARNING] Could not verify open positions for {symbol}: {e}")
    return False


def calculate_ema(prices, period):
    """Calculates Exponential Moving Average (EMA)."""
    if len(prices) < period:
        return []
    multiplier = 2 / (period + 1)
    ema = [sum(prices[:period]) / period]
    for price in prices[period:]:
        ema.append((price - ema[-1]) * multiplier + ema[-1])
    return ema


def run_market_strategy(exchange):
    """Executes the 4H macro trend & 15m structural liquidity sweep strategy with 5% risk & SL/TP."""
    print("--- Running Market Structure & Liquidity Sweep Scan ---")
    
    for symbol in SYMBOLS:
        try:
            setup_market_leverage(exchange, symbol)
            
            # Guard check to prevent double-trading
            if has_active_position_or_order(exchange, symbol):
                continue
            
            # Fetch candle data
            ohlcv_4h = exchange.fetch_ohlcv(symbol, timeframe='4h', limit=50)
            ohlcv_15m = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
            
            if not ohlcv_4h or not ohlcv_15m or len(ohlcv_4h) < 30 or len(ohlcv_15m) < 30:
                print(f"[SKIP] {symbol}: Insufficient candle data fetched.")
                continue
            
            # 1. 4H Macro Trend Evaluation
            closes_4h = [candle[4] for candle in ohlcv_4h]
            ema20_4h = calculate_ema(closes_4h, 20)
            ema50_4h = calculate_ema(closes_4h, 50)
            
            if not ema20_4h or not ema50_4h:
                continue
                
            is_bullish_macro = ema20_4h[-1] > ema50_4h[-1]
            is_bearish_macro = ema20_4h[-1] < ema50_4h[-1]
            
            # 2. 15m Liquidity Sweep Evaluation
            closes_15m = [candle[4] for candle in ohlcv_15m]
            lows_15m = [candle[3] for candle in ohlcv_15m]
            highs_15m = [candle[2] for candle in ohlcv_15m]
            
            current_price = closes_15m[-1]
            recent_low = min(lows_15m[-20:-1])
            recent_high = max(highs_15m[-20:-1])
            
            swept_low = lows_15m[-1] < recent_low and current_price > recent_low
            swept_high = highs_15m[-1] > recent_high and current_price < recent_high
            
            # 3. Execution Logic
            if is_bullish_macro and swept_low:
                print(f"🚨 [SIGNAL] Bullish liquidity sweep on {symbol} at {current_price}")
                
                entry_price = current_price
                stop_loss = recent_low * 0.995  # Protected SL below sweep
                take_profit = entry_price + (entry_price - stop_loss) * 2.5  # 1:2.5 R:R
                
                balance = exchange.fetch_balance()['USDT']['free']
                risk_amount = balance * RISK_PERCENTAGE
                price_risk_per_unit = entry_price - stop_loss
                
                if price_risk_per_unit > 0:
                    raw_size = risk_amount / price_risk_per_unit
                    market = exchange.market(symbol)
                    amount = float(f"{raw_size:.{market['precision']['amount']}f}")
                    
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
                            f"✅ *Long Order Executed (Demo - 5% Risk)*\n\n"
                            f"*Asset:* `{symbol}`\n"
                            f"*Size:* `{amount}`\n"
                            f"*Entry:* `{entry_price}`\n"
                            f"*Stop Loss:* `{stop_loss:.2f}`\n"
                            f"*Take Profit:* `{take_profit:.2f}`\n"
                            f"*Order ID:* `{order['id']}`"
                        )
                        print(success_msg)
                        send_telegram_alert(success_msg)
                        log_trade_to_memory({"symbol": symbol, "side": "buy", "entry": entry_price, "amount": amount, "status": "executed"})

            elif is_bearish_macro and swept_high:
                print(f"🚨 [SIGNAL] Bearish liquidity sweep on {symbol} at {current_price}")
                
                entry_price = current_price
                stop_loss = recent_high * 1.005  # Protected SL above sweep
                take_profit = entry_price - (stop_loss - entry_price) * 2.5  # 1:2.5 R:R
                
                balance = exchange.fetch_balance()['USDT']['free']
                risk_amount = balance * RISK_PERCENTAGE
                price_risk_per_unit = stop_loss - entry_price
                
                if price_risk_per_unit > 0:
                    raw_size = risk_amount / price_risk_per_unit
                    market = exchange.market(symbol)
                    amount = float(f"{raw_size:.{market['precision']['amount']}f}")
                    
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
                        
                        success_msg = (
                            f"✅ *Short Order Executed (Demo - 5% Risk)*\n\n"
                            f"*Asset:* `{symbol}`\n"
                            f"*Size:* `{amount}`\n"
                            f"*Entry:* `{entry_price}`\n"
                            f"*Stop Loss:* `{stop_loss:.2f}`\n"
                            f"*Take Profit:* `{take_profit:.2f}`\n"
                            f"*Order ID:* `{order['id']}`"
                        )
                        print(success_msg)
                        send_telegram_alert(success_msg)
                        log_trade_to_memory({"symbol": symbol, "side": "sell", "entry": entry_price, "amount": amount, "status": "executed"})
            else:
                print(f"[HOLD] {symbol}: No clean setup. Monitoring structural sweep.")
                
        except Exception as e:
            err_msg = f"⚠️ *Bot Error* on `{symbol}`:\n`{str(e)}`"
            print(err_msg)
            send_telegram_alert(err_msg)


if __name__ == "__main__":
    print("[INIT] Starting structural blueprint scan & position guard check...")
    
    if not API_KEY or not API_SECRET:
        print(f"[CRITICAL] Missing API credentials! API_KEY: {bool(API_KEY)}, API_SECRET: {bool(API_SECRET)}")
        sys.exit(1)
        
    bot_exchange = initialize_exchange()
    run_market_strategy(bot_exchange)
    print("[COMPLETE] Scan and risk guard execution finished safely.")
