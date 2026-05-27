"""
Day Trading Agent — intraday momentum, all positions closed by 3:45pm ET.

Modes:
  --research    9:00am ET  — pre-market fundamentals + Claude brief for full watchlist
  --prescan     9:45am ET  — discover & score candidates, save to JSON, NO orders
  --morning     alias for --prescan (backwards compat)
  --scan        10:00am+   — load prescan candidates, validate, execute
  --paper       simulate full --scan logic without placing real orders
  --monitor     every 2 min — check trailing stops, time exits, momentum flips
  --close       3:45pm ET  — force-close all positions
  --report      4:00pm ET  — basic P&L from Alpaca
  --performance 4:15pm ET  — full analytics dashboard (expectancy, PF, windows)
  --status      any time   — current positions and P&L
"""
import argparse
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

# Ensure UTF-8 output on Windows so Unicode symbols in print() don't crash
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from alpaca.trading.client import TradingClient
from config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, PAPER_TRADING,
    MIN_SCORE_TO_TRADE, CHOPPY_MIN_SCORE, WATCHLIST_SCORE, KILL_SWITCH,
    BLOCK_MIDDAY, BLOCK_MIDDAY_START, BLOCK_MIDDAY_END,
    MIN_MOMENTUM_TO_TRADE,
    LOW_VOLUME_MIN_SCORE, LOW_VOLUME_MAX_TRADES,
    LOW_VOLUME_STOCK_RVOL, LOW_VOLUME_EXCEPTIONAL_SCORE, LOW_VOLUME_EXCEPTIONAL_NEWS,
    CHAOS_LOCKOUT_END_ET,
    TIER_HIGH_MIN, TIER_ELITE_MIN, ELITE_SIZE_BOOST, MAX_POSITION_SIZE_PCT,
    TAKE_PROFIT_PCT,
)
from scanner import scan_for_candidates
from analyst import analyse_candidates
from executor import place_bracket_order, close_all_positions
from risk import (
    can_trade, check_candidate_risk, position_size, open_symbols,
    get_setup_tier, check_volatility_extension, atr_aware_stop_pct,
    detect_failed_breakout,
)
from logger import init_db, log_audit, log_paper_trade, today_summary, all_time_summary, log_telemetry
from candidates import save_candidates, load_valid_candidates
from regime import detect_regime, is_tradeable, get_regime_context, LOW_VOLUME, CHOPPY
from sizing import dynamic_position_size
from exits import record_entry, monitor_positions
from momentum import analyse_momentum, STRENGTHENING, STABLE
from intraday import get_intraday_alignment, is_aligned_for_longs
from shared_lock import is_symbol_taken, claim_symbol, refresh_symbols
from risk import suggested_stop_pct
from orb import get_orb_status
from pullback import check_pullback_entry
from shortlist_monitor import add_to_shortlist, monitor_shortlist, clear_shortlist
from performance import (
    generate_daily_performance, print_performance_report,
    should_pause_trading, get_recent_performance,
    get_weak_windows, get_expectancy_by_dimension,
)
from feed_health import run_health_check, log_health_event
from polygon_feed import validate_cross_provider, detect_alpaca_subscription
from massive_feed import (
    stream_quotes_burst, get_intraday_quality,
    active_providers,
)
from feed_logger import log_trade_feed_inputs, log_feed_event, \
    generate_feed_quality_report, print_feed_quality_report
from event_risk import check_earnings, check_halt

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


def _run_feed_health(paper_mode: bool) -> tuple[bool, bool]:
    """
    Run feed health check. Print summary. Return (healthy, forced_paper).
    forced_paper=True means live trading was downgraded to paper.
    """
    healthy, issues, status = run_health_check()
    sub = detect_alpaca_subscription()
    if sub != "SIP":
        print(f"   [FEED] Alpaca subscription: {sub} (no real-time SIP data)")
    if issues:
        for issue in issues:
            print(f"   [FEED HEALTH] {issue}")
    if not healthy and not paper_mode:
        print(f"   [FEED HEALTH] Critical failure — downgrading to PAPER mode")
        return False, True
    return healthy, False


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

    # Feed health check (non-blocking for prescan — we never place orders here)
    _run_feed_health(paper_mode=True)

    # Regime check
    regime, regime_reason = detect_regime()
    print(f"   Regime : {regime} — {regime_reason}")
    log_audit("REGIME_DETECTED", details={"regime": regime, "reason": regime_reason})

    if not is_tradeable(regime):
        print(f"   [NO TRADE] Regime {regime!r} not in tradeable set — prescan aborted.")
        log_audit("PRESCAN_SKIPPED", details={"reason": f"regime={regime}"})
        return

    low_vol_mode = (regime == LOW_VOLUME)
    if low_vol_mode:
        ctx = get_regime_context()
        print(f"   [LOW_VOLUME] REDUCED_RISK mode active — restrictions at scan time:")
        print(f"     score >= {LOW_VOLUME_MIN_SCORE}  |  stock RVOL >= {LOW_VOLUME_STOCK_RVOL}x  "
              f"|  max {LOW_VOLUME_MAX_TRADES} trade(s)  |  size -50%")
        print(f"     vix={ctx.get('vix','?')}  vol_10d={ctx.get('vol_ratio_10d','?')}  "
              f"vol_weekday={ctx.get('vol_ratio_weekday','?')}  "
              f"baseline={ctx.get('vol_baseline','?')}")
        log_audit("LOW_VOLUME_MODE", details={
            "restrictions": {
                "min_score":   LOW_VOLUME_MIN_SCORE,
                "min_rvol":    LOW_VOLUME_STOCK_RVOL,
                "max_trades":  LOW_VOLUME_MAX_TRADES,
                "size_cut":    "50%",
            },
            **ctx,
        })

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

    scored = analyse_candidates(candidates)

    # In CHOPPY regime lower the bar slightly — flat markets produce fewer high-scorers
    if regime == CHOPPY:
        for p in scored:
            if CHOPPY_MIN_SCORE <= p.get("score", 0) < MIN_SCORE_TO_TRADE:
                p["tradeable"] = True
                p["watchlist"] = False

    tradeable = [p for p in scored if p.get("tradeable")]
    watchlist = [p for p in scored if p.get("watchlist")]

    print(f"\n   PRESCAN RESULTS (regime: {regime}):")
    print(f"   Tradeable (score >= {MIN_SCORE_TO_TRADE}): {len(tradeable)}")
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

    # Feed health check — may force-downgrade live session to paper
    _, forced_paper = _run_feed_health(paper_mode)
    if forced_paper:
        paper_mode = True

    # Opening chaos lockout — no entries before 9:45 ET
    now_et      = datetime.now(ET)
    lockout_end = now_et.replace(
        hour=CHAOS_LOCKOUT_END_ET[0], minute=CHAOS_LOCKOUT_END_ET[1], second=0, microsecond=0
    )
    if now_et < lockout_end:
        print(f"   [LOCKOUT] Opening chaos lockout active until "
              f"{CHAOS_LOCKOUT_END_ET[0]}:{CHAOS_LOCKOUT_END_ET[1]:02d} ET — no new entries")
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

    low_vol_mode = (regime == LOW_VOLUME)
    if low_vol_mode:
        ctx = get_regime_context()
        print(f"   [LOW_VOLUME] REDUCED_RISK: score>={LOW_VOLUME_MIN_SCORE}  "
              f"RVOL>={LOW_VOLUME_STOCK_RVOL}x  max {LOW_VOLUME_MAX_TRADES} trade  size-50%")
        log_audit("LOW_VOLUME_MODE", details={"restrictions": {
            "min_score":  LOW_VOLUME_MIN_SCORE,
            "min_rvol":   LOW_VOLUME_STOCK_RVOL,
            "max_trades": LOW_VOLUME_MAX_TRADES,
            "size_cut":   "50%",
        }, **ctx})

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

    # Pre-warm WS cache with a burst streaming session for candidate symbols
    # before entering the per-symbol loop (non-blocking if no key configured)
    _candidate_symbols = [
        c["symbol"] for c in (prescan or []) if c.get("tradeable")
    ]
    if _candidate_symbols and active_providers():
        print(f"   Streaming quotes for {len(_candidate_symbols)} candidate(s) "
              f"({active_providers()[0]})...")
        stream_quotes_burst(_candidate_symbols)
    if prescan:
        candidates = [c for c in prescan if c.get("tradeable") and c["symbol"] not in held]
        if not candidates:
            print("   No tradeable prescan candidates available.")
            return
        # Re-score with live data to drop any candidates that have faded since prescan
        print(f"   Re-scoring {len(candidates)} prescan candidate(s) with live data...")
        candidates = analyse_candidates(candidates)
        candidates = [c for c in candidates if c.get("tradeable") and c["symbol"] not in held]
        if not candidates:
            print("   No candidates passed re-score threshold.")
            return
        print(f"   {len(candidates)} candidate(s) passed re-score | WR={recent_wr:.0%} | "
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

    # Add tradeable candidates to the shortlist before processing
    try:
        clear_shortlist()
        for c in candidates:
            add_to_shortlist(c)
    except Exception:
        pass

    # Check shortlist for readiness signals (informational only in scan mode)
    shortlist_ready: dict[str, dict] = {}
    try:
        ready_entries = monitor_shortlist()
        for r in ready_entries:
            shortlist_ready[r["symbol"]] = r
        if shortlist_ready:
            print(f"   [SHORTLIST] {len(shortlist_ready)} candidate(s) flagged ready: "
                  f"{', '.join(shortlist_ready.keys())}")
    except Exception:
        pass

    trades_this_scan = 0
    for pick in candidates:
        ok, portfolio, reason = can_trade()
        if not ok:
            print(f"   [RISK] {reason} — stopping.")
            break

        # LOW_VOLUME cap — stop after max allowed trades for this scan cycle
        if low_vol_mode and trades_this_scan >= LOW_VOLUME_MAX_TRADES:
            print(f"   [LOW_VOLUME] {LOW_VOLUME_MAX_TRADES} trade cap reached — stopping scan.")
            log_audit("SCAN_CAPPED", details={"reason": "low_volume_max_trades"})
            break

        symbol        = pick["symbol"]
        score         = pick.get("score", 0)
        reasoning     = pick.get("reasoning", "")
        prescan_price = pick.get("price")
        vol_pct       = pick.get("volatility_pct", 0.0)
        spread_pct    = pick.get("spread_pct", 0.0)
        setup_type    = pick.get("setup_type", "unknown")

        # Timestamps for telemetry
        signal_ts   = pick.get("shortlisted_at") or pick.get("_prescan_ts")
        decision_ts = datetime.now(ET).isoformat()

        # Tier classification (spec point 5)
        tier = get_setup_tier(score)

        # Cross-agent duplicate check — block if IBKR already holds this symbol
        taken, holder = is_symbol_taken(symbol)
        if taken:
            print(f"   [SKIP] {symbol}: already held by {holder} agent — duplicate exposure blocked")
            log_audit("TRADE_REJECTED", symbol, {
                "score": score, "reason": f"duplicate_exposure:{holder}",
                "setup_type": setup_type,
            })
            continue

        # Event risk: hard-block on imminent earnings or halt
        earn_block, earn_desc = check_earnings(symbol)
        if earn_block:
            print(f"   [SKIP] {symbol}: event risk — {earn_desc}")
            log_audit("TRADE_REJECTED", symbol, {
                "score": score, "reason": f"earnings_block: {earn_desc}",
                "setup_type": setup_type,
            })
            continue
        halted, halt_desc = check_halt(symbol)
        if halted:
            print(f"   [SKIP] {symbol}: {halt_desc}")
            log_audit("TRADE_REJECTED", symbol, {
                "score": score, "reason": f"halt: {halt_desc}",
                "setup_type": setup_type,
            })
            continue

        # LOW_VOLUME confidence gate — require higher score than normal
        if low_vol_mode and score < LOW_VOLUME_MIN_SCORE:
            print(f"   [SKIP] {symbol}: LOW_VOLUME mode requires score>={LOW_VOLUME_MIN_SCORE} "
                  f"(got {score})")
            log_audit("TRADE_REJECTED", symbol, {
                "score": score,
                "reason": f"low_volume_min_score: {score}<{LOW_VOLUME_MIN_SCORE}",
                "setup_type": setup_type,
            })
            continue

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

        # Fetch 1-min bars (needed for ORB, pullback, ATR, failed-breakout checks)
        from momentum import _get_1min_bars
        bars_1min = []
        try:
            bars_1min = _get_1min_bars(symbol, 30)   # 30 bars covers ORB + ATR
        except Exception:
            pass

        # ORB check (spec point 3) — informational quality signal, not a blocker
        orb_status: dict = {}
        try:
            orb_status = get_orb_status(bars_1min)
        except Exception:
            orb_status = {"orb_breakout": False}
        orb_breakout = orb_status.get("orb_breakout", False)
        print(f"   ORB {symbol}: breakout={'YES' if orb_breakout else 'no'} "
              f"[5m={orb_status.get('orb_5min', {}).get('is_orb_breakout', False)} "
              f"15m={orb_status.get('orb_15min', {}).get('is_orb_breakout', False)}]")

        # Volatility extension check (spec point 6) — ATR-aware, replaces fixed check when bars available
        price = pick["price"]
        vwap  = pick.get("vwap", 0.0)
        vol_extended, vol_ext_reason = check_volatility_extension(price, vwap, bars_1min)
        if vol_extended:
            print(f"   [SKIP] {symbol}: {vol_ext_reason}")
            log_audit("TRADE_REJECTED", symbol, {
                "score": score, "reason": f"vol_extension: {vol_ext_reason}",
                "setup_type": setup_type,
            })
            continue

        # Failed breakout detection (spec point 8)
        breakout_price = pick.get("open_price", price)
        fb_failed, fb_reason = detect_failed_breakout(bars_1min, breakout_price)
        if fb_failed:
            print(f"   [SKIP] {symbol}: failed breakout — {fb_reason}")
            log_audit("TRADE_REJECTED", symbol, {
                "score": score, "reason": f"failed_breakout: {fb_reason}",
                "setup_type": setup_type,
            })
            continue

        # Pullback check (spec point 2) — only for HIGH and ELITE tiers
        pullback_result: dict = {"pullback_quality": "N/A", "pullback_detected": False}
        if tier in ("HIGH", "ELITE") and bars_1min:
            try:
                pullback_result = check_pullback_entry(pick, bars_1min)
            except Exception:
                pass

            if pullback_result.get("reject"):
                print(f"   [SKIP] {symbol}: pullback failed — {pullback_result['pullback_reason']}")
                log_audit("TRADE_REJECTED", symbol, {
                    "score": score, "reason": f"pullback_reject: {pullback_result['pullback_reason']}",
                    "setup_type": setup_type, "tier": tier,
                })
                continue

            if pullback_result.get("should_wait"):
                print(f"   [WAIT] {symbol}: {tier} tier — waiting for pullback "
                      f"({pullback_result['pullback_reason']})")
                log_audit("TRADE_WAITING", symbol, {
                    "score": score, "reason": pullback_result["pullback_reason"],
                    "tier": tier,
                })
                continue

            pq = pullback_result.get("pullback_quality", "NONE")
            print(f"   Pullback {symbol}: {pq} — {pullback_result.get('pullback_reason', '')}")

        # ATR-aware stop (spec point 7)
        atr_stop = atr_aware_stop_pct(bars_1min, price)
        stop_pct = atr_stop if bars_1min else suggested_stop_pct(mom["strength"], regime)
        # Take profit = max(TAKE_PROFIT_PCT, 2x stop) to maintain R/R >= 1
        tp_pct   = max(TAKE_PROFIT_PCT, 2.0 * stop_pct)

        shares, size_pct, size_note = dynamic_position_size(
            portfolio, price, score, vol_pct, spread_pct, regime, recent_wr
        )

        # ELITE size boost (spec point 5) — only if spread tight and market aligned
        if tier == "ELITE" and spread_pct < 0.15 and alignment in ("BULLISH", "STRONG_BULLISH", "ALIGNED"):
            boosted_pct = min(MAX_POSITION_SIZE_PCT, size_pct * (1 + ELITE_SIZE_BOOST))
            if boosted_pct > size_pct:
                shares   = max(1, int(portfolio * boosted_pct / price))
                size_pct = boosted_pct
                size_note += f" | ELITE+{ELITE_SIZE_BOOST:.0%}"

        cost = shares * price

        # Intraday quality gate — RVOL, VWAP distance, spread stability, exhaustion
        now_et_inner  = datetime.now(ET)
        mins_elapsed  = max(1, (now_et_inner.hour * 60 + now_et_inner.minute) - (9 * 60 + 30))
        iq = get_intraday_quality(
            symbol, price, spread_pct,
            volume=pick.get("today_volume", 0),
            avg_daily_volume=0,   # 0 = RVOL skipped (scanner has it via yfinance)
            mins_elapsed=mins_elapsed,
            baseline_spread=spread_pct,
        )
        print(f"   Quality {symbol}: score={iq['score']}/100  {iq['reason']}  [{iq['data_source']}]")
        if not iq["ok"]:
            print(f"   [SKIP] {symbol}: intraday quality {iq['score']}/100 — {iq['reason']}")
            log_feed_event("TRADE_REJECTED_DATA", {
                "symbol": symbol,
                "reason": f"quality_score={iq['score']}: {iq['reason']}",
            })
            log_audit("TRADE_REJECTED", symbol, {
                "score": score,
                "reason": f"intraday_quality: {iq['reason']}",
                "setup_type": setup_type,
            })
            continue

        # LOW_VOLUME stock-level RVOL gate
        # Exceptional candidate (very high score + strong news) bypasses RVOL check
        if low_vol_mode and iq["rvol"] > 0:
            top_news_impact = pick.get("_top_news_impact", 0)
            is_exceptional  = (
                score >= LOW_VOLUME_EXCEPTIONAL_SCORE
                or top_news_impact >= LOW_VOLUME_EXCEPTIONAL_NEWS
            )
            if not is_exceptional and iq["rvol"] < LOW_VOLUME_STOCK_RVOL:
                print(f"   [SKIP] {symbol}: LOW_VOLUME stock RVOL {iq['rvol']:.1f}x "
                      f"< {LOW_VOLUME_STOCK_RVOL}x required (score={score}, "
                      f"news_impact={top_news_impact})")
                log_audit("TRADE_REJECTED", symbol, {
                    "score": score,
                    "reason": (f"low_volume_stock_rvol: {iq['rvol']:.1f}x"
                               f"<{LOW_VOLUME_STOCK_RVOL}x"),
                    "setup_type": setup_type,
                })
                continue
            if is_exceptional and iq["rvol"] < LOW_VOLUME_STOCK_RVOL:
                print(f"   [LOW_VOLUME] {symbol}: RVOL {iq['rvol']:.1f}x below threshold "
                      f"but EXCEPTIONAL candidate (score={score}, "
                      f"news={top_news_impact}) — allowing")

        # Cross-provider quote validation — compare Alpaca vs Massive/Polygon before executing
        quote_ok, quote_reason, poly_quote = validate_cross_provider(
            symbol, price, spread_pct, pick.get("today_volume", 0)
        )
        if not quote_ok:
            print(f"   [DATA QUALITY] {symbol}: {quote_reason}")
            log_feed_event("TRADE_REJECTED_DATA", {"symbol": symbol, "reason": quote_reason})
            log_audit("TRADE_REJECTED", symbol, {
                "score": score, "reason": f"data_quality: {quote_reason}",
                "setup_type": setup_type,
            })
            continue
        if "volume_divergence" in quote_reason:
            log_feed_event("VOLUME_DIVERGENCE", {"symbol": symbol, "detail": quote_reason})

        audit_details = {
            "score":            score,
            "tier":             tier,
            "shares":           shares,
            "price":            price,
            "cost":             round(cost, 2),
            "size_pct":         round(size_pct, 3),
            "sizing":           size_note,
            "regime":           regime,
            "alignment":        alignment,
            "momentum":         mom["strength"],
            "setup_type":       setup_type,
            "stop_pct":         round(stop_pct, 4),
            "atr_stop_pct":     round(atr_stop, 5),
            "orb_breakout":     orb_breakout,
            "pullback_quality": pullback_result.get("pullback_quality", "N/A"),
            "vwap":             vwap,
            "spread_pct":       spread_pct,
        }

        # Log all feed inputs used for this decision
        data_sources = ["alpaca", "finnhub"]
        if poly_quote:
            data_sources.append(poly_quote.get("provider", "secondary"))
        if iq["data_source"] not in ("alpaca_only", "none"):
            if iq.get("provider") and iq["provider"] not in data_sources:
                data_sources.append(iq["provider"])
        log_trade_feed_inputs(symbol, {
            "score":                 score,
            "tier":                  tier,
            "regime":                regime,
            "alignment":             alignment,
            "momentum":              mom["strength"],
            "alpaca_price":          price,
            "alpaca_spread_pct":     spread_pct,
            "secondary_quote":       poly_quote,
            "quote_ok":              quote_ok,
            "quote_validation_done": True,
            "intraday_quality":      iq,
            "news_count":            len(pick.get("news", [])),
            "top_news_impact":       pick.get("_top_news_impact", 0),
            "setup_type":            setup_type,
            "data_sources":          data_sources,
            "paper_mode":            paper_mode,
            "orb_breakout":          orb_breakout,
            "pullback_quality":      pullback_result.get("pullback_quality", "N/A"),
            "atr_stop_pct":          round(atr_stop, 5),
        })

        submit_ts = datetime.now(ET).isoformat()

        if paper_mode:
            print(f"   [PAPER] WOULD BUY {symbol}  score={score}/100  tier={tier}  "
                  f"{size_note}  stop={stop_pct:.1%}  setup={setup_type}  "
                  f"orb={'Y' if orb_breakout else 'n'}  pullback={pullback_result.get('pullback_quality','N/A')}")
            print(f"   [PAPER] {shares}sh @ ${price:.2f} = ${cost:,.0f} | {reasoning}")
            log_paper_trade(symbol, shares, price, "BUY", score, reasoning)
            log_audit("PAPER_TRADE", symbol, audit_details)
        else:
            print(f"   BUY {symbol}  score={score}/100  tier={tier}  {size_note}  "
                  f"stop={stop_pct:.1%}  setup={setup_type}  "
                  f"orb={'Y' if orb_breakout else 'n'}  pullback={pullback_result.get('pullback_quality','N/A')}")
            print(f"   {shares}sh @ ${price:.2f} = ${cost:,.0f} | {reasoning}")
            order = place_bracket_order(
                symbol, shares, price,
                score=score, size_pct=size_pct, sizing_note=size_note,
                stop_pct=stop_pct, take_profit_pct=tp_pct,
            )
            record_entry(symbol, price)
            claim_symbol(symbol)
            log_audit("ORDER_PLACED", symbol, audit_details)

            # Execution telemetry (spec point 9)
            fill_price = None
            try:
                fill_price = float(order.filled_avg_price) if hasattr(order, "filled_avg_price") and order.filled_avg_price else None
            except Exception:
                pass
            slippage_pct = None
            if fill_price and price > 0:
                slippage_pct = round((fill_price - price) / price * 100, 5)
            log_telemetry({
                "symbol":                symbol,
                "signal_ts":             signal_ts,
                "decision_ts":           decision_ts,
                "submit_ts":             submit_ts,
                "signal_price":          prescan_price,
                "decision_price":        price,
                "submitted_limit_price": round(price * 1.001, 4),
                "fill_price":            fill_price,
                "slippage_pct":          slippage_pct,
                "spread_at_decision":    spread_pct,
                "score":                 score,
                "tier":                  tier,
                "orb_breakout":          orb_breakout,
                "pullback_quality":      pullback_result.get("pullback_quality", "N/A"),
                "atr_stop_pct":          round(atr_stop, 5),
            })

        trades_this_scan += 1


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
    parser.add_argument("--monitor",     action="store_true", help="Check trailing stops and advanced exits (every 2 min)")
    parser.add_argument("--close",       action="store_true", help="Force-close all positions (3:45pm ET)")
    parser.add_argument("--report",      action="store_true", help="Basic P&L report (4:00pm ET)")
    parser.add_argument("--performance", action="store_true", help="Full analytics dashboard (4:15pm ET)")
    parser.add_argument("--feedreport",  action="store_true", help="Feed quality report — provider health, mismatches, rejections")
    parser.add_argument("--research",    action="store_true", help="Pre-market research — fundamentals + Claude brief for all watchlist symbols (9:00am ET)")
    parser.add_argument("--verify",      action="store_true", help="Emergency flatness check at 3:55pm ET — close anything still open")
    parser.add_argument("--status",      action="store_true", help="Current positions and P&L")
    args = parser.parse_args()

    if args.research:
        _header("PRE-MARKET RESEARCH")
        from research import run_premarket_research
        run_premarket_research()

    elif args.prescan or args.morning:
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
        # Shortlist readiness check — informational only, no orders placed in monitor mode
        try:
            ready = monitor_shortlist()
            if ready:
                print(f"\n   [SHORTLIST] {len(ready)} candidate(s) now ready for entry "
                      f"(will execute in next --scan): "
                      f"{', '.join(r['symbol'] for r in ready)}")
            else:
                print("   [SHORTLIST] No shortlisted candidates ready for entry.")
        except Exception:
            pass

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
        refresh_symbols(set())
        log_audit("EOD_VERIFIED", details={
            "positions": len(remaining),
            "orders":    len(open_ords),
        })
        print()
        _status()

    elif args.verify:
        _header("EMERGENCY VERIFY — 3:55pm ET flatness check")
        import time
        client    = _client()
        remaining = client.get_all_positions()
        open_ords = client.get_orders()
        if remaining or open_ords:
            print(f"   [VERIFY] {len(remaining)} position(s), {len(open_ords)} order(s) still open — closing...")
            if open_ords:
                client.cancel_orders()
                time.sleep(1)
            for p in remaining:
                try:
                    client.close_position(p.symbol)
                    print(f"   [CLOSED] {p.symbol}")
                except Exception as e:
                    print(f"   [ERROR] {p.symbol}: {e}")
            time.sleep(3)
            remaining = client.get_all_positions()
            open_ords = client.get_orders()
        print(f"\n  EOD_VERIFY_355:")
        print(f"  Open positions : {len(remaining)}")
        print(f"  Open orders    : {len(open_ords)}")
        if not remaining and not open_ords:
            print("  Fully flat — no overnight exposure")
        else:
            print("  WARNING: still have open items — check immediately!")
        refresh_symbols(set())
        log_audit("EOD_VERIFY_355", details={
            "positions": len(remaining),
            "orders":    len(open_ords),
        })

    elif args.report:
        _header("DAILY REPORT")
        _report()

    elif args.performance:
        _header("PERFORMANCE DASHBOARD")
        perf = generate_daily_performance()
        print_performance_report(perf)
        # Append feed quality summary after performance report
        feed_rpt = generate_feed_quality_report()
        print_feed_quality_report(feed_rpt)

    elif args.feedreport:
        _header("FEED QUALITY REPORT")
        feed_rpt = generate_feed_quality_report()
        print_feed_quality_report(feed_rpt)

    elif args.status:
        _header("STATUS")
        _status()

    else:
        print("Usage: python agent.py "
              "--research | --prescan | --scan | --paper | --monitor | "
              "--close | --report | --performance | --feedreport | --status")
