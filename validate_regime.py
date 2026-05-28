"""
Historical regime validation script.

Replays the time-of-day normalized volume ratio across the last 30 trading
days and across key snap-shot times (9:45, 10:00, 10:30, 11:00, 12:00, 14:00).

For each day shows what regime the volume check alone would have produced,
letting you verify:
  - Normal days are not falsely flagged as LOW_VOLUME/NO_TRADE at open
  - Genuinely thin days (e.g., pre-holiday, post-event calm) are detected
  - Abnormal event sessions (FOMC, CPI, quad-witching) are filtered out of baseline

Usage:
    python validate_regime.py              # last 30 trading days, SPY only
    python validate_regime.py --tickers QQQ IWM   # additional tickers
    python validate_regime.py --mins 30 60 90 120  # custom snap-shot minutes
"""
from __future__ import annotations
import argparse
import sys
from datetime import datetime, timedelta, date
from datetime import time as dtime
from zoneinfo import ZoneInfo
import numpy as np
import yfinance as yf

ET                    = ZoneInfo("America/New_York")
LOW_VOLUME_ABORT      = 0.35
LOW_VOLUME_THRESHOLD  = 0.60
OUTLIER_ZSCORE        = 2.5
MIN_SAMPLES           = 3
SAME_WD_WEIGHT        = 2
SNAP_MINUTES          = [15, 30, 60, 90, 120, 150, 180, 240]  # mins after 9:30


def _label(ratio: float) -> str:
    if ratio < LOW_VOLUME_ABORT:
        return f"\033[91mNO_TRADE ({ratio:.0%})\033[0m"
    if ratio < LOW_VOLUME_THRESHOLD:
        return f"\033[93mLOW_VOL  ({ratio:.0%})\033[0m"
    return f"\033[92mNORMAL   ({ratio:.0%})\033[0m"


def _load_intraday(ticker: str) -> "pd.DataFrame | None":
    try:
        df = yf.Ticker(ticker).history(period="60d", interval="5m", auto_adjust=True)
        if df.empty:
            return None
        df.index = df.index.tz_convert(ET)
        df = df[(df.index.time >= dtime(9, 30)) & (df.index.time < dtime(16, 0))]
        return df
    except Exception as e:
        print(f"  ERROR fetching {ticker}: {e}")
        return None


def _ratio_at_minute(
    df,
    target_date: date,
    mins_since_open: int,
    today_dow: int,
) -> tuple[float, int, int, int]:
    """
    Returns (ratio, today_vol, expected_vol, n_samples).
    Uses same logic as regime._intraday_vol_ratio.
    """
    # Target date cumulative volume
    bars   = df[df.index.date == target_date].sort_index()
    if bars.empty:
        return 0.0, 0, 0, 0
    t_open   = bars.index[0].replace(hour=9, minute=30, second=0, microsecond=0)
    cutoff   = t_open + timedelta(minutes=mins_since_open + 5)
    window   = bars[bars.index <= cutoff]
    today_v  = int(window["Volume"].sum())

    # Past sessions
    past = sorted({d for d in (i.date() for i in df.index) if d < target_date})
    past = past[-28:]  # buffer for outlier removal

    sessions = []
    for d in past:
        sess = df[df.index.date == d].sort_index()
        if len(sess) < 6:
            continue
        s_open  = sess.index[0].replace(hour=9, minute=30, second=0, microsecond=0)
        cut2    = s_open + timedelta(minutes=mins_since_open + 5)
        w2      = sess[sess.index <= cut2]
        if w2.empty:
            continue
        cv  = int(w2["Volume"].sum())
        fdv = int(sess["Volume"].sum())
        if cv <= 0 or fdv <= 0:
            continue
        sessions.append((d, cv, fdv, d.weekday() == today_dow))

    if len(sessions) < MIN_SAMPLES:
        return 1.0, today_v, 0, 0

    # Outlier removal
    fdv_arr   = np.array([s[2] for s in sessions], dtype=float)
    mean_fdv  = fdv_arr.mean()
    std_fdv   = fdv_arr.std()
    if std_fdv > 0:
        sessions = [s for s, ok in zip(sessions, np.abs(fdv_arr - mean_fdv) < OUTLIER_ZSCORE * std_fdv) if ok]

    if len(sessions) < MIN_SAMPLES:
        return 1.0, today_v, 0, 0

    same_wd  = [s[1] for s in sessions if     s[3]]
    other_wd = [s[1] for s in sessions if not s[3]]

    if same_wd:
        total_w  = sum(same_wd) * SAME_WD_WEIGHT + sum(other_wd)
        count_w  = len(same_wd)  * SAME_WD_WEIGHT + len(other_wd)
        expected = total_w / count_w
    elif other_wd:
        expected = sum(other_wd) / len(other_wd)
    else:
        return 1.0, today_v, 0, 0

    ratio = today_v / max(expected, 1)
    return round(ratio, 2), today_v, int(expected), len(sessions)


def run_validation(tickers: list[str], snap_minutes: list[int]):
    print(f"\n{'='*80}")
    print(f"  REGIME VOLUME VALIDATION — time-of-day normalized baseline")
    print(f"  Tickers: {tickers}  |  Snap-shots: {[f'+{m}min' for m in snap_minutes]}")
    print(f"  Thresholds: NO_TRADE < {LOW_VOLUME_ABORT:.0%}  |  LOW_VOL < {LOW_VOLUME_THRESHOLD:.0%}")
    print(f"{'='*80}\n")

    # Load data for all tickers
    data = {}
    for t in tickers:
        print(f"  Fetching {t} 5-min intraday (60d)…", end=" ", flush=True)
        df = _load_intraday(t)
        if df is not None:
            data[t] = df
            print(f"OK ({len(set(df.index.date))} days)")
        else:
            print("FAILED")

    if "SPY" not in data:
        print("ERROR: SPY data required")
        sys.exit(1)

    spy_df = data["SPY"]
    all_dates = sorted({d for d in (i.date() for i in spy_df.index)})
    # Only include dates where a full session exists (>= 60 bars)
    full_days = [d for d in all_dates
                 if len(spy_df[spy_df.index.date == d]) >= 60]
    replay_dates = full_days[-30:]  # last 30 complete sessions

    print(f"\n  Replaying {len(replay_dates)} sessions...\n")

    # Header
    snap_cols = "  ".join(f"+{m:>3}m" for m in snap_minutes)
    print(f"  {'Date':<12} {'DoW':<4}  {snap_cols}")
    print(f"  {'-'*12} {'-'*4}  " + "  ".join(["-"*9]*len(snap_minutes)))

    anomalies = []

    for d in replay_dates:
        dow_name = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][d.weekday()]
        row_parts = []
        for mins in snap_minutes:
            ratio, tv, ev, n = _ratio_at_minute(spy_df, d, mins, d.weekday())
            if n == 0:
                row_parts.append("  n/a  ")
            else:
                flag = "▲" if ratio >= LOW_VOLUME_THRESHOLD else ("▼" if ratio < LOW_VOLUME_ABORT else "~")
                row_parts.append(f"{flag}{ratio:.0%} ({n:>2})")

            # Flag if any early snap (<= 60 min) shows NO_TRADE
            if mins <= 60 and ratio < LOW_VOLUME_ABORT and n >= MIN_SAMPLES:
                anomalies.append((d, mins, ratio, n))

        print(f"  {d}  {dow_name:<4}  " + "  ".join(row_parts))

    # Summary
    print(f"\n{'='*80}")
    if anomalies:
        print(f"  ANOMALIES — sessions where NO_TRADE fired in first 60 min:")
        for d, mins, ratio, n in anomalies:
            dow_name = ["Mon","Tue","Wed","Thu","Fri","Fri","Sun"][d.weekday()]
            print(f"    {d} {dow_name} @+{mins}min  ratio={ratio:.0%}  samples={n}  "
                  f"← verify if genuinely thin or data gap")
    else:
        print(f"  No false early-session NO_TRADE triggers found in last {len(replay_dates)} sessions.")

    # Per-ticker secondary check at +60min
    if len(data) > 1:
        print(f"\n  Cross-ticker ratio at +60min (last 10 sessions):")
        print(f"  {'Date':<12}", end="")
        for t in tickers:
            print(f"  {t:<10}", end="")
        print()
        for d in replay_dates[-10:]:
            print(f"  {d}", end="")
            for t in tickers:
                if t not in data:
                    print(f"  {'n/a':<10}", end="")
                    continue
                r, tv, ev, n = _ratio_at_minute(data[t], d, 60, d.weekday())
                sym = "▲" if r >= LOW_VOLUME_THRESHOLD else ("▼" if r < LOW_VOLUME_ABORT else "~")
                print(f"  {sym}{r:.0%} ({n:>2})  ", end="")
            print()

    print(f"\n{'='*80}")
    print("  Legend: ▲=NORMAL  ~=LOW_VOL  ▼=NO_TRADE  (n=baseline sessions)")
    print(f"{'='*80}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Validate regime volume baseline historically")
    parser.add_argument("--tickers", nargs="+", default=["SPY", "QQQ", "IWM"])
    parser.add_argument("--mins",    nargs="+", type=int, default=SNAP_MINUTES)
    args = parser.parse_args()
    run_validation(args.tickers, args.mins)
