"""Position sizing and daily risk limits."""
from alpaca.trading.client import TradingClient
from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, MAX_POSITIONS, POSITION_SIZE_PCT, DAILY_LOSS_LIMIT


def _client() -> TradingClient:
    return TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)


def can_trade() -> tuple[bool, float]:
    """Returns (ok_to_trade, portfolio_value)."""
    client   = _client()
    acct     = client.get_account()
    portfolio = float(acct.portfolio_value)
    start    = float(acct.last_equity)

    daily_pnl_pct = (portfolio - start) / start
    if daily_pnl_pct < -DAILY_LOSS_LIMIT:
        print(f"   [RISK] Daily loss limit hit ({daily_pnl_pct:.1%}). No more trades today.")
        return False, portfolio

    positions = client.get_all_positions()
    if len(positions) >= MAX_POSITIONS:
        print(f"   [RISK] Max positions ({MAX_POSITIONS}) reached.")
        return False, portfolio

    return True, portfolio


def position_size(portfolio_value: float, price: float) -> int:
    shares = int(portfolio_value * POSITION_SIZE_PCT / price)
    return max(shares, 1)


def open_symbols() -> set[str]:
    return {p.symbol for p in _client().get_all_positions()}
