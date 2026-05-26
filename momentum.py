"""
Intraday 1-minute bar momentum analysis.

Classifies momentum as STRENGTHENING / STABLE / WEAKENING / EXHAUSTED
by examining bar-by-bar volume acceleration, price trend, and spike patterns.
Called at execution time for tradeable candidates only — not during prescan.
"""
from datetime import datetime, timedelta, timezone
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, INTRADAY_BARS

STRENGTHENING = "STRENGTHENING"
STABLE        = "STABLE"
WEAKENING     = "WEAKENING"
EXHAUSTED     = "EXHAUSTED"


def _get_1min_bars(symbol: str, n: int) -> list:
    client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    end    = datetime.now(timezone.utc)
    start  = end - timedelta(minutes=n + 15)   # buffer for gaps / pre-market
    try:
        req  = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            start=start,
            end=end,
        )
        data = client.get_stock_bars(req)
        bars = list(data[symbol]) if symbol in data else []
        return bars[-n:] if len(bars) >= n else bars
    except Exception:
        return []


def analyse_momentum(symbol: str) -> dict:
    """
    Returns dict with keys:
      strength      — STRENGTHENING | STABLE | WEAKENING | EXHAUSTED
      reason        — human-readable explanation
      vol_accel     — recent vol / early vol ratio (>1 = accelerating)
      spike_detected — True if one candle dominates early volume
      price_rising  — True if making higher highs in second half
      price_fading  — True if close declining in second half
    """
    bars = _get_1min_bars(symbol, INTRADAY_BARS)

    if len(bars) < 5:
        return {"strength": STABLE, "reason": f"Only {len(bars)} bars — defaulting to STABLE",
                "vol_accel": 1.0, "spike_detected": False,
                "price_rising": False, "price_fading": False}

    closes  = [float(b.close)  for b in bars]
    volumes = [float(b.volume) for b in bars]
    highs   = [float(b.high)   for b in bars]
    lows    = [float(b.low)    for b in bars]
    opens   = [float(b.open)   for b in bars]
    n       = len(bars)
    half    = n // 2

    # Volume acceleration — is recent activity busier than early activity?
    early_vol  = sum(volumes[:half]) / max(1, half)
    recent_vol = sum(volumes[half:]) / max(1, n - half)
    vol_accel  = recent_vol / early_vol if early_vol > 0 else 1.0

    # Price trend — higher highs and not breaking earlier lows?
    early_high  = max(highs[:half])
    recent_high = max(highs[half:])
    early_low   = min(lows[:half])
    recent_low  = min(lows[half:])
    price_rising = recent_high > early_high and recent_low >= early_low * 0.999
    price_fading = closes[-1] < closes[half]

    # One-candle spike detection — single dominant bar in first half
    max_vol   = max(volumes)
    avg_vol   = sum(volumes) / n
    spike_idx = volumes.index(max_vol)
    is_spike  = max_vol > avg_vol * 3.5 and spike_idx < half

    # Candle body strength — full-bodied = conviction, doji = indecision
    bodies = []
    for i in range(half, n):
        rng = highs[i] - lows[i]
        if rng > 0:
            bodies.append(abs(closes[i] - opens[i]) / rng)
    avg_body = sum(bodies) / len(bodies) if bodies else 0.5

    # Classify
    if is_spike and price_fading and vol_accel < 0.8:
        strength = EXHAUSTED
        reason   = (f"One-candle spike (bar {spike_idx+1}/{n}), price fading, "
                    f"follow-through vol only {vol_accel:.1f}×")

    elif price_fading and vol_accel < 0.70:
        strength = WEAKENING
        reason   = (f"Price declining second half, vol slowing to {vol_accel:.1f}× — "
                    f"momentum fading")

    elif price_fading and not is_spike:
        strength = WEAKENING
        reason   = f"Price fading off highs (vol_accel={vol_accel:.1f}×)"

    elif price_rising and vol_accel >= 1.0 and avg_body >= 0.50:
        strength = STRENGTHENING
        reason   = (f"Higher highs, vol {vol_accel:.1f}× accelerating, "
                    f"candles {avg_body:.0%} full-bodied")

    else:
        strength = STABLE
        reason   = (f"Consolidating — vol_accel={vol_accel:.1f}×, "
                    f"body={avg_body:.0%}")

    return {
        "strength":       strength,
        "reason":         reason,
        "vol_accel":      round(vol_accel, 2),
        "spike_detected": is_spike,
        "price_rising":   price_rising,
        "price_fading":   price_fading,
        "body_ratio":     round(avg_body, 2),
    }
