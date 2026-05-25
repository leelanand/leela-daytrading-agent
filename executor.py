"""Place and close orders on Alpaca."""
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, STOP_LOSS_PCT, TAKE_PROFIT_PCT


def _client() -> TradingClient:
    return TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)


def place_bracket_order(symbol: str, shares: int, price: float):
    stop  = round(price * (1 - STOP_LOSS_PCT), 2)
    limit = round(price * (1 + TAKE_PROFIT_PCT), 2)

    req = MarketOrderRequest(
        symbol=symbol,
        qty=shares,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        order_class=OrderClass.BRACKET,
        take_profit=TakeProfitRequest(limit_price=limit),
        stop_loss=StopLossRequest(stop_price=stop),
    )
    order = _client().submit_order(req)
    print(f"   [ORDER] {symbol} x{shares} @ ~${price:.2f} | stop ${stop} | target ${limit}")
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
