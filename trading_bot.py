import os
import ccxt
import requests
import pandas as pd
import time
import json
from datetime import datetime

# ==========================================
# CONFIGURATION & SECRETS (OKX)
# ==========================================
API_KEY = os.getenv("OKX_API_KEY")
API_SECRET = os.getenv("OKX_SECRET")
API_PASSWORD = os.getenv("OKX_PASSWORD")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOLS = ["BTC/USDT:USDT"]
LEVERAGE = 3
RISK_PERCENTAGE = 0.03  # 3% Risk per trade
RISK_REWARD = 2.0
JOURNAL_FILE = "trade_journal.json"

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[TELEGRAM SKIPPED] {message}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"[TELEGRAM EXCEPTION] {e}")

def load_journal():
    if os.path.exists(JOURNAL_FILE):
        try:
            with open(JOURNAL_FILE, "r") as f: return json.load(f)
        except: return []
    return []

def save_journal(journal):
    with open(JOURNAL_FILE, "w") as f: json.dump(journal, f, indent=4)

def initialize_exchange():
    ex = ccxt.okx({
        'apiKey': API_KEY, 'secret': API_SECRET, 'password': API_PASSWORD,
        'enableRateLimit': True, 'options': {'defaultType': 'swap'}
    })
    ex.set_sandbox_mode(True) # DEMO MODE
    return ex

def check_closed_trades(exchange, journal):
    updated = False
    for trade in journal:
        if trade.get("status") == "OPEN":
            symbol = trade["symbol"]
            try:
                positions = exchange.fetch_positions([symbol])
                has_position = any(float(p.get('contracts', 0)) > 0 for p in positions)
                
                if not has_position:
                    send_telegram(f"🔔 *TRADE CLOSED: {symbol}*\nCheck OKX app for final PnL.")
                    print(f"Trade closed for {symbol}")
                    trade["status"] = "CLOSED"
                    updated = True
            except Exception as e:
                print(f"[RESOLVER ERROR] {symbol}: {e}")
    return updated

def run_scanner():
    exchange = initialize_exchange()
    journal = load_journal()
    
    if check_closed_trades(exchange, journal):
        save_journal(journal)

    print(f"--- Running FORCED TEST Scan at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---")
    
    for symbol in SYMBOLS:
        if any(t['symbol'] == symbol and t['status'] == "OPEN" for t in journal):
            print(f"[{symbol}] Trade already open. Skipping.")
            continue

        try:
            # 🚨 FORCED TRADE TEST 🚨
            signal = "LONG"
            side = 'buy'
            
            # Fetch live price
            current_price = float(exchange.fetch_ticker(symbol)['last'])
            
            # Set safe Stop Loss (1% away) and Take Profit (2% away)
            sl = current_price * 0.99 
            risk = current_price - sl
            tp = current_price + (risk * RISK_REWARD)

            print(f"🚨 FORCED {symbol} SIGNAL: {signal} | Price: {current_price}")

            # --- EXECUTE TRADE ---
            try:
                exchange.set_leverage(LEVERAGE, symbol, {'mgnMode': 'cross'})
                balance = exchange.fetch_balance()['USDT']['free']
                
                if balance <= 0:
                    print("No balance available.")
                    continue

                risk_amount = balance * RISK_PERCENTAGE
                price_risk = abs(current_price - sl)
                
                market = exchange.market(symbol)
                contract_size = market['contractSize']
                
                raw_contracts = (risk_amount / price_risk) / contract_size
                max_position_value = (balance * LEVERAGE) * 0.90
                max_contracts_margin = max_position_value / current_price / contract_size
                
                final_contracts = min(raw_contracts, max_contracts_margin)
                amount = float(exchange.amount_to_precision(symbol, final_contracts))
                
                if amount > 0:
                    params = {
                        'stopLoss': {'triggerPrice': float(exchange.price_to_precision(symbol, sl))},
                        'takeProfit': {'triggerPrice': float(exchange.price_to_precision(symbol, tp))}
                    }
                    
                    exchange.create_order(symbol, 'market', side, amount, params=params)
                    
                    journal.append({
                        "symbol": symbol, "side": side, "entry": float(current_price),
                        "sl": float(sl), "tp": float(tp), "amount": float(amount),
                        "status": "OPEN", "time": datetime.now().isoformat()
                    })
                    
                    send_telegram(
                        f"✅ *FORCED TEST EXECUTED*\n"
                        f"Asset: {symbol}\n"
                        f"Size: {amount} contracts\n"
                        f"Entry: {current_price:.2f}\n"
                        f"SL: {sl:.2f}\n"
                        f"TP: {tp:.2f}\n\n"
                        f"👉 *GO CHECK YOUR OKX APP NOW*"
                    )
            except Exception as e:
                print(f"Order Failed: {e}")
                send_telegram(f"⚠️ Order Failed on {symbol}:\n{str(e)}")
                    
        except Exception as e:
            print(f"[ERROR] {symbol}: {e}")

    save_journal(journal)

if __name__ == "__main__":
    run_scanner()
