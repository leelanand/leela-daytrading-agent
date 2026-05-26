"""
Dynamic position sizing — scales trade size based on conviction and market conditions.
Never overrides MAX_POSITIONS or DAILY_LOSS_LIMIT; only adjusts per-trade allocation.
"""
from config import (
    POSITION_SIZE_PCT, MIN_POSITION_SIZE_PCT, MAX_POSITION_SIZE_PCT,
    SCORE_SIZE_BOOST_THRESHOLD, HIGH_VOL_THRESHOLD, MIN_SCORE_TO_TRADE,
)


def dynamic_position_size(
    portfolio_value: float,
    price: float,
    score: int,
    volatility_pct: float,
    spread_pct: float,
    regime: str = "TRENDING_UP",
    recent_win_rate: float = 0.50,
) -> tuple[int, float, str]:
    """
    Returns (shares, size_pct_applied, sizing_rationale).

    Base = POSITION_SIZE_PCT (20%). Modifiers stack multiplicatively.
    Hard bounds: [MIN_POSITION_SIZE_PCT, MAX_POSITION_SIZE_PCT].
    """
    notes = []

    # ── Score modifier ────────────────────────────────────────────────────────
    if score >= SCORE_SIZE_BOOST_THRESHOLD:
        score_m = 1.25
        notes.append(f"score {score} +25%")
    elif score >= MIN_SCORE_TO_TRADE:
        score_m = 1.0
    else:
        score_m = 0.75
        notes.append(f"score {score} −25%")

    # ── Volatility modifier ───────────────────────────────────────────────────
    if volatility_pct > HIGH_VOL_THRESHOLD:      # > 3.0%
        vol_m = 0.70
        notes.append(f"vol {volatility_pct:.1f}% −30%")
    elif volatility_pct > 2.0:
        vol_m = 0.85
        notes.append(f"vol {volatility_pct:.1f}% −15%")
    else:
        vol_m = 1.0

    # ── Spread modifier ───────────────────────────────────────────────────────
    if spread_pct > 0.15:
        spread_m = 0.85
        notes.append(f"spread {spread_pct:.2f}% −15%")
    else:
        spread_m = 1.0

    # ── Regime modifier ───────────────────────────────────────────────────────
    regime_cuts = {"HIGH_VOL": 0.75, "CHOPPY": 0.85, "TRENDING_DOWN": 0.50, "LOW_VOLUME": 0.80}
    regime_m    = regime_cuts.get(regime, 1.0)
    if regime_m < 1.0:
        notes.append(f"{regime} −{int((1 - regime_m) * 100)}%")

    # ── Recent performance modifier ───────────────────────────────────────────
    if recent_win_rate < 0.40:
        perf_m = 0.80
        notes.append(f"WR {recent_win_rate:.0%} −20%")
    elif recent_win_rate > 0.65:
        perf_m = 1.10
        notes.append(f"WR {recent_win_rate:.0%} +10%")
    else:
        perf_m = 1.0

    combined  = score_m * vol_m * spread_m * regime_m * perf_m
    final_pct = max(MIN_POSITION_SIZE_PCT, min(MAX_POSITION_SIZE_PCT, POSITION_SIZE_PCT * combined))
    shares    = max(1, int(portfolio_value * final_pct / price))

    note      = " | ".join(notes) if notes else "base"
    rationale = f"size={final_pct:.0%} [{note}]"
    return shares, final_pct, rationale
