"""
BACKTEST FILTER: Historical edge detection for structural edges (Option 2)

Primary signal: 12+ months of historical PEAD + reconstitution (LIVE DATA)
Secondary validation: 1 week of paper trading (execution check)

This is a FILTER, not a confirmation:
- If backtest shows edge: candidate for live trading (paper execution validates mechanism)
- If backtest shows no edge: kill immediately (data says no edge exists)

One week can reject false edges. Cannot confirm true ones.
"""
import json
from pathlib import Path
from datetime import datetime

from pead_backtest import backtest_pead, report_pead_backtest
from recon_backtest import backtest_reconstitution, report_recon_backtest
from backtest_data_loader import load_all_backtest_data


def run_full_backtest(
    lookback_months: int = 12,
) -> dict:
    """
    Run historical backtests on both PEAD and reconstitution using LIVE DATA.

    Uses: Finnhub (earnings) + Massive (prices) + Russell (rebalances)

    Returns: {
        timestamp: datetime of backtest run,
        pead: {result from backtest_pead()},
        reconstitution: {result from backtest_reconstitution()},
        filter_signal: {which edges pass, which fail},
        recommendation: "proceed with X, kill Y",
    }
    """
    results = {
        "timestamp": datetime.now().isoformat(),
        "pead": None,
        "reconstitution": None,
        "filter_signal": {},
        "recommendation": None,
    }

    # Load real data from live feeds
    print("\n=== LOADING LIVE DATA ===")
    earnings_data, rebalance_data = load_all_backtest_data(lookback_months=lookback_months)

    # PEAD backtest
    print("\n=== RUNNING PEAD BACKTEST ===")
    try:
        pead_result = backtest_pead(
            earnings_data=earnings_data,
            holding_days=5,
            min_surprise_pct=5.0,
        )
        results["pead"] = pead_result
        print(report_pead_backtest(pead_result))
    except Exception as e:
        print(f"PEAD backtest failed: {e}")
        results["pead"] = {"error": str(e)}

    # Reconstitution backtest
    print("\n=== RUNNING RECONSTITUTION BACKTEST ===")
    try:
        recon_result = backtest_reconstitution(
            rebalance_data=rebalance_data,
        )
        results["reconstitution"] = recon_result
        print(report_recon_backtest(recon_result))
    except Exception as e:
        print(f"Reconstitution backtest failed: {e}")
        results["reconstitution"] = {"error": str(e)}

    # Filter signal
    print("\n=== FILTER SIGNAL ===")
    filter_signal = {}

    if results["pead"] and "summary" in results["pead"]:
        pead_summary = results["pead"]["summary"]
        if pead_summary["total_trades"] >= 30:
            if pead_summary["expectancy_net"] > 0:
                filter_signal["pead"] = {
                    "signal": "PROCEED",
                    "reason": f"positive edge: ${pead_summary['expectancy_net']:.2f}/trade, n={pead_summary['total_trades']}",
                    "confidence": pead_summary["confidence"],
                }
            else:
                filter_signal["pead"] = {
                    "signal": "KILL",
                    "reason": f"negative edge: ${pead_summary['expectancy_net']:.2f}/trade, n={pead_summary['total_trades']}",
                    "confidence": pead_summary["confidence"],
                }
        else:
            filter_signal["pead"] = {
                "signal": "INSUFFICIENT_DATA",
                "reason": f"need n≥30, got {pead_summary['total_trades']}",
                "confidence": "low",
            }
    else:
        filter_signal["pead"] = {
            "signal": "ERROR",
            "reason": "backtest failed to load",
            "confidence": "none",
        }

    if results["reconstitution"] and "summary" in results["reconstitution"]:
        recon_summary = results["reconstitution"]["summary"]
        if recon_summary["total_trades"] >= 20:
            if recon_summary["expectancy_net"] > 0:
                filter_signal["reconstitution"] = {
                    "signal": "PROCEED",
                    "reason": f"positive edge: ${recon_summary['expectancy_net']:.2f}/trade, n={recon_summary['total_trades']}",
                    "confidence": recon_summary["confidence"],
                }
            else:
                filter_signal["reconstitution"] = {
                    "signal": "KILL",
                    "reason": f"negative edge: ${recon_summary['expectancy_net']:.2f}/trade, n={recon_summary['total_trades']}",
                    "confidence": recon_summary["confidence"],
                }
        else:
            filter_signal["reconstitution"] = {
                "signal": "INSUFFICIENT_DATA",
                "reason": f"need n≥20, got {recon_summary['total_trades']}",
                "confidence": "low",
            }
    else:
        filter_signal["reconstitution"] = {
            "signal": "ERROR",
            "reason": "backtest failed to load",
            "confidence": "none",
        }

    results["filter_signal"] = filter_signal

    # Recommendation
    proceed = [k for k, v in filter_signal.items() if v["signal"] == "PROCEED"]
    kill = [k for k, v in filter_signal.items() if v["signal"] == "KILL"]
    insufficient = [k for k, v in filter_signal.items() if v["signal"] == "INSUFFICIENT_DATA"]

    recommendation_parts = []
    if proceed:
        recommendation_parts.append(f"PROCEED with: {', '.join(proceed)} (paper trading validates execution)")
    if kill:
        recommendation_parts.append(f"KILL: {', '.join(kill)} (historical data shows no edge)")
    if insufficient:
        recommendation_parts.append(f"SKIP: {', '.join(insufficient)} (insufficient historical data to filter)")

    results["recommendation"] = " | ".join(recommendation_parts) if recommendation_parts else "No clear signal"

    return results


def report_filter_signal(results: dict) -> str:
    """Format filter signal as actionable recommendation."""
    lines = [
        "\n" + "=" * 70,
        "BACKTEST FILTER REPORT (OPTION 2)",
        "=" * 70,
        f"Run timestamp: {results['timestamp']}",
        "\nFILTER SIGNAL (one week can reject, not confirm):",
        "-" * 70,
    ]

    for mechanism, signal in results["filter_signal"].items():
        lines.append(f"\n{mechanism.upper()}:")
        lines.append(f"  Signal: {signal['signal']}")
        lines.append(f"  Reason: {signal['reason']}")
        lines.append(f"  Confidence: {signal['confidence']}")

    lines.append("\n" + "-" * 70)
    lines.append("\nRECOMMENDATION:")
    lines.append(results["recommendation"])
    lines.append("\nNEXT STEP:")
    lines.append("  1. Paper trade the 'PROCEED' mechanisms for 1 week (execution validation)")
    lines.append("  2. If paper execution matches backtest assumptions → consider live")
    lines.append("  3. If paper execution diverges → investigate why (slippage, timing, regime)")
    lines.append("  4. Kill all 'KILL' mechanisms immediately (historical data is decisive)")
    lines.append("\n" + "=" * 70)

    return "\n".join(lines)


if __name__ == "__main__":
    print("BACKTEST FILTER READY")
    print("\nUsage:")
    print("  python backtest_filter.py")
    print("\nTo run with real data:")
    print("  python backtest_filter.py --earnings-csv path/to/earnings.csv --rebalances-csv path/to/rebalances.csv")
