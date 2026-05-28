"""
Polygon.io / Massive.com secondary data facade.

quote fetching is delegated to massive_feed (which handles provider failover).
This module keeps:
  - detect_alpaca_subscription()  — Alpaca-specific, not a data-provider concern
  - validate_cross_provider()     — comparison logic between Alpaca and secondary
"""
import time
from config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY,
    QUOTE_PRICE_MAX_DIFF_PCT, QUOTE_SPREAD_MAX_DIFF_PCT,
    QUOTE_VOLUME_MAX_DIFF_PCT, QUOTE_MAX_STALENESS_SECS,
)
from massive_feed import get_rest_quote, get_cached_quote


# ── Alpaca subscription detection ─────────────────────────────────────────────

def detect_alpaca_subscription() -> str:
    """
    Returns 'SIP', 'IEX', or 'unknown'.
    Tries a SIP-feed latest-quote call; subscription error → IEX.
    """
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestQuoteRequest
        from alpaca.data.enums import DataFeed
        client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
        client.get_stock_latest_quote(
            StockLatestQuoteRequest(symbol_or_symbols="SPY", feed=DataFeed.SIP)
        )
        return "SIP"
    except Exception as e:
        msg = str(e).lower()
        if "subscription" in msg or "permit" in msg:
            return "IEX"
        return "unknown"


# ── Secondary quote (Massive → Polygon fallback) ──────────────────────────────

def get_polygon_quote(symbol: str) -> dict | None:
    """
    Get a secondary-provider quote.
    Checks WS cache first (freshest), then falls back to REST.
    Returns Polygon-compatible normalised dict or None.
    """
    cached = get_cached_quote(symbol, max_age_secs=20)
    if cached and not cached.get("stale"):
        return cached
    return get_rest_quote(symbol)


# ── Cross-provider validation ─────────────────────────────────────────────────

def validate_cross_provider(
    symbol: str,
    alpaca_price: float,
    alpaca_spread_pct: float,
    alpaca_volume: int,
) -> tuple[bool, str, dict]:
    """
    Compare Alpaca quote with secondary provider (Massive/Polygon).
    Returns (ok, reason, secondary_quote_or_empty_dict).

    ok=False → reject the trade and log as data-quality rejection.
    ok=True  with reason='no_secondary_provider' → proceed (graceful skip).
    """
    secondary = get_polygon_quote(symbol)

    if secondary is None:
        return True, "no_secondary_provider", {}

    has_rt = secondary.get("has_realtime_quotes", True)

    # Stale timestamp — only enforce for real-time quotes; bar data is inherently 1-2 min old
    if has_rt and (secondary.get("stale") or secondary.get("age_secs", 0) > QUOTE_MAX_STALENESS_SECS):
        age = secondary.get("age_secs", "?")
        return False, (
            f"secondary_quote_stale: {age}s old (max {QUOTE_MAX_STALENESS_SECS}s)"
        ), secondary

    # IEX subscription returns $0.00 when no real-time quote is available;
    # treat as unavailable rather than a mismatch against the secondary feed.
    if alpaca_price <= 0:
        return True, "no_secondary_provider: alpaca_iex_unavailable", secondary

    mid = secondary.get("mid", 0)

    # Price divergence check (works for both real-time and bar-data mid)
    if mid > 0:
        price_diff_pct = abs(alpaca_price - mid) / mid * 100
        if price_diff_pct > QUOTE_PRICE_MAX_DIFF_PCT:
            provider = secondary.get("provider", "secondary")
            return False, (
                f"price_mismatch: alpaca ${alpaca_price:.2f} vs "
                f"{provider} ${mid:.2f} "
                f"({price_diff_pct:.3f}% > {QUOTE_PRICE_MAX_DIFF_PCT}% limit)"
            ), secondary

    # Spread divergence check — only when real-time bid/ask is available
    if has_rt:
        sec_spread  = secondary.get("spread_pct") or 0
        spread_diff = abs(alpaca_spread_pct - sec_spread)
        if spread_diff > QUOTE_SPREAD_MAX_DIFF_PCT:
            return False, (
                f"spread_mismatch: alpaca {alpaca_spread_pct:.3f}% vs "
                f"secondary {sec_spread:.3f}% "
                f"(diff {spread_diff:.3f}% > {QUOTE_SPREAD_MAX_DIFF_PCT}% limit)"
            ), secondary

    # Volume divergence — log-only (timing lags normal between providers)
    sec_vol = secondary.get("volume", 0) or secondary.get("acc_volume", 0)
    if alpaca_volume > 0 and sec_vol > 0:
        vol_diff_pct = (
            abs(alpaca_volume - sec_vol) / max(alpaca_volume, sec_vol) * 100
        )
        if vol_diff_pct > QUOTE_VOLUME_MAX_DIFF_PCT:
            return True, f"volume_divergence: {vol_diff_pct:.1f}% diff (logged)", secondary

    return True, "ok", secondary
