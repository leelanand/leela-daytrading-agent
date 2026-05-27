"""Position sizing, daily risk limits, per-candidate safety checks, and correlation gates."""
import yfinance as yf
from alpaca.trading.client import TradingClient
from config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, PAPER_TRADING,
    MAX_POSITIONS, POSITION_SIZE_PCT, DAILY_LOSS_LIMIT,
    MAX_TRADES_PER_DAY, MAX_SECTOR_EXPOSURE, MAX_SPREAD_PCT,
    MIN_VOLUME_DAILY, GAP_TOLERANCE_PCT, KILL_SWITCH,
    THEME_MAP, MAX_THEME_POSITIONS,
    STOP_LOSS_PCT, TIGHT_STOP_PCT, TIGHT_STOP_REGIMES,
    MAX_MOVE_BEFORE_ENTRY_PCT,
    TIER_NORMAL_MIN, TIER_HIGH_MIN, TIER_ELITE_MIN,
    ATR_PERIOD, VOLATILITY_EXTENSION_ATR_MULT, VOLATILITY_EXTENSION_ENABLED,
    ATR_STOP_ENABLED, ATR_STOP_MULTIPLIER, ATR_MIN_STOP_PCT, ATR_MAX_STOP_PCT,
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

    # From-open move check — catches extended intraday moves since prescan
    open_price = candidate.get("open_price")
    if open_price and open_price > 0:
        move_from_open = (price - open_price) / open_price * 100
        if move_from_open > MAX_MOVE_BEFORE_ENTRY_PCT:
            return False, (f"Already moved {move_from_open:.1f}% from open at execution time "
                           f"(max {MAX_MOVE_BEFORE_ENTRY_PCT}%)")

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

    # Theme/correlation check — avoid multiple correlated positions
    held   = open_symbols()
    themes = [theme for theme, members in THEME_MAP.items() if symbol in members]
    for theme in themes:
        members_held = [s for s in held if s in THEME_MAP[theme] and s != symbol]
        if len(members_held) >= MAX_THEME_POSITIONS:
            return False, (f"Theme concentration: already hold {members_held} "
                           f"in {theme!r} group (max {MAX_THEME_POSITIONS})")

    return True, "ok"


def suggested_stop_pct(momentum_strength: str = "", regime: str = "") -> float:
    """Return tighter stop when momentum is WEAKENING or regime is HIGH_VOL."""
    if momentum_strength == "WEAKENING" or regime in TIGHT_STOP_REGIMES:
        return TIGHT_STOP_PCT
    return STOP_LOSS_PCT


def position_size(portfolio_value: float, price: float) -> int:
    shares = int(portfolio_value * POSITION_SIZE_PCT / price)
    return max(shares, 1)


def open_symbols() -> set[str]:
    return {p.symbol for p in _client().get_all_positions()}


# ── New quality-enhancement functions (spec points 5, 6, 7, 8) ────────────────

def get_setup_tier(score: int) -> str:
    """Returns 'ELITE', 'HIGH', 'NORMAL', or 'BELOW_THRESHOLD'."""
    if score >= TIER_ELITE_MIN:
        return "ELITE"
    elif score >= TIER_HIGH_MIN:
        return "HIGH"
    elif score >= TIER_NORMAL_MIN:
        return "NORMAL"
    return "BELOW_THRESHOLD"


def _calc_atr(bars: list[dict], period: int) -> float:
    """Simplified ATR using bar ranges (high - low) for intraday bars."""
    if not bars or len(bars) < 2:
        return 0.0
    ranges = [b["high"] - b["low"] for b in bars[-period:] if b["high"] > b["low"]]
    return sum(ranges) / len(ranges) if ranges else 0.0


def check_volatility_extension(
    price: float, vwap: float, bars: list[dict], atr_period: int = ATR_PERIOD
) -> tuple[bool, str]:
    """
    Returns (overextended: bool, reason: str).

    Primary gate: price > VOLATILITY_EXTENSION_ATR_MULT * ATR above VWAP.
    Fallback: uses existing MAX_MOVE_BEFORE_ENTRY_PCT if bars insufficient.

    Does NOT replace the existing move-from-open check in check_candidate_risk();
    this is an additional dynamic gate layered on top.
    """
    if not VOLATILITY_EXTENSION_ENABLED:
        return False, "volatility extension check disabled"

    if not bars or len(bars) < 5:
        # Fall back to fixed check using VWAP as reference
        if vwap > 0:
            move_from_vwap = (price - vwap) / vwap * 100
            if move_from_vwap > MAX_MOVE_BEFORE_ENTRY_PCT:
                return True, (f"price {move_from_vwap:.1f}% above VWAP "
                              f"(fallback, insufficient bars for ATR)")
        return False, "insufficient bars for ATR, fallback check passed"

    atr = _calc_atr(bars, atr_period)
    if atr <= 0:
        return False, "ATR calculation returned zero"

    if vwap <= 0:
        return False, "VWAP unavailable"

    threshold = vwap + (VOLATILITY_EXTENSION_ATR_MULT * atr)
    if price > threshold:
        ext_atr = (price - vwap) / atr
        return True, (f"price ${price:.2f} is {ext_atr:.1f}x ATR above VWAP ${vwap:.2f} "
                      f"(ATR={atr:.4f}, threshold=${threshold:.2f})")

    return False, f"price within {VOLATILITY_EXTENSION_ATR_MULT:.1f}x ATR of VWAP (ATR={atr:.4f})"


def atr_aware_stop_pct(bars: list[dict], entry_price: float) -> float:
    """
    Returns stop distance as a fraction of entry price.
    Clamped between ATR_MIN_STOP_PCT and ATR_MAX_STOP_PCT.
    Falls back to STOP_LOSS_PCT if bars insufficient or ATR_STOP_ENABLED is False.
    """
    if not ATR_STOP_ENABLED or not bars or len(bars) < 5:
        return STOP_LOSS_PCT

    atr = _calc_atr(bars, ATR_PERIOD)
    if atr <= 0 or entry_price <= 0:
        return STOP_LOSS_PCT

    raw_stop_pct = (atr * ATR_STOP_MULTIPLIER) / entry_price
    clamped      = max(ATR_MIN_STOP_PCT, min(ATR_MAX_STOP_PCT, raw_stop_pct))
    return round(clamped, 5)


def detect_failed_breakout(bars: list[dict], breakout_price: float) -> tuple[bool, str]:
    """
    Given recent 1-min bars and the breakout price level, detect failure patterns.
    Returns (failed: bool, reason: str).

    Failure patterns detected:
    - Upper wick > 60% of bar range on last bar (rejection candle)
    - Price closed below breakout level after breaching it
    - Volume on last 2 bars < 50% of average (fading volume)
    - Price immediately reversed > 1% from bar high
    """
    if not bars or len(bars) < 3:
        return False, "insufficient bars for breakout detection"

    try:
        avg_volume = sum(b["volume"] for b in bars) / len(bars) if bars else 1
        last       = bars[-1]
        prev       = bars[-2]

        # Check 1: rejection candle — large upper wick on last bar
        bar_range = last["high"] - last["low"]
        if bar_range > 0:
            upper_wick = last["high"] - last["close"]
            wick_ratio = upper_wick / bar_range
            if wick_ratio > 0.60:
                return True, (f"rejection candle: upper wick {wick_ratio:.0%} of range "
                              f"(close=${last['close']:.2f}, high=${last['high']:.2f})")

        # Check 2: closed below breakout level after breaching it
        if last["high"] >= breakout_price and last["close"] < breakout_price:
            return True, (f"closed below breakout ${breakout_price:.2f} after breach "
                          f"(close=${last['close']:.2f})")

        # Check 3: fading volume on last 2 bars
        last2_vol = (last["volume"] + prev["volume"]) / 2
        if last2_vol < avg_volume * 0.50:
            return True, (f"volume fading: last 2 bars avg {last2_vol:.0f} "
                          f"< 50% of session avg {avg_volume:.0f}")

        # Check 4: price reversed > 1% from bar high
        if last["high"] > 0:
            reversal = (last["high"] - last["close"]) / last["high"] * 100
            if reversal > 1.0:
                return True, (f"reversed {reversal:.2f}% from bar high "
                              f"(high=${last['high']:.2f}, close=${last['close']:.2f})")

        return False, "no breakout failure pattern detected"

    except Exception as e:
        return False, f"breakout detection error: {e}"
