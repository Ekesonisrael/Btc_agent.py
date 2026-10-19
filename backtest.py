#!/usr/bin/env python3
"""
backtest.py - Daily Donchian Ensemble (Trend Following)
"""
import argparse
import sys
import time
import os

import numpy as np
import pandas as pd
import ccxt

# --------------------------------------------------------------------------
# DATA FETCHING (OKX 1D)
# --------------------------------------------------------------------------
def fetch_okx_data(symbol="BTC/USDT:USDT", tf="1Dutc", years=6):
    exchange = ccxt.okx({'options': {'defaultType': 'swap'}})
    
    # Calculate start time (now - years)
    start_ms = exchange.milliseconds() - int(years * 365 * 24 * 60 * 60 * 1000)
    since = start_ms
    all_ohlcv = []
    
    print(f"Fetching {symbol} {tf} data from OKX...")
    while True:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, tf, since=since, limit=100)
        except Exception as e:
            print(f"\nNetwork error: {e}. Retrying in 2s...")
            time.sleep(2)
            continue
            
        if not ohlcv:
            break
            
        all_ohlcv.extend(ohlcv)
        since = ohlcv[-1][0] + 1
        
        if since > exchange.milliseconds():
            break
            
        if len(all_ohlcv) % 500 == 0:
            print(f"  Fetched {len(all_ohlcv)} bars...", end="\r")
            
        time.sleep(exchange.rateLimit / 1000)
        
    print(f"\nTotal fetched: {len(all_ohlcv)} bars")
    
    if len(all_ohlcv) == 0:
        sys.exit("Error: 0 bars returned from OKX. Check the timeframe or start date.")
        
    df = pd.DataFrame(all_ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
    df['time'] = pd.to_datetime(df['time'], unit='ms', utc=True)
    df = df.drop_duplicates('time').sort_values('time').reset_index(drop=True)
    return df

# --------------------------------------------------------------------------
# INDICATORS (Donchian + ATR)
# --------------------------------------------------------------------------
def add_indicators(df):
    c = df["close"]
    
    # ATR for volatility sizing and trailing stop
    d = c.diff()
    pc = c.shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    
    # Donchian Channels (Ensemble: 20, 50, 100 days)
    # Shift by 1 to prevent look-ahead bias
    df["hh_20"] = df["high"].rolling(20).max().shift(1)
    df["ll_20"] = df["low"].rolling(20).min().shift(1)
    
    df["hh_50"] = df["high"].rolling(50).max().shift(1)
    df["ll_50"] = df["low"].rolling(50).min().shift(1)
    
    df["hh_100"] = df["high"].rolling(100).max().shift(1)
    df["ll_100"] = df["low"].rolling(100).min().shift(1)
    
    return df

def make_signals(df):
    # Only trigger if ALL three Donchian channels agree on a breakout.
    long_breakout = (df["close"] > df["hh_20"]) & (df["close"] > df["hh_50"]) & (df["close"] > df["hh_100"])
    short_breakout = (df["close"] < df["ll_20"]) & (df["close"] < df["ll_50"]) & (df["close"] < df["ll_100"])
    
    sig = np.where(long_breakout, 1, np.where(short_breakout, -1, 0))
    
    # Warmup period
    warm = np.arange(len(df)) >= 100
    ok = df[["atr"]].notna().all(axis=1).to_numpy() & warm
    return np.where(ok, sig, 0)

# --------------------------------------------------------------------------
# SIMULATION ENGINE
# --------------------------------------------------------------------------
def simulate(arr, sig, a, equity0):
    o, h, l, c, atr, t = (arr[k] for k in ("o", "h", "l", "c", "atr", "t"))
    n = len(o)
    eq = equity0
    out = []
    i = 0
    while i < n - 1:
        s = int(sig[i])
        if s == 0:
            i += 1
            continue
        e = i + 1
        # Wider stop for daily: 3.0x ATR
        risk = a.atr_mult * atr[i]
        
        if not np.isfinite(risk) or risk <= 0:
            i += 1
            continue
            
        entry = o[e] * (1 + s * a.slip)
        stop = entry - s * risk
        
        j, px, why = e, None, "timeout"
        highest_high = h[e]
        lowest_low = l[e]
        
        while j < n:
            if s == 1:
                highest_high = max(highest_high, h[j])
                stop = highest_high - risk
                sl_hit = l[j] <= stop
            else:
                lowest_low = min(lowest_low, l[j])
                stop = lowest_low + risk
                sl_hit = h[j] >= stop
                
            if sl_hit:
                px = min(stop, o[j]) if s == 1 else max(stop, o[j])
                why = "SL"
                break
                
            # Check for exit signal (opposite breakout)
            if j < n - 1:
                if s == 1 and sig[j] == -1:
                    px = o[j+1]
                    why = "Signal"
                    break
                if s == -1 and sig[j] == 1:
                    px = o[j+1]
                    why = "Signal"
                    break
                    
            j += 1
            
        if px is None:
            j, px = n - 1, c[n - 1]

        exit_eff = px * (1 - s * a.slip)
        net = s * (exit_eff - entry) - a.fee * (entry + exit_eff)
        r_mult = net / risk
        
        # Volatility-based sizing
        size = (eq * a.risk) / risk
        size = min(size, eq * a.max_lev / entry)
        
        eq = max(eq + size * net, 0.0)
        
        out.append((t[e], t[j], s, entry, exit_eff, why, r_mult, a.fee * (entry + exit_eff) / risk, eq, j - e + 1))
        
        if eq <= 0:
            break
        i = j
    
    cols = ["entry_time", "exit_time", "side", "entry", "exit", "why", "R", "cost_R", "equity", "bars"]
    return pd.DataFrame(out, columns=cols)

# --------------------------------------------------------------------------
# STATS
# --------------------------------------------------------------------------
def line(R, label):
    n = len(R)
    if n == 0:
        return f"{label:<14} n=0"
    gp, gl = R[R > 0].sum(), -R[R < 0].sum()
    pf = gp / gl if gl > 0 else float("inf")
    return (f"{label:<14} n={n:<5} win={100 * (R > 0).mean():5.1f}%  "
            f"avgR={R.mean():+.3f}  PF={pf:4.2f}  totR={R.sum():+8.1f}")

def bootstrap_ci(R, sims=5000, seed=1):
    rng = np.random.default_rng(seed)
    means = rng.choice(R, size=(sims, len(R)), replace=True).mean(axis=1)
    return np.percentile(means, [2.5, 97.5])

# --------------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--atr-mult", type=float, default=3.0)
    p.add_argument("--risk", type=float, default=0.02)
    p.add_argument("--fee", type=float, default=0.0005)
    p.add_argument("--slip", type=float, default=0.0005)
    p.add_argument("--max-lev", type=float, default=5.0)
    p.add_argument("--equity", type=float, default=10000.0)
    p.add_argument("--split", type=float, default=0.85)
    a = p.parse_args()

    df = fetch_okx_data(years=6)
    df = add_indicators(df)

    print(f"\nData: {df['time'].iloc[0]:%Y-%m-%d} -> {df['time'].iloc[-1]:%Y-%m-%d}, {len(df)} bars")

    sig = make_signals(df)
    arr = {k: df[v].to_numpy() for k, v in dict(o="open", h="high", l="low", c="close", atr="atr").items()}
    arr["t"] = df["time"].to_numpy()

    tr = simulate(arr, sig, a, a.equity)
    if tr.empty:
        sys.exit("No trades generated.")
    tr.to_csv("backtest_trades.csv", index=False)
    R = tr["R"].to_numpy()

    eq = np.concatenate([[a.equity], tr["equity"].to_numpy()])
    max_dd = (eq / np.maximum.accumulate(eq) - 1).min()
    
    bh_df = df.iloc[100:].copy()
    bh_equity = a.equity * (bh_df["close"] / bh_df["close"].iloc[0])
    
    print(f"\nEntry mode: Donchian Ensemble | risk {a.risk:.1%} | SL {a.atr_mult}xATR")
    print("-" * 78)
    print(line(R, "ALL"))
    lo, hi = bootstrap_ci(R)
    print(f"mean R 95% CI [{lo:+.3f}, {hi:+.3f}]  avg cost/trade={tr['cost_R'].mean():.3f}R")
    print(f"Strategy Return: {eq[-1] / a.equity - 1:+.1%} | Max DD {max_dd:.1%}")
    print(f"Buy & Hold Return: {bh_equity.iloc[-1] / a.equity - 1:+.1%}")
    
    mix = tr["why"].value_counts(normalize=True).mul(100).round(1).to_dict()
    print(f"exit mix %: {mix} | avg bars held {tr['bars'].mean():.1f}")

    print("-" * 78)
    cut_idx = int(len(tr) * a.split)
    in_sample = R[:cut_idx]
    out_sample = R[cut_idx:]
    
    print(line(in_sample, "IN-SAMPLE"))
    print(line(out_sample, "OUT-OF-SAMPLE"))
    
    print("-" * 78)
    strat_sharpe = R.mean() / R.std(ddof=1) if R.std(ddof=1) > 0 else 0
    bh_returns = bh_df["close"].pct_change().dropna()
    bh_sharpe = bh_returns.mean() / bh_returns.std() if bh_returns.std() > 0 else 0
    
    print(f"Strategy Sharpe: {strat_sharpe:.2f}")
    print(f"Buy&Hold Sharpe: {bh_sharpe:.2f}")
    
    os_lo, os_hi = bootstrap_ci(out_sample)
    os_positive = os_lo > 0
    
    if strat_sharpe > bh_sharpe and os_positive:
        print("\nVERDICT: PASS. Strategy beats Buy&Hold on risk-adjusted return and OOS CI > 0.")
    else:
        print("\nVERDICT: FAIL. Did not meet pre-registered criteria.")

if __name__ == "__main__":
    main()
