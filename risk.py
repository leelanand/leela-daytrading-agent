"""Position sizing, daily risk limits, and per-candidate safety checks."""
import yfinance as yf
from alpaca.trading.client import TradingClient
from config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, PAPER_TRADING,
    MAX_POSITIONS, POSITION_SIZE_PCT, DAILY_LOSS_LIMIT,
    MAX_TRADES_PER_DAY, MAX_SECTOR_EXPOSURE, MAX_SPREAD_PCT,
    MIN_VOLUME_DAILY, GAP_TOLERANCE_PCT, KILL_SWITCH,
)
from logger import today_audit


def _client() -> TradingClient:
    return TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=PAPER_TRADING)


def can_trade() -> tuple[bool, float, str]:
    """Returns (ok_to_trade, portfolio_value, reason)."""
    if KILL_SWITCH:
        return False, 0.0, "KILL_SWITCH is active — all trading disabled"

    client    = _client()
    acct      = client.get_account()
    portfolio = float(acct.portfolio_value)
    start     = float(acct.last_equity)

    daily_pnl_pct = (portfolio - start) / start
    if daily_pnl_pct < -DAILY_LOSS_LIMIT:
        return False, portfolio, f"Daily loss limit hit ({daily_pnl_pct:.1%})"

    positions = client.get_all_positions()
    if len(positions) >= MAX_POSITIONS:
        return False, portfolio, f"Max positions ({MAX_POSITIONS}) reached"

    trades_today = sum(1 for r in today_audit() if r["action"] == "ORDER_PLACED")
    if trades_today >= MAX_TRADES_PER_DAY:
        return False, portfolio, f"Max trades per day ({MAX_TRADES_PER_DAY}) reached"

    return True, portfolio, "ok"


def check_candidate_risk(candidate: dict, portfolio_value: float, prescan_price: float | None = None) -> tuple[bool, str]:
    """
    Validate a candidate passes all safety checks before placing an order.
    Returns (ok, reason).
    """
    symbol = candidate["symbol"]
    price  = candidate["price"]

    # Spread check
    spread_pct = candidate.get("spread_pct", 0)
    if spread_pct > MAX_SPREAD_PCT:
        return False, f"Spread too wide: {spread_pct:.2f}% > {MAX_SPREAD_PCT}%"

    # Volume check
    today_vol = candidate.get("today_volume", 0)
    if today_vol < MIN_VOLUME_DAILY:
        return False, f"Volume too low: {today_vol:,} < {MIN_VOLUME_DAILY:,}"

    # Gap/price drift from prescan — ensures we're not chasing a moved price
    if prescan_price is not None and prescan_price > 0:
        drift_pct = abs(price - prescan_price) / prescan_price * 100
        if drift_pct > GAP_TOLERANCE_PCT:
            return False, f"Price drifted {drift_pct:.1f}% from prescan (max {GAP_TOLERANCE_PCT}%)"

    # Sector exposure check
    sector = candidate.get("sector", "Unknown")
    if sector and sector != "Unknown" and portfolio_value > 0:
        positions = _client().get_all_positions()
        sector_value = 0.0
        for p in positions:
            try:
                info = yf.Ticker(p.symbol).info
                if info.get("sector") == sector:
                    sector_value += abs(float(p.market_value))
            except Exception:
                pass
        candidate_value = position_size(portfolio_value, price) * price
        if (sector_value + candidate_value) / portfolio_value > MAX_SECTOR_EXPOSURE:
            return False, f"Sector {sector!r} would exceed {MAX_SECTOR_EXPOSURE:.0%} exposure limit"

    return True, "ok"


def position_size(portfolio_value: float, price: float) -> int:
    shares = int(portfolio_value * POSITION_SIZE_PCT / price)
    return max(shares, 1)


def open_symbols() -> set[str]:
    return {p.symbol for p in _client().get_all_positions()}
