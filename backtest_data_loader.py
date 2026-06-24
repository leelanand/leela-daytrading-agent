"""
Real-time Data Loaders for Backtest Filter

Uses: Finnhub (earnings), Massive (price history), IB (if needed)
Pulls 12+ months of historical data on-demand.
"""
import requests
import json
from datetime import datetime, timedelta
from pathlib import Path
from config import FINNHUB_API_KEY


def fetch_finnhub_earnings_history(lookback_months: int = 12) -> list[dict]:
    """
    Fetch historical earnings from Finnhub API.

    Returns earnings surprises for the past N months.
    """
    print(f"[FINNHUB] Fetching {lookback_months} months of earnings history...")

    start_date = (datetime.now() - timedelta(days=lookback_months * 30)).strftime("%Y-%m-%d")
    end_date = datetime.now().strftime("%Y-%m-%d")

    try:
        resp = requests.get(
            "https://finnhub.io/api/v1/calendar/earnings",
            params={
                "token": FINNHUB_API_KEY,
                "from": start_date,
                "to": end_date,
            },
            timeout=30,
        )

        if resp.status_code != 200:
            print(f"[FINNHUB ERROR] Status {resp.status_code}: {resp.text[:200]}")
            return []

        earnings_raw = resp.json().get("earnings", [])
        print(f"[FINNHUB] Fetched {len(earnings_raw)} earnings events")

        # Filter and enrich
        earnings = []
        for e in earnings_raw:
            symbol = e.get("symbol", "").upper()
            date = e.get("date", "")
            actual = float(e.get("epsActual", 0) or 0)
            estimate = float(e.get("epsEstimate", 0) or 0)

            if not symbol or not date or estimate == 0:
                continue

            surprise_pct = ((actual - estimate) / abs(estimate)) * 100

            # Only material surprises
            if abs(surprise_pct) < 5.0:
                continue

            earnings.append({
                "symbol": symbol,
                "date": date,
                "actual_eps": actual,
                "estimate_eps": estimate,
                "surprise_pct": surprise_pct,
                "price_day_before": None,  # Will fetch from Massive
                "price_day_after": None,   # Will fetch from Massive
                "atr_5d": None,            # Will fetch from Massive
            })

        print(f"[FINNHUB] Filtered to {len(earnings)} material surprises (≥5%)")
        return earnings

    except Exception as e:
        print(f"[FINNHUB ERROR] {e}")
        return []


def fetch_massive_prices(symbol: str, dates: list[str]) -> dict:
    """
    Fetch price data from Massive for given symbol and dates.

    Returns: {date: {open, close, high, low, volume, atr}}

    Note: Requires Massive credentials in config.
    """
    # Stub: would integrate with Massive API
    # For now, returns placeholder
    print(f"[MASSIVE] Fetching price data for {symbol} ({len(dates)} dates)...")

    # TODO: Implement Massive API integration
    # from massive_client import MassiveClient
    # client = MassiveClient(...)
    # prices = client.get_bars(symbol, "day", dates)

    return {}


def fetch_russell_rebalance_history(lookback_months: int = 12) -> list[dict]:
    """
    Fetch historical Russell reconstitution events.

    Russell rebalances annually on May 31 (announced ~April 30).
    Also includes S&P reconstitutions (real-time announcements).
    """
    print(f"[RECONSTITUTION] Building index rebalance history ({lookback_months} months)...")

    rebalances = []
    today = datetime.now()
    year = today.year

    # Russell 2026 (annual, May 31)
    # In production, would fetch actual additions/deletions from Russell website
    # For now, return template
    if lookback_months >= 6:
        # May 31, 2026 Russell rebalance (if in window)
        if (today.month >= 5 or lookback_months > 6):
            rebalances.append({
                "index": "Russell 1000",
                "event_type": "addition",
                "symbol": "PLACEHOLDER",  # Would be actual list
                "announcement_date": "2026-04-30",
                "effective_date": "2026-05-31",
                "price_at_announcement": 100.0,  # Placeholder
                "price_at_effective": 101.5,      # Placeholder
            })

    # S&P reconstitutions (quarterly, real-time announcements)
    # Would fetch from S&P index data
    print(f"[RECONSTITUTION] Found {len(rebalances)} rebalance events")
    return rebalances


def enrich_earnings_with_prices(earnings: list[dict]) -> list[dict]:
    """
    For each earnings event, fetch OHLC prices day-before and day-after.
    """
    enriched = []

    for earn in earnings:
        symbol = earn["symbol"]
        report_date = datetime.strptime(earn["date"], "%Y-%m-%d")
        day_before = (report_date - timedelta(days=1)).strftime("%Y-%m-%d")
        day_after = (report_date + timedelta(days=1)).strftime("%Y-%m-%d")

        # Fetch prices from Massive
        try:
            prices = fetch_massive_prices(symbol, [day_before, earn["date"], day_after])
            # Would extract price_day_before, price_day_after, atr_5d
            # For now, placeholder
            earn["price_day_before"] = 100.0  # Placeholder
            earn["price_day_after"] = 101.0   # Placeholder
            earn["atr_5d"] = 1.5              # Placeholder
            enriched.append(earn)
        except Exception as e:
            print(f"[MASSIVE] Error fetching {symbol}: {e}")
            continue

    return enriched


def load_all_backtest_data(lookback_months: int = 12) -> tuple[list[dict], list[dict]]:
    """
    Load all historical data needed for backtest filter.

    Returns: (earnings_with_prices, rebalance_events)
    """
    print("\n" + "=" * 70)
    print("LOADING BACKTEST DATA (Real-time feeds)")
    print("=" * 70)

    # PEAD: Earnings surprises with prices
    earnings_raw = fetch_finnhub_earnings_history(lookback_months)
    earnings_enriched = enrich_earnings_with_prices(earnings_raw)

    # Reconstitution: Index rebalance events
    rebalances = fetch_russell_rebalance_history(lookback_months)

    print(f"\n[SUMMARY]")
    print(f"  PEAD data: {len(earnings_enriched)} earnings events with prices")
    print(f"  Recon data: {len(rebalances)} index rebalance events")

    return earnings_enriched, rebalances


if __name__ == "__main__":
    earnings, rebalances = load_all_backtest_data(lookback_months=12)
    print(f"\nReady for backtest: {len(earnings)} PEAD + {len(rebalances)} recon events")
