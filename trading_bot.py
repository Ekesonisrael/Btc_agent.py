import os
import json
import time
import ccxt
import requests
import pandas as pd
from datetime import datetime, timezone

# ==========================================
# CONFIGURATION & SECRETS (OKX)
# ==========================================
API_KEY = os.getenv("OKX_API_KEY")
API_SECRET = os.getenv("OKX_SECRET")
API_PASSWORD = os.getenv("OKX_PASSWORD")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SANDBOX = os.getenv("OKX_SANDBOX", "true").lower() == "true"
POS_MODE = os.getenv("OKX_POS_MODE", "net").lower()
HEDGE_MODE = POS_MODE == "long_short"

# ONLY SOLANA
SYMBOLS = ["SOL/USDT:USDT"]

LEVERAGE = 3
RISK_PERCENTAGE = 0.03
RISK_REWARD = 2.0
ATR_MULTIPLIER = 2.5         # Wide stop for patience
VOLUME_MULTIPLIER = 1.5
LOOKBACK = 20

# STACKING LOGIC
MAX_OPEN_PER_SYMBOL = 5
MAX_TOTAL_OPEN = 5
# Cap on combined risk of all open trades, as % of equity.
# 0.09 = at most three full-size 3% trades open at once.
MAX_OPEN_RISK_PCT = 0.09
DAILY_LOSS_LIMIT_PCT = 0.06  # realized losses today (UTC) that pause new entries

USE_WICK_STOP = False
WICK_BUFFER_ATR = 0.1

DEBUG_COOLDOWN_SECONDS = 3600

JOURNAL_FILE = "trade_journal.json"
STATE_FILE = "scanner_state.json"


# ==========================================
# HELPERS
# ==========================================
def now_utc():
    return datetime.now(timezone.utc)


def atomic_write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=4)
    os.replace(tmp, path)


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"[LOAD ERROR] {path}: {e}")
    return default


def load_journal():
    data = load_json(JOURNAL_FILE, [])
    return data if isinstance(data, list) else []


def save_journal(journal):
    atomic_write_json(JOURNAL_FILE, journal)


def load_state():
    state = load_json(STATE_FILE, {})
    state.setdefault("last_debug", {})
    state.setdefault("attempted", [])
    return state


def save_state(state):
    state["attempted"] = state["attempted"][-200:]
    atomic_write_json(STATE_FILE, state)


def send_telegram(message, markdown=True):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[TELEGRAM SKIPPED] {message}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    if markdown:
        payload["parse_mode"] = "Markdown"
    try:
        r = requests.post(url, json=payload, timeout=10)
        if not r.ok and markdown:
            send_telegram(message, markdown=False)
    except Exception as e:
        print(f"[TELEGRAM EXCEPTION] {e}")


def trade_entry_ms(trade):
    if trade.get("entry_ts_ms"):
        return int(trade["entry_ts_ms"])
    dt = datetime.fromisoformat(trade["time"])
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def initialize_exchange():
    ex = ccxt.okx({
        "apiKey": API_KEY, "secret": API_SECRET, "password": API_PASSWORD,
        "enableRateLimit": True, "options": {"defaultType": "swap"},
    })
    ex.set_sandbox_mode(SANDBOX)
    ex.load_markets()
    return ex


def has_live_position(exchange, symbol, side):
    """True if the exchange still shows an open position in this trade's direction."""
    want = "long" if side == "buy" else "short"
    for p in exchange.fetch_positions([symbol]):
        if float(p.get("contracts") or 0) <= 0:
            continue
        pside = p.get("side")
        if pside is None or pside == want:
            return True
    return False


def vwap(fills):
    qty = sum(float(t["amount"]) for t in fills)
    if qty <= 0:
        return None
    return sum(float(t["price"]) * float(t["amount"]) for t in fills) / qty


def daily_pnl(journal):
    today = now_utc().date().isoformat()
    return sum(
        float(t.get("pnl") or 0) for t in journal
        if t.get("status") == "CLOSED" and str(t.get("closed_at", ""))[:10] == today
    )


# ==========================================
# TRADE RESOLVER (position level)
# ==========================================
def check_closed_trades(exchange, journal):
    """
    Stacked trades in the same direction share one position, so they are resolved
    together: once the position is flat, ALL open trades for that symbol/side are
    closed in one go. PnL is computed once from the real fills, then split across
    the trades by contract size.
    """
    updated = False

    groups = {}
    for t in journal:
        if t.get("status") == "OPEN":
            groups.setdefault((t["symbol"], t["side"]), []).append(t)

    for (symbol, side), trades in groups.items():
        try:
            entry_times = [trade_entry_ms(t) for t in trades]
            first_ms, last_ms = min(entry_times), max(entry_times)

            # Give the exchange time to show the newest position
            if exchange.milliseconds() - last_ms < 60_000:
                continue

            # Still in the market -> nothing to resolve yet
            if has_live_position(exchange, symbol, side):
                continue

            fills = exchange.fetch_my_trades(symbol, since=first_ms - 5000, limit=100)
            fills = [t for t in fills if t.get("timestamp") and t["timestamp"] >= first_ms - 5000]

            exit_side = "sell" if side == "buy" else "buy"
            exits = [t for t in fills if t["side"] == exit_side and t["timestamp"] >= first_ms]
            entries = [t for t in fills if t["side"] == side]

            total_amount = sum(float(t["amount"]) for t in trades)
            exit_contracts = sum(float(t["amount"]) for t in exits)
            if exit_contracts < total_amount * 0.99:
                print(f"[RESOLVER] {symbol}: flat but exit fills not found yet "
                      f"({exit_contracts} of {total_amount} contracts), leaving OPEN")
                continue

            exit_price = vwap(exits)
            entry_price = vwap(entries)
            if entry_price is None:
                entry_price = sum(float(t["entry"]) * float(t["amount"]) for t in trades) / total_amount

            contract_size = float(exchange.market(symbol).get("contractSize") or 1)
            direction = 1 if side == "buy" else -1
            gross = (exit_price - entry_price) * direction * total_amount * contract_size

            fees = 0.0
            for t in fills:
                fee = t.get("fee") or {}
                if fee.get("cost") is not None and fee.get("currency") in (None, "USDT"):
                    fees += abs(float(fee["cost"]))
            profit = gross - fees

            closed_at = now_utc().isoformat()
            for t in trades:
                share = float(t["amount"]) / total_amount
                t["status"] = "CLOSED"
                t["exit"] = float(exit_price)
                t["pnl"] = float(profit * share)
                t["fees"] = float(fees * share)
                t["closed_at"] = closed_at
                t["pnl_allocated"] = len(trades) > 1
            updated = True

            result_emoji = "🟢 WIN" if profit > 0 else "🔴 LOSS"
            report = (
                f"🔔 *TRADE CLOSED: {symbol}*\n\n"
                f"Result: {result_emoji}\n"
                f"Side: {'LONG' if side == 'buy' else 'SHORT'}\n"
                f"Entries closed: {len(trades)}\n"
                f"Avg Entry: ${entry_price:.2f}\n"
                f"Avg Exit: ${exit_price:.2f}\n"
                f"PnL: ${profit:+.2f} USDT (fees ${fees:.2f})\n"
            )
            send_telegram(report)
            print(report)
        except Exception as e:
            print(f"[RESOLVER ERROR] {symbol}: {e}")
    return updated


# ==========================================
# DATA FETCH
# ==========================================
def fetch_history(exchange, symbol, timeframe, limit):
    all_ohlcv = []
    since = exchange.milliseconds() - (limit * exchange.parse_timeframe(timeframe) * 1000)
    for _ in range(20):
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=100)
        except Exception as e:
            print(f"[FETCH ERROR] {symbol} {timeframe}: {e}")
            break
        if not ohlcv:
            break
        all_ohlcv.extend(ohlcv)
        since = ohlcv[-1][0] + 1
        if len(all_ohlcv) >= limit:
            break
        time.sleep(0.1)
    df = pd.DataFrame(all_ohlcv, columns=["time", "open", "high", "low", "close", "volume"])
    if df.empty:
        return df
    df["time"] = pd.to_datetime(df["time"], unit="ms")
    return df.drop_duplicates("time").sort_values("time").reset_index(drop=True)


# ==========================================
# SCANNER
# ==========================================
def run_scanner():
    exchange = initialize_exchange()
    journal = load_journal()
    state = load_state()

    if check_closed_trades(exchange, journal):
        save_journal(journal)

    print(f"--- Scan at {now_utc().strftime('%Y-%m-%d %H:%M:%S')} UTC ({'DEMO' if SANDBOX else 'LIVE'}) ---")

    for symbol in SYMBOLS:
        try:
            df_4h = fetch_history(exchange, symbol, "4h", 100)
            df_15m = fetch_history(exchange, symbol, "15m", 100)

            # Drop the still-forming candle: only closed candles are used
            if len(df_4h) > 0:
                df_4h = df_4h.iloc[:-1].reset_index(drop=True)
            if len(df_15m) > 0:
                df_15m = df_15m.iloc[:-1].reset_index(drop=True)

            if len(df_4h) < 60 or len(df_15m) < LOOKBACK + 5:
                print(f"[SKIP] {symbol}: not enough candles (4h={len(df_4h)}, 15m={len(df_15m)})")
                continue

            df_4h["ema20"] = df_4h["close"].ewm(span=20, adjust=False).mean()
            df_4h["ema50"] = df_4h["close"].ewm(span=50, adjust=False).mean()
            is_bullish = df_4h["ema20"].iloc[-1] > df_4h["ema50"].iloc[-1]
            is_bearish = df_4h["ema20"].iloc[-1] < df_4h["ema50"].iloc[-1]

            high_low = df_15m["high"] - df_15m["low"]
            high_close = (df_15m["high"] - df_15m["close"].shift()).abs()
            low_close = (df_15m["low"] - df_15m["close"].shift()).abs()
            tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
            df_15m["atr"] = tr.rolling(window=14).mean()
            df_15m["vol_ma"] = df_15m["volume"].rolling(window=20).mean()

            sweep_candle = df_15m.iloc[-2]
            conf_candle = df_15m.iloc[-1]

            if pd.isna(sweep_candle["atr"]) or pd.isna(sweep_candle["vol_ma"]):
                print(f"[SKIP] {symbol}: indicators not ready")
                continue

            recent_low = df_15m["low"].iloc[-(LOOKBACK + 2):-2].min()
            recent_high = df_15m["high"].iloc[-(LOOKBACK + 2):-2].max()

            vol_ok = sweep_candle["volume"] > sweep_candle["vol_ma"] * VOLUME_MULTIPLIER
            swept_low = (sweep_candle["low"] < recent_low) and (sweep_candle["close"] > recent_low) and vol_ok
            swept_high = (sweep_candle["high"] > recent_high) and (sweep_candle["close"] < recent_high) and vol_ok

            conf_long = conf_candle["close"] > conf_candle["open"]
            conf_short = conf_candle["close"] < conf_candle["open"]

            current_price = float(exchange.fetch_ticker(symbol)["last"])
            atr_value = float(sweep_candle["atr"])

            signal = None
            side = None
            sl = tp = None

            if is_bullish and swept_low and conf_long:
                signal, side = "LONG", "buy"
                sl = current_price - (atr_value * ATR_MULTIPLIER)
                if USE_WICK_STOP:
                    sl = min(sl, float(sweep_candle["low"]) - atr_value * WICK_BUFFER_ATR)
                risk = current_price - sl
                tp = current_price + (risk * RISK_REWARD)
            elif is_bearish and swept_high and conf_short:
                signal, side = "SHORT", "sell"
                sl = current_price + (atr_value * ATR_MULTIPLIER)
                if USE_WICK_STOP:
                    sl = max(sl, float(sweep_candle["high"]) + atr_value * WICK_BUFFER_ATR)
                risk = sl - current_price
                tp = current_price - (risk * RISK_REWARD)

            if signal:
                print(f"🚨 {symbol} SIGNAL: {signal}")

                sweep_key = f"{symbol}|{sweep_candle['time']}"
                already = sweep_key in state["attempted"] or any(t.get("sweep_key") == sweep_key for t in journal)
                if already:
                    print(f"[SKIP] {symbol}: sweep {sweep_key} already traded/attempted")
                    continue

                open_trades = [t for t in journal if t.get("status") == "OPEN"]

                # In net mode an opposite-direction order would shrink/flip the shared
                # position and confuse the journal, so don't open one while trades are open.
                if not HEDGE_MODE and any(t["symbol"] == symbol and t["side"] != side for t in open_trades):
                    print(f"[SKIP] {symbol}: opposite-direction trade still open (net mode)")
                    continue
                if sum(1 for t in open_trades if t["symbol"] == symbol) >= MAX_OPEN_PER_SYMBOL:
                    print(f"[SKIP] {symbol}: max open trades for this symbol")
                    continue
                if len(open_trades) >= MAX_TOTAL_OPEN:
                    print(f"[SKIP] {symbol}: max total open trades")
                    continue

                try:
                    balances = exchange.fetch_balance()["USDT"]
                    equity = float(balances.get("total") or 0)
                    free = float(balances.get("free") or 0)

                    if daily_pnl(journal) <= -(equity * DAILY_LOSS_LIMIT_PCT):
                        # mark as attempted first so the alert fires once, not every run
                        state["attempted"].append(sweep_key)
                        save_state(state)
                        print("[SKIP] daily loss limit hit")
                        send_telegram("🛑 Daily loss limit reached. Pausing for today.")
                        continue

                    risk_amount = equity * RISK_PERCENTAGE
                    price_risk = abs(current_price - sl)
                    if price_risk <= 0:
                        print(f"[SKIP] {symbol}: zero stop distance")
                        continue

                    market = exchange.market(symbol)
                    contract_size = float(market["contractSize"])

                    raw_contracts = (risk_amount / price_risk) / contract_size
                    max_position_value = (free * LEVERAGE) * 0.90
                    max_contracts_margin = max_position_value / current_price / contract_size

                    final_contracts = min(raw_contracts, max_contracts_margin)
                    amount = float(exchange.amount_to_precision(symbol, final_contracts))

                    if amount <= 0:
                        print(f"[SKIP] {symbol}: computed size is zero (free margin {free:.2f})")
                        continue

                    # Real risk of this trade (smaller than 3% when the margin cap binds)
                    new_risk = amount * contract_size * price_risk
                    open_risk = sum(
                        float(t.get("risk_usd") or equity * RISK_PERCENTAGE) for t in open_trades
                    )
                    if open_risk + new_risk > equity * MAX_OPEN_RISK_PCT:
                        print(f"[SKIP] {symbol}: open risk cap "
                              f"({open_risk:.2f} + {new_risk:.2f} > {equity * MAX_OPEN_RISK_PCT:.2f})")
                        continue

                    # Record the attempt BEFORE ordering so a failure can't loop every run
                    state["attempted"].append(sweep_key)
                    save_state(state)

                    margin_params = {"mgnMode": "isolated"}
                    if HEDGE_MODE:
                        margin_params["posSide"] = "long" if side == "buy" else "short"
                    exchange.set_leverage(LEVERAGE, symbol, margin_params)

                    params = {
                        "tdMode": "isolated",
                        "stopLoss": {"triggerPrice": float(exchange.price_to_precision(symbol, sl))},
                        "takeProfit": {"triggerPrice": float(exchange.price_to_precision(symbol, tp))},
                    }
                    if HEDGE_MODE:
                        params["posSide"] = "long" if side == "buy" else "short"

                    order = exchange.create_order(symbol, "market", side, amount, params=params)

                    trigger_data = {
                        "trend": "BULLISH" if is_bullish else "BEARISH",
                        "swept_low": bool(swept_low),
                        "swept_high": bool(swept_high),
                        "volume_ratio": float(sweep_candle["volume"] / sweep_candle["vol_ma"]),
                        "atr": float(atr_value),
                    }

                    journal.append({
                        "symbol": symbol, "side": side, "entry": float(current_price),
                        "sl": float(sl), "tp": float(tp), "amount": float(amount),
                        "risk_usd": float(new_risk),
                        "status": "OPEN",
                        "time": now_utc().isoformat(),
                        "entry_ts_ms": int(exchange.milliseconds()),
                        "order_id": order.get("id"),
                        "sweep_key": sweep_key,
                        "trigger_data": trigger_data,
                    })
                    save_journal(journal)

                    send_telegram(
                        f"✅ *{signal} EXECUTED*\n"
                        f"Asset: {symbol}\n"
                        f"Entry: {current_price:.4f}\n"
                        f"SL (Wide 2.5 ATR): {sl:.4f}\n"
                        f"TP: {tp:.4f}\n"
                        f"Size: {amount} contracts\n"
                        f"Risk: ${new_risk:.2f}\n"
                        f"Open trades: {len(open_trades) + 1}\n\n"
                        f"🔍 *Trigger Data:*\n"
                        f"Trend: {trigger_data['trend']}\n"
                        f"Vol Ratio: {trigger_data['volume_ratio']:.2f}x\n"
                        f"Swept: {'Low' if swept_low else 'High'}\n"
                        f"ATR: {atr_value:.2f}"
                    )
                except Exception as e:
                    print(f"Order Failed: {e}")
                    send_telegram(f"⚠️ Order Failed on {symbol}:\n{e}", markdown=False)
            else:
                last = float(state["last_debug"].get(symbol, 0))
                if time.time() - last > DEBUG_COOLDOWN_SECONDS:
                    trend_str = "BULLISH" if is_bullish else "BEARISH" if is_bearish else "FLAT"
                    debug_msg = (
                        f"🔍 *{symbol} ANALYSIS (HOLD)*\n\n"
                        f"Price: {current_price:.2f}\n"
                        f"4H Trend: {trend_str}\n"
                        f"Swept Low? {bool(swept_low)}\n"
                        f"Swept High? {bool(swept_high)}\n"
                        f"Volume Filter: {'PASS' if vol_ok else 'FAIL'}\n"
                        f"Result: Waiting."
                    )
                    send_telegram(debug_msg)
                    state["last_debug"][symbol] = time.time()

        except Exception as e:
            print(f"[ERROR] {symbol}: {e}")

    save_journal(journal)
    save_state(state)


if __name__ == "__main__":
    run_scanner()
