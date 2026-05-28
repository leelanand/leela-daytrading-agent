"""
Market regime detection — time-of-day normalized volume baseline.

Volume ratio compares today's cumulative SPY volume against the historical
average cumulative volume at the SAME minute since market open — NOT against
full-day averages, which always make early-session volume look artificially low.

Abnormal sessions (FOMC, CPI, quad-witching, half-days) are automatically
excluded via full-day volume z-score filtering (sessions with full-day vol
> 2.5σ from the mean are dropped from the baseline).

Same-weekday sessions are weighted 2× over cross-weekday sessions.

Secondary validation via QQQ + IWM: if SPY looks thin but QQQ/IWM are
normal, confidence in the low reading is reduced (uses median not SPY alone).

Thresholds (unchanged):
  effective_ratio >= 0.60 → normal vol regime (TRENDING_UP / CHOPPY)
  effective_ratio  0.35–0.60 → LOW_VOLUME → REDUCED_RISK mode
  effective_ratio  < 0.35  → NO_TRADE (genuine liquidity collapse)

Safety override: if a stock has RVOL >= 3.0 in LOW_VOLUME, the scan layer
can allow limited entry — this is signalled via metrics["high_conviction_ok"].
"""
from __future__ import annotations
import json
import numpy as np
import yfinance as yf
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from zoneinfo import ZoneInfo
from config import (
    REGIME_CACHE_MINS, EARLY_SESSION_GRACE_MINS,
    SPY_TREND_DAYS, TRADEABLE_REGIMES, REGIME_CACHE_FILE,
    LOW_VOLUME_ABORT_RATIO,
    HIGH_VOL_ABORT_ATR_PCT, HIGH_VOL_ABORT_VIX,
)

TRENDING_UP   = "TRENDING_UP"
TRENDING_DOWN = "TRENDING_DOWN"
CHOPPY        = "CHOPPY"
HIGH_VOL      = "HIGH_VOL"
LOW_VOLUME    = "LOW_VOLUME"
NO_TRADE      = "NO_TRADE"

ET                      = ZoneInfo("America/New_York")
_LOOKBACK_DAYS          = 20   # baseline sessions to use (after outlier removal)
_OUTLIER_ZSCORE         = 2.5  # full-day vol z-score for abnormal session filter
_MIN_BASELINE_SAMPLES   = 3    # minimum sessions needed to trust the baseline
_SAME_WD_WEIGHT         = 2    # same-weekday sessions count this many times


# ── Cache ──────────────────────────────────────────────────────────────────────

def _load_cache() -> dict | None:
    if not REGIME_CACHE_FILE.exists():
        return None
    try:
        data     = json.loads(REGIME_CACHE_FILE.read_text())
        saved    = datetime.fromisoformat(data["saved_at"])
        age_mins = (datetime.now(timezone.utc) - saved).total_seconds() / 60
        if age_mins < REGIME_CACHE_MINS:
            return data
    except Exception:
        pass
    return None


def _save(regime: str, reason: str, metrics: dict) -> None:
    REGIME_CACHE_FILE.write_text(json.dumps({
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "regime":   regime,
        "reason":   reason,
        "metrics":  metrics,
    }, indent=2))


# ── Data helpers ───────────────────────────────────────────────────────────────

def _fetch_vix() -> float:
    try:
        data = yf.Ticker("^VIX").history(period="2d", interval="1d")
        if len(data) > 0:
            return round(float(data["Close"].iloc[-1]), 1)
    except Exception:
        pass
    return 18.0


def _intraday_vol_ratio(ticker: str, mins_since_open: int) -> dict:
    """
    Compute ratio = today_cumulative_vol / avg_historical_cumulative_vol_at_same_minute.

    Uses 5-min bars over the last ~60 days.  Same-weekday sessions weighted 2×.
    Sessions with abnormal full-day volume (FOMC, witching, half-days) are
    excluded via z-score filtering before computing the baseline.

    Returns a dict with keys:
        ratio, today_vol, expected_vol, samples, same_wd_n, other_n, mins_since_open
    Falls back to ratio=1.0 (neutral) on any error or insufficient data.
    """
    _fallback = {"ratio": 1.0, "today_vol": 0, "expected_vol": 0,
                 "samples": 0, "same_wd_n": 0, "other_n": 0,
                 "mins_since_open": mins_since_open}
    try:
        now_et    = datetime.now(ET)
        today     = now_et.date()
        today_dow = now_et.weekday()

        df = yf.Ticker(ticker).history(period="60d", interval="5m", auto_adjust=True)
        if df.empty:
            return _fallback

        df.index = df.index.tz_convert(ET)
        # Regular hours only (9:30–16:00 ET)
        df = df[(df.index.time >= dtime(9, 30)) & (df.index.time < dtime(16, 0))]

        # Today's cumulative volume so far
        today_bars   = df[df.index.date == today].sort_index()
        today_cumvol = int(today_bars["Volume"].sum())

        # Collect past sessions
        past_dates = sorted({d for d in (idx.date() for idx in df.index) if d < today})
        past_dates  = past_dates[-(_LOOKBACK_DAYS + 8):]

        sessions = []  # (date, cumvol_at_min, full_day_vol, is_same_weekday)
        for d in past_dates:
            sess = df[df.index.date == d].sort_index()
            if len(sess) < 6:          # < ~30 min of data — half-day or bad data
                continue
            # Find market open for this date (first bar at or after 9:30)
            sess_open = sess.index[0].replace(hour=9, minute=30, second=0, microsecond=0)
            cutoff_dt = sess_open + timedelta(minutes=mins_since_open + 5)  # +5 bar alignment
            window    = sess[sess.index <= cutoff_dt]
            if window.empty:
                continue
            cumvol_at_min = int(window["Volume"].sum())
            full_day_vol  = int(sess["Volume"].sum())
            if cumvol_at_min <= 0 or full_day_vol <= 0:
                continue
            sessions.append((d, cumvol_at_min, full_day_vol, d.weekday() == today_dow))

        if len(sessions) < _MIN_BASELINE_SAMPLES:
            return {**_fallback, "today_vol": today_cumvol}

        # ── Outlier removal: exclude abnormal sessions by full-day volume ─────
        fdv       = np.array([s[2] for s in sessions], dtype=float)
        fdv_mean  = fdv.mean()
        fdv_std   = fdv.std()
        if fdv_std > 0:
            keep     = np.abs(fdv - fdv_mean) < _OUTLIER_ZSCORE * fdv_std
            sessions = [s for s, ok in zip(sessions, keep) if ok]

        if len(sessions) < _MIN_BASELINE_SAMPLES:
            return {**_fallback, "today_vol": today_cumvol}

        # ── Weighted baseline: same-weekday 2×, other weekdays 1× ────────────
        same_wd  = [s[1] for s in sessions if     s[3]]
        other_wd = [s[1] for s in sessions if not s[3]]

        if same_wd:
            total_w  = sum(same_wd) * _SAME_WD_WEIGHT + sum(other_wd)
            count_w  = len(same_wd)  * _SAME_WD_WEIGHT + len(other_wd)
            expected = total_w / count_w
        elif other_wd:
            expected = sum(other_wd) / len(other_wd)
        else:
            return {**_fallback, "today_vol": today_cumvol}

        expected = max(expected, 1.0)
        ratio    = today_cumvol / expected

        return {
            "ratio":           round(ratio, 2),
            "today_vol":       today_cumvol,
            "expected_vol":    int(expected),
            "samples":         len(sessions),
            "same_wd_n":       len(same_wd),
            "other_n":         len(other_wd),
            "mins_since_open": mins_since_open,
        }

    except Exception as e:
        return {**_fallback, "err": str(e)}


def _effective_ratio_and_note(spy: dict, qqq: dict, iwm: dict) -> tuple[float, str]:
    """
    Blend SPY / QQQ / IWM ratios.

    If SPY signals LOW/NO_TRADE but QQQ+IWM look healthy, use the median
    instead of SPY alone — avoids false positives from SPY-specific anomalies.
    If all three confirm low vol, trust SPY.
    """
    r_spy = spy.get("ratio", 1.0)
    r_qqq = qqq.get("ratio", 1.0)
    r_iwm = iwm.get("ratio", 1.0)

    secondary_avg = (r_qqq + r_iwm) / 2.0

    if r_spy < LOW_VOLUME_ABORT_RATIO and secondary_avg >= 0.55:
        # SPY signals collapse but QQQ+IWM healthy — use median, don't abort
        eff  = round(sorted([r_spy, r_qqq, r_iwm])[1], 2)   # median
        note = (f"SPY={r_spy:.2f} low but QQQ={r_qqq:.2f} IWM={r_iwm:.2f} healthy — "
                f"using median={eff:.2f}")
    elif r_spy < 0.60 and secondary_avg < 0.50:
        # All confirm thin market — use SPY (most representative)
        eff  = r_spy
        note = f"SPY={r_spy:.2f} confirmed by QQQ={r_qqq:.2f} IWM={r_iwm:.2f}"
    else:
        # Use conservative min of SPY and secondary mean
        eff  = round(min(r_spy, secondary_avg), 2)
        note = f"SPY={r_spy:.2f} QQQ={r_qqq:.2f} IWM={r_iwm:.2f}"

    return eff, note


# ── Main detection ─────────────────────────────────────────────────────────────

def detect_regime() -> tuple[str, str]:
    """
    Returns (regime_name, reason_string).
    Result is cached for REGIME_CACHE_MINS minutes.
    """
    cached = _load_cache()
    if cached:
        return cached["regime"], f"[cached] {cached['reason']}"

    try:
        et_now          = datetime.now(ET)
        mkt_open        = et_now.replace(hour=9, minute=30, second=0, microsecond=0)
        mins_since_open = max(0, int((et_now - mkt_open).total_seconds() / 60))
        in_early_session = mins_since_open < EARLY_SESSION_GRACE_MINS

        # ── Daily data: trend + ATR ───────────────────────────────────────────
        spy_daily = yf.Ticker("SPY").history(period="30d", interval="1d")
        qqq_daily = yf.Ticker("QQQ").history(period="5d",  interval="1d")

        if len(spy_daily) < SPY_TREND_DAYS + 2:
            _save(NO_TRADE, "Insufficient SPY data", {})
            return NO_TRADE, "Insufficient SPY data"

        closes = spy_daily["Close"].values
        highs  = spy_daily["High"].values
        lows   = spy_daily["Low"].values

        window    = closes[-(SPY_TREND_DAYS + 1):]
        daily_chg = [(window[i] - window[i-1]) / window[i-1] * 100
                     for i in range(1, len(window))]
        avg_trend = sum(daily_chg) / len(daily_chg)

        trs = [max(highs[i] - lows[i],
                   abs(highs[i] - closes[i-1]),
                   abs(lows[i]  - closes[i-1]))
               for i in range(1, len(closes))]
        atr_pct = float(np.mean(trs[-14:])) / closes[-1] * 100

        vix = _fetch_vix()

        qqq_trend = 0.0
        if len(qqq_daily) >= 2:
            qqq_trend = (float(qqq_daily["Close"].iloc[-1]) - float(qqq_daily["Close"].iloc[-2])) \
                        / float(qqq_daily["Close"].iloc[-2]) * 100

        # ── Intraday TOD-normalized volume ratios ─────────────────────────────
        spy_r = _intraday_vol_ratio("SPY", mins_since_open)
        qqq_r = _intraday_vol_ratio("QQQ", mins_since_open)
        iwm_r = _intraday_vol_ratio("IWM", mins_since_open)

        eff_ratio, ratio_note = _effective_ratio_and_note(spy_r, qqq_r, iwm_r)

        metrics = {
            # ── Primary: TOD-normalized ──
            "spy_intraday_ratio":    spy_r.get("ratio", 1.0),
            "qqq_intraday_ratio":    qqq_r.get("ratio", 1.0),
            "iwm_intraday_ratio":    iwm_r.get("ratio", 1.0),
            "effective_vol_ratio":   eff_ratio,
            "ratio_note":            ratio_note,
            "spy_today_vol":         spy_r.get("today_vol", 0),
            "spy_expected_vol":      spy_r.get("expected_vol", 0),
            "spy_baseline_samples":  spy_r.get("samples", 0),
            "spy_same_wd_n":         spy_r.get("same_wd_n", 0),
            "mins_since_open":       mins_since_open,
            # ── Trend ──
            "spy_avg_trend":         round(avg_trend, 3),
            "atr_pct":               round(atr_pct, 2),
            "qqq_trend":             round(qqq_trend, 3),
            "vix":                   vix,
            # ── Flags ──
            "low_vol_mode":          False,
            "low_vol_abort":         False,
            "in_early_session":      in_early_session,
            "high_conviction_ok":    False,  # set True in LOW_VOLUME for RVOL>=3 override
        }

        # ── Regime classification ─────────────────────────────────────────────
        if atr_pct > 2.0:
            if atr_pct > HIGH_VOL_ABORT_ATR_PCT and vix > HIGH_VOL_ABORT_VIX:
                regime = NO_TRADE
                reason = (f"EXTREME volatility: ATR {atr_pct:.1f}% > {HIGH_VOL_ABORT_ATR_PCT}% "
                          f"AND VIX {vix:.0f} > {HIGH_VOL_ABORT_VIX} — abort all trading")
            else:
                regime = HIGH_VOL
                reason = (f"SPY ATR {atr_pct:.1f}% — elevated volatility "
                          f"(REDUCED_RISK: +5pts score req, RVOL>=2x, max 1 trade, size-30%)")

        elif eff_ratio < LOW_VOLUME_ABORT_RATIO and not in_early_session:
            regime = NO_TRADE
            metrics["low_vol_abort"] = True
            reason = (
                f"Liquidity collapse — SPY {spy_r.get('ratio',0):.0%} of expected "
                f"@{mins_since_open}min | QQQ {qqq_r.get('ratio',0):.0%} | "
                f"IWM {iwm_r.get('ratio',0):.0%} | effective={eff_ratio:.0%} "
                f"< {LOW_VOLUME_ABORT_RATIO:.0%} threshold"
            )

        elif eff_ratio < 0.60 and not in_early_session:
            regime = LOW_VOLUME
            metrics["low_vol_mode"]     = True
            metrics["high_conviction_ok"] = True  # RVOL>=3 setups still allowed
            reason = (
                f"LOW_VOLUME — SPY {spy_r.get('ratio',0):.0%} of expected "
                f"@{mins_since_open}min | QQQ {qqq_r.get('ratio',0):.0%} | "
                f"IWM {iwm_r.get('ratio',0):.0%} | effective={eff_ratio:.0%} "
                f"(REDUCED_RISK: score>={85}, RVOL>=1.5x, max 1 trade, size-50%)"
            )

        elif avg_trend > 0.25 and qqq_trend >= 0:
            regime = TRENDING_UP
            reason = f"SPY +{avg_trend:.2f}%/day trend, QQQ aligned"

        elif avg_trend < -0.25 and qqq_trend <= 0:
            regime = TRENDING_DOWN
            reason = f"SPY {avg_trend:.2f}%/day downtrend — avoid long bias"

        else:
            regime = CHOPPY
            reason = (f"SPY flat/indecisive ({avg_trend:+.2f}%/day) — "
                      f"selective entries only")

        _save(regime, reason, metrics)
        return regime, reason

    except Exception as e:
        _save(NO_TRADE, f"Regime error: {e}", {})
        return NO_TRADE, f"Regime detection failed: {e}"


def get_regime_context() -> dict:
    """Return full metrics from the last regime detection. Triggers detection if cache cold."""
    cached = _load_cache()
    if not cached:
        detect_regime()
        cached = _load_cache()
    return cached.get("metrics", {}) if cached else {}


def is_tradeable(regime: str) -> bool:
    return regime in TRADEABLE_REGIMES


# ── Standalone diagnostic ──────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    print("Running regime detection (bypassing cache)…")
    REGIME_CACHE_FILE.unlink(missing_ok=True)
    regime, reason = detect_regime()
    ctx = get_regime_context()
    print(f"\nRegime  : {regime}")
    print(f"Reason  : {reason}")
    print(f"\nDiagnostics:")
    print(f"  mins_since_open   : {ctx.get('mins_since_open')} min")
    print(f"  SPY intraday ratio: {ctx.get('spy_intraday_ratio')} "
          f"(today={ctx.get('spy_today_vol'):,} vs expected={ctx.get('spy_expected_vol'):,})")
    print(f"  QQQ intraday ratio: {ctx.get('qqq_intraday_ratio')}")
    print(f"  IWM intraday ratio: {ctx.get('iwm_intraday_ratio')}")
    print(f"  Effective ratio   : {ctx.get('effective_vol_ratio')}  [{ctx.get('ratio_note')}]")
    print(f"  Baseline samples  : {ctx.get('spy_baseline_samples')} "
          f"(same-wd={ctx.get('spy_same_wd_n')})")
    print(f"  SPY trend         : {ctx.get('spy_avg_trend'):+.3f}%/day | ATR {ctx.get('atr_pct')}%")
    print(f"  QQQ trend         : {ctx.get('qqq_trend'):+.3f}%")
    print(f"  VIX               : {ctx.get('vix')}")
    print(f"  In early session  : {ctx.get('in_early_session')}")
    print(f"  low_vol_abort     : {ctx.get('low_vol_abort')}")
    print(f"  high_conviction_ok: {ctx.get('high_conviction_ok')}")
