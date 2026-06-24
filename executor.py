"""Place and close orders on Alpaca. Logs execution quality for slippage tracking."""
import math
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    LimitOrderRequest, MarketOrderRequest,
    TakeProfitRequest, StopLossRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, PAPER_TRADING,
    STOP_LOSS_PCT, TAKE_PROFIT_PCT, USE_LIMIT_ORDERS, LIMIT_OFFSET_PCT,
    MAX_SPREAD_PCT,
)
from logger import log_execution
from trade_journal import log_entry, log_exit
from cost_modeling import estimate_spread_pct, estimate_slippage_pct


def _validate_prices(symbol: str, entry: float, bid: float, ask: float,
                     stop: float, take_profit: float, quantity: int) -> tuple[bool, str]:
    """
    Reject any order where a critical price field is NaN, None, zero,
    negative, or non-finite. Must be called before any broker object is created.
    """
    fields = {
        "entry": entry, "bid": bid, "ask": ask,
        "stop": stop, "take_profit": take_profit, "quantity": quantity,
    }
    for name, val in fields.items():
        if val is None:
            return False, f"price_guard: {name} is None for {symbol}"
        try:
            v = float(val)
        except (TypeError, ValueError):
            return False, f"price_guard: {name}={val!r} not numeric for {symbol}"
        if not math.isfinite(v):
            return False, f"price_guard: {name}={val} is non-finite (NaN/Inf) for {symbol}"
        if v <= 0:
            return False, f"price_guard: {name}={val} is zero or negative for {symbol}"
    spread_pct = (ask - bid) / entry * 100
    if spread_pct > MAX_SPREAD_PCT:
        return False, f"price_guard: spread={spread_pct:.3f}% > MAX_SPREAD_PCT={MAX_SPREAD_PCT} for {symbol}"
    if stop >= entry:
        return False, f"price_guard: stop={stop} >= entry={entry} for {symbol}"
    if take_profit <= entry:
        return False, f"price_guard: take_profit={take_profit} <= entry={entry} for {symbol}"
    return True, "ok"


def _client() -> TradingClient:
    return TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=PAPER_TRADING)


def place_bracket_order(
    symbol: str, shares: int, price: float,
    score: int = 0, size_pct: float = 0.0, sizing_note: str = "",
    stop_pct: float | None = None,
    take_profit_pct: float | None = None,
    claude_score: int | None = None,
    local_score: int | None = None,
    setup_type: str = "UNKNOWN",
    regime: str = "UNKNOWN",
    atr_at_entry: float = 0.0,
    atr_stop_pct: float = 0.0,
    account_risk_pct: float = 0.01,
    daily_volume: int = 1_000_000,
    volatility_20d: float = 0.02,
    volatility_1d: float = 0.015,
    volume_ratio: float = 1.0,
    entry_bid: float | None = None,
    entry_ask: float | None = None,
) -> tuple:
    """
    Place bracket order and log to trade journal with cost modeling.

    Returns: (order, trade_id) for later exit logging.
    """
    sp     = stop_pct        if stop_pct        is not None else STOP_LOSS_PCT
    tp     = take_profit_pct if take_profit_pct is not None else TAKE_PROFIT_PCT
    # Anchor stop/target to the ENTRY price (the limit we'll actually pay), not the raw
    # quote, so the 1.5%/2.5% risk:reward is preserved exactly regardless of the limit
    # offset or slippage. For market orders entry==quote, so this is a no-op. (Fixed 2026-06-04.)
    entry  = round(price * (1 + LIMIT_OFFSET_PCT), 2) if USE_LIMIT_ORDERS else round(price, 2)
    stop   = round(entry * (1 - sp), 2)
    target = round(entry * (1 + tp), 2)

    # Safety guard — must pass before any broker object is created
    ok, reason = _validate_prices(symbol, entry, entry * 0.999, entry * 1.001, stop, target, shares)
    if not ok:
        raise ValueError(reason)

    if USE_LIMIT_ORDERS:
        limit_price = round(price * (1 + LIMIT_OFFSET_PCT), 2)
        req = LimitOrderRequest(
            symbol=symbol,
            qty=shares,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.BRACKET,
            limit_price=limit_price,
            take_profit=TakeProfitRequest(limit_price=target),
            stop_loss=StopLossRequest(stop_price=stop),
        )
        order_type = "limit"
        stop_label = f"-{sp:.1%}"
        print(f"   [ORDER] {symbol} x{shares} limit @${limit_price} | stop ${stop} ({stop_label}) | target ${target}")
    else:
        limit_price = price
        req = MarketOrderRequest(
            symbol=symbol,
            qty=shares,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=target),
            stop_loss=StopLossRequest(stop_price=stop),
        )
        order_type = "market"
        print(f"   [ORDER] {symbol} x{shares} market ~${price:.2f} | stop ${stop} | target ${target}")

    order = _client().submit_order(req)

    log_execution(
        symbol=symbol, intended_price=price, limit_price=limit_price,
        shares=shares, order_type=order_type,
        score=score, size_pct=size_pct, sizing_note=sizing_note,
    )

    # Log to trade journal with realistic costs
    if entry_bid is None:
        entry_bid = price * 0.999  # estimate
    if entry_ask is None:
        entry_ask = price * 1.001  # estimate

    entry_spread_pct = estimate_spread_pct(price, daily_volume, volatility_20d)
    entry_slippage_pct = estimate_slippage_pct("BUY", volatility_1d, volume_ratio)
    intended_r_r = tp / sp if sp > 0 else 0

    trade_id = log_entry(
        symbol=symbol,
        entry_price=price,
        entry_qty=shares,
        entry_bid=entry_bid,
        entry_ask=entry_ask,
        claude_score=claude_score,
        local_score=local_score,
        setup_type=setup_type,
        regime=regime,
        atr_at_entry=atr_at_entry,
        atr_stop_pct=atr_stop_pct,
        account_risk_pct=account_risk_pct,
        stop_price=stop,
        target_price=target,
        intended_r_r=intended_r_r,
        entry_spread_pct=entry_spread_pct,
        entry_slippage_pct=entry_slippage_pct,
    )

    return order, trade_id


def log_trade_exit(
    trade_id: int,
    exit_price: float,
    exit_reason: str,
    mae_pct: float = 0.0,
    mae_price: float = 0.0,
    holding_minutes: int = 0,
    exit_bid: float | None = None,
    exit_ask: float | None = None,
    daily_volume: int = 1_000_000,
    volatility_1d: float = 0.015,
):
    """Log a trade exit with realistic exit costs."""
    if exit_bid is None:
        exit_bid = exit_price * 0.999
    if exit_ask is None:
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


def close_all_positions():
    """Force-close everything — called at 3:45pm ET."""
    client    = _client()
    client.cancel_orders()
    positions = client.get_all_positions()
    if not positions:
        print("   No open positions to close.")
        return
    for p in positions:
        try:
            client.close_position(p.symbol)
            pl = float(p.unrealized_pl)
            print(f"   [CLOSE] {p.symbol} {p.qty} shares | P&L ${pl:+.2f}")
        except Exception as e:
            print(f"   [CLOSE ERROR] {p.symbol}: {e}")
