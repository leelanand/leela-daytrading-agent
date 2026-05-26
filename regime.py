"""
Market regime detection — gates whether momentum day-trading is appropriate.

Regimes: TRENDING_UP, TRENDING_DOWN, CHOPPY, HIGH_VOL, LOW_VOLUME, NO_TRADE
Uses SPY + QQQ daily bars. Cached to avoid repeated yfinance calls.
"""
import json
import numpy as np
import yfinance as yf
from datetime import datetime, timezone
from config import (
    REGIME_CACHE_MINS, SPY_TREND_DAYS, TRADEABLE_REGIMES, REGIME_CACHE_FILE,
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


def detect_regime() -> tuple[str, str]:
    """
    Returns (regime_name, reason_string).
    Result is cached for REGIME_CACHE_MINS minutes to avoid repeated API calls.
    """
    cached = _load_cache()
    if cached:
        return cached["regime"], f"[cached] {cached['reason']}"

    try:
        spy = yf.Ticker("SPY").history(period="15d", interval="1d")
        qqq = yf.Ticker("QQQ").history(period="5d",  interval="1d")

        if len(spy) < SPY_TREND_DAYS + 2:
            _save(NO_TRADE, "Insufficient SPY data", {})
            return NO_TRADE, "Insufficient SPY data"

        closes = spy["Close"].values
        highs  = spy["High"].values
        lows   = spy["Low"].values

        # Rolling N-day average daily change
        window      = closes[-(SPY_TREND_DAYS + 1):]
        daily_chg   = [(window[i] - window[i-1]) / window[i-1] * 100
                       for i in range(1, len(window))]
        avg_trend   = sum(daily_chg) / len(daily_chg)

        # 14-day ATR as % of price
        trs = [max(highs[i] - lows[i],
                   abs(highs[i] - closes[i-1]),
                   abs(lows[i]  - closes[i-1]))
               for i in range(1, len(closes))]
        atr_pct = float(np.mean(trs[-14:])) / closes[-1] * 100

        # Volume vs 10-day average
        today_vol = int(spy["Volume"].iloc[-1])
        avg_vol   = spy["Volume"].iloc[-11:-1].mean()
        vol_ratio = today_vol / avg_vol if avg_vol > 0 else 1.0

        # QQQ directional agreement
        qqq_trend = 0.0
        if len(qqq) >= 2:
            qqq_trend = (float(qqq["Close"].iloc[-1]) - float(qqq["Close"].iloc[-2])) \
                        / float(qqq["Close"].iloc[-2]) * 100

        metrics = {
            "spy_avg_trend": round(avg_trend, 3),
            "atr_pct":       round(atr_pct, 2),
            "vol_ratio":     round(vol_ratio, 2),
            "qqq_trend":     round(qqq_trend, 3),
        }

        if atr_pct > 2.0:
            regime = HIGH_VOL
            reason = f"SPY ATR {atr_pct:.1f}% — elevated volatility (trade with reduced size)"
        elif vol_ratio < 0.60:
            regime = LOW_VOLUME
            reason = f"SPY volume {vol_ratio:.0%} of average — thin market, poor fills"
        elif avg_trend > 0.25 and qqq_trend >= 0:
            regime = TRENDING_UP
            reason = f"SPY +{avg_trend:.2f}%/day trend, QQQ aligned"
        elif avg_trend < -0.25 and qqq_trend <= 0:
            regime = TRENDING_DOWN
            reason = f"SPY {avg_trend:.2f}%/day downtrend — avoid long bias"
        else:
            regime = CHOPPY
            reason = f"SPY flat/indecisive ({avg_trend:+.2f}%/day) — selective entries only"

        _save(regime, reason, metrics)
        return regime, reason

    except Exception as e:
        _save(NO_TRADE, f"Regime error: {e}", {})
        return NO_TRADE, f"Regime detection failed: {e}"


def is_tradeable(regime: str) -> bool:
    """Returns True if trading is allowed in this regime."""
    return regime in TRADEABLE_REGIMES
