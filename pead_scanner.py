"""
Post-Earnings Announcement Drift Scanner (PEAD)

Identifies stocks with recent earnings surprises that may continue drifting.
Mechanism: Behavioral underreaction — market slow to update on public information.
Counterparty: Retail/passive investors slow to process earnings surprises.

FACT-CHECKLIST (mechanism_confidence is binary 1.0 or 0.0 based on observable preconditions):
  1. Earnings surprise >= PEAD_MIN_SURPRISE_PCT (5.0%)
  2. Days since earnings <= PEAD_MAX_DAYS_SINCE_REPORT (5 days)
  3. Next earnings date > exit_date (don't hold into next catalyst)

All three must be true for mechanism_confidence = 1.0. Otherwise 0.0 (gate rejects).
Expected drift window: 3–5 trading days from report date.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
import requests
from config import FINNHUB_API_KEY

PEAD_MIN_SURPRISE_PCT = 5.0  # Only surprises >= 5% are material
PEAD_MAX_DAYS_SINCE_REPORT = 5  # Only recent reports (< 5 days old)
PEAD_EXPECTED_DRIFT_DAYS = 5  # Hold for up to 5 days post-earnings
PEAD_MIN_DRIFT_PER_SURPRISE = 0.2  # Conservative: 20% of surprise is drift (old: 30%)


def calculate_surprise_magnitude(actual_eps: float, estimate_eps: float) -> float:
    """
    Calculate earnings surprise as % of estimate.
    Positive = beat, negative = miss.
    """
    if estimate_eps == 0:
        return 0.0
    return ((actual_eps - estimate_eps) / abs(estimate_eps)) * 100


def check_pead_preconditions(
    symbol: str,
    surprise_pct: float,
    days_since_earnings: int,
    next_earnings_date: datetime = None,
) -> dict:
    """
    Fact-checklist for PEAD mechanism.

    Returns: {
        "fact_1_surprise_gt_min": bool,
        "fact_2_days_since_lt_max": bool,
        "fact_3_no_catalyst_in_window": bool,
        "all_facts_met": bool (mechanism_confidence will be 1.0 or 0.0),
    }
    """
    now = datetime.now()
    exit_date = now + timedelta(days=PEAD_EXPECTED_DRIFT_DAYS)

    fact_1 = abs(surprise_pct) >= PEAD_MIN_SURPRISE_PCT
    fact_2 = days_since_earnings <= PEAD_MAX_DAYS_SINCE_REPORT
    fact_3 = (
        next_earnings_date is None or
        next_earnings_date > exit_date
    )

    return {
        "fact_1_surprise_gte_5pct": fact_1,
        "fact_2_days_since_earnings_lte_5": fact_2,
        "fact_3_no_catalyst_in_drift_window": fact_3,
        "all_facts_met": fact_1 and fact_2 and fact_3,
    }


def scan_pead_candidates(lookback_days: int = 5) -> list[dict]:
    """
    Find stocks that reported earnings in the last N days with meaningful surprises.

    Returns list of {symbol, mechanism metadata, precondition facts, holding info}
    FACT-CHECKLIST SCORING: mechanism_confidence = 1.0 only if all preconditions met, else 0.0
    """
    candidates = []

    try:
        resp = requests.get(
            "https://finnhub.io/api/v1/calendar/earnings",
            params={
                "token": FINNHUB_API_KEY,
                "from": (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d"),
                "to": datetime.now().strftime("%Y-%m-%d"),
            },
            timeout=10
        )

        if resp.status_code != 200:
            return []

        earnings = resp.json().get("earnings", [])
        today = datetime.now()

        for report in earnings:
            symbol = report.get("symbol", "").upper()
            earnings_date_str = report.get("date", "")
            actual_eps = float(report.get("epsActual", 0) or 0)
            estimate_eps = float(report.get("epsEstimate", 0) or 0)

            if not symbol or not earnings_date_str:
                continue

            try:
                earnings_date = datetime.strptime(earnings_date_str, "%Y-%m-%d")
            except ValueError:
                continue

            days_since = (today - earnings_date).days

            # Only candidates from last lookback_days
            if days_since < 0 or days_since > lookback_days:
                continue

            surprise_pct = calculate_surprise_magnitude(actual_eps, estimate_eps)

            # Check preconditions (fact-checklist)
            # next_earnings_date would be fetched from Finnhub earnings calendar (TODO)
            preconditions = check_pead_preconditions(
                symbol=symbol,
                surprise_pct=surprise_pct,
                days_since_earnings=days_since,
                next_earnings_date=None,  # TODO: fetch from Finnhub
            )

            # mechanism_confidence is BINARY: 1.0 if all facts met, 0.0 otherwise
            mechanism_confidence = 1.0 if preconditions["all_facts_met"] else 0.0

            # Only include if confidence is 1.0 (all preconditions met)
            if mechanism_confidence < 1.0:
                continue

            # Estimate drift magnitude: conservative 20% of surprise magnitude
            expected_drift_pct = abs(surprise_pct) * PEAD_MIN_DRIFT_PER_SURPRISE

            candidates.append({
                "symbol": symbol,
                "mechanism": "pead",
                "counterparty": "slow_information_processors",
                "mechanism_confidence": mechanism_confidence,  # 1.0 (all facts met) or 0.0 (skip)
                "mechanism_precondition": "post_earnings_unannounced",
                "mechanism_precondition_facts": preconditions,  # Full checklist for audit
                "earnings_date": earnings_date_str,
                "surprise_pct": round(surprise_pct, 2),
                "days_since_earnings": days_since,
                "expected_drift_pct": round(expected_drift_pct, 2),
                "holding_period_days": PEAD_EXPECTED_DRIFT_DAYS,  # Swing path: hold for N days
                "holding_rationale": f"PEAD drift window post-earnings, {surprise_pct:+.1f}% surprise",
            })

        return candidates

    except Exception as e:
        print(f"[PEAD] Error fetching earnings data: {e}")
        return []


if __name__ == "__main__":
    results = scan_pead_candidates(lookback_days=5)
    print(f"Found {len(results)} PEAD candidates (fact-checklist met):")
    for c in results:
        print(f"  {c['symbol']}: {c['surprise_pct']:+.1f}% surprise, "
              f"{c['expected_drift_pct']:.1f}% drift expected, "
              f"hold {c['holding_period_days']} days")
