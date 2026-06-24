"""
Swing-path executor — multi-day holding for structural edges (PEAD, reconstitution, forced-selling).

Entry: mechanism precondition facts (earnings surprise, index addition, constraint window).
Hold: holding_period_days or explicit exit_date from metadata.
Exit: window expiry, profit target, or stop (with overnight gap protection).
Sizing: 0.60× intraday position size (conservative prior on overnight gap risk).

All trades logged to trade_journal with gap events, slippage, and mechanism metadata.
NO EOD force-close; NO intraday timing gates; NO ORB/momentum setup requirements.
Each edge measured in its native time horizon.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, PAPER_TRADING,
    SWING_SIZE_MULTIPLIER, CATALYST_CALENDAR_LOOKBACK_DAYS,
)
from trade_journal import log_entry, log_exit, log_repair
from cost_modeling import estimate_spread_pct, estimate_slippage_pct

ET = ZoneInfo("America/New_York")


def _client() -> TradingClient:
    return TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=PAPER_TRADING)


def check_catalyst_calendar(symbol: str, exit_date: datetime) -> dict:
    """
    Check if symbol has a scheduled catalyst (earnings, binary event) between now and exit_date.

    Returns: {has_catalyst: bool, catalyst_type: str, catalyst_date: datetime, risk: str}
    """
    now = datetime.now(ET)

    # TODO: integrate Finnhub earnings calendar
    # For now, return stub (to be filled by real earnings calendar lookup)
    return {
        "has_catalyst": False,
        "catalyst_type": None,
        "catalyst_date": None,
        "risk": "unknown",  # Will be "safe" or "risky" based on Finnhub data
    }


def validate_swing_entry(candidate: dict, symbol: str, current_price: float) -> tuple[bool, str]:
    """
    Validate swing entry against mechanism preconditions and overnight risk.

    Returns: (ok: bool, reason: str)
    """
    # Mechanism precondition must be met
    mechanism = candidate.get("mechanism", "unknown")
    mechanism_confidence = candidate.get("mechanism_confidence", 0.0)

    if mechanism_confidence < 1.0:  # Fact-checklist must be 100% met
        return False, f"Mechanism precondition not fully met (confidence={mechanism_confidence:.1%})"

    # Check catalyst calendar
    exit_date = candidate.get("exit_date")  # For reconstitution, explicit date
    holding_days = candidate.get("holding_period_days", 5)  # For PEAD, N-day hold

    if not exit_date:
        exit_date = now = datetime.now(ET)
        exit_date += timedelta(days=holding_days)

    catalyst_check = check_catalyst_calendar(symbol, exit_date)

    if catalyst_check["has_catalyst"] and catalyst_check["risk"] == "risky":
        return False, f"Catalyst {catalyst_check['catalyst_type']} on {catalyst_check['catalyst_date']} within holding window"

    return True, "ok"


def place_swing_order(
    symbol: str,
    shares: int,
    entry_price: float,
    candidate: dict,
    daily_volume: int = 1_000_000,
    volatility_20d: float = 0.02,
    volatility_1d: float = 0.015,
    volume_ratio: float = 1.0,
) -> tuple[dict, int]:
    """
    Place a swing (multi-day holding) order.

    Entry: mechanism precondition facts
    Hold: holding_period_days or explicit exit_date
    Exit: window expiry, profit target (2.5%), or stop (1.5%, gap-aware)
    Size: 0.60× intraday (conservative overnight gap prior)

    Returns: (order, trade_id)
    """
    # Validate entry
    ok, reason = validate_swing_entry(candidate, symbol, entry_price)
    if not ok:
        print(f"   [SWING ENTRY REJECTED] {symbol}: {reason}")
        return None, None

    # Determine holding window
    now = datetime.now(ET)
    exit_date = candidate.get("exit_date")
    holding_days = candidate.get("holding_period_days", 5)

    if not exit_date:
        exit_date = now + timedelta(days=holding_days)

    holding_minutes = int((exit_date - now).total_seconds() / 60)

    # Position sizing: 0.60× intraday
    intraday_size = shares  # Input is already intraday-sized
    swing_size = int(intraday_size * SWING_SIZE_MULTIPLIER)

    if swing_size < 1:
        print(f"   [SWING SIZE TOO SMALL] {symbol}: intraday={intraday_size}, swing={swing_size}. Rejected.")
        return None, None

    # Price targets (anchored to entry)
    stop_price = round(entry_price * 0.985, 2)  # -1.5%
    profit_target = round(entry_price * 1.025, 2)  # +2.5%

    # Cost modeling
    entry_bid = entry_price * 0.999
    entry_ask = entry_price * 1.001
    entry_spread = estimate_spread_pct(entry_price, daily_volume, volatility_20d)
    entry_slip = estimate_slippage_pct("BUY", volatility_1d, volume_ratio)

    # Place market order (swing, not limit; we don't care about entry precision on multi-day)
    try:
        req = MarketOrderRequest(
            symbol=symbol,
            qty=swing_size,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,  # Each day, we decide to hold or exit
        )
        order = _client().submit_order(req)

        # Log entry to swing journal
        mechanism = candidate.get("mechanism", "unknown")
        counterparty = candidate.get("counterparty", "unknown")

        trade_id = log_entry(
            symbol=symbol,
            entry_price=entry_price,
            entry_qty=swing_size,
            entry_bid=entry_bid,
            entry_ask=entry_ask,
            claude_score=None,
            local_score=None,
            score_confidence=0.0,  # Swing path doesn't use scores
            setup_type=f"swing_{mechanism}",
            regime="SWING",  # Separate regime for swing trades
            mechanism=mechanism,
            counterparty=counterparty,
            mechanism_confidence=candidate.get("mechanism_confidence", 0.0),
            mechanism_precondition=candidate.get("mechanism_precondition", "none"),
            atr_at_entry=0.0,  # Not applicable to swing
            atr_stop_pct=0.0,
            account_risk_pct=0.01 * SWING_SIZE_MULTIPLIER,  # Scaled down by 0.60
            stop_price=stop_price,
            target_price=profit_target,
            intended_r_r=profit_target / stop_price,
            entry_spread_pct=entry_spread,
            entry_slippage_pct=entry_slip,
            spread_at_prescan_pct=entry_spread,
            volume_at_prescan=daily_volume,
        )

        print(f"   [SWING ORDER] {symbol} x{swing_size} @ ${entry_price:.2f} | "
              f"hold until {exit_date.strftime('%Y-%m-%d')} | "
              f"stop ${stop_price:.2f} / target ${profit_target:.2f}")

        return order, trade_id

    except Exception as e:
        print(f"   [SWING ORDER ERROR] {symbol}: {e}")
        return None, None


def monitor_swing_positions(trade_id: int, symbol: str, entry_price: float, stop_price: float, target_price: float, exit_date: datetime):
    """
    Monitor an open swing position for exits.

    Called daily during market hours; checks:
    1. Exit window expired?
    2. Profit target hit?
    3. Stop loss hit (including gap-aware)?
    4. Catalyst calendar — still safe to hold?
    """
    # TODO: implement live monitoring
    # For now, stub for structure
    pass


def log_overnight_gap(trade_id: int, symbol: str, prev_close: float, open_price: float, stop_price: float):
    """
    Log an overnight gap event for this trade.

    Measures actual gap behavior to replace 0.60 multiplier guess with evidence.
    """
    gap_pct = (open_price - prev_close) / prev_close if prev_close > 0 else 0.0
    gap_through_stop = open_price < stop_price
    slippage_vs_stop = max(0, stop_price - open_price)  # How much stop was missed

    gap_event = {
        "date": datetime.now(ET).strftime("%Y-%m-%d"),
        "close_prev": round(prev_close, 2),
        "open_price": round(open_price, 2),
        "gap_pct": round(gap_pct * 100, 2),
        "stop_price": round(stop_price, 2),
        "gap_through_stop": gap_through_stop,
        "slippage_vs_stop": round(slippage_vs_stop, 2),
    }

    # TODO: update trade_journal with this gap event
    # Append to gap_events JSON array for this trade_id
    pass


if __name__ == "__main__":
    print("Swing executor ready for PEAD testing.")
