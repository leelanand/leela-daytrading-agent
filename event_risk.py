"""
Event-risk checks: earnings windows, trading halts, and high-impact economic events.

All checks fail gracefully — missing data never blocks trading unilaterally.
Blocking decisions stay with agent.py; this module only surfaces facts.
"""
import yfinance as yf
from datetime import date, datetime, timedelta, timezone
from alpaca.trading.client import TradingClient
from config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, PAPER_TRADING,
    BLOCK_ON_EARNINGS_WITHIN_DAYS, EARNINGS_RISK_WITHIN_DAYS,
)

# ── Known high-impact economic dates for 2026 ──────────────────────────────────
# Update annually. Day of announcement counts as risk day.

_FOMC_DATES_2026 = {
    (1, 28), (3, 18), (4, 29), (6, 10),
    (7, 29), (9, 16), (10, 28), (12, 16),
}
_CPI_DATES_2026 = {
    (1, 15), (2, 11), (3, 11), (4, 10), (5, 13), (6, 11),
    (7, 15), (8, 12), (9, 10), (10, 14), (11, 12), (12, 11),
}
_GDP_DATES_2026 = {
    (1, 29), (3, 26), (4, 29), (6, 25),
    (7, 30), (9, 24), (10, 29), (12, 22),
}


def _is_first_friday() -> bool:
    """True if today is the first Friday of the month (NFP release day)."""
    today = date.today()
    return today.weekday() == 4 and today.day <= 7


# ── Earnings check ─────────────────────────────────────────────────────────────

def check_earnings(symbol: str) -> tuple[bool, str]:
    """
    Returns (block, description).
    block=True  → earnings within BLOCK_ON_EARNINGS_WITHIN_DAYS days.
    block=False → upcoming (warn-only window) or no data.
    """
    try:
        cal = yf.Ticker(symbol).calendar
        if cal is None:
            return False, "no earnings data"

        # yfinance returns dict in newer versions, DataFrame in older
        dates: list = []
        if isinstance(cal, dict):
            raw = cal.get("Earnings Date") or cal.get("Earnings Date Range") or []
            dates = list(raw) if raw is not None else []
        elif hasattr(cal, "columns") and "Earnings Date" in cal.columns:
            dates = cal["Earnings Date"].tolist()

        if not dates:
            return False, "no earnings scheduled"

        today = date.today()
        for d in dates:
            try:
                ed    = d.date() if hasattr(d, "date") else date.fromisoformat(str(d)[:10])
                delta = (ed - today).days
                if 0 <= delta <= BLOCK_ON_EARNINGS_WITHIN_DAYS:
                    return True, f"earnings in {delta}d on {ed}"
                if 0 < delta <= EARNINGS_RISK_WITHIN_DAYS:
                    return False, f"earnings in {delta}d on {ed} (warn)"
            except Exception:
                continue

        return False, "no near-term earnings"
    except Exception:
        return False, "earnings check unavailable"


# ── Halt check ─────────────────────────────────────────────────────────────────

def check_halt(symbol: str) -> tuple[bool, str]:
    """Returns (halted, reason) based on Alpaca asset tradability status."""
    try:
        client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=PAPER_TRADING)
        asset  = client.get_asset(symbol)
        if not asset.tradable:
            return True, f"{symbol} non-tradable on Alpaca"
        return False, "tradable"
    except Exception:
        return False, "halt check unavailable"


# ── Economic calendar ──────────────────────────────────────────────────────────

def get_economic_events_today() -> list[str]:
    """Return list of high-impact economic event names scheduled for today."""
    today  = date.today()
    events = []
    md     = (today.month, today.day)

    if _is_first_friday():
        events.append("NFP Non-Farm Payrolls release")
    if md in _FOMC_DATES_2026:
        events.append("FOMC interest rate decision")
    if md in _CPI_DATES_2026:
        events.append("CPI inflation data release")
    if md in _GDP_DATES_2026:
        events.append("GDP advance estimate release")

    return events


# ── Combined risk summary ─────────────────────────────────────────────────────

def get_risk_summary(symbols: list[str]) -> dict:
    """
    Returns:
    {
        "economic_events": ["NFP release"],
        "earnings_blocks": {"NVDA": "earnings in 0d on 2026-05-28"},   # hard-block
        "earnings_warns":  {"AMD":  "earnings in 2d on 2026-05-28"},   # warn only
        "halts":           {"COIN": "COIN non-tradable on Alpaca"},
    }
    """
    econ = get_economic_events_today()

    earnings_blocks: dict[str, str] = {}
    earnings_warns:  dict[str, str] = {}
    halts:           dict[str, str] = {}

    for sym in symbols:
        block, desc = check_earnings(sym)
        if block:
            earnings_blocks[sym] = desc
        elif "warn" in desc or "d on" in desc:
            earnings_warns[sym] = desc

        halted, hdesc = check_halt(sym)
        if halted:
            halts[sym] = hdesc

    return {
        "economic_events": econ,
        "earnings_blocks": earnings_blocks,
        "earnings_warns":  earnings_warns,
        "halts":           halts,
    }
