"""Place and close orders on Alpaca. Logs execution quality for slippage tracking."""
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    LimitOrderRequest, MarketOrderRequest,
    TakeProfitRequest, StopLossRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, PAPER_TRADING,
    STOP_LOSS_PCT, TAKE_PROFIT_PCT, USE_LIMIT_ORDERS, LIMIT_OFFSET_PCT,
)
from logger import log_execution


def _client() -> TradingClient:
    return TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=PAPER_TRADING)


def place_bracket_order(
    symbol: str, shares: int, price: float,
    score: int = 0, size_pct: float = 0.0, sizing_note: str = "",
    stop_pct: float | None = None,
):
    sp     = stop_pct if stop_pct is not None else STOP_LOSS_PCT
    stop   = round(price * (1 - sp), 2)
    target = round(price * (1 + TAKE_PROFIT_PCT), 2)

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
    return order


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
