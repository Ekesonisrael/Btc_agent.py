#!/usr/bin/env python3
"""
backtest.py - backtest of the btc_agent.py rule set on 4H timeframe.
"""
import argparse
import sys
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import ccxt

# --------------------------------------------------------------------------
# DATA FETCHING (OKX 4H)
# --------------------------------------------------------------------------
def fetch_okx_data(symbol="BTC/USDT:USDT", tf="4h", years=4):
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
            
        if len(all_ohlcv) % 1000 == 0:
            print(f"  Fetched {len(all_ohlcv)} bars...", end="\r")
            
        time.sleep(exchange.rateLimit / 1000)
        
    print(f"\nTotal fetched: {len(all_ohlcv)} bars")
    
    df = pd.DataFrame(all_ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
    df['time'] = pd.to_datetime(df['time'], unit='ms', utc=True)
    df = df.drop_duplicates('time').sort_values('time').reset_index(drop=True)
    return df

# --------------------------------------------------------------------------
# INDICATORS + SIGNALS
# --------------------------------------------------------------------------
def add_indicators(df):
    c = df["close"]
    df["ema20"] = c.ewm(span=20, adjust=False).mean()
    df["ema50"] = c.ewm(span=50, adjust=False).mean()
    d = c.diff()
    up, dn = d.clip(lower=0), (-d).clip(lower=0)
    pc = c.shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    
    avg_up = up.rolling(14).mean()
    avg_dn = dn.rolling(14).mean()
    df["atr"] = tr.rolling(14).mean()
    
    with np.errstate(divide="ignore", invalid="ignore"):
        df["rsi"] = 100 - 100 / (1 + avg_up / avg_dn)
    return df

def make_signals(df, entry_mode):
    long_ = (df["close"] > df["ema20"]) & (df["ema20"] > df["ema50"]) & (df["rsi"] < 70)
    short_ = (df["close"] < df["ema20"]) & (df["ema20"] < df["ema50"]) & (df["rsi"] > 30)
    sig = np.where(long_, 1, np.where(short_, -1, 0))
    
    if entry_mode == "event":
        prev = np.roll(sig, 1)
        prev[0] = 0
        sig = np.where(sig != prev, sig, 0)
        
    warm = np.arange(len(df)) >= 60
    ok = df[["ema20", "ema50", "rsi", "atr"]].notna().all(axis=1).to_numpy() & warm
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
        risk = a.atr_mult * atr[i]
        
        if not np.isfinite(risk) or risk <= 0:
            i += 1
            continue
            
        entry = o[e] * (1 + s * a.slip)
        stop = entry - s * risk
        tp = entry + s * a.rr * risk
        last = n - 1 if a.max_hold == 0 else min(n - 1, e + a.max_hold - 1)

        j, px, why = e, None, "timeout"
        while j <= last:
            if s == 1:
                sl_hit, tp_hit = l[j] <= stop, h[j] >= tp
            else:
                sl_hit, tp_hit = h[j] >= stop, l[j] <= tp
            if sl_hit:
                px = min(stop, o[j]) if s == 1 else max(stop, o[j])
                why = "SL"
                break
            if tp_hit:
                px, why = tp, "TP"
                break
            j += 1
            
        if px is None:
            j, px = last, c[last]

        exit_eff = px * (1 - s * a.slip)
        net = s * (exit_eff - entry) - a.fee * (entry + exit_eff)
        r_mult = net / risk
        size = min(eq * a.risk / risk, eq * a.max_lev / entry)
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
    p.add_argument("--entry", choices=["state", "event"], default="state")
    p.add_argument("--atr-mult", type=float, default=1.5)
    p.add_argument("--rr", type=float, default=2.0)
    p.add_argument("--risk", type=float, default=0.02)
    p.add_argument("--fee", type=float, default=0.0005)
    p.add_argument("--slip", type=float, default=0.0002)
    p.add_argument("--max-lev", type=float, default=5.0)
    p.add_argument("--max-hold", type=int, default=0)
    p.add_argument("--equity", type=float, default=10000.0)
    p.add_argument("--split", type=float, default=0.7)
    p.add_argument("--null-sims", type=int, default=100)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out", default="backtest_trades.csv")
    a = p.parse_args()

    # Fetch 4H data
    df = fetch_okx_data(years=4)
    df = add_indicators(df)

    step = df["time"].diff().mode().iloc[0]
    gaps = int(((df["time"].diff() > step * 1.5)).sum())
    print(f"\nData: {df['time'].iloc[0]:%Y-%m-%d} -> {df['time'].iloc[-1]:%Y-%m-%d}, {len(df)} bars, {gaps} gaps")

    sig = make_signals(df, a.entry)
    arr = {k: df[v].to_numpy() for k, v in dict(o="open", h="high", l="low", c="close", atr="atr").items()}
    arr["t"] = df["time"].to_numpy()

    tr = simulate(arr, sig, a, a.equity)
    if tr.empty:
        sys.exit("No trades generated.")
    tr.to_csv(a.out, index=False)
    R = tr["R"].to_numpy()

    eq = np.concatenate([[a.equity], tr["equity"].to_numpy()])
    max_dd = (eq / np.maximum.accumulate(eq) - 1).min()
    bh = df["close"].iloc[-1] / df["close"].iloc[60] - 1
    
    print(f"Entry mode: {a.entry} | risk {a.risk:.1%} | SL {a.atr_mult}xATR | RR {a.rr}")
    print("-" * 78)
    print(line(R, "ALL"))
    lo, hi = bootstrap_ci(R)
    t_stat = R.mean() / (R.std(ddof=1) / np.sqrt(len(R))) if len(R) > 1 else float("nan")
    print(f"mean R 95% CI [{lo:+.3f}, {hi:+.3f}]  t={t_stat:+.2f}  avg cost/trade={tr['cost_R'].mean():.3f}R")
    print(f"equity {a.equity:,.0f} -> {eq[-1]:,.0f} ({eq[-1] / a.equity - 1:+.1%}), max DD {max_dd:.1%}, buy&hold {bh:+.1%}")
    
    mix = tr["why"].value_counts(normalize=True).mul(100).round(1).to_dict()
    print(f"exit mix %: {mix} | avg bars held {tr['bars'].mean():.1f}")

    print("-" * 78)
    for name, s in (("LONG", 1), ("SHORT", -1)):
        print(line(tr.loc[tr["side"] == s, "R"].to_numpy(), name))
        
    tr["year"] = pd.to_datetime(tr["entry_time"]).dt.year
    for y, g in tr.groupby("year"):
        print(line(g["R"].to_numpy(), str(y)))

    print("-" * 78)
    rng = np.random.default_rng(a.seed)
    nz = sig != 0
    null_means = []
    for _ in range(a.null_sims):
        rs = np.where(nz, rng.choice([-1, 1], size=len(sig)), 0)
        t0 = simulate(arr, rs, a, a.equity)
        if len(t0):
            null_means.append(t0["R"].mean())
            
    null_means = np.array(null_means)
    pval = (null_means >= R.mean()).mean() if len(null_means) else float("nan")
    print(f"Null (same entry bars, RANDOM direction): mean avgR {null_means.mean():+.3f}")
    print(f"Strategy avgR {R.mean():+.3f} beats {100 * (1 - pval):.0f}% of null runs (p~{pval:.3f})")

    print("-" * 78)
    passes = lo > 0 and pval < 0.05
    if passes:
        print("VERDICT: CLEARS SCREEN. Edge demonstrated.")
    else:
        print("VERDICT: edge NOT demonstrated. Shelve the strategy.")

if __name__ == "__main__":
    main()
