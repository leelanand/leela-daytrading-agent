"""
Index Reconstitution Scanner

Identifies stocks being added/removed from major indices (Russell, S&P 500, etc.)
Mechanism: Forced flows — index funds must buy additions, sell deletions regardless of price.
Counterparty: Price-insensitive index funds and passive investors (structural mandate).

Expected edge: +1–3% for additions (forced buying), -1–2% for deletions (forced selling).
Window: Announcement to effective date (~1–3 weeks).
"""
from datetime import datetime, timedelta
from pathlib import Path


def load_known_reconstitution_events() -> list[dict]:
    """
    Returns known/upcoming index reconstitution events.
    In production, would subscribe to real-time reconstitution data from:
    - Russell (May 31 annual rebalance, announced ~April 30)
    - S&P Dow Jones (real-time announcements)
    - Nasdaq (real-time announcements)

    For now, returns template for integration.
    """
    today = datetime.now()

    # Template: actual data would come from index provider APIs
    return [
        {
            "symbol": "EXAMPLE_ADD",
            "mechanism": "reconstitution",
            "counterparty": "forced_index_buyers",
            "mechanism_confidence": 0.95,
            "mechanism_precondition": "reconstitution_window_active",
            "index": "Russell 1000",
            "event_type": "addition",
            "announcement_date": "2026-04-30",
            "effective_date": "2026-05-31",
            "days_to_effective": 30,
            "expected_flow_pct": 2.5,
            "expected_drift_pct": 2.0,
        },
        # More events would be populated by live index data
    ]


def scan_recon_candidates() -> list[dict]:
    """
    Scan for active reconstitution windows.

    Returns candidates in active rebalance window (announced but not yet effective).
    """
    today = datetime.now()
    candidates = []

    for event in load_known_reconstitution_events():
        effective_date = datetime.strptime(event["effective_date"], "%Y-%m-%d")

        # Only include if event is within the window: announced and not yet effective
        if today <= effective_date and (effective_date - today).days <= 30:
            candidates.append(event)

    return candidates


def get_russell_rebalance_window() -> dict:
    """
    Returns current Russell rebalance window dates (May 31 annually).
    Announcement: ~April 30, Effective: May 31.
    """
    today = datetime.now()
    year = today.year

    announcement_date = datetime(year, 4, 30)
    effective_date = datetime(year, 5, 31)

    # If we've passed this year's dates, next year's is the window
    if today > effective_date:
        announcement_date = datetime(year + 1, 4, 30)
        effective_date = datetime(year + 1, 5, 31)

    return {
        "announcement_date": announcement_date,
        "effective_date": effective_date,
        "window_active": announcement_date <= today <= effective_date,
        "days_to_effective": max(0, (effective_date - today).days),
    }


if __name__ == "__main__":
    candidates = scan_recon_candidates()
    print(f"Found {len(candidates)} reconstitution candidates:")
    for c in candidates:
        print(f"  {c['symbol']}: {c['event_type']} to {c['index']}, "
              f"effective {c['effective_date']} ({c['days_to_effective']} days)")

    russell_window = get_russell_rebalance_window()
    print(f"\nRussell rebalance window: active={russell_window['window_active']}, "
          f"effective={russell_window['effective_date']}")
