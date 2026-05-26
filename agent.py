"""
Day Trading Agent — intraday momentum, all positions closed by 3:45pm ET.

Modes:
  --prescan   9:45am ET — discover & score candidates, save to JSON, NO orders
  --morning   alias for --prescan (backwards compat)
  --scan      10:00am+ every 30 min — load prescan candidates, validate, execute
  --paper     simulate full --scan logic without placing real orders
  --close     3:45pm ET — force-close all positions, no overnight risk
  --report    4:30pm ET — daily P&L summary
  --status    any time  — current positions and P&L
"""
import argparse
from datetime import datetime
from zoneinfo import ZoneInfo

from alpaca.trading.client import TradingClient
from config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, PAPER_TRADING,
    MIN_SCORE_TO_TRADE, WATCHLIST_SCORE, KILL_SWITCH,
)
from scanner import scan_for_candidates
from analyst import analyse_candidates
from executor import place_bracket_order, close_all_positions
from risk import can_trade, check_candidate_risk, position_size, open_symbols
from logger import init_db, log_audit, log_paper_trade, today_summary, all_time_summary
from candidates import save_candidates, load_valid_candidates

ET = ZoneInfo("America/New_York")


def _client() -> TradingClient:
    return TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=PAPER_TRADING)


def _market_open() -> bool:
    return _client().get_clock().is_open


def _header(mode: str):
    now = datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")
    tag = "[PAPER]" if PAPER_TRADING else "[LIVE]"
    print(f"\n{'='*60}")
    print(f"  LEELA DAY TRADING AGENT {tag} -- {now}")
    print(f"  Mode: {mode}")
    print(f"{'='*60}\n")


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

    log_audit("PRESCAN_START")
    candidates = scan_for_candidates()
    if not candidates:
        print("   No momentum candidates found in prescan.")
        log_audit("PRESCAN_DONE", details={"candidates": 0})
        return

    scored = analyse_candidates(candidates)

    tradeable = [p for p in scored if p.get("tradeable")]
    watchlist = [p for p in scored if p.get("watchlist")]

    print(f"\n   PRESCAN RESULTS:")
    print(f"   Tradeable (score >= {MIN_SCORE_TO_TRADE}): {len(tradeable)}")
    for p in tradeable:
        print(f"     {p['symbol']:6s} score={p['score']:3d} | {p['reasoning'][:72]}")
    print(f"   Watchlist (score {WATCHLIST_SCORE}-{MIN_SCORE_TO_TRADE - 1}): {len(watchlist)}")
    for p in watchlist:
        print(f"     {p['symbol']:6s} score={p['score']:3d} | {p['reasoning'][:72]}")

    # Merge scanner fields back into scored results for use at execution time
    scan_map = {c["symbol"]: c for c in candidates}
    for p in scored:
        scan_data = scan_map.get(p["symbol"], {})
        for k, v in scan_data.items():
            p.setdefault(k, v)

    save_candidates(scored)
    log_audit("PRESCAN_DONE", details={
        "total":     len(scored),
        "tradeable": len(tradeable),
        "watchlist": len(watchlist),
    })


def _scan_and_trade(paper_mode: bool = False):
    """Load prescan candidates, re-validate prices and risk, then execute (or simulate)."""
    if not _market_open():
        print("   Market is closed — skipping scan.")
        return

    ok, portfolio, reason = can_trade()
    if not ok:
        print(f"   [RISK] Cannot trade: {reason}")
        log_audit("TRADE_BLOCKED", details={"reason": reason})
        return

    held = open_symbols()

    prescan = load_valid_candidates()
    if prescan:
        candidates = [c for c in prescan if c.get("tradeable") and c["symbol"] not in held]
        if not candidates:
            print("   No tradeable prescan candidates available.")
            return
        print(f"   Using {len(candidates)} prescan candidate(s).")
    else:
        print("   No valid prescan found — running fresh scan...")
        fresh     = scan_for_candidates()
        filtered  = [c for c in fresh if c["symbol"] not in held]
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

        risk_ok, risk_reason = check_candidate_risk(pick, portfolio, prescan_price)
        if not risk_ok:
            print(f"   [SKIP] {symbol}: {risk_reason}")
            log_audit("TRADE_REJECTED", symbol, {"score": score, "reason": risk_reason})
            continue

        price  = pick["price"]
        shares = position_size(portfolio, price)
        cost   = shares * price

        if paper_mode:
            print(f"   [PAPER] WOULD BUY {symbol} (score {score}/100): {reasoning}")
            print(f"   [PAPER] {shares} shares @ ${price:.2f} = ${cost:,.0f} | stop -1.5% | target +3%")
            log_paper_trade(symbol, shares, price, "BUY", score, reasoning)
            log_audit("PAPER_TRADE", symbol, {"score": score, "shares": shares, "price": price})
        else:
            print(f"   BUY {symbol} (score {score}/100): {reasoning}")
            print(f"   {shares} shares @ ${price:.2f} = ${cost:,.0f} | stop -1.5% | target +3%")
            place_bracket_order(symbol, shares, price)
            log_audit("ORDER_PLACED", symbol, {"score": score, "shares": shares, "price": price, "cost": cost})


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
            print(f"    {sym}: {shares} shares | ${entry:.2f} -> ${exit_p:.2f} "
                  f"| ${pnl:+.2f} ({pnl_pct:+.1f}%)")
            total += pnl
        print(f"\n  Closed P&L today: ${total:+.2f}")
    else:
        print("\n  No closed trades recorded today.")

    stats = all_time_summary()
    if stats["trades"]:
        print(f"\n  ALL TIME: {stats['trades']} trades | "
              f"${stats['total_pnl']:+.2f} total | "
              f"{stats['win_rate']:.0f}% win rate")

    print(f"{'='*60}\n")


if __name__ == "__main__":
    init_db()

    parser = argparse.ArgumentParser()
    parser.add_argument("--prescan", action="store_true", help="Discover & score candidates, NO orders (9:45am)")
    parser.add_argument("--morning", action="store_true", help="Alias for --prescan (backwards compat)")
    parser.add_argument("--scan",    action="store_true", help="Load prescan candidates and execute orders (10:00am+)")
    parser.add_argument("--paper",   action="store_true", help="Simulate scan/execute without placing real orders")
    parser.add_argument("--close",   action="store_true", help="Force-close all positions (3:45pm ET)")
    parser.add_argument("--report",  action="store_true", help="Daily P&L report (4:30pm ET)")
    parser.add_argument("--status",  action="store_true", help="Show current positions and P&L")
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

    elif args.close:
        _header("FORCE CLOSE — 3:45pm ET")
        close_all_positions()
        log_audit("FORCE_CLOSE")
        print()
        _status()

    elif args.report:
        _header("DAILY REPORT")
        _report()

    elif args.status:
        _header("STATUS")
        _status()

    else:
        print("Usage: python agent.py --prescan | --scan | --paper | --close | --report | --status")
