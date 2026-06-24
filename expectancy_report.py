"""
Expectancy analysis and reporting.

Analyzes trade outcomes by:
- Overall (all trades)
- By setup type (ORB, gap_and_go, pullback, news_momentum)
- By regime (TRENDING_UP, CHOPPY, LOW_VOLUME, HIGH_VOL)
- By (setup, regime) pair
- By scoring method (Claude vs local)

Only reports stats for segments with N >= MIN_SAMPLE_SIZE to avoid noise.
"""
import sqlite3
from collections import defaultdict
from pathlib import Path
from config import DB_PATH


MIN_SAMPLE_SIZE = 10  # minimum trades to report stats for a segment
CONFIDENCE_MIN_SAMPLE = 100  # minimum for confident edge claim


def _fetch_trades():
    """Load all completed trades from journal."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    trades = [dict(row) for row in con.execute("""
        SELECT * FROM trade_journal WHERE outcome IS NOT NULL
        ORDER BY ts_entry ASC
    """).fetchall()]
    con.close()
    return trades


def _calc_metrics(trades: list[dict]) -> dict:
    """Calculate core metrics for a trade sample."""
    if not trades:
        return None

    wins = [t for t in trades if t["outcome"] == "WIN"]
    losses = [t for t in trades if t["outcome"] == "LOSS"]
    breakevens = [t for t in trades if t["outcome"] == "BREAKEVEN"]

    n_trades = len(trades)
    n_wins = len(wins)
    n_losses = len(losses)

    win_pct = n_wins / n_trades if n_trades > 0 else 0
    avg_winner = sum(t["realized_pnl"] for t in wins) / n_wins if wins else 0
    avg_loser = sum(t["realized_pnl"] for t in losses) / n_losses if losses else 0

    # Expectancy = (win% * avg_win) + ((1-win%) * avg_loss)
    expectancy = (win_pct * avg_winner) + ((1 - win_pct) * avg_loser)

    # Net of costs = expectancy - avg cost
    avg_cost = sum(t["realized_cost_total"] for t in trades) / n_trades
    expectancy_net = expectancy - avg_cost

    # Profit factor = sum(wins) / abs(sum(losses))
    sum_wins = sum(t["realized_pnl"] for t in wins)
    sum_losses = abs(sum(t["realized_pnl"] for t in losses))
    pf = sum_wins / sum_losses if sum_losses > 0 else 0

    # Total realized P&L
    total_pnl = sum(t["realized_pnl"] for t in trades)
    total_pnl_gross = sum(t["realized_pnl_pre_cost"] for t in trades)
    total_costs = sum(t["realized_cost_total"] for t in trades)

    return {
        "n_trades": n_trades,
        "n_wins": n_wins,
        "n_losses": n_losses,
        "n_breakeven": len(breakevens),
        "win_pct": win_pct,
        "avg_winner": avg_winner,
        "avg_loser": avg_loser,
        "expectancy_gross": expectancy,
        "avg_cost": avg_cost,
        "expectancy_net": expectancy_net,
        "pf": pf,
        "total_pnl": total_pnl,
        "total_pnl_gross": total_pnl_gross,
        "total_costs": total_costs,
        "has_edge": expectancy_net > 0,
        "confident": n_trades >= CONFIDENCE_MIN_SAMPLE,
    }


def _format_metrics(metrics: dict, label: str) -> str:
    """Format metrics dict as readable report section."""
    if not metrics:
        return f"{label}: No trades\n"

    edge_flag = "✓ EDGE" if metrics["has_edge"] else "✗ NO EDGE"
    confident = "(confident)" if metrics["confident"] else "(n<100, caution)"

    lines = [
        f"\n{label} {edge_flag} {confident}",
        f"  Trades: {metrics['n_trades']} (W:{metrics['n_wins']} L:{metrics['n_losses']} BE:{metrics['n_breakeven']})",
        f"  Win%: {metrics['win_pct']:.1%}",
        f"  Avg Winner: ${metrics['avg_winner']:.2f}  Avg Loser: ${metrics['avg_loser']:.2f}",
        f"  Expectancy (gross): ${metrics['expectancy_gross']:.2f}",
        f"  Avg Cost / Trade: ${metrics['avg_cost']:.2f}",
        f"  Expectancy (net): ${metrics['expectancy_net']:.2f}",
        f"  Profit Factor: {metrics['pf']:.2f}",
        f"  Total P&L: ${metrics['total_pnl']:.2f} (gross: ${metrics['total_pnl_gross']:.2f}, costs: ${metrics['total_costs']:.2f})",
    ]

    return "\n".join(lines)


def generate_report():
    """Generate full expectancy report."""
    trades = _fetch_trades()

    if not trades:
        print("\n=== NO COMPLETED TRADES YET ===\n")
        return

    print("\n" + "=" * 80)
    print("EXPECTANCY ANALYSIS REPORT")
    print("=" * 80)

    # 1. Overall
    overall = _calc_metrics(trades)
    print(_format_metrics(overall, "OVERALL"))

    # 2. By Setup Type
    print("\n\n--- BY SETUP TYPE ---")
    by_setup = defaultdict(list)
    for t in trades:
        setup = t["setup_type"] or "UNKNOWN"
        by_setup[setup].append(t)

    for setup in sorted(by_setup.keys()):
        trades_setup = by_setup[setup]
        if len(trades_setup) >= MIN_SAMPLE_SIZE:
            metrics = _calc_metrics(trades_setup)
            print(_format_metrics(metrics, f"SETUP: {setup}"))
        else:
            print(f"\nSETUP: {setup}: {len(trades_setup)} trades (n<{MIN_SAMPLE_SIZE}, skipped)")

    # 3. By Regime
    print("\n\n--- BY REGIME ---")
    by_regime = defaultdict(list)
    for t in trades:
        regime = t["regime"] or "UNKNOWN"
        by_regime[regime].append(t)

    for regime in sorted(by_regime.keys()):
        trades_regime = by_regime[regime]
        if len(trades_regime) >= MIN_SAMPLE_SIZE:
            metrics = _calc_metrics(trades_regime)
            print(_format_metrics(metrics, f"REGIME: {regime}"))
        else:
            print(f"\nREGIME: {regime}: {len(trades_regime)} trades (n<{MIN_SAMPLE_SIZE}, skipped)")

    # 4. By (Setup, Regime) Pair
    print("\n\n--- BY (SETUP, REGIME) PAIR ---")
    by_pair = defaultdict(list)
    for t in trades:
        setup = t["setup_type"] or "UNKNOWN"
        regime = t["regime"] or "UNKNOWN"
        by_pair[(setup, regime)].append(t)

    for (setup, regime) in sorted(by_pair.keys()):
        trades_pair = by_pair[(setup, regime)]
        if len(trades_pair) >= 5:  # lower bar for 2D breakdown
            metrics = _calc_metrics(trades_pair)
            label = f"SETUP={setup}, REGIME={regime}"
            print(_format_metrics(metrics, label))

    # 5. By Scoring Method
    print("\n\n--- BY SCORING METHOD ---")
    claude_trades = [t for t in trades if t["score_used"] == t["claude_score"] and t["claude_score"] is not None]
    local_trades = [t for t in trades if t["score_used"] == t["local_score"] and t["local_score"] is not None]

    if len(claude_trades) >= MIN_SAMPLE_SIZE:
        metrics = _calc_metrics(claude_trades)
        print(_format_metrics(metrics, "CLAUDE SCORING"))
    else:
        print(f"\nCLAUDE SCORING: {len(claude_trades)} trades (n<{MIN_SAMPLE_SIZE}, skipped)")

    if len(local_trades) >= MIN_SAMPLE_SIZE:
        metrics = _calc_metrics(local_trades)
        print(_format_metrics(metrics, "LOCAL SCORING"))
    else:
        print(f"\nLOCAL SCORING: {len(local_trades)} trades (n<{MIN_SAMPLE_SIZE}, skipped)")

    # 6. Score Bucket Analysis (low/med/high)
    print("\n\n--- SCORE BUCKETING ---")
    low_score = [t for t in trades if (t["score_used"] or 0) < 70]
    med_score = [t for t in trades if 70 <= (t["score_used"] or 0) < 85]
    high_score = [t for t in trades if (t["score_used"] or 0) >= 85]

    for bucket, label in [(low_score, "LOW (<70)"), (med_score, "MED (70–84)"), (high_score, "HIGH (≥85)")]:
        if len(bucket) >= 5:
            metrics = _calc_metrics(bucket)
            print(_format_metrics(metrics, f"SCORE: {label}"))

    # 7. Summary and conclusions
    print("\n\n--- SUMMARY & EDGE DETERMINATION ---")
    print(f"\nTotal trades analyzed: {len(trades)}")
    print(f"Date range: {trades[0]['ts_entry'][:10] if trades else 'N/A'} to {trades[-1]['ts_entry'][:10] if trades else 'N/A'}")

    if overall and overall["has_edge"]:
        print(f"\n✓ OVERALL EDGE DETECTED: ${overall['expectancy_net']:.2f} per trade (net of costs)")
        if overall["confident"]:
            print(f"  Confidence: HIGH ({overall['n_trades']} trades)")
        else:
            print(f"  Confidence: MODERATE (n={overall['n_trades']}, target ≥100)")
    else:
        print(f"\n✗ NO OVERALL EDGE: ${overall['expectancy_net']:.2f} per trade (net of costs)")

    # Highlight which components have edge
    print("\n--- COMPONENT EDGES ---")
    print("Setups with positive expectancy (net of costs):")
    for setup in sorted(by_setup.keys()):
        if len(by_setup[setup]) >= MIN_SAMPLE_SIZE:
            m = _calc_metrics(by_setup[setup])
            status = "✓" if m["has_edge"] else "✗"
            print(f"  {status} {setup}: ${m['expectancy_net']:.2f}/trade (n={m['n_trades']})")

    print("\nRegimes with positive expectancy:")
    for regime in sorted(by_regime.keys()):
        if len(by_regime[regime]) >= MIN_SAMPLE_SIZE:
            m = _calc_metrics(by_regime[regime])
            status = "✓" if m["has_edge"] else "✗"
            print(f"  {status} {regime}: ${m['expectancy_net']:.2f}/trade (n={m['n_trades']})")

    print("\n" + "=" * 80 + "\n")
