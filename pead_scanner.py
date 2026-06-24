"""
Post-Earnings Announcement Drift Scanner (PEAD)

Identifies stocks with recent earnings surprises that may continue drifting.
Mechanism: Behavioral underreaction — market slow to update on public information.
Counterparty: Retail/passive investors slow to process earnings surprises.

Expected drift: 3–5 trading days, magnitude 1–3% depending on surprise size.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
import requests
from config import FINNHUB_API_KEY


def calculate_surprise_magnitude(actual_eps: float, estimate_eps: float) -> float:
    """
    Calculate earnings surprise as % of estimate.
    Positive = beat, negative = miss.
    """
    if estimate_eps == 0:
        return 0.0
    return ((actual_eps - estimate_eps) / abs(estimate_eps)) * 100


def scan_pead_candidates(lookback_days: int = 5) -> list[dict]:
    """
    Find stocks that reported earnings in the last N days with meaningful surprises.

    Returns list of {symbol, earnings_date, surprise_pct, days_since_earnings, expected_drift_pct}
    """
    candidates = []

    # Get list of recent earnings (would use Finnhub earnings calendar API)
    # For now, return structure for integration
    # In production, would call: https://finnhub.io/api/v1/calendar/earnings

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

            # Only meaningful surprises (>5% beat or miss)
            if abs(surprise_pct) < 5.0:
                continue

            # Estimate drift magnitude: larger surprise = larger expected drift
            expected_drift_pct = abs(surprise_pct) * 0.3  # Drift is ~30% of surprise magnitude

            candidates.append({
                "symbol": symbol,
                "mechanism": "pead",
                "counterparty": "slow_information_processors",
                "mechanism_confidence": min(0.9, abs(surprise_pct) / 100),  # Higher surprise = higher confidence
                "mechanism_precondition": "post_earnings_unannounced",
                "earnings_date": earnings_date_str,
                "surprise_pct": round(surprise_pct, 2),
                "days_since_earnings": days_since,
                "expected_drift_pct": round(expected_drift_pct, 2),
                "expected_hold_days": 3,
            })

        return candidates

    except Exception as e:
        print(f"[PEAD] Error fetching earnings data: {e}")
        return []


if __name__ == "__main__":
    results = scan_pead_candidates(lookback_days=5)
    print(f"Found {len(results)} PEAD candidates:")
    for c in results:
        print(f"  {c['symbol']}: {c['surprise_pct']:+.1f}% surprise, {c['expected_drift_pct']:.1f}% drift expected")
