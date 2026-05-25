"""
Day Trading Agent — intraday momentum, all positions closed by 3:45pm ET.

Modes:
  --morning   9:45am ET  — first scan after opening volatility settles
  --scan      every 30min 10am-3pm — check for new entries
  --close     3:45pm ET  — force-close all positions, no overnight risk
  --report    4:30pm ET  — daily P&L summary
  --status    any time   — show current positions and today's P&L
"""
import argparse
from datetime import datetime
from zoneinfo import ZoneInfo

from alpaca.trading.client import TradingClient
from config import ALPACA_API_KEY, ALPACA_SECRET_KEY
from scanner import scan_for_candidates
from analyst import analyse_candidates
from executor import place_bracket_order, close_all_positions
from risk import can_trade, position_size, open_symbols
from logger import init_db, today_summary, all_time_summary

ET = ZoneInfo("America/New_York")


def _client() -> TradingClient:
    return TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)


def _market_open() -> bool:
    clock = _client().get_clock()
    return clock.is_open


def _header(mode: str):
    now = datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")
    print(f"\n{'='*54}")
    print(f"  LEELA DAY TRADING AGENT [PAPER] -- {now}")
    print(f"  Mode: {mode}")
    print(f"{'='*54}\n")


def _status():
    client    = _client()
    acct      = client.get_account()
    portfolio = float(acct.portfolio_value)
    start     = float(acct.last_equity)
    daily_pnl = portfolio - start
    daily_pct = daily_pnl / start * 100

    print(f"  Portfolio  : ${portfolio:,.2f}")
    print(f"  Today P&L  : ${daily_pnl:+,.2f} ({daily_pct:+.2f}%)")

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


def _scan_and_trade():
    if not _market_open():
        print("   Market is closed — skipping scan.")
        return

    ok, portfolio = can_trade()
    if not ok:
        return

    held = open_symbols()
    candidates = [c for c in scan_for_candidates() if c["symbol"] not in held]
    if not candidates:
        print("   No new momentum candidates.")
        return

    picks = analyse_candidates(candidates)
    if not picks:
        print("   Claude: no high-confidence setups right now.")
        return

    for pick in picks:
        ok, portfolio = can_trade()
        if not ok:
            break

        symbol     = pick["symbol"]
        confidence = pick["confidence"]
        reasoning  = pick["reasoning"]
        cand       = next((c for c in candidates if c["symbol"] == symbol), None)
        if not cand:
            continue

        price  = cand["price"]
        shares = position_size(portfolio, price)
        cost   = shares * price

        print(f"   BUY {symbol} (confidence {confidence}/10): {reasoning}")
        print(f"   [RISK] {symbol}: {shares} shares @ ${price:.2f} = ${cost:,.0f} "
              f"| stop -1.5% | target +3%")
        place_bracket_order(symbol, shares, price)


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

    print(f"{'='*54}\n")


if __name__ == "__main__":
    init_db()

    parser = argparse.ArgumentParser()
    parser.add_argument("--morning", action="store_true", help="Morning scan (9:45am ET)")
    parser.add_argument("--scan",    action="store_true", help="Mid-day scan (every 30 min)")
    parser.add_argument("--close",   action="store_true", help="Force close all (3:45pm ET)")
    parser.add_argument("--report",  action="store_true", help="Daily P&L report (4:30pm ET)")
    parser.add_argument("--status",  action="store_true", help="Current positions and P&L")
    args = parser.parse_args()

    if args.morning:
        _header("MORNING SCAN")
        _status()
        print()
        _scan_and_trade()

    elif args.scan:
        _header("MID-DAY SCAN")
        _status()
        print()
        _scan_and_trade()

    elif args.close:
        _header("FORCE CLOSE — 3:45pm ET")
        close_all_positions()
        print()
        _status()

    elif args.report:
        _header("DAILY REPORT")
        _report()

    elif args.status:
        _header("STATUS")
        _status()

    else:
        print("Usage: python agent.py --morning | --scan | --close | --report | --status")
