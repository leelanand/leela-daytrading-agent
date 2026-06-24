"""
PEAD (Post-Earnings Announcement Drift) Historical Backtest

Simulates PEAD trades over 12+ months of historical earnings data.
Measures: win%, expectancy (gross and net of realistic costs), confidence.

Data source: Finnhub earnings calendar (requires historical pull or CSV).
Realistic costs: spread + slippage both sides (using cost_modeling).
Holding period: 5 days post-earnings (or until exit condition).

Output: Edge signal for filtering (not final verdict).
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

from cost_modeling import estimate_spread_pct, estimate_slippage_pct


def load_historical_earnings(csv_path: str = None) -> list[dict]:
    """
    Load historical earnings data.

    Expected CSV format:
    symbol,date,actual_eps,estimate_eps,price_day_before,price_day_after,atr_5d

    For now, returns stub. In production, would:
    - Pull from Finnhub API (historical)
    - Or load from pre-cached CSV
    """
    if csv_path and Path(csv_path).exists():
        import csv
        earnings = []
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                earnings.append({
                    "symbol": row["symbol"],
                    "date": row["date"],
                    "actual_eps": float(row["actual_eps"]),
                    "estimate_eps": float(row["estimate_eps"]),
                    "price_day_before": float(row["price_day_before"]),
                    "price_day_after": float(row["price_day_after"]),
                    "atr_5d": float(row["atr_5d"]),
                })
        return earnings

    # Stub for integration
    return []


def calculate_pead_drift(symbol: str, entry_price: float, prices_5days: list[float]) -> dict:
    """
    Calculate actual drift over 5-day post-earnings window.

    prices_5days: [day0_close, day1_close, day2_close, day3_close, day4_close]

    Returns: {max_price, min_price, drift_pct_max, drift_pct_min, days_to_max}
    """
    if not prices_5days or len(prices_5days) < 2:
        return {
            "max_price": entry_price,
            "min_price": entry_price,
            "drift_pct_max": 0.0,
            "drift_pct_min": 0.0,
            "days_to_max": 0,
        }

    max_price = max(prices_5days)
    min_price = min(prices_5days)
    max_idx = prices_5days.index(max_price)

    drift_max = (max_price - entry_price) / entry_price if entry_price > 0 else 0
    drift_min = (min_price - entry_price) / entry_price if entry_price > 0 else 0

    return {
        "max_price": round(max_price, 2),
        "min_price": round(min_price, 2),
        "drift_pct_max": round(drift_max * 100, 2),
        "drift_pct_min": round(drift_min * 100, 2),
        "days_to_max": max_idx,
    }


def backtest_pead(
    earnings_data: list[dict],
    holding_days: int = 5,
    min_surprise_pct: float = 5.0,
) -> dict:
    """
    Backtest PEAD strategy over historical earnings.

    Assumptions:
    - Entry: market open day after earnings report
    - Size: 1 share (for simplicity; can scale by account size)
    - Exit: after holding_days or profit target (+2.5%) or stop loss (-1.5%)
    - Costs: realistic spread + slippage on entry and exit

    Returns: {
        trades: [list of completed trades],
        summary: {
            total_trades: int,
            win_count: int,
            loss_count: int,
            win_pct: float,
            avg_winner: float,
            avg_loser: float,
            expectancy_gross: float,
            avg_cost_per_trade: float,
            expectancy_net: float,
            profit_factor: float,
            total_pnl_gross: float,
            total_pnl_net: float,
            confidence: str,  # "low" (<30 trades), "medium" (30-100), "high" (>100)
        }
    }
    """
    trades = []

    for earn in earnings_data:
        symbol = earn["symbol"]
        report_date = datetime.strptime(earn["date"], "%Y-%m-%d").date()
        surprise_pct = (
            (earn["actual_eps"] - earn["estimate_eps"]) / abs(earn["estimate_eps"]) * 100
            if earn["estimate_eps"] != 0 else 0
        )

        # Filter: only material surprises
        if abs(surprise_pct) < min_surprise_pct:
            continue

        # Entry: day after earnings (open)
        entry_price = earn["price_day_after"]
        if entry_price <= 0:
            continue

        # Simulate 5-day price series (stub; in production, would fetch from data provider)
        # For now, assume prices from historical data
        prices_5d = [entry_price]  # Day 0 (entry)
        # TODO: fetch actual prices for days 1-5 from data provider

        # Cost modeling
        daily_volume = 1_000_000  # Assumed; would come from data provider
        volatility_20d = 0.02  # Assumed; would come from data provider
        volatility_1d = 0.015  # Assumed; would come from data provider

        entry_spread = estimate_spread_pct(entry_price, daily_volume, volatility_20d)
        entry_slip = estimate_slippage_pct("BUY", volatility_1d, 1.0)

        # Effective entry price (after costs)
        effective_entry = entry_price * (1 + entry_spread / 2 + entry_slip)

        # Targets
        stop_price = entry_price * 0.985  # -1.5%
        target_price = entry_price * 1.025  # +2.5%

        # Exit price (stub: assume mid-range; in production, would fetch actual exit)
        # For backtest, we'd need historical price data for days 1-5
        exit_price = entry_price  # Placeholder: no drift in this stub

        exit_spread = estimate_spread_pct(exit_price, daily_volume, volatility_20d)
        exit_slip = estimate_slippage_pct("SELL", volatility_1d, 1.0)

        # Effective exit price (after costs)
        effective_exit = exit_price * (1 - exit_spread / 2 - exit_slip)

        # P&L
        realized_pnl_gross = exit_price - entry_price
        realized_cost = (
            abs(entry_spread / 2 * entry_price) +
            abs(entry_slip * entry_price) +
            abs(exit_spread / 2 * exit_price) +
            abs(exit_slip * exit_price)
        )
        realized_pnl_net = realized_pnl_gross - realized_cost

        # Outcome
        if realized_pnl_net > 0.5:
            outcome = "WIN"
        elif realized_pnl_net < -0.5:
            outcome = "LOSS"
        else:
            outcome = "BREAKEVEN"

        trades.append({
            "symbol": symbol,
            "report_date": report_date.isoformat(),
            "surprise_pct": round(surprise_pct, 2),
            "entry_price": round(entry_price, 2),
            "exit_price": round(exit_price, 2),
            "entry_spread_pct": round(entry_spread * 100, 3),
            "entry_slippage_pct": round(entry_slip * 100, 3),
            "exit_spread_pct": round(exit_spread * 100, 3),
            "exit_slippage_pct": round(exit_slip * 100, 3),
            "realized_cost": round(realized_cost, 2),
            "realized_pnl_gross": round(realized_pnl_gross, 2),
            "realized_pnl_net": round(realized_pnl_net, 2),
            "outcome": outcome,
        })

    # Summary statistics
    if not trades:
        return {
            "trades": [],
            "summary": {
                "total_trades": 0,
                "win_count": 0,
                "loss_count": 0,
                "win_pct": 0.0,
                "avg_winner": 0.0,
                "avg_loser": 0.0,
                "expectancy_gross": 0.0,
                "avg_cost_per_trade": 0.0,
                "expectancy_net": 0.0,
                "profit_factor": 0.0,
                "total_pnl_gross": 0.0,
                "total_pnl_net": 0.0,
                "confidence": "insufficient_data",
            }
        }

    wins = [t for t in trades if t["outcome"] == "WIN"]
    losses = [t for t in trades if t["outcome"] == "LOSS"]

    total_trades = len(trades)
    win_count = len(wins)
    loss_count = len(losses)
    win_pct = win_count / total_trades if total_trades > 0 else 0

    avg_winner = sum(t["realized_pnl_net"] for t in wins) / len(wins) if wins else 0
    avg_loser = sum(t["realized_pnl_net"] for t in losses) / len(losses) if losses else 0
    avg_cost = sum(t["realized_cost"] for t in trades) / len(trades)

    expectancy_gross = (win_pct * avg_winner) + ((1 - win_pct) * avg_loser)
    expectancy_net = expectancy_gross - avg_cost

    sum_wins = sum(t["realized_pnl_net"] for t in wins)
    sum_losses = abs(sum(t["realized_pnl_net"] for t in losses))
    pf = sum_wins / sum_losses if sum_losses > 0 else 0

    total_pnl_gross = sum(t["realized_pnl_gross"] for t in trades)
    total_pnl_net = sum(t["realized_pnl_net"] for t in trades)

    # Confidence level
    if total_trades < 30:
        confidence = "low"
    elif total_trades < 100:
        confidence = "medium"
    else:
        confidence = "high"

    return {
        "trades": trades,
        "summary": {
            "total_trades": total_trades,
            "win_count": win_count,
            "loss_count": loss_count,
            "win_pct": round(win_pct * 100, 1),
            "avg_winner": round(avg_winner, 2),
            "avg_loser": round(avg_loser, 2),
            "expectancy_gross": round(expectancy_gross, 2),
            "avg_cost_per_trade": round(avg_cost, 2),
            "expectancy_net": round(expectancy_net, 2),
            "profit_factor": round(pf, 2),
            "total_pnl_gross": round(total_pnl_gross, 2),
            "total_pnl_net": round(total_pnl_net, 2),
            "confidence": confidence,
        }
    }


def report_pead_backtest(result: dict) -> str:
    """Format backtest result as readable report."""
    summary = result["summary"]
    trades = result["trades"]

    lines = [
        "\n=== PEAD HISTORICAL BACKTEST REPORT ===\n",
        f"Sample size: {summary['total_trades']} trades (confidence: {summary['confidence']})",
        f"Win rate: {summary['win_pct']:.1f}% ({summary['win_count']} wins, {summary['loss_count']} losses)",
        f"Avg winner: ${summary['avg_winner']:.2f}  |  Avg loser: ${summary['avg_loser']:.2f}",
        f"Expectancy (gross): ${summary['expectancy_gross']:.2f}/trade",
        f"Avg cost/trade: ${summary['avg_cost_per_trade']:.2f}",
        f"Expectancy (net): ${summary['expectancy_net']:.2f}/trade",
        f"Profit factor: {summary['profit_factor']:.2f}",
        f"Total P&L (gross): ${summary['total_pnl_gross']:.2f}",
        f"Total P&L (net): ${summary['total_pnl_net']:.2f}",
    ]

    # Edge signal
    if summary["total_trades"] >= 30:
        if summary["expectancy_net"] > 0:
            edge_signal = "✓ EDGE DETECTED (net positive expectancy)"
        else:
            edge_signal = "✗ NO EDGE (net negative or zero expectancy)"
    else:
        edge_signal = "⏳ INSUFFICIENT DATA (need n≥30 for signal)"

    lines.append(f"\n{edge_signal}")

    if summary["confidence"] == "high":
        lines.append("   Confidence: HIGH — sample ≥100, results reliable")
    elif summary["confidence"] == "medium":
        lines.append("   Confidence: MEDIUM — sample 30-100, provisional signal")
    else:
        lines.append("   Confidence: LOW — sample <30, do not trust")

    return "\n".join(lines)


if __name__ == "__main__":
    # Stub test
    print("PEAD backtest module ready.")
    print("Usage: from pead_backtest import backtest_pead, report_pead_backtest")
    print("Expected input: historical earnings data (CSV or Finnhub API)")
