"""
Market regime detection — gates whether momentum day-trading is appropriate.

Regimes: TRENDING_UP, TRENDING_DOWN, CHOPPY, HIGH_VOL, LOW_VOLUME, NO_TRADE

LOW_VOLUME is REDUCED_RISK mode, not a full abort.  Full abort only triggers
when vol_ratio falls below LOW_VOLUME_ABORT_RATIO (genuine liquidity collapse).

Multi-factor LOW_VOLUME detection uses:
  - SPY volume vs same-weekday historical baseline (reduces false positives on
    light-trading days like day-before-holiday, post-earnings calm, etc.)
  - VIX level (thin + high-fear is more dangerous than thin + low-fear)
  - 10-day rolling average as secondary cross-check
"""
import json
import numpy as np
import yfinance as yf
from datetime import datetime, timezone
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


def _save(regime: str, reason: str, metrics: dict):
    REGIME_CACHE_FILE.write_text(json.dumps({
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "regime":   regime,
        "reason":   reason,
        "metrics":  metrics,
    }, indent=2))


def _fetch_vix() -> float:
    """Return current VIX close. Returns 18.0 (neutral) on failure."""
    try:
        data = yf.Ticker("^VIX").history(period="2d", interval="1d")
        if len(data) > 0:
            return round(float(data["Close"].iloc[-1]), 1)
    except Exception:
        pass
    return 18.0


def _weekday_vol_ratio(spy_df, today_vol: int) -> tuple[float, int]:
    """
    Compare today's SPY volume against the mean of recent same-weekday sessions.
    Returns (ratio, num_samples).  Falls back to (0.0, 0) if insufficient data.
    """
    try:
        today_dow   = datetime.now().weekday()
        same_day    = spy_df[spy_df.index.dayofweek == today_dow]["Volume"]
        # Exclude today (last row may be partial)
        same_day    = same_day.iloc[:-1]
        if len(same_day) < 2:
            return 0.0, 0
        avg         = same_day.mean()
        return (round(today_vol / avg, 2) if avg > 0 else 0.0), int(len(same_day))
    except Exception:
        return 0.0, 0


def detect_regime() -> tuple[str, str]:
    """
    Returns (regime_name, reason_string).
    Result is cached for REGIME_CACHE_MINS minutes to avoid repeated API calls.
    """
    cached = _load_cache()
    if cached:
        return cached["regime"], f"[cached] {cached['reason']}"

    try:
        # 30d history for reliable same-weekday baseline (4-6 same-day samples)
        spy = yf.Ticker("SPY").history(period="30d", interval="1d")
        qqq = yf.Ticker("QQQ").history(period="5d",  interval="1d")

        if len(spy) < SPY_TREND_DAYS + 2:
            _save(NO_TRADE, "Insufficient SPY data", {})
            return NO_TRADE, "Insufficient SPY data"

        closes = spy["Close"].values
        highs  = spy["High"].values
        lows   = spy["Low"].values

        # Rolling N-day average daily change
        window    = closes[-(SPY_TREND_DAYS + 1):]
        daily_chg = [(window[i] - window[i-1]) / window[i-1] * 100
                     for i in range(1, len(window))]
        avg_trend = sum(daily_chg) / len(daily_chg)

        # 14-day ATR as % of price
        trs     = [max(highs[i] - lows[i],
                       abs(highs[i] - closes[i-1]),
                       abs(lows[i]  - closes[i-1]))
                   for i in range(1, len(closes))]
        atr_pct = float(np.mean(trs[-14:])) / closes[-1] * 100

        # Volume: 10-day rolling average (primary)
        today_vol  = int(spy["Volume"].iloc[-1])
        avg_vol_10 = spy["Volume"].iloc[-11:-1].mean()
        vol_ratio  = today_vol / avg_vol_10 if avg_vol_10 > 0 else 1.0

        # Volume: same-weekday baseline (more reliable on seasonally light days)
        wd_ratio, wd_samples = _weekday_vol_ratio(spy, today_vol)

        # Use the more favourable (higher) ratio if weekday sample is meaningful
        effective_vol_ratio = max(vol_ratio, wd_ratio) if wd_samples >= 2 else vol_ratio
        vol_baseline        = "weekday" if wd_samples >= 2 else "10day"

        # Early session grace: skip volume-based NO_TRADE/LOW_VOLUME for first
        # EARLY_SESSION_GRACE_MINS after open — cumulative vol is always low then
        et_now = datetime.now(ZoneInfo("America/New_York"))
        mkt_open = et_now.replace(hour=9, minute=30, second=0, microsecond=0)
        mins_since_open = (et_now - mkt_open).total_seconds() / 60
        in_early_session = 0 <= mins_since_open < EARLY_SESSION_GRACE_MINS

        # VIX context
        vix = _fetch_vix()

        # QQQ directional agreement
        qqq_trend = 0.0
        if len(qqq) >= 2:
            qqq_trend = (float(qqq["Close"].iloc[-1]) - float(qqq["Close"].iloc[-2])) \
                        / float(qqq["Close"].iloc[-2]) * 100

        metrics = {
            "spy_avg_trend":      round(avg_trend, 3),
            "atr_pct":            round(atr_pct, 2),
            "vol_ratio_10d":      round(vol_ratio, 2),
            "vol_ratio_weekday":  round(wd_ratio, 2),
            "vol_ratio_eff":      round(effective_vol_ratio, 2),
            "vol_baseline":       vol_baseline,
            "wd_samples":         wd_samples,
            "qqq_trend":          round(qqq_trend, 3),
            "vix":                vix,
            "low_vol_mode":       False,
            "low_vol_abort":      False,
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

        elif effective_vol_ratio < LOW_VOLUME_ABORT_RATIO and not in_early_session:
            # Genuine liquidity collapse — abort all trading
            # (skipped in first EARLY_SESSION_GRACE_MINS: cumulative vol always low at open)
            regime = NO_TRADE
            metrics["low_vol_abort"] = True
            factors = [f"SPY vol {effective_vol_ratio:.0%} of {vol_baseline} avg"]
            if vix > 25:
                factors.append(f"VIX {vix:.0f} elevated")
            reason = (f"Liquidity collapse: {', '.join(factors)} — "
                      f"below {LOW_VOLUME_ABORT_RATIO:.0%} abort threshold")

        elif effective_vol_ratio < 0.60 and not in_early_session:
            # Thin but not collapsed — REDUCED_RISK mode
            regime = LOW_VOLUME
            metrics["low_vol_mode"] = True
            factors = [f"vol {effective_vol_ratio:.0%} of {vol_baseline} avg"]
            if vix < 15:
                factors.append(f"VIX {vix:.0f} (low fear, thin market)")
            elif vix > 22:
                factors.append(f"VIX {vix:.0f} (elevated fear)")
            if wd_samples >= 2 and wd_ratio > vol_ratio:
                factors.append(f"weekday adjusted ↑ from {vol_ratio:.0%}")
            reason = (f"LOW_VOLUME: {', '.join(factors)} — "
                      f"REDUCED_RISK mode (higher thresholds, smaller size, max 1 trade)")

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
    """
    Return full metrics from the last regime detection (volume ratios, VIX, flags).
    Triggers detection if cache is cold.
    """
    cached = _load_cache()
    if not cached:
        detect_regime()
        cached = _load_cache()
    return cached.get("metrics", {}) if cached else {}


def is_tradeable(regime: str) -> bool:
    """Returns True if trading is allowed in this regime."""
    return regime in TRADEABLE_REGIMES
