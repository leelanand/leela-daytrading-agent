"""
Real-time intraday market alignment using SPY/QQQ 1-minute bars.

Different from regime.py (which uses daily closes for multi-day trend).
This detects same-session direction: is the market selling off RIGHT NOW?
Cached for INTRADAY_ALIGN_CACHE_MINS to avoid repeated bar API calls.
"""
import json
from datetime import datetime, timedelta, timezone
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY,
    INTRADAY_ALIGN_CACHE_MINS, INTRADAY_CACHE_FILE, BLOCK_ON_SELLING_OFF,
)

INTRADAY_UP          = "UP"
INTRADAY_FLAT        = "FLAT"
INTRADAY_DOWN        = "DOWN"
INTRADAY_SELLING_OFF = "SELLING_OFF"


def _load_cache() -> dict | None:
    if not INTRADAY_CACHE_FILE.exists():
        return None
    try:
        data     = json.loads(INTRADAY_CACHE_FILE.read_text())
        saved    = datetime.fromisoformat(data["saved_at"])
        age_mins = (datetime.now(timezone.utc) - saved).total_seconds() / 60
        if age_mins < INTRADAY_ALIGN_CACHE_MINS:
            return data
    except Exception:
        pass
    return None


def _save_cache(alignment: str, reason: str, metrics: dict):
    INTRADAY_CACHE_FILE.write_text(json.dumps({
        "saved_at":  datetime.now(timezone.utc).isoformat(),
        "alignment": alignment,
        "reason":    reason,
        "metrics":   metrics,
    }, indent=2))


def get_intraday_alignment() -> tuple[str, str]:
    """
    Returns (alignment, reason). Cached for INTRADAY_ALIGN_CACHE_MINS minutes.
    Uses last 20 SPY + QQQ 1-min bars to detect real-time market direction.
    """
    cached = _load_cache()
    if cached:
        return cached["alignment"], f"[cached] {cached['reason']}"

    spy_closes, spy_vols, qqq_closes = [], [], []

    # Primary: Alpaca historical data API
    try:
        client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
        end    = datetime.now(timezone.utc)
        start  = end - timedelta(minutes=35)
        req    = StockBarsRequest(
            symbol_or_symbols=["SPY", "QQQ"],
            timeframe=TimeFrame.Minute,
            start=start,
            end=end,
            feed="sip",
        )
        data    = client.get_stock_bars(req)
        spy_raw = list(data["SPY"]) if "SPY" in data else []
        qqq_raw = list(data["QQQ"]) if "QQQ" in data else []
        spy_raw = spy_raw[-20:] if len(spy_raw) >= 5 else spy_raw
        qqq_raw = qqq_raw[-20:] if len(qqq_raw) >= 5 else qqq_raw
        if len(spy_raw) >= 5:
            spy_closes = [float(b.close)  for b in spy_raw]
            spy_vols   = [float(b.volume) for b in spy_raw]
            qqq_closes = [float(b.close)  for b in qqq_raw] if len(qqq_raw) >= 5 else spy_closes
    except Exception:
        pass

    # Fallback: yfinance
    if len(spy_closes) < 5:
        try:
            import yfinance as yf
            spy_df = yf.download("SPY", period="1d", interval="1m", progress=False, auto_adjust=True)
            qqq_df = yf.download("QQQ", period="1d", interval="1m", progress=False, auto_adjust=True)
            if not spy_df.empty and len(spy_df) >= 5:
                spy_closes = spy_df["Close"].dropna().tolist()[-20:]
                spy_vols   = spy_df["Volume"].tolist()[-20:]
                qqq_closes = qqq_df["Close"].dropna().tolist()[-20:] if not qqq_df.empty else spy_closes
        except Exception:
            pass

    try:
        spy = spy_closes
        qqq = qqq_closes

        if not spy or len(spy) < 5:
            return INTRADAY_FLAT, "Insufficient SPY bars — defaulting to FLAT"

        # Net change over full window
        spy_chg     = (spy_closes[-1] - spy_closes[0]) / spy_closes[0] * 100
        qqq_chg     = (qqq_closes[-1] - qqq_closes[0]) / qqq_closes[0] * 100

        # Recent 5-bar momentum (momentum of the last 5 min)
        spy_recent  = (spy_closes[-1] - spy_closes[-5]) / spy_closes[-5] * 100

        # Volume acceleration in SPY (is selling volume increasing?)
        n    = len(spy_vols)
        half = n // 2
        early_vol  = sum(spy_vols[:half]) / max(1, half)
        recent_vol = sum(spy_vols[half:]) / max(1, n - half)
        vol_accel  = recent_vol / early_vol if early_vol > 0 else 1.0

        metrics = {
            "spy_chg":    round(spy_chg, 3),
            "qqq_chg":    round(qqq_chg, 3),
            "spy_recent": round(spy_recent, 3),
            "vol_accel":  round(vol_accel, 2),
        }

        # Classify — SELLING_OFF requires both decline AND accelerating volume
        if spy_chg < -0.25 and spy_recent < -0.12 and vol_accel >= 1.2:
            alignment = INTRADAY_SELLING_OFF
            reason    = (f"SPY {spy_chg:+.2f}% with vol {vol_accel:.1f}× "
                         f"— active selloff in progress")

        elif spy_chg < -0.20 and qqq_chg < -0.15:
            alignment = INTRADAY_DOWN
            reason    = f"SPY {spy_chg:+.2f}%, QQQ {qqq_chg:+.2f}% — both drifting lower"

        elif spy_chg > 0.10 and qqq_chg > 0.05:
            alignment = INTRADAY_UP
            reason    = f"SPY {spy_chg:+.2f}%, QQQ {qqq_chg:+.2f}% — aligned higher"

        else:
            alignment = INTRADAY_FLAT
            reason    = f"SPY {spy_chg:+.2f}% — directionless / consolidating"

        _save_cache(alignment, reason, metrics)
        return alignment, reason

    except Exception as e:
        return INTRADAY_FLAT, f"Alignment check failed ({e}) — defaulting to FLAT"


def is_aligned_for_longs(alignment: str) -> bool:
    """Returns False only during active SELLING_OFF (if BLOCK_ON_SELLING_OFF=True)."""
    if not BLOCK_ON_SELLING_OFF:
        return True
    return alignment != INTRADAY_SELLING_OFF
