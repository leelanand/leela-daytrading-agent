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
from performance import (
    generate_daily_performance, print_performance_report,
    should_pause_trading, get_recent_performance,
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


def _prescan():
    """Discover and score candidates. Save to JSON. NEVER places orders."""
    if not _market_open():
        print("   Market is closed — skipping prescan.")
        return

    # Regime check
    regime, regime_reason = detect_regime()
    print(f"   Regime: {regime} — {regime_reason}")
    log_audit("REGIME_DETECTED", details={"regime": regime, "reason": regime_reason})

    if not is_tradeable(regime):
        print(f"   [NO TRADE] Regime {regime!r} not in tradeable set — prescan aborted.")
        log_audit("PRESCAN_SKIPPED", details={"reason": f"regime={regime}"})
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
    """Load prescan candidates, re-validate, execute limit orders (or simulate)."""
    if not _market_open():
        print("   Market is closed — skipping scan.")
        return

    # Midday block
    if _in_midday_block():
        now = datetime.now(ET).strftime("%H:%M")
        print(f"   [TIME GATE] Midday block active ({now} ET) — skipping scan.")
        log_audit("SCAN_SKIPPED", details={"reason": "midday_block"})
        return

    # Regime check
    regime, regime_reason = detect_regime()
    print(f"   Regime: {regime} — {regime_reason}")
    if not is_tradeable(regime):
        print(f"   [NO TRADE] Regime {regime!r} — scan aborted.")
        log_audit("SCAN_SKIPPED", details={"reason": f"regime={regime}"})
        return

    # Adaptive pause check
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

    # Get rolling win rate for dynamic sizing
    rolling       = get_recent_performance()
    recent_wr     = rolling.get("win_rate", 0.50)

    held = open_symbols()

    prescan = load_valid_candidates()
    if prescan:
        candidates = [c for c in prescan if c.get("tradeable") and c["symbol"] not in held]
        if not candidates:
            print("   No tradeable prescan candidates available.")
            return
        print(f"   Using {len(candidates)} prescan candidate(s) [rolling WR={recent_wr:.0%}].")
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

        risk_ok, risk_reason = check_candidate_risk(pick, portfolio, prescan_price)
        if not risk_ok:
            print(f"   [SKIP] {symbol}: {risk_reason}")
            log_audit("TRADE_REJECTED", symbol, {"score": score, "reason": risk_reason})
            continue

        price  = pick["price"]
        shares, size_pct, size_note = dynamic_position_size(
            portfolio, price, score, vol_pct, spread_pct, regime, recent_wr
        )
        cost = shares * price

        if paper_mode:
            print(f"   [PAPER] WOULD BUY {symbol} score={score}/100  {size_note}")
            print(f"   [PAPER] {shares} shares @ ${price:.2f} = ${cost:,.0f} | reasoning: {reasoning}")
            log_paper_trade(symbol, shares, price, "BUY", score, reasoning)
            log_audit("PAPER_TRADE", symbol, {
                "score": score, "shares": shares, "price": price, "size_pct": round(size_pct, 3),
            })
        else:
            print(f"   BUY {symbol} score={score}/100  {size_note}")
            print(f"   {shares} shares @ ${price:.2f} = ${cost:,.0f} | {reasoning}")
            place_bracket_order(symbol, shares, price, score=score, size_pct=size_pct, sizing_note=size_note)
            record_entry(symbol, price)
            log_audit("ORDER_PLACED", symbol, {
                "score": score, "shares": shares, "price": price,
                "cost": round(cost, 2), "size_pct": round(size_pct, 3), "sizing": size_note,
            })


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
        close_all_positions()
        log_audit("FORCE_CLOSE")
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
