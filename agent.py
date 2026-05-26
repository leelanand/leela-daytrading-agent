"""
Day Trading Agent — intraday momentum, all positions closed by 3:45pm ET.

Modes:
  --prescan     9:45am ET  — discover & score candidates, save to JSON, NO orders
  --morning     alias for --prescan (backwards compat)
  --scan        10:00am+   — load prescan candidates, validate, execute
  --paper       simulate full --scan logic without placing real orders
  --monitor     every 15 min — check trailing stops, time exits, momentum flips
  --close       3:45pm ET  — force-close all positions
  --report      4:00pm ET  — basic P&L from Alpaca
  --performance 4:15pm ET  — full analytics dashboard (expectancy, PF, windows)
  --status      any time   — current positions and P&L
"""
import argparse
from datetime import datetime
from zoneinfo import ZoneInfo

from alpaca.trading.client import TradingClient
from config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, PAPER_TRADING,
    MIN_SCORE_TO_TRADE, WATCHLIST_SCORE, KILL_SWITCH,
    BLOCK_MIDDAY, BLOCK_MIDDAY_START, BLOCK_MIDDAY_END,
    MIN_MOMENTUM_TO_TRADE,
)
from scanner import scan_for_candidates
from analyst import analyse_candidates
from executor import place_bracket_order, close_all_positions
from risk import can_trade, check_candidate_risk, position_size, open_symbols
from logger import init_db, log_audit, log_paper_trade, today_summary, all_time_summary
from candidates import save_candidates, load_valid_candidates
from regime import detect_regime, is_tradeable
from sizing import dynamic_position_size
from exits import record_entry, monitor_positions
from momentum import analyse_momentum, STRENGTHENING, STABLE
from intraday import get_intraday_alignment, is_aligned_for_longs
from risk import suggested_stop_pct
from performance import (
    generate_daily_performance, print_performance_report,
    should_pause_trading, get_recent_performance,
    get_weak_windows, get_expectancy_by_dimension,
)

ET = ZoneInfo("America/New_York")


def _client() -> TradingClient:
    return TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=PAPER_TRADING)


def _market_open() -> bool:
    return _client().get_clock().is_open


def _header(mode: str):
    now = datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")
    tag = "[PAPER]" if PAPER_TRADING else "[LIVE]"
    print(f"\n{'='*62}")
    print(f"  LEELA DAY TRADING AGENT {tag} -- {now}")
    print(f"  Mode: {mode}")
    print(f"{'='*62}\n")


def _in_midday_block() -> bool:
    """Returns True if current ET time falls in the configured midday block."""
    if not BLOCK_MIDDAY:
        return False
    now   = datetime.now(ET)
    start = now.replace(hour=BLOCK_MIDDAY_START[0], minute=BLOCK_MIDDAY_START[1], second=0)
    end   = now.replace(hour=BLOCK_MIDDAY_END[0],   minute=BLOCK_MIDDAY_END[1],   second=0)
    return start <= now < end


def _status():
    client    = _client()
    acct      = client.get_account()
    portfolio = float(acct.portfolio_value)
    start     = float(acct.last_equity)
    daily_pnl = portfolio - start
    daily_pct = daily_pnl / start * 100

    print(f"  Portfolio  : ${portfolio:,.2f}")
    print(f"  Today P&L  : ${daily_pnl:+,.2f} ({daily_pct:+.2f}%)")

    if KILL_SWITCH:
        print(f"\n  *** KILL SWITCH ACTIVE — all trading disabled ***")

    positions = client.get_all_positions()
    if positions:
        print(f"\n  OPEN POSITIONS ({len(positions)}):")
        for p in positions:
            pl     = float(p.unrealized_pl)
            pl_pct = float(p.unrealized_plpc) * 100
            print(f"    {p.symbol}: {p.qty} @ ${float(p.avg_entry_price):.2f} "
                  f"| now ${float(p.current_price):.2f} | {pl:+.2f} ({pl_pct:+.1f}%)")
    else:
        print("\n  No open positions.")


def _current_window() -> str:
    """Return the named trading window for the current ET time."""
    now  = datetime.now(ET)
    mins = now.hour * 60 + now.minute
    if mins < 10 * 60 + 30:  return "open"
    if mins < 12 * 60:        return "late_morning"
    if mins < 13 * 60:        return "midday"
    if mins < 15 * 60:        return "afternoon"
    return "power_hour"


def _prescan():
    """Discover and score candidates. Save to JSON. NEVER places orders."""
    if not _market_open():
        print("   Market is closed — skipping prescan.")
        return

    # Regime check
    regime, regime_reason = detect_regime()
    print(f"   Regime : {regime} — {regime_reason}")
    log_audit("REGIME_DETECTED", details={"regime": regime, "reason": regime_reason})

    if not is_tradeable(regime):
        print(f"   [NO TRADE] Regime {regime!r} not in tradeable set — prescan aborted.")
        log_audit("PRESCAN_SKIPPED", details={"reason": f"regime={regime}"})
        return

    # Intraday alignment check
    alignment, align_reason = get_intraday_alignment()
    print(f"   Intraday: {alignment} — {align_reason}")
    if not is_aligned_for_longs(alignment):
        print(f"   [NO TRADE] Intraday selloff in progress — prescan aborted.")
        log_audit("PRESCAN_SKIPPED", details={"reason": f"intraday={alignment}"})
        return

    log_audit("PRESCAN_START")
    candidates = scan_for_candidates()
    if not candidates:
        print("   No momentum candidates found in prescan.")
        log_audit("PRESCAN_DONE", details={"candidates": 0})
        return

    scored    = analyse_candidates(candidates)
    tradeable = [p for p in scored if p.get("tradeable")]
    watchlist = [p for p in scored if p.get("watchlist")]

    print(f"\n   PRESCAN RESULTS (regime: {regime}):")
    print(f"   Tradeable (score ≥ {MIN_SCORE_TO_TRADE}): {len(tradeable)}")
    for p in tradeable:
        print(f"     {p['symbol']:6s} score={p['score']:3d} | {p['reasoning'][:72]}")
    print(f"   Watchlist (score {WATCHLIST_SCORE}–{MIN_SCORE_TO_TRADE - 1}): {len(watchlist)}")
    for p in watchlist:
        print(f"     {p['symbol']:6s} score={p['score']:3d} | {p['reasoning'][:72]}")

    # Merge scanner data back for execution time
    scan_map = {c["symbol"]: c for c in candidates}
    for p in scored:
        for k, v in scan_map.get(p["symbol"], {}).items():
            p.setdefault(k, v)

    # Store regime in each candidate so execution phase can use it
    for p in scored:
        p["regime"] = regime

    save_candidates(scored)
    log_audit("PRESCAN_DONE", details={
        "total":     len(scored),
        "tradeable": len(tradeable),
        "watchlist": len(watchlist),
        "regime":    regime,
    })


def _scan_and_trade(paper_mode: bool = False):
    """Load prescan candidates, validate all gates, execute limit orders (or simulate)."""
    if not _market_open():
        print("   Market is closed — skipping scan.")
        return

    # Midday block
    if _in_midday_block():
        now = datetime.now(ET).strftime("%H:%M")
        print(f"   [TIME GATE] Midday block ({now} ET) — skipping scan.")
        log_audit("SCAN_SKIPPED", details={"reason": "midday_block"})
        return

    # Time-window adaptive block (based on historical performance in this window)
    current_win  = _current_window()
    weak_windows = get_weak_windows()
    if current_win in weak_windows:
        print(f"   [TIME GATE] Window {current_win!r} blocked by adaptive performance data.")
        log_audit("SCAN_SKIPPED", details={"reason": f"weak_window:{current_win}"})
        return

    # Regime check
    regime, regime_reason = detect_regime()
    print(f"   Regime  : {regime} — {regime_reason}")
    if not is_tradeable(regime):
        print(f"   [NO TRADE] Regime {regime!r} — scan aborted.")
        log_audit("SCAN_SKIPPED", details={"reason": f"regime={regime}"})
        return

    # Intraday alignment check (real-time SPY direction)
    alignment, align_reason = get_intraday_alignment()
    print(f"   Intraday: {alignment} — {align_reason}")
    if not is_aligned_for_longs(alignment):
        print(f"   [NO TRADE] Intraday selloff — blocking all longs.")
        log_audit("SCAN_SKIPPED", details={"reason": f"intraday_selloff"})
        return

    # Adaptive pause check (rolling win rate)
    pause, pause_reason = should_pause_trading()
    if pause:
        print(f"   [ADAPTIVE PAUSE] {pause_reason}")
        log_audit("SCAN_SKIPPED", details={"reason": f"adaptive_pause: {pause_reason}"})
        return

    ok, portfolio, reason = can_trade()
    if not ok:
        print(f"   [RISK] Cannot trade: {reason}")
        log_audit("TRADE_BLOCKED", details={"reason": reason})
        return

    rolling   = get_recent_performance()
    recent_wr = rolling.get("win_rate", 0.50)
    held      = open_symbols()

    prescan = load_valid_candidates()
    if prescan:
        candidates = [c for c in prescan if c.get("tradeable") and c["symbol"] not in held]
        if not candidates:
            print("   No tradeable prescan candidates available.")
            return
        print(f"   {len(candidates)} prescan candidate(s) | WR={recent_wr:.0%} | "
              f"window={current_win} | align={alignment}")
    else:
        print("   No valid prescan — running fresh scan...")
        fresh    = scan_for_candidates()
        filtered = [c for c in fresh if c["symbol"] not in held]
        if not filtered:
            print("   No new momentum candidates.")
            return
        scored    = analyse_candidates(filtered)
        scan_map  = {c["symbol"]: c for c in fresh}
        candidates = []
        for p in scored:
            if p.get("tradeable"):
                for k, v in scan_map.get(p["symbol"], {}).items():
                    p.setdefault(k, v)
                p["regime"] = regime
                candidates.append(p)

    for pick in candidates:
        ok, portfolio, reason = can_trade()
        if not ok:
            print(f"   [RISK] {reason} — stopping.")
            break

        symbol        = pick["symbol"]
        score         = pick.get("score", 0)
        reasoning     = pick.get("reasoning", "")
        prescan_price = pick.get("price")
        vol_pct       = pick.get("volatility_pct", 0.0)
        spread_pct    = pick.get("spread_pct", 0.0)
        setup_type    = pick.get("setup_type", "unknown")

        # Risk gate
        risk_ok, risk_reason = check_candidate_risk(pick, portfolio, prescan_price)
        if not risk_ok:
            print(f"   [SKIP] {symbol}: {risk_reason}")
            log_audit("TRADE_REJECTED", symbol, {
                "score": score, "reason": risk_reason, "setup_type": setup_type,
            })
            continue

        # Momentum analysis — 1-min bars check (fresh, per symbol)
        mom = analyse_momentum(symbol)
        print(f"   Momentum {symbol}: {mom['strength']} — {mom['reason']}")
        if mom["strength"] not in MIN_MOMENTUM_TO_TRADE:
            log_audit("TRADE_REJECTED", symbol, {
                "score": score, "reason": f"momentum={mom['strength']}: {mom['reason']}",
                "setup_type": setup_type,
            })
            print(f"   [SKIP] {symbol}: momentum {mom['strength']} — not tradeable")
            continue

        price  = pick["price"]
        shares, size_pct, size_note = dynamic_position_size(
            portfolio, price, score, vol_pct, spread_pct, regime, recent_wr
        )
        stop_pct = suggested_stop_pct(mom["strength"], regime)
        cost     = shares * price

        audit_details = {
            "score":      score,
            "shares":     shares,
            "price":      price,
            "cost":       round(cost, 2),
            "size_pct":   round(size_pct, 3),
            "sizing":     size_note,
            "regime":     regime,
            "alignment":  alignment,
            "momentum":   mom["strength"],
            "setup_type": setup_type,
            "stop_pct":   round(stop_pct, 4),
        }

        if paper_mode:
            print(f"   [PAPER] WOULD BUY {symbol}  score={score}/100  {size_note}  "
                  f"stop={stop_pct:.1%}  setup={setup_type}")
            print(f"   [PAPER] {shares}sh @ ${price:.2f} = ${cost:,.0f} | {reasoning}")
            log_paper_trade(symbol, shares, price, "BUY", score, reasoning)
            log_audit("PAPER_TRADE", symbol, audit_details)
        else:
            print(f"   BUY {symbol}  score={score}/100  {size_note}  "
                  f"stop={stop_pct:.1%}  setup={setup_type}")
            print(f"   {shares}sh @ ${price:.2f} = ${cost:,.0f} | {reasoning}")
            place_bracket_order(
                symbol, shares, price,
                score=score, size_pct=size_pct, sizing_note=size_note, stop_pct=stop_pct,
            )
            record_entry(symbol, price)
            log_audit("ORDER_PLACED", symbol, audit_details)


def _report():
    client    = _client()
    acct      = client.get_account()
    portfolio = float(acct.portfolio_value)
    start     = float(acct.last_equity)
    daily_pnl = portfolio - start
    daily_pct = daily_pnl / start * 100

    print(f"  Portfolio  : ${portfolio:,.2f}")
    print(f"  Today P&L  : ${daily_pnl:+,.2f} ({daily_pct:+.2f}%)")

    trades = today_summary()
    if trades:
        print(f"\n  CLOSED TRADES TODAY ({len(trades)}):")
        total = 0
        for sym, shares, entry, exit_p, pnl, pnl_pct in trades:
            print(f"    {sym}: {shares}sh | ${entry:.2f}→${exit_p:.2f} "
                  f"| ${pnl:+.2f} ({pnl_pct:+.1f}%)")
            total += pnl
        print(f"\n  Closed P&L today: ${total:+.2f}")
    else:
        print("\n  No closed trades recorded today.")

    stats = all_time_summary()
    if stats["trades"]:
        print(f"\n  ALL TIME: {stats['trades']} trades | "
              f"${stats['total_pnl']:+.2f} | {stats['win_rate']:.0f}% WR")

    print(f"{'='*62}\n")


if __name__ == "__main__":
    init_db()

    parser = argparse.ArgumentParser()
    parser.add_argument("--prescan",     action="store_true", help="Discover & score candidates, NO orders (9:45am)")
    parser.add_argument("--morning",     action="store_true", help="Alias for --prescan")
    parser.add_argument("--scan",        action="store_true", help="Load prescan candidates and execute (10:00am+)")
    parser.add_argument("--paper",       action="store_true", help="Simulate scan/execute without real orders")
    parser.add_argument("--monitor",     action="store_true", help="Check trailing stops and advanced exits (every 15 min)")
    parser.add_argument("--close",       action="store_true", help="Force-close all positions (3:45pm ET)")
    parser.add_argument("--report",      action="store_true", help="Basic P&L report (4:00pm ET)")
    parser.add_argument("--performance", action="store_true", help="Full analytics dashboard (4:15pm ET)")
    parser.add_argument("--status",      action="store_true", help="Current positions and P&L")
    args = parser.parse_args()

    if args.prescan or args.morning:
        _header("PRESCAN — no orders placed")
        _status()
        print()
        _prescan()

    elif args.scan:
        _header("SCAN & EXECUTE")
        _status()
        print()
        _scan_and_trade(paper_mode=False)

    elif args.paper:
        _header("PAPER SIMULATION — no real orders")
        _status()
        print()
        _scan_and_trade(paper_mode=True)

    elif args.monitor:
        _header("MONITOR — advanced exits")
        monitor_positions()

    elif args.close:
        _header("FORCE CLOSE — 3:45pm ET")
        import time
        close_all_positions()
        log_audit("FORCE_CLOSE")

        # EOD verification — confirm fully flat, no overnight exposure
        time.sleep(3)
        client   = _client()
        remaining = client.get_all_positions()
        open_ords = client.get_orders()

        if remaining:
            print(f"\n   [RETRY] {len(remaining)} position(s) still open — retrying...")
            for p in remaining:
                try:
                    client.close_position(p.symbol)
                    print(f"   [CLOSED] {p.symbol}")
                except Exception as e:
                    print(f"   [ERROR] {p.symbol}: {e}")
            time.sleep(2)
            remaining = client.get_all_positions()

        if open_ords:
            print(f"   [CLEANUP] Cancelling {len(open_ords)} open order(s)...")
            client.cancel_orders()
            time.sleep(1)
            open_ords = client.get_orders()

        print(f"\n  EOD VERIFICATION:")
        print(f"  Open positions : {len(remaining)}")
        print(f"  Open orders    : {len(open_ords)}")
        if not remaining and not open_ords:
            print(f"  ✓ Fully flat — no overnight exposure")
        else:
            print(f"  ⚠ WARNING: still have open items — check immediately!")
        log_audit("EOD_VERIFIED", details={
            "positions": len(remaining),
            "orders":    len(open_ords),
        })
        print()
        _status()

    elif args.report:
        _header("DAILY REPORT")
        _report()

    elif args.performance:
        _header("PERFORMANCE DASHBOARD")
        perf = generate_daily_performance()
        print_performance_report(perf)

    elif args.status:
        _header("STATUS")
        _status()

    else:
        print("Usage: python agent.py "
              "--prescan | --scan | --paper | --monitor | "
              "--close | --report | --performance | --status")
