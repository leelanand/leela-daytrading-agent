"""
Simplified trade logging integration for backtest harness.

Wraps trade_journal to make it easy to log from agent.py without needing
all cost modeling parameters upfront. Applies sensible defaults and fills
in missing data.
"""
import json
from pathlib import Path
from trade_journal import log_entry, log_exit
from cost_modeling import estimate_spread_pct, estimate_slippage_pct

# In-memory trade context for the current session
_trade_map = {}  # symbol -> trade_id


def register_trade_entry(
    symbol: str,
    entry_price: float,
    entry_qty: int,
    score: int,
    setup_type: str,
    regime: str,
    atr_at_entry: float,
    atr_stop_pct: float,
    stop_price: float,
    target_price: float,
    daily_volume: int = 1_000_000,
    volatility_20d: float = 0.02,
    volatility_1d: float = 0.015,
    volume_ratio: float = 1.0,
) -> int:
    """
    Register a trade entry for logging.

    Returns trade_id for later exit logging.
    Estimates bid/ask spread and slippage automatically.
    """
    entry_bid = entry_price * 0.999
    entry_ask = entry_price * 1.001

    entry_spread_pct = estimate_spread_pct(entry_price, daily_volume, volatility_20d)
    entry_slippage_pct = estimate_slippage_pct("BUY", volatility_1d, volume_ratio)

    intended_r_r = (target_price / stop_price) if stop_price > 0 else 0

    trade_id = log_entry(
        symbol=symbol,
        entry_price=entry_price,
        entry_qty=entry_qty,
        entry_bid=entry_bid,
        entry_ask=entry_ask,
        claude_score=score,
        local_score=score,
        setup_type=setup_type,
        regime=regime,
        atr_at_entry=atr_at_entry,
        atr_stop_pct=atr_stop_pct,
        account_risk_pct=0.01,
        stop_price=stop_price,
        target_price=target_price,
        intended_r_r=intended_r_r,
        entry_spread_pct=entry_spread_pct,
        entry_slippage_pct=entry_slippage_pct,
    )

    _trade_map[symbol] = trade_id
    return trade_id


def register_trade_exit(
    symbol: str,
    exit_price: float,
    exit_reason: str,
    mae_pct: float = 0.0,
    mae_price: float = 0.0,
    holding_minutes: int = 0,
    daily_volume: int = 1_000_000,
    volatility_1d: float = 0.015,
) -> bool:
    """
    Register a trade exit for logging.

    Returns True if exit was logged, False if trade_id not found.
    """
    trade_id = _trade_map.get(symbol)
    if not trade_id:
        return False

    exit_bid = exit_price * 0.999
    exit_ask = exit_price * 1.001

    exit_spread_pct = estimate_spread_pct(exit_price, daily_volume, 0.02)
    exit_slippage_pct = estimate_slippage_pct("SELL", volatility_1d, 1.0)

    log_exit(
        trade_id=trade_id,
        exit_price=exit_price,
        exit_bid=exit_bid,
        exit_ask=exit_ask,
        exit_reason=exit_reason,
        mae_pct=mae_pct,
        mae_price=mae_price,
        holding_minutes=holding_minutes,
        exit_spread_pct=exit_spread_pct,
        exit_slippage_pct=exit_slippage_pct,
    )

    del _trade_map[symbol]
    return True


def get_trade_id(symbol: str) -> int | None:
    """Get trade_id for a symbol if registered."""
    return _trade_map.get(symbol)
