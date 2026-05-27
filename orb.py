"""
Opening Range Breakout (ORB) detection.

Calculates the high/low of the first N minutes of the session and checks
whether the current price represents a confirmed breakout above that range.

ORB is a positive quality signal only — not a standalone buy trigger.
It supplements entry decisions but does not block them if not confirmed.
"""
from config import (
    ORB_ENABLED, ORB_5MIN_ENABLED, ORB_15MIN_ENABLED,
    ORB_VOLUME_CONFIRM_RATIO,
)


def calculate_orb(bars: list[dict], period_mins: int) -> dict:
    """
    From the first `period_mins` bars of the session, calculate the Opening Range.

    Args:
        bars: list of 1-min OHLCV dicts {open, high, low, close, volume},
              ordered oldest→newest, starting from market open (9:30 ET).
        period_mins: how many bars to include in the opening range (5 or 15).

    Returns:
        {orb_high, orb_low, orb_range_pct, calculated: bool}
    """
    empty = {"orb_high": 0.0, "orb_low": 0.0, "orb_range_pct": 0.0, "calculated": False}
    if not ORB_ENABLED:
        return empty
    if not bars or len(bars) < period_mins:
        return empty

    try:
        range_bars = bars[:period_mins]
        orb_high   = max(b["high"] for b in range_bars)
        orb_low    = min(b["low"]  for b in range_bars)
        orb_range  = (orb_high - orb_low) / orb_low * 100 if orb_low > 0 else 0.0
        return {
            "orb_high":      round(orb_high, 4),
            "orb_low":       round(orb_low, 4),
            "orb_range_pct": round(orb_range, 3),
            "calculated":    True,
        }
    except Exception:
        return empty


def check_orb_breakout(price: float, volume: int, avg_volume: int, orb: dict) -> dict:
    """
    Check whether the current price represents a confirmed ORB breakout.

    Args:
        price:      current live price
        volume:     current bar volume (or most recent bar volume)
        avg_volume: average bar volume across the session bars
        orb:        result dict from calculate_orb()

    Returns:
        {
          above_orb:          bool  — price > orb_high
          volume_confirmed:   bool  — breakout bar volume >= ORB_VOLUME_CONFIRM_RATIO × avg
          is_orb_breakout:    bool  — above_orb AND volume_confirmed
          orb_extension_pct:  float — how far above ORB high (%)
        }
    """
    null = {
        "above_orb":         False,
        "volume_confirmed":  False,
        "is_orb_breakout":   False,
        "orb_extension_pct": 0.0,
    }
    if not ORB_ENABLED or not orb.get("calculated"):
        return null

    try:
        orb_high   = orb["orb_high"]
        above_orb  = price > orb_high

        extension  = (price - orb_high) / orb_high * 100 if (above_orb and orb_high > 0) else 0.0

        vol_ok = False
        if avg_volume > 0 and volume > 0:
            vol_ok = (volume / avg_volume) >= ORB_VOLUME_CONFIRM_RATIO

        return {
            "above_orb":         above_orb,
            "volume_confirmed":  vol_ok,
            "is_orb_breakout":   above_orb and vol_ok,
            "orb_extension_pct": round(extension, 3),
        }
    except Exception:
        return null


def get_orb_status(bars: list[dict]) -> dict:
    """
    Convenience function: compute 5-min and 15-min ORBs from a bar list
    and check whether the most recent bar price is breaking out.

    Returns a combined status dict suitable for injection into the audit record.
    """
    if not bars or not ORB_ENABLED:
        return {"orb_enabled": False}

    current_price  = bars[-1]["close"]
    current_volume = bars[-1]["volume"]
    avg_volume     = sum(b["volume"] for b in bars) / len(bars) if bars else 1

    out: dict = {"orb_enabled": True}

    if ORB_5MIN_ENABLED:
        orb5  = calculate_orb(bars, 5)
        brk5  = check_orb_breakout(current_price, current_volume, avg_volume, orb5)
        out["orb_5min"] = {**orb5, **brk5}

    if ORB_15MIN_ENABLED:
        orb15 = calculate_orb(bars, 15)
        brk15 = check_orb_breakout(current_price, current_volume, avg_volume, orb15)
        out["orb_15min"] = {**orb15, **brk15}

    # Composite: any confirmed breakout
    is_breakout = (
        out.get("orb_5min",  {}).get("is_orb_breakout", False)
        or out.get("orb_15min", {}).get("is_orb_breakout", False)
    )
    out["orb_breakout"] = is_breakout
    return out
