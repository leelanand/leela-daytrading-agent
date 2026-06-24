"""
Index Reconstitution Historical Backtest

Simulates reconstitution trades (Russell, S&P) over 12+ months.
Measures: win%, expectancy (gross and net), edge signal.

Mechanism: Forced flows — index funds must buy additions, sell deletions.
Data source: Historical Russell/S&P rebalance announcements + prices.
Realistic costs: spread + slippage on entry and exit.
Holding period: announcement date to effective date (~1-3 weeks).

Output: Edge signal for filtering (not final verdict).
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

from cost_modeling import estimate_spread_pct, estimate_slippage_pct


def load_historical_rebalances(csv_path: str = None) -> list[dict]:
    """
    Load historical index rebalance events.

    Expected CSV format:
    index,event_type,symbol,announcement_date,effective_date,price_at_announcement,price_at_effective

    For now, returns stub. In production, would:
    - Load from Russell/S&P historical data (available on their websites)
    - Or use pre-cached CSV of known rebalances
    """
    if csv_path and Path(csv_path).exists():
        import csv
        rebalances = []
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                rebalances.append({
                    "index": row["index"],
                    "event_type": row["event_type"],  # "addition" or "deletion"
                    "symbol": row["symbol"],
                    "announcement_date": row["announcement_date"],
                    "effective_date": row["effective_date"],
                    "price_at_announcement": float(row["price_at_announcement"]),
                    "price_at_effective": float(row["price_at_effective"]),
                })
        return rebalances

    # Stub for integration
    return []


def backtest_reconstitution(
    rebalance_data: list[dict],
) -> dict:
    """
    Backtest reconstitution strategy over historical rebalances.

    Assumptions:
    - Entry: market close on announcement date (or next day open)
    - Exit: at or after effective date (or before if target/stop hit)
    - Size: 1 share (for simplicity; can scale by account size)
    - Costs: realistic spread + slippage on entry and exit
    - Direction: BUY additions (+expected flow), SELL deletions (-expected flow)

    For MVP, we only test ADDITIONS (positive flow).

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
            avg_holding_days: float,
            confidence: str,
        }
    }
    """
    trades = []

    for rebal in rebalance_data:
        symbol = rebal["symbol"]
        event_type = rebal["event_type"]

        # For now, only test ADDITIONS (positive forced flow)
        # Deletions can be added later
        if event_type != "addition":
            continue

        announcement_date = datetime.strptime(rebal["announcement_date"], "%Y-%m-%d").date()
        effective_date = datetime.strptime(rebal["effective_date"], "%Y-%m-%d").date()
        holding_days = (effective_date - announcement_date).days

        # Entry price (announcement day)
        entry_price = rebal["price_at_announcement"]
        if entry_price <= 0:
            continue

        # Exit price (effective date or actual price at exit)
        exit_price = rebal["price_at_effective"]
        if exit_price <= 0:
            continue

        # Cost modeling
        daily_volume = 2_000_000  # Assumed higher for index constituents
        volatility_20d = 0.018  # Assumed lower for large-caps
        volatility_1d = 0.012

        entry_spread = estimate_spread_pct(entry_price, daily_volume, volatility_20d)
        entry_slip = estimate_slippage_pct("BUY", volatility_1d, 1.0)

        exit_spread = estimate_spread_pct(exit_price, daily_volume, volatility_20d)
        exit_slip = estimate_slippage_pct("SELL", volatility_1d, 1.0)

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
            "index": rebal["index"],
            "event_type": event_type,
            "announcement_date": announcement_date.isoformat(),
            "effective_date": effective_date.isoformat(),
            "holding_days": holding_days,
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
                "avg_holding_days": 0.0,
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
    avg_holding = sum(t["holding_days"] for t in trades) / len(trades)

    expectancy_gross = (win_pct * avg_winner) + ((1 - win_pct) * avg_loser)
    expectancy_net = expectancy_gross - avg_cost

    sum_wins = sum(t["realized_pnl_net"] for t in wins)
    sum_losses = abs(sum(t["realized_pnl_net"] for t in losses))
    pf = sum_wins / sum_losses if sum_losses > 0 else 0

    total_pnl_gross = sum(t["realized_pnl_gross"] for t in trades)
    total_pnl_net = sum(t["realized_pnl_net"] for t in trades)

    # Confidence level
    if total_trades < 20:
        confidence = "low"
    elif total_trades < 50:
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
            "avg_holding_days": round(avg_holding, 1),
            "confidence": confidence,
        }
    }


def report_recon_backtest(result: dict) -> str:
    """Format backtest result as readable report."""
    summary = result["summary"]
    trades = result["trades"]

    lines = [
        "\n=== RECONSTITUTION HISTORICAL BACKTEST REPORT ===\n",
        f"Sample size: {summary['total_trades']} trades (confidence: {summary['confidence']})",
        f"Avg holding period: {summary['avg_holding_days']:.1f} days",
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
    if summary["total_trades"] >= 20:
        if summary["expectancy_net"] > 0:
            edge_signal = "✓ EDGE DETECTED (net positive expectancy)"
        else:
            edge_signal = "✗ NO EDGE (net negative or zero expectancy)"
    else:
        edge_signal = "⏳ INSUFFICIENT DATA (need n≥20 for signal)"

    lines.append(f"\n{edge_signal}")

    if summary["confidence"] == "high":
        lines.append("   Confidence: HIGH — sample ≥50, results reliable")
    elif summary["confidence"] == "medium":
        lines.append("   Confidence: MEDIUM — sample 20-50, provisional signal")
    else:
        lines.append("   Confidence: LOW — sample <20, do not trust")

    return "\n".join(lines)


if __name__ == "__main__":
    # Stub test
    print("Reconstitution backtest module ready.")
    print("Usage: from recon_backtest import backtest_reconstitution, report_recon_backtest")
    print("Expected input: historical rebalance data (CSV or index provider API)")
