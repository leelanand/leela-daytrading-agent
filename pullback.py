"""
Pullback entry logic.

Evaluates whether a candidate's current price represents a quality pullback
entry point (toward 9 EMA or VWAP) after initial momentum.

Only applied to HIGH and ELITE tier candidates (score >= 85).
NORMAL tier candidates (78-84) bypass this check and enter on signal.
"""
from config import (
    PULLBACK_ENABLED, PULLBACK_EMA_PERIOD, PULLBACK_VWAP_MAX_PCT,
)


def _calc_ema(closes: list[float], period: int) -> float:
    """Exponential Moving Average of the most recent bar."""
    if len(closes) < period:
        return closes[-1] if closes else 0.0
    k   = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period   # seed with simple average
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return ema


def check_pullback_entry(candidate: dict, bars: list[dict]) -> dict:
    """
    Given a candidate and recent 1-min bars (list of {open,high,low,close,volume}),
    determine if this is a good pullback entry point.

    Returns dict:
      pullback_detected:  bool
      pullback_quality:   "STRONG" | "ACCEPTABLE" | "NONE"
      pullback_reason:    str
      should_wait:        bool  — True = not yet at pullback level but still valid
      reject:             bool  — True = momentum has failed, discard candidate
    """
    null_result = {
        "pullback_detected": False,
        "pullback_quality":  "NONE",
        "pullback_reason":   "pullback check disabled or insufficient data",
        "should_wait":       False,
        "reject":            False,
    }

    if not PULLBACK_ENABLED:
        return {**null_result, "pullback_reason": "pullback disabled in config"}

    if not bars or len(bars) < max(PULLBACK_EMA_PERIOD, 3):
        return {**null_result, "pullback_reason": f"insufficient bars ({len(bars)})"}

    try:
        closes  = [b["close"]  for b in bars]
        volumes = [b["volume"] for b in bars]
        highs   = [b["high"]   for b in bars]
        lows    = [b["low"]    for b in bars]

        current_price = closes[-1]
        avg_volume    = sum(volumes) / len(volumes) if volumes else 1
        last_volume   = volumes[-1]

        # Calculate 9-period EMA
        ema = _calc_ema(closes, PULLBACK_EMA_PERIOD)

        # VWAP from candidate dict (set during scanning)
        vwap = candidate.get("vwap", 0.0)

        # Prescan entry price (reference low)
        prescan_low = candidate.get("open_price", current_price)

        # --- Rejection checks (fail fast) ---

        # 1. Price broke below VWAP — bearish
        if vwap > 0 and current_price < vwap * 0.999:
            return {
                "pullback_detected": False,
                "pullback_quality":  "NONE",
                "pullback_reason":   f"price ${current_price:.2f} below VWAP ${vwap:.2f} — bearish",
                "should_wait":       False,
                "reject":            True,
            }

        # 2. Volume collapsed on pullback bar (< 40% of average)
        if last_volume < avg_volume * 0.40:
            return {
                "pullback_detected": False,
                "pullback_quality":  "NONE",
                "pullback_reason":   f"volume collapse on pullback bar ({last_volume:.0f} < 40% of avg {avg_volume:.0f})",
                "should_wait":       False,
                "reject":            True,
            }

        # 3. Price made a new low below the prescan/open price
        if current_price < prescan_low * 0.995:
            return {
                "pullback_detected": False,
                "pullback_quality":  "NONE",
                "pullback_reason":   f"new low ${current_price:.2f} below entry reference ${prescan_low:.2f}",
                "should_wait":       False,
                "reject":            True,
            }

        # --- Positive checks ---

        # Distance from EMA
        ema_dist_pct = abs(current_price - ema) / ema * 100 if ema > 0 else 99.0
        near_ema     = ema_dist_pct <= 0.5    # strong: within 0.5%
        ok_ema       = ema_dist_pct <= 1.5    # acceptable: within 1.5%

        # Distance from VWAP
        vwap_dist_pct = abs(current_price - vwap) / vwap * 100 if vwap > 0 else 99.0
        near_vwap     = vwap_dist_pct <= PULLBACK_VWAP_MAX_PCT

        # Volume health on pullback: last bar >= 70% of average = healthy
        vol_healthy = last_volume >= avg_volume * 0.70

        # Classify quality
        if near_ema and (near_vwap or vol_healthy):
            quality = "STRONG"
            reason  = (f"price within {ema_dist_pct:.2f}% of 9-EMA ${ema:.2f}"
                       f"{', near VWAP' if near_vwap else ''}"
                       f"{', vol healthy' if vol_healthy else ''}")
            detected = True
            wait     = False

        elif ok_ema and (near_vwap or vol_healthy):
            quality  = "ACCEPTABLE"
            reason   = (f"price within {ema_dist_pct:.2f}% of 9-EMA ${ema:.2f} (acceptable range)"
                        f"{', near VWAP' if near_vwap else ''}")
            detected = True
            wait     = False

        elif current_price > ema * 1.015:
            # Price still above EMA — not yet pulled back, keep watching
            quality  = "NONE"
            reason   = f"price ${current_price:.2f} still {ema_dist_pct:.2f}% above EMA — waiting for pullback"
            detected = False
            wait     = True

        else:
            # EMA gap but no VWAP/vol confirmation
            quality  = "NONE"
            reason   = f"EMA dist={ema_dist_pct:.2f}%, VWAP dist={vwap_dist_pct:.2f}% — no clean pullback"
            detected = False
            wait     = False

        return {
            "pullback_detected": detected,
            "pullback_quality":  quality,
            "pullback_reason":   reason,
            "should_wait":       wait,
            "reject":            False,
            "ema":               round(ema, 4),
            "ema_dist_pct":      round(ema_dist_pct, 3),
            "vwap_dist_pct":     round(vwap_dist_pct, 3),
            "vol_healthy":       vol_healthy,
        }

    except Exception as e:
        return {**null_result, "pullback_reason": f"pullback check error: {e}"}
