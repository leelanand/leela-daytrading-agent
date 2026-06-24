"""
Forced-Selling Windows Scanner

Identifies periods when structural selling pressure exists due to:
1. Tax-loss harvesting season (Nov-Dec)
2. Margin call cascades (high-volatility spikes)
3. Fund redemption windows (quarterly, post-volatility events)

Mechanism: Constraint-driven selling, not value-driven.
Counterparty: Distressed sellers (individuals, leveraged funds) forced by rules/constraints.

Expected edge: -0.5–1.5% underperformance in forced-selling windows, reversible 1–3 weeks after.
"""
from datetime import datetime, timedelta
from config import EXTREME_HIGH_VOL_VIX


def is_tax_loss_harvesting_season() -> dict:
    """
    Tax-loss harvesting is concentrated late Nov–Dec in US.
    Mechanics: Investors sell losers to realize losses before year-end for tax deductions.
    """
    today = datetime.now()

    # Tax-loss harvesting peak: Nov 15 – Dec 31
    season_start = datetime(today.year, 11, 15)
    season_end = datetime(today.year, 12, 31)

    in_season = season_start <= today <= season_end
    days_in_season = (today - season_start).days if in_season else 0
    days_to_season = (season_start - today).days if not in_season else 0

    return {
        "mechanism": "forced_selling",
        "counterparty": "tax_loss_harvesters",
        "constraint_type": "tax_calendar",
        "season_active": in_season,
        "season_start": season_start.strftime("%Y-%m-%d"),
        "season_end": season_end.strftime("%Y-%m-%d"),
        "days_in_season": days_in_season,
        "days_to_season": days_to_season,
        "confidence_multiplier": 0.7,  # Lower than margin cascades
    }


def is_margin_cascade_window(current_vix: float) -> dict:
    """
    High-volatility spikes trigger margin calls, forcing liquidations.
    Mechanics: Leveraged funds and traders get margin calls, forced to sell regardless of price.
    """
    today = datetime.now()

    # Margin cascade window: VIX spike + following 3 trading days
    cascade_active = current_vix > EXTREME_HIGH_VOL_VIX  # e.g., 35+
    cascade_window_days = 3

    return {
        "mechanism": "forced_selling",
        "counterparty": "margin_call_liquidators",
        "constraint_type": "leverage_risk",
        "cascade_active": cascade_active,
        "current_vix": current_vix,
        "cascade_threshold_vix": EXTREME_HIGH_VOL_VIX,
        "window_days": cascade_window_days,
        "confidence_multiplier": 0.85,  # Very high confidence when VIX spikes
    }


def is_fund_redemption_window() -> dict:
    """
    Mutual funds and ETFs have redemption windows (quarterly, post-event).
    Mechanics: Heavy redemptions force fund managers to sell to raise cash, regardless of price.
    """
    today = datetime.now()

    # Redemption windows: End of quarter (Mar 31, Jun 30, Sep 30, Dec 31)
    # Plus 1-2 weeks after major volatility events (not predictable)
    quarter_end_dates = [
        datetime(today.year, 3, 31),
        datetime(today.year, 6, 30),
        datetime(today.year, 9, 30),
        datetime(today.year, 12, 31),
    ]

    # Find closest quarter end
    upcoming_quarter_ends = [d for d in quarter_end_dates if d >= today]
    days_to_quarter_end = (upcoming_quarter_ends[0] - today).days if upcoming_quarter_ends else 999

    in_redemption_window = days_to_quarter_end <= 7

    return {
        "mechanism": "forced_selling",
        "counterparty": "fund_redemption_sellers",
        "constraint_type": "fund_liquidity",
        "redemption_window_active": in_redemption_window,
        "next_quarter_end": upcoming_quarter_ends[0].strftime("%Y-%m-%d") if upcoming_quarter_ends else "N/A",
        "days_to_quarter_end": days_to_quarter_end,
        "confidence_multiplier": 0.6,  # Lower; more dispersed
    }


def scan_forced_seller_candidates() -> list[dict]:
    """
    Scan for active forced-selling windows and return candidates.

    Returns list of {constraint_type, counterparty, mechanism_confidence, ...}
    """
    candidates = []

    # Tax-loss harvesting window
    tax_window = is_tax_loss_harvesting_season()
    if tax_window["season_active"]:
        candidates.append({
            "mechanism": "forced_selling",
            "counterparty": tax_window["counterparty"],
            "mechanism_confidence": tax_window["confidence_multiplier"],
            "mechanism_precondition": "tax_loss_season_active",
            "constraint_type": tax_window["constraint_type"],
            "window_name": "tax_loss_harvesting",
            "days_remaining": (
                (datetime.strptime(tax_window["season_end"], "%Y-%m-%d") - datetime.now()).days
            ),
        })

    # Margin cascade window (would need live VIX feed)
    # For now, skip unless explicitly triggered
    # margin_cascade = is_margin_cascade_window(current_vix=25.0)

    # Redemption window
    redemption = is_fund_redemption_window()
    if redemption["redemption_window_active"]:
        candidates.append({
            "mechanism": "forced_selling",
            "counterparty": redemption["counterparty"],
            "mechanism_confidence": redemption["confidence_multiplier"],
            "mechanism_precondition": "fund_redemption_window_active",
            "constraint_type": redemption["constraint_type"],
            "window_name": "fund_redemption",
            "days_remaining": redemption["days_to_quarter_end"],
        })

    return candidates


if __name__ == "__main__":
    candidates = scan_forced_seller_candidates()
    print(f"Found {len(candidates)} forced-selling candidates:")
    for c in candidates:
        print(f"  {c['window_name']}: {c['constraint_type']}, "
              f"confidence={c['mechanism_confidence']:.2f}, "
              f"days_remaining={c['days_remaining']}")

    print(f"\nTax window: {is_tax_loss_harvesting_season()}")
    print(f"Redemption window: {is_fund_redemption_window()}")
