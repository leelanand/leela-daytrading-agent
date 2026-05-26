"""
Feed health monitor.

Checks run at the start of each prescan/scan cycle:
  1. Alpaca broker connectivity        (critical — blocks live trading if down)
  2. Alpaca data quote freshness       (warning — IEX subscription may be slow)
  3. Polygon secondary provider        (non-critical — graceful fallback)
  4. Candle spike detection            (warning — flags bad tick data)

State is persisted to FEED_HEALTH_FILE. If critical checks fail, the caller
should downgrade live trading to paper mode.
"""
import json
import time
from datetime import datetime, timezone
from alpaca.trading.client import TradingClient
from config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, PAPER_TRADING,
    FEED_HEALTH_FILE, FEED_LOG_FILE,
    FEED_HEALTH_STALE_QUOTE_SECS, FEED_HEALTH_SPIKE_PCT,
    POLYGON_API_KEY,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_state() -> dict:
    if FEED_HEALTH_FILE.exists():
        try:
            return json.loads(FEED_HEALTH_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"status": "unknown", "block_live_trading": False, "last_check": None, "issues": []}


def _save_state(state: dict):
    FEED_HEALTH_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def log_health_event(event_type: str, details: dict | None = None):
    """Append a structured health event to the feed log."""
    entry = {"ts": _now_iso(), "event": event_type, **(details or {})}
    with open(FEED_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


# ── Individual checks ─────────────────────────────────────────────────────────

def _check_alpaca_connectivity() -> tuple[bool, str]:
    try:
        client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=PAPER_TRADING)
        client.get_account()
        return True, "ok"
    except Exception as e:
        return False, f"alpaca_unreachable: {str(e)[:80]}"


def _check_quote_freshness() -> tuple[bool, float]:
    """
    Fetch latest SPY quote and measure age.
    Returns (fresh, age_secs). Fails open — subscription issues ≠ staleness.
    """
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestQuoteRequest
        client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
        resp   = client.get_stock_latest_quote(
            StockLatestQuoteRequest(symbol_or_symbols="SPY")
        )
        quote = resp.get("SPY")
        if quote is None or quote.timestamp is None:
            return True, 0.0
        ts  = quote.timestamp
        age = time.time() - (ts.timestamp() if hasattr(ts, "timestamp") else 0)
        return age < FEED_HEALTH_STALE_QUOTE_SECS, round(age, 1)
    except Exception:
        return True, 0.0  # SIP subscription errors ≠ stale — fail open


def _check_secondary_provider() -> tuple[bool, str]:
    """
    Check secondary provider (Massive/Polygon) connectivity.
    Uses daily-bar endpoint (available on free tier) for connectivity ping,
    not the snapshot endpoint (requires paid plan).
    """
    from massive_feed import active_providers, get_historical_stats
    providers = active_providers()
    if not providers:
        return True, "not_configured"
    # Use historical stats (free-tier endpoint) for the connectivity check
    stats = get_historical_stats("SPY", days=2)
    if not stats:
        return False, f"secondary_unreachable ({providers[0]})"
    return True, f"ok ({stats.get('provider', '?')}, daily-bars tier)"


def _check_bid_ask_present(symbol: str = "SPY") -> tuple[bool, str]:
    """
    Verify secondary provider returns valid bid/ask.
    Providers that only return bar data (no real-time quotes) are non-blocking.
    """
    from massive_feed import get_rest_quote
    q = get_rest_quote(symbol)
    if q is None:
        return True, "no_realtime_snapshot (free tier)"
    if not q.get("has_realtime_quotes", True):
        return True, f"bar_data_only ({q.get('provider', '?')}) — bid/ask not in this plan tier"
    bid = q.get("bid", 0)
    ask = q.get("ask", 0)
    if not bid or not ask or bid <= 0 or ask <= 0:
        return False, f"missing_bid_ask: bid={bid} ask={ask}"
    if ask <= bid:
        return False, f"inverted_spread: bid={bid} ask={ask}"
    return True, "ok"


def _check_ws_cache_freshness() -> tuple[bool, str]:
    """Check if WS cache was recently populated (within 5 minutes)."""
    from config import WS_CACHE_FILE
    import json as _json
    if not WS_CACHE_FILE.exists():
        return True, "no_ws_cache"  # not a failure — WS is optional
    try:
        payload  = _json.loads(WS_CACHE_FILE.read_text(encoding="utf-8"))
        saved_at = datetime.fromisoformat(payload.get("saved_at", "2000-01-01"))
        age_secs = (datetime.now(timezone.utc) - saved_at.replace(tzinfo=timezone.utc)).total_seconds()
        if age_secs > 300:
            return True, f"ws_cache_stale ({age_secs/60:.0f}m old, but non-critical)"
        return True, f"ws_cache_fresh ({age_secs:.0f}s)"
    except Exception:
        return True, "ws_cache_unreadable"


def _check_price_spike(prices: list[float]) -> tuple[bool, str]:
    """Detect abnormal consecutive-bar price jumps (bad tick / data error)."""
    if len(prices) < 3:
        return True, "ok"
    for i in range(1, len(prices)):
        prev = prices[i - 1]
        if prev > 0:
            chg = abs(prices[i] - prev) / prev * 100
            if chg > FEED_HEALTH_SPIKE_PCT:
                return False, f"spike: {chg:.1f}% between bars {i-1}→{i}"
    return True, "ok"


# ── Public API ────────────────────────────────────────────────────────────────

def run_health_check() -> tuple[bool, list[str], dict]:
    """
    Run all feed health checks.
    Returns (healthy, issues, status_dict).

    Critical (flips healthy=False, blocks live trading):
      - Alpaca broker unreachable

    Non-critical (reported but trading continues):
      - Stale Alpaca quote
      - Secondary provider unreachable or stale
      - Missing bid/ask
      - WS cache stale
    """
    issues   = []
    critical = False
    poly_msg = "not_checked"
    age      = 0.0

    # 1. Alpaca broker connectivity (critical)
    alpaca_ok, alpaca_msg = _check_alpaca_connectivity()
    if not alpaca_ok:
        issues.append(alpaca_msg)
        critical = True
        log_health_event("ALPACA_OUTAGE", {"reason": alpaca_msg})

    # 2. Alpaca quote freshness (warning)
    fresh_ok, age = _check_quote_freshness()
    if not fresh_ok:
        issues.append(f"stale_quote: SPY {age:.0f}s old")
        log_health_event("STALE_QUOTE", {"symbol": "SPY", "age_secs": age})

    # 3. Secondary provider REST (non-critical)
    sec_ok, poly_msg = _check_secondary_provider()
    if not sec_ok:
        issues.append(poly_msg)
        log_health_event("SECONDARY_OUTAGE", {"reason": poly_msg})

    # 4. Bid/ask presence on secondary provider (non-critical)
    ba_ok, ba_msg = _check_bid_ask_present()
    if not ba_ok:
        issues.append(ba_msg)
        log_health_event("MISSING_BID_ASK", {"detail": ba_msg})

    # 5. WebSocket cache freshness (informational)
    _, ws_msg = _check_ws_cache_freshness()

    healthy = not critical

    status = {
        "last_check":         _now_iso(),
        "status":             "ok" if not issues else ("degraded" if healthy else "critical"),
        "block_live_trading": critical,
        "alpaca":             "ok" if alpaca_ok else alpaca_msg,
        "secondary":          poly_msg,
        "ws_cache":           ws_msg,
        "quote_age_secs":     age,
        "issues":             issues,
    }

    _save_state(status)
    log_health_event("HEALTH_CHECK", {
        "status":  status["status"],
        "issues":  issues,
    })
    return healthy, issues, status


def get_last_status() -> dict:
    return _load_state()


def should_block_live_trading() -> bool:
    """Read persisted health state; True = Alpaca broker was unreachable last check."""
    return _load_state().get("block_live_trading", False)
