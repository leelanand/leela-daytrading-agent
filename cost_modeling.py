"""
Realistic cost modeling: spread, slippage, and ATR-based stops.

Assumptions:
- Entry: slippage upward (buyer's price impact)
- Exit on TP: slippage downward (seller's liquidity cost)
- Exit on SL: worst case (panic sell into bid)
- Spread: increases with volatility and decreases with liquidity (modeled via 20-day vol)
"""


def estimate_spread_pct(price: float, daily_volume: int, volatility_20d: float) -> float:
    """
    Estimate bid/ask spread as % of price.

    Model: base spread (0.02%) + vol factor (0.01% per 1% daily vol) + liquidity factor (inverse volume)
    Typical ranges: liquid (0.01–0.05%), illiquid (0.10–0.30%).
    """
    base_spread = 0.0002  # 0.02%
    vol_factor = volatility_20d * 0.0001  # 0.01% per 1% vol
    liquidity_factor = 5_000_000 / max(daily_volume, 100_000)  # inv. volume

    spread = base_spread + vol_factor + (liquidity_factor * 0.0001)
    return min(spread, 0.003)  # cap at 0.30%


def estimate_slippage_pct(side: str, volatility_1d: float, volume_ratio: float) -> float:
    """
    Estimate execution slippage (market impact + timing risk).

    - BUY: positive (pay more), scaled by volatility and inverse volume
    - SELL: negative (receive less), worse on low volume
    """
    base_slippage = 0.0001 if volume_ratio >= 1.0 else 0.0002
    vol_factor = volatility_1d * 0.0001  # 0.01% per 1% intraday vol
    volume_penalty = max(0, (2.0 - volume_ratio) * 0.0001)  # penalize low-volume bars

    slippage = base_slippage + vol_factor + volume_penalty

    if side == "BUY":
        return min(slippage, 0.005)  # cap at 0.5%
    else:  # SELL
        return -min(slippage, 0.005)  # negative = unfavorable


def atr_based_stop(atr: float, entry_price: float, atr_multiplier: float = 1.5) -> float:
    """
    Calculate ATR-based stop loss price.

    Stop = entry - (ATR * multiplier).
    This adapts stop distance to volatility while keeping account risk fixed.
    """
    stop_distance = atr * atr_multiplier
    return round(entry_price - stop_distance, 2)


def fixed_account_risk_position_size(
    portfolio_equity: float,
    entry_price: float,
    stop_price: float,
    risk_pct: float = 0.01,  # 1% portfolio risk per trade
) -> int:
    """
    Calculate position size to achieve fixed account risk % per trade.

    Position Size = (Portfolio * risk_pct) / (Entry - Stop) in dollars
    """
    risk_dollars = portfolio_equity * risk_pct
    risk_per_share = entry_price - stop_price

    if risk_per_share <= 0:
        return 0

    shares = int(risk_dollars / risk_per_share)
    return max(shares, 1)


def realized_cost_total(
    entry_price: float,
    entry_qty: int,
    exit_price: float,
    entry_spread_pct: float,
    entry_slippage_pct: float,
    exit_spread_pct: float,
    exit_slippage_pct: float,
) -> float:
    """
    Total cost of round-trip trade (spread + slippage on both sides).

    Returns cost in dollars.
    """
    entry_cost = (
        (entry_spread_pct / 2 * entry_price) +
        (entry_slippage_pct * entry_price)
    ) * entry_qty

    exit_cost = (
        (exit_spread_pct / 2 * exit_price) +
        (abs(exit_slippage_pct) * exit_price)  # slippage is already signed
    ) * entry_qty

    return entry_cost + exit_cost


def effective_entry_price(
    quote_price: float,
    spread_pct: float,
    slippage_pct: float,
    side: str = "BUY",
) -> float:
    """
    Effective entry price after spread and slippage.

    For BUY: pay more (slippage_pct is positive).
    For SELL: receive less (slippage_pct is negative).
    """
    if side == "BUY":
        return quote_price * (1 + slippage_pct) * (1 + spread_pct / 2)
    else:  # SELL
        return quote_price * (1 + slippage_pct) * (1 - spread_pct / 2)


def effective_exit_price(
    quote_price: float,
    spread_pct: float,
    slippage_pct: float,
) -> float:
    """
    Effective exit price (always SELL side).

    Slippage is negative (unfavorable), spread is cost.
    """
    return quote_price * (1 + slippage_pct) * (1 - spread_pct / 2)
