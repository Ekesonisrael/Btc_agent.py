#!/usr/bin/env python3
"""
backtest_trend.py - daily Donchian-ensemble trend following on BTC vs buy-and-hold.
Data Source: OKX (Spot)
"""
import argparse
import sys
import time

import numpy as np
import pandas as pd
import ccxt

LOOKBACKS = [5, 10, 20, 30, 60, 90, 150, 250, 360]
ANN = 365  # crypto trades every day

# --------------------------------------------------------------------------
# DATA FETCHING (OKX SPOT)
# --------------------------------------------------------------------------
def fetch_okx_data(symbol="BTC/USDT", tf="1d", years=10):
    exchange = ccxt.okx({
        'enableRateLimit': True,
        'options': {'defaultType': 'spot'}
    })
    
    # Calculate start time (now - years)
    start_ms = exchange.milliseconds() - int(years * 365 * 24 * 60 * 60 * 1000)
    since = start_ms
    all_ohlcv = []
    error_count = 0
    
    print(f"Fetching {symbol} {tf} data from OKX Spot...")
    while True:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, tf, since=since, limit=100)
            error_count = 0 # Reset on success
        except Exception as e:
            print(f"\nNetwork error: {e}. Retrying in 2s...")
            time.sleep(2)
            error_count += 1
            if error_count > 5:
                sys.exit("Too many consecutive errors. Check the timeframe or symbol.")
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
        sys.exit("Error: 0 bars returned from OKX. Check the symbol or start date.")
        
    df = pd.DataFrame(all_ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
    df['time'] = pd.to_datetime(df['time'], unit='ms', utc=True)
    df = df.drop_duplicates('time').sort_values('time').reset_index(drop=True)
    return df

def load_csv(path):
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    tcol = next((c for c in ("time", "timestamp", "open_time", "date") if c in df.columns), None)
    if tcol is None:
        sys.exit("CSV needs a time column: time / timestamp / open_time / date")
    if pd.api.types.is_numeric_dtype(df[tcol]):
        first = df[tcol].iloc[0]
        unit = "us" if first > 1e14 else ("ms" if first > 1e11 else "s")
        df["time"] = pd.to_datetime(df[tcol], unit=unit, utc=True)
    else:
        df["time"] = pd.to_datetime(df[tcol], utc=True)
    df = df[["time", "open", "high", "low", "close", "volume"]].drop_duplicates("time")
    df = df.sort_values("time").reset_index(drop=True)
    step = df["time"].diff().median()
    if step < pd.Timedelta(hours=23):
        print(f"Resampling {step} bars to daily")
        df = (df.set_index("time")
                .resample("1D")
                .agg({"open": "first", "high": "max", "low": "min",
                      "close": "last", "volume": "sum"})
                .dropna().reset_index())
    return df

# --------------------------------------------------------------------------
# STRATEGY
# --------------------------------------------------------------------------
def donchian_ensemble(df):
    """Fraction of lookbacks currently long. Channel uses PRIOR N days (shifted)."""
    c = df["close"].to_numpy()
    n = len(df)
    states = []
    for N in LOOKBACKS:
        up = df["high"].rolling(N).max().shift(1).to_numpy()
        lo = df["low"].rolling(N).min().shift(1).to_numpy()
        mid = (up + lo) / 2
        st = np.zeros(n)
        on = 0
        for i in range(n):
            if np.isfinite(up[i]):
                if on == 0 and c[i] > up[i]:
                    on = 1
                elif on == 1 and c[i] < mid[i]:
                    on = 0
            st[i] = on
        states.append(st)
    return np.mean(states, axis=0)

def band_rebalance(target, band):
    w = np.zeros(len(target))
    cur = 0.0
    for i, t in enumerate(target):
        if t == 0.0 and cur != 0.0:
            cur = 0.0
        elif abs(t - cur) > band:
            cur = t
        w[i] = cur
    return w

def run_weights(w, oo, cost):
    wa = np.r_[0.0, w[:-1]]
    prev = np.r_[0.0, wa[:-1]]
    turn = np.abs(wa - prev)
    return wa * oo - turn * cost, turn, wa

# --------------------------------------------------------------------------
# STATS
# --------------------------------------------------------------------------
def perf(r):
    r = np.asarray(r)
    eq = np.cumprod(1 + r)
    yrs = len(r) / ANN
    sd = r.std(ddof=1)
    peak = np.maximum.accumulate(np.r_[1.0, eq])[1:]
    dd = (eq / peak - 1).min()
    cagr = eq[-1] ** (1 / yrs) - 1
    return dict(total=eq[-1] - 1, cagr=cagr, vol=sd * np.sqrt(ANN),
                sharpe=r.mean() / sd * np.sqrt(ANN) if sd > 0 else float("nan"),
                maxdd=dd, calmar=cagr / abs(dd) if dd < 0 else float("nan"))

def row(name, p):
    return (f"{name:<16} {p['total']:>+9.0%} {p['cagr']:>+7.1%} {p['vol']:>6.1%} "
            f"{p['sharpe']:>6.2f} {p['maxdd']:>7.1%} {p['calmar']:>6.2f}")

def sharpe_rows(x):
    sd = x.std(axis=1, ddof=1)
    return np.where(sd > 0, x.mean(axis=1) / sd * np.sqrt(ANN), np.nan)

def block_bootstrap_sharpe_diff(a, b, block=30, sims=2000, seed=7):
    rng = np.random.default_rng(seed)
    m = len(a)
    nb = int(np.ceil(m / block))
    starts = rng.integers(0, m, size=(sims, nb))
    idx = ((starts[:, :, None] + np.arange(block)) % m).reshape(sims, -1)[:, :m]
    d = sharpe_rows(a[idx]) - sharpe_rows(b[idx])
    return np.nanpercentile(d, [2.5, 50, 97.5])

# --------------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--csv")
    p.add_argument("--years", type=int, default=10, help="years of OKX data to try")
    p.add_argument("--target-vol", type=float, default=0.40)
    p.add_argument("--cap", type=float, default=1.0, help="max weight (1.0 = no leverage)")
    p.add_argument("--band", type=float, default=0.10)
    p.add_argument("--fee", type=float, default=0.0010, help="fee per unit traded (OKX spot taker ~0.10%%)")
    p.add_argument("--slip", type=float, default=0.0005)
    a = p.parse_args()

    df = load_csv(a.csv) if a.csv else fetch_okx_data(years=a.years)
    df = df.reset_index(drop=True)
    n = len(df)
    if n < max(LOOKBACKS) + 400:
        print(f"WARNING: only {n} daily bars. After the {max(LOOKBACKS)}-day warm-up "
              f"that leaves {max(n - max(LOOKBACKS), 0)} days. Too short to trust anything.")
    cost = a.fee + a.slip

    o = df["open"].to_numpy()
    lr = np.log(df["close"]).diff()
    vol = (lr.rolling(30).std() * np.sqrt(ANN)).to_numpy()
    ok = np.isfinite(vol) & (vol > 0)
    sig = donchian_ensemble(df)
    tgt_trend = np.where(ok, np.minimum(sig * a.target_vol / np.where(ok, vol, 1), a.cap), 0.0)
    tgt_vt = np.where(ok, np.minimum(a.target_vol / np.where(ok, vol, 1), a.cap), 0.0)
    w_trend, w_vt = band_rebalance(tgt_trend, a.band), band_rebalance(tgt_vt, a.band)

    oo = np.r_[o[1:] / o[:-1] - 1, np.nan]
    r_trend, turn_t, wa_t = run_weights(w_trend, oo, cost)
    r_vt, _, _ = run_weights(w_vt, oo, cost)
    r_bh = oo.copy()

    i0, i1 = max(LOOKBACKS), n - 1
    sl = slice(i0, i1)
    rt, rv, rb = r_trend[sl], r_vt[sl], r_bh[sl]
    days = df["time"].iloc[sl].reset_index(drop=True)

    print(f"\nData: {df['time'].iloc[0]:%Y-%m-%d} -> {df['time'].iloc[-1]:%Y-%m-%d} ({n} days). "
          f"Evaluated: {days.iloc[0]:%Y-%m-%d} -> {days.iloc[-1]:%Y-%m-%d} ({len(rt)} days)")
    print(f"target vol {a.target_vol:.0%} | cap {a.cap} | band {a.band} | cost {cost:.2%} per unit traded")
    print("-" * 74)
    print(f"{'':<16} {'Total':>9} {'CAGR':>7} {'Vol':>6} {'Sharpe':>6} {'MaxDD':>7} {'Calmar':>6}")
    pt, pv, pb = perf(rt), perf(rv), perf(rb)
    print(row("TREND ENSEMBLE", pt))
    print(row("VOL-TARGET HOLD", pv))
    print(row("BUY & HOLD", pb))

    exposure = wa_t[sl].mean()
    yrs = len(rt) / ANN
    print("-" * 74)
    print(f"trend: avg exposure {exposure:.0%} | days invested {np.mean(wa_t[sl] > 0):.0%} | "
          f"trades/yr {np.sum(turn_t[sl] > 0) / yrs:.1f} | turnover/yr {turn_t[sl].sum() / yrs:.1f}x | "
          f"cost drag {turn_t[sl].sum() * cost / yrs:.2%}/yr")

    print("-" * 74)
    print(f"{'Year':<6} {'Trend':>8} {'VolHold':>8} {'B&H':>8}   (calendar-year returns)")
    yr = days.dt.year.to_numpy()
    for y in np.unique(yr):
        m = yr == y
        f = lambda r: np.prod(1 + r[m]) - 1
        print(f"{y:<6} {f(rt):>+8.1%} {f(rv):>+8.1%} {f(rb):>+8.1%}")
    last = slice(-365, None)
    print(f"{'L365d':<6} {perf(rt[last])['total']:>+8.1%} {perf(rv[last])['total']:>+8.1%} "
          f"{perf(rb[last])['total']:>+8.1%}   Sharpe {perf(rt[last])['sharpe']:.2f} / "
          f"{perf(rv[last])['sharpe']:.2f} / {perf(rb[last])['sharpe']:.2f}")

    print("-" * 74)
    lo_b, med_b, hi_b = block_bootstrap_sharpe_diff(rt, rb)
    lo_v, med_v, hi_v = block_bootstrap_sharpe_diff(rt, rv)
    print(f"Sharpe diff, Trend - Buy&Hold   : median {med_b:+.2f}, 95% CI [{lo_b:+.2f}, {hi_b:+.2f}]")
    print(f"Sharpe diff, Trend - VolTarget  : median {med_v:+.2f}, 95% CI [{lo_v:+.2f}, {hi_v:+.2f}]")
    print("(block bootstrap, 30-day blocks; the second line is the value of TIMING itself)")

    print("-" * 74)
    A = pt["sharpe"] > pb["sharpe"]
    B = pt["maxdd"] > pb["maxdd"]
    C = lo_v > 0
    print(f"[{'x' if A else ' '}] A. Sharpe beats buy & hold ({pt['sharpe']:.2f} vs {pb['sharpe']:.2f})")
    print(f"[{'x' if B else ' '}] B. Max drawdown shallower than buy & hold ({pt['maxdd']:.0%} vs {pb['maxdd']:.0%})")
    print(f"[{'x' if C else ' '}] C. Timing beats plain vol-targeting with CI above 0")
    if A and B and C:
        print("VERDICT: passes all three. Still one history, one asset. Paper trade before real money.")
    elif A and B:
        print("VERDICT: NO PROOF OF TIMING SKILL. It beats buy & hold on risk, but A and B alone are "
              "easy (pure noise passes them: cash and vol-targeting cut drawdowns by construction). "
              "Without C, this is risk management, not an edge.")
    else:
        print("VERDICT: FAILS the pre-set screen. Do not trade it. Do not tune parameters to rescue it.")

    out = pd.DataFrame({"time": days, "trend": rt, "voltarget": rv, "buyhold": rb,
                        "weight": wa_t[sl]})
    out.to_csv("trend_daily_returns.csv", index=False)
    print("Daily returns saved to trend_daily_returns.csv")

if __name__ == "__main__":
    main()
