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
    ALPACA_API_KEY, ALPACA_SECRET_KEY, PAPER_TRADING, TRADING_MODE,
    MIN_SCORE_TO_TRADE, CHOPPY_MIN_SCORE, WATCHLIST_SCORE, KILL_SWITCH,
    BLOCK_MIDDAY, BLOCK_MIDDAY_START, BLOCK_MIDDAY_END,
    MIN_MOMENTUM_TO_TRADE,
    LOW_VOLUME_MIN_SCORE, LOW_VOLUME_MAX_TRADES,
    LOW_VOLUME_STOCK_RVOL, LOW_VOLUME_EXCEPTIONAL_SCORE, LOW_VOLUME_EXCEPTIONAL_NEWS,
    HIGH_VOL_MIN_SCORE, HIGH_VOL_MAX_TRADES, HIGH_VOL_STOCK_RVOL,
    HIGH_VOL_SIZE_CUT, HIGH_VOL_MIN_SCORE_EXTRA, HIGH_VOL_ALLOWED_SETUPS,
    CHAOS_LOCKOUT_END_ET,
    TIER_HIGH_MIN, TIER_ELITE_MIN, ELITE_SIZE_BOOST, MAX_POSITION_SIZE_PCT,
    MIN_POSITION_SIZE_PCT, DAILY_LOSS_LIMIT, MAX_POSITIONS,
    TAKE_PROFIT_PCT,
    get_min_score,
    LIVE_BASE_SCORE,
    DECAY_BAND_1_MINS, DECAY_BAND_1_POINTS,
    DECAY_BAND_2_MINS, DECAY_BAND_2_POINTS,
    DECAY_BAND_3_MINS, DECAY_BAND_3_POINTS,
    QUALITY_OVERRIDE_MIN_RVOL, QUALITY_OVERRIDE_MAX_SPREAD,
    QUALITY_OVERRIDE_NEWS_IMPACT, QUALITY_OVERRIDE_REQUIRE_ALL,
    QUALITY_OVERRIDE_MIN_CONDITIONS, QUALITY_OVERRIDE_MAX_GAP_PTS,
    PREFERRED_SPREAD_PCT, SPREAD_PENALTY_ABOVE, SPREAD_SIZE_PENALTY_PCT,
    HIGH_VOL_MODERATE_ATR_PCT, HIGH_VOL_MODERATE_EXTRA_PTS, HIGH_VOL_MODERATE_SIZE_CUT,
    LIMIT_OFFSET_TIGHT_PCT, LIMIT_OFFSET_NORMAL_PCT, LIMIT_OFFSET_WIDE_PCT, MAX_LIMIT_SLIPPAGE_PCT,
    LIVE_PROMOTED_SETUPS, LIVE_REQUIRE_PROMOTED_SETUPS,
    PAPER_EXPLORATORY_LOW_VOLUME_MIN_SCORE, PAPER_LIVE_REALISTIC_LOW_VOLUME_MIN_SCORE, LIVE_LOW_VOLUME_MIN_SCORE,
    EXTREME_HIGH_VOL_VIX, EXTREME_HIGH_VOL_ATR_PCT, EXTREME_HIGH_VOL_SPREAD_MULT,
    MAX_TRADES_PER_DAY, AUDIT_LOG_FILE,
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
from regime import detect_regime, is_tradeable, get_regime_context, LOW_VOLUME, CHOPPY, HIGH_VOL
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


# ── Mode helpers ──────────────────────────────────────────────────────────────

def _reject(symbol: str, score: int, setup_type: str, stage: str, rule: str,
            observed=None, threshold=None, is_experimental: bool = False):
    """Log a precise trade rejection with stage/rule/observed/threshold details."""
    log_audit("TRADE_REJECTED", symbol, {
        "score":           score,
        "setup_type":      setup_type,
        "trading_mode":    TRADING_MODE,
        "stage_rejected":  stage,
        "rule_failed":     rule,
        "observed_value":  observed,
        "threshold_value": threshold,
        "is_experimental": is_experimental,
    })


def _apply_score_decay(score: int, age_mins: float) -> tuple[int, int]:
    """Returns (decayed_score, decay_points). Decay increases with candidate age."""
    if age_mins <= DECAY_BAND_1_MINS:
        return score, 0
    elif age_mins <= DECAY_BAND_2_MINS:
        return score - DECAY_BAND_2_POINTS, DECAY_BAND_2_POINTS
    else:
        return score - DECAY_BAND_3_POINTS, DECAY_BAND_3_POINTS


def _quality_override(
    score: int, effective_min: int,
    rvol: float, spread_pct: float, news_impact: int,
    orb_breakout: bool, pullback_ok: bool,
    alignment: str, mom_ok: bool, no_failed_bo: bool,
) -> tuple[bool, str]:
    """
    Check whether a quality override permits a below-threshold candidate.
    PAPER: 5/7 conditions; LIVE: all 7 conditions.
    Returns (allowed, reason_string).
    """
    if score >= effective_min:
        return False, "already meets threshold"
    gap = effective_min - score
    if gap > QUALITY_OVERRIDE_MAX_GAP_PTS:
        return False, f"gap {gap}pts exceeds override ceiling"

    conditions = {
        "rvol_strong":   rvol >= QUALITY_OVERRIDE_MIN_RVOL,
        "tight_spread":  spread_pct <= QUALITY_OVERRIDE_MAX_SPREAD,
        "setup_confirm": orb_breakout or pullback_ok,
        "mkt_aligned":   alignment in ("BULLISH", "STRONG_BULLISH", "ALIGNED"),
        "news_strong":   news_impact >= QUALITY_OVERRIDE_NEWS_IMPACT,
        "momentum_ok":   mom_ok,
        "no_failed_bo":  no_failed_bo,
    }
    met    = sum(v for v in conditions.values())
    failed = [k for k, v in conditions.items() if not v]

    if QUALITY_OVERRIDE_REQUIRE_ALL:
        allowed = all(conditions.values())
    else:
        # PAPER 5/7 mode: RVOL, spread, and momentum are always mandatory
        mandatory_met = conditions["rvol_strong"] and conditions["tight_spread"] and conditions["momentum_ok"]
        allowed = met >= QUALITY_OVERRIDE_MIN_CONDITIONS and mandatory_met

    if allowed:
        return True, f"quality_override: {met}/7 conditions met"
    return False, f"quality_override_failed: {met}/7 — missing: {failed[:3]}"


def _score_band(score: int) -> str:
    if score >= 90: return "elite"
    if score >= 85: return "high"
    if score >= 78: return "normal"
    if score >= 70: return "below_live"
    return "experimental"


def _limit_offset(spread_pct: float) -> float:
    """Adaptive limit offset: tighter spread needs less aggressive overpay."""
    if spread_pct < 0.10:
        return LIMIT_OFFSET_TIGHT_PCT
    elif spread_pct <= 0.20:
        return LIMIT_OFFSET_NORMAL_PCT
    else:
        return LIMIT_OFFSET_WIDE_PCT


def _is_extreme_high_vol() -> tuple[bool, str]:
    """Returns (is_extreme, reason). Triggers when VIX + ATR both exceed extreme thresholds."""
    try:
        ctx     = get_regime_context()
        vix     = float(ctx.get("vix", 0) or 0)
        atr_pct = float(ctx.get("atr_pct", 0) or 0)
        if vix >= EXTREME_HIGH_VOL_VIX and atr_pct >= EXTREME_HIGH_VOL_ATR_PCT:
            return True, (f"extreme_high_vol: VIX={vix:.1f}>={EXTREME_HIGH_VOL_VIX}, "
                          f"ATR={atr_pct:.1f}%>={EXTREME_HIGH_VOL_ATR_PCT}%")
    except Exception:
        pass
    return False, ""


def _paper_category_report():
    """Read today's audit log and print paper trade category breakdown."""
    import sqlite3, json as _json
    from config import DB_PATH
    today = datetime.now(ET).strftime("%Y-%m-%d")
    try:
        con = sqlite3.connect(DB_PATH)
        rows = con.execute(
            "SELECT action, symbol, details FROM audit_log WHERE date=?", (today,)
        ).fetchall()
        con.close()
        live_realistic, exploratory, hypothetical = [], [], []
        claude_live = claude_exp = 0
        for action, sym, det_str in rows:
            if action == "PAPER_LIVE_REALISTIC":
                live_realistic.append(sym)
            elif action == "PAPER_EXPLORATORY_ONLY":
                exploratory.append(sym)
            elif action == "NO_TRADE_HYPOTHETICAL":
                hypothetical.append(sym)
            elif action == "PAPER_TRADE_TAGS":
                try:
                    d = _json.loads(det_str)
                    if d.get("claude_involved"):
                        if d.get("would_reject_live"):
                            claude_exp += 1
                        else:
                            claude_live += 1
                except Exception:
                    pass
        if live_realistic or exploratory or hypothetical:
            print(f"\n  PAPER CLASSIFICATION (today):")
            print(f"  Live-realistic    : {len(live_realistic)}  {live_realistic or '-'}")
            print(f"  Exploratory-only  : {len(exploratory)}  {exploratory or '-'}")
            print(f"  Hypothetical      : {len(hypothetical)}  {hypothetical or '-'}")
            if claude_live or claude_exp:
                print(f"  Claude live-ready : {claude_live}")
                print(f"  Claude exploratory: {claude_exp}")
    except Exception:
        pass


def _client() -> TradingClient:
    return TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=PAPER_TRADING)


def _pre_submit_check(symbol: str, intended_price: float, intended_spread: float,
                      portfolio: float) -> tuple[bool, str]:
    """Re-check 12 conditions immediately before order submission."""
    # 1. Halt status
    try:
        halted, halt_desc = check_halt(symbol)
        if halted:
            return False, f"halt: {halt_desc}"
    except Exception:
        pass

    # 2. Cross-agent lock re-check
    taken, holder = is_symbol_taken(symbol)
    if taken:
        return False, f"cross_agent_lock: claimed by {holder}"

    # 3–5. Portfolio state (daily loss limit, max positions, kill switch)
    ok, _, reason = can_trade()
    if not ok:
        return False, f"portfolio: {reason}"

    # 6. Market open
    if not _market_open():
        return False, "market_closed"

    # 7. Earnings re-check
    try:
        earn_block, earn_desc = check_earnings(symbol)
        if earn_block:
            return False, f"earnings_recheck: {earn_desc}"
    except Exception:
        pass

    # 8. Max trades today
    try:
        closed_today = len(today_summary())
        if closed_today >= MAX_TRADES_PER_DAY:
            return False, f"max_trades_today: {closed_today}/{MAX_TRADES_PER_DAY}"
    except Exception:
        pass

    try:
        client = _client()

        # 9. Duplicate open order check
        open_orders = client.get_orders()
        dupes = [o for o in open_orders if hasattr(o, "symbol") and o.symbol == symbol]
        if dupes:
            return False, f"duplicate_order: {len(dupes)} order(s) already open for {symbol}"

        # 10. Buying power minimum
        acct = client.get_account()
        bp   = float(acct.buying_power or 0)
        min_cost = intended_price * max(1, int(portfolio * MIN_POSITION_SIZE_PCT / max(intended_price, 0.01)))
        if bp < min_cost:
            return False, f"buying_power: ${bp:.0f} < min_cost ${min_cost:.0f}"

        # 11. Spread re-check (intended vs configured max)
        from config import MAX_SPREAD_PCT
        if intended_spread > MAX_SPREAD_PCT:
            return False, f"spread_widened: {intended_spread:.2f}% > {MAX_SPREAD_PCT}%"

        # 12. Price sanity (not aberrantly priced)
        if intended_price <= 0 or intended_price > 50_000:
            return False, f"price_invalid: ${intended_price:.2f}"

    except Exception:
        pass  # non-blocking — don't fail a good trade on check errors

    return True, "ok"


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
        if PAPER_TRADING:
            print(f"   [NO_TRADE_HYPOTHETICAL] Regime {regime!r} — prescan continuing in hypothetical mode")
            log_audit("PRESCAN_HYPOTHETICAL", details={"reason": f"regime={regime}", "mode": "hypothetical"})
        else:
            print(f"   [NO TRADE] Regime {regime!r} not in tradeable set — prescan aborted.")
            log_audit("PRESCAN_SKIPPED", details={"reason": f"regime={regime}"})
            return

    low_vol_mode  = (regime == LOW_VOLUME)
    high_vol_mode = (regime == HIGH_VOL)
    if TRADING_MODE == "LIVE":
        _lv_score_label = str(LIVE_LOW_VOLUME_MIN_SCORE)
        _lv_score_log   = LIVE_LOW_VOLUME_MIN_SCORE
    else:
        _lv_score_label = (f"{PAPER_EXPLORATORY_LOW_VOLUME_MIN_SCORE}(exp)/"
                           f"{PAPER_LIVE_REALISTIC_LOW_VOLUME_MIN_SCORE}(real)")
        _lv_score_log   = PAPER_EXPLORATORY_LOW_VOLUME_MIN_SCORE
    if low_vol_mode:
        ctx = get_regime_context()
        print(f"   [LOW_VOLUME] REDUCED_RISK mode active — restrictions at scan time:")
        print(f"     score >= {_lv_score_label} ({'live' if TRADING_MODE == 'LIVE' else 'paper'})  "
              f"|  stock RVOL >= {LOW_VOLUME_STOCK_RVOL}x  "
              f"|  max {LOW_VOLUME_MAX_TRADES} trade(s)  |  size -50%")
        print(f"     vix={ctx.get('vix','?')}  vol_10d={ctx.get('vol_ratio_10d','?')}  "
              f"vol_weekday={ctx.get('vol_ratio_weekday','?')}  "
              f"baseline={ctx.get('vol_baseline','?')}")
        log_audit("LOW_VOLUME_MODE", details={
            "restrictions": {
                "min_score":   _lv_score_log,
                "min_rvol":    LOW_VOLUME_STOCK_RVOL,
                "max_trades":  LOW_VOLUME_MAX_TRADES,
                "size_cut":    "50%",
                "mode":        TRADING_MODE,
            },
            **ctx,
        })
    elif high_vol_mode:
        ctx = get_regime_context()
        print(f"   [HIGH_VOL] REDUCED_RISK prescan — score>={HIGH_VOL_MIN_SCORE}+{HIGH_VOL_MIN_SCORE_EXTRA}  "
              f"setups={HIGH_VOL_ALLOWED_SETUPS}")
        print(f"     severity bands: mild ATR<{HIGH_VOL_MODERATE_ATR_PCT}% size-{HIGH_VOL_SIZE_CUT:.0%}  "
              f"moderate ATR>={HIGH_VOL_MODERATE_ATR_PCT}% size-{HIGH_VOL_MODERATE_SIZE_CUT:.0%}+{HIGH_VOL_MODERATE_EXTRA_PTS}pts")
        log_audit("HIGH_VOL_MODE", details={
            "restrictions": {
                "min_score_base":    HIGH_VOL_MIN_SCORE,
                "extra_pts":         HIGH_VOL_MIN_SCORE_EXTRA,
                "min_rvol":          HIGH_VOL_STOCK_RVOL,
                "max_trades":        HIGH_VOL_MAX_TRADES,
                "size_cut_mild":     f"{HIGH_VOL_SIZE_CUT:.0%}",
                "size_cut_moderate": f"{HIGH_VOL_MODERATE_SIZE_CUT:.0%}",
                "moderate_atr_pct":  HIGH_VOL_MODERATE_ATR_PCT,
                "moderate_extra_pts": HIGH_VOL_MODERATE_EXTRA_PTS,
                "allowed_setups":    HIGH_VOL_ALLOWED_SETUPS,
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

    # Store regime and prescan timestamp so execution can measure signal-to-entry latency
    prescan_ts = datetime.now(ET).isoformat()
    for p in scored:
        p["regime"] = regime
        p["_prescan_ts"] = prescan_ts

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

    # Intraday gapper refresh — runs before any time gate so gappers stay fresh during midday block
    try:
        from gapper import refresh_gappers_intraday
        refresh_gappers_intraday()
    except Exception:
        pass

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
    no_trade_hypothetical = False
    if not is_tradeable(regime):
        if paper_mode or PAPER_TRADING:
            print(f"   [NO_TRADE_HYPOTHETICAL] Regime {regime!r} — scanning in paper-hypothetical mode")
            log_audit("SCAN_HYPOTHETICAL", details={"reason": f"regime={regime}", "mode": "hypothetical"})
            no_trade_hypothetical = True
        else:
            print(f"   [NO TRADE] Regime {regime!r} — scan aborted.")
            log_audit("SCAN_SKIPPED", details={"reason": f"regime={regime}"})
            return

    low_vol_mode  = (regime == LOW_VOLUME)
    high_vol_mode = (regime == HIGH_VOL)
    if TRADING_MODE == "LIVE":
        _lv_score_label = str(LIVE_LOW_VOLUME_MIN_SCORE)
        _lv_score_log   = LIVE_LOW_VOLUME_MIN_SCORE
    else:
        _lv_score_label = (f"{PAPER_EXPLORATORY_LOW_VOLUME_MIN_SCORE}(exp)/"
                           f"{PAPER_LIVE_REALISTIC_LOW_VOLUME_MIN_SCORE}(real)")
        _lv_score_log   = PAPER_EXPLORATORY_LOW_VOLUME_MIN_SCORE
    if low_vol_mode:
        ctx = get_regime_context()
        print(f"   [LOW_VOLUME] REDUCED_RISK: score>={_lv_score_label} ({'live' if TRADING_MODE == 'LIVE' else 'paper'})  "
              f"RVOL>={LOW_VOLUME_STOCK_RVOL}x  max {LOW_VOLUME_MAX_TRADES} trade  size-50%")
        log_audit("LOW_VOLUME_MODE", details={"restrictions": {
            "min_score":  _lv_score_log,
            "min_rvol":   LOW_VOLUME_STOCK_RVOL,
            "max_trades": LOW_VOLUME_MAX_TRADES,
            "size_cut":   "50%",
            "mode":       TRADING_MODE,
        }, **ctx})
    elif high_vol_mode:
        ctx = get_regime_context()
        hv_min = HIGH_VOL_MIN_SCORE
        print(f"   [HIGH_VOL] REDUCED_RISK: score>={hv_min}+{HIGH_VOL_MIN_SCORE_EXTRA}  "
              f"RVOL>={HIGH_VOL_STOCK_RVOL}x  max {HIGH_VOL_MAX_TRADES} trade  "
              f"size-{HIGH_VOL_SIZE_CUT:.0%} (mild) / size-{HIGH_VOL_MODERATE_SIZE_CUT:.0%} (moderate)  "
              f"setups={HIGH_VOL_ALLOWED_SETUPS}")
        log_audit("HIGH_VOL_MODE", details={"restrictions": {
            "min_score_base":    hv_min,
            "extra_pts":         HIGH_VOL_MIN_SCORE_EXTRA,
            "min_rvol":          HIGH_VOL_STOCK_RVOL,
            "max_trades":        HIGH_VOL_MAX_TRADES,
            "size_cut_mild":     f"{HIGH_VOL_SIZE_CUT:.0%}",
            "size_cut_moderate": f"{HIGH_VOL_MODERATE_SIZE_CUT:.0%}",
            "moderate_atr_pct":  HIGH_VOL_MODERATE_ATR_PCT,
            "moderate_extra_pts": HIGH_VOL_MODERATE_EXTRA_PTS,
            "allowed_setups":    HIGH_VOL_ALLOWED_SETUPS,
        }, **ctx})
        # Extreme HIGH_VOL: no new trades (hard stop live, paper-hypothetical in paper)
        extreme_hv, extreme_hv_reason = _is_extreme_high_vol()
        if extreme_hv:
            if paper_mode or PAPER_TRADING:
                print(f"   [EXTREME_HIGH_VOL] {extreme_hv_reason} — paper-hypothetical scan only")
                log_audit("EXTREME_HIGH_VOL_HYPOTHETICAL", details={"reason": extreme_hv_reason})
                no_trade_hypothetical = True
            else:
                print(f"   [EXTREME_HIGH_VOL] {extreme_hv_reason} — no new trades.")
                log_audit("EXTREME_HIGH_VOL_ABORT", details={"reason": extreme_hv_reason})
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

        # Apply confidence decay based on candidate age then re-check thresholds
        for c in candidates:
            age_mins = c.get("_age_mins", 0.0)
            raw_score = c.get("score", 0)
            decayed, decay_pts = _apply_score_decay(raw_score, age_mins)
            if decay_pts:
                c["score"]         = decayed
                c["_decay_points"] = decay_pts
                c["_raw_score"]    = raw_score
                # Re-evaluate tradeable with decayed score
                eff_min = get_min_score(regime, c.get("setup_type"))
                c["tradeable"]      = decayed >= eff_min
                c["_effective_min"] = eff_min

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
        save_candidates(candidates)

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

        # LOW_VOLUME / HIGH_VOL trade caps
        if low_vol_mode and trades_this_scan >= LOW_VOLUME_MAX_TRADES:
            print(f"   [LOW_VOLUME] {LOW_VOLUME_MAX_TRADES} trade cap reached — stopping scan.")
            log_audit("SCAN_CAPPED", details={"reason": "low_volume_max_trades"})
            break
        if high_vol_mode and trades_this_scan >= HIGH_VOL_MAX_TRADES:
            print(f"   [HIGH_VOL] {HIGH_VOL_MAX_TRADES} trade cap reached — stopping scan.")
            log_audit("SCAN_CAPPED", details={"reason": "high_vol_max_trades"})
            break

        symbol        = pick["symbol"]
        score         = pick.get("score", 0)
        reasoning     = pick.get("reasoning", "")
        prescan_price = pick.get("price")
        vol_pct       = pick.get("volatility_pct", 0.0)
        spread_pct    = pick.get("spread_pct", 0.0)
        setup_type    = pick.get("setup_type", "unknown")
        age_mins      = pick.get("_age_mins", 0.0)
        is_stale      = pick.get("_stale", False)

        # Effective minimum for this regime + setup type
        effective_min = get_min_score(regime, setup_type)

        # HIGH_VOL setup restriction — only ORB/gap/news setups allowed
        avoid_override_pending = pick.get("research_avoid_override_pending", False)
        if high_vol_mode and setup_type not in HIGH_VOL_ALLOWED_SETUPS:
            print(f"   [SKIP] {symbol}: HIGH_VOL mode — setup {setup_type!r} not in allowed setups")
            _reject(symbol, score, setup_type, "high_vol_gate", "setup_not_allowed",
                    observed=setup_type, threshold=str(HIGH_VOL_ALLOWED_SETUPS))
            continue

        # Timestamps for telemetry
        signal_ts   = pick.get("shortlisted_at") or pick.get("_prescan_ts")
        decision_ts = datetime.now(ET).isoformat()

        # Tier classification (spec point 5)
        tier = get_setup_tier(score)

        is_experimental = score < LIVE_BASE_SCORE

        # Cross-agent duplicate check — block if IBKR already holds this symbol
        taken, holder = is_symbol_taken(symbol)
        if taken:
            print(f"   [SKIP] {symbol}: already held by {holder} agent — duplicate exposure blocked")
            _reject(symbol, score, setup_type, "cross_agent_gate", "duplicate_exposure",
                    observed=holder, is_experimental=is_experimental)
            continue

        # Event risk: hard-block on imminent earnings or halt
        earn_block, earn_desc = check_earnings(symbol)
        if earn_block:
            print(f"   [SKIP] {symbol}: event risk — {earn_desc}")
            _reject(symbol, score, setup_type, "event_risk_gate", "earnings_block",
                    observed=earn_desc, is_experimental=is_experimental)
            continue
        halted, halt_desc = check_halt(symbol)
        if halted:
            print(f"   [SKIP] {symbol}: {halt_desc}")
            _reject(symbol, score, setup_type, "event_risk_gate", "halt",
                    observed=halt_desc, is_experimental=is_experimental)
            continue

        # Score gate — hard reject if gap exceeds QO ceiling; AVOID override bypasses for PAPER
        score_below_min = score < effective_min
        if score_below_min and not avoid_override_pending:
            gap = effective_min - score
            if gap > QUALITY_OVERRIDE_MAX_GAP_PTS:
                _reject(symbol, score, setup_type, "score_gate", "score_below_min",
                        observed=score, threshold=effective_min,
                        is_experimental=is_experimental)
                print(f"   [SKIP] {symbol}: score {score} < {effective_min}, gap {gap}pts exceeds QO ceiling")
                continue
            print(f"   [BELOW-MIN] {symbol}: score {score} < {effective_min} (gap {gap}pts ≤ QO ceiling) — quality override pending")

        # HIGH_VOL extra score requirement (above effective_min)
        hv_required = effective_min + HIGH_VOL_MIN_SCORE_EXTRA
        if high_vol_mode and score < hv_required:
            print(f"   [SKIP] {symbol}: HIGH_VOL requires score>={hv_required} (got {score})")
            _reject(symbol, score, setup_type, "high_vol_gate", "high_vol_min_score",
                    observed=score, threshold=hv_required, is_experimental=is_experimental)
            continue

        # LOW_VOLUME confidence gate — strict for LIVE, two-tier for PAPER
        # LIVE: reject below LIVE_LOW_VOLUME_MIN_SCORE (no exploratory relaxation)
        # PAPER: reject below exploratory threshold; flag 70-74 for PAPER_EXPLORATORY_ONLY tagging
        lv_exploratory_allowed = False
        if low_vol_mode:
            if TRADING_MODE == "LIVE":
                if score < LIVE_LOW_VOLUME_MIN_SCORE:
                    print(f"   [SKIP] {symbol}: LOW_VOLUME requires score>={LIVE_LOW_VOLUME_MIN_SCORE} "
                          f"(live) (got {score})")
                    _reject(symbol, score, setup_type, "low_vol_gate", "low_volume_min_score",
                            observed=score, threshold=LIVE_LOW_VOLUME_MIN_SCORE,
                            is_experimental=is_experimental)
                    continue
            else:
                if score < PAPER_EXPLORATORY_LOW_VOLUME_MIN_SCORE:
                    print(f"   [SKIP] {symbol}: LOW_VOLUME requires score>={PAPER_EXPLORATORY_LOW_VOLUME_MIN_SCORE} "
                          f"(paper exploratory min) (got {score})")
                    _reject(symbol, score, setup_type, "low_vol_gate", "low_volume_min_score",
                            observed=score, threshold=PAPER_EXPLORATORY_LOW_VOLUME_MIN_SCORE,
                            is_experimental=is_experimental)
                    continue
                elif score < PAPER_LIVE_REALISTIC_LOW_VOLUME_MIN_SCORE:
                    lv_exploratory_allowed = True
                    print(f"   [PAPER_EXP] {symbol}: LOW_VOLUME score {score} in exploratory band "
                          f"({PAPER_EXPLORATORY_LOW_VOLUME_MIN_SCORE}-"
                          f"{PAPER_LIVE_REALISTIC_LOW_VOLUME_MIN_SCORE - 1}) "
                          f"— will tag PAPER_EXPLORATORY_ONLY")

        # LOW_VOLUME spread gate — spread must be tight in low-vol sessions
        if low_vol_mode and spread_pct > PREFERRED_SPREAD_PCT:
            print(f"   [SKIP] {symbol}: LOW_VOLUME spread {spread_pct:.3%} > preferred {PREFERRED_SPREAD_PCT:.3%}")
            _reject(symbol, score, setup_type, "low_vol_gate", "spread_above_preferred",
                    observed=round(spread_pct, 4), threshold=PREFERRED_SPREAD_PCT,
                    is_experimental=is_experimental)
            continue

        # Risk gate
        risk_ok, risk_reason = check_candidate_risk(pick, portfolio, prescan_price)
        if not risk_ok:
            print(f"   [SKIP] {symbol}: {risk_reason}")
            _reject(symbol, score, setup_type, "risk_gate", risk_reason,
                    is_experimental=is_experimental)
            continue

        # Momentum analysis — 1-min bars check (fresh, per symbol)
        mom = analyse_momentum(symbol)
        print(f"   Momentum {symbol}: {mom['strength']} — {mom['reason']}")
        if mom["strength"] not in MIN_MOMENTUM_TO_TRADE:
            _reject(symbol, score, setup_type, "momentum_gate", "momentum_weak",
                    observed=mom["strength"], threshold=str(MIN_MOMENTUM_TO_TRADE),
                    is_experimental=is_experimental)
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
            _reject(symbol, score, setup_type, "volatility_gate", "vol_extension",
                    observed=vol_ext_reason, is_experimental=is_experimental)
            log_feed_event("TRADE_REJECTED_DATA", {"symbol": symbol, "reason": vol_ext_reason})
            continue

        # Failed breakout detection (spec point 8)
        breakout_price = pick.get("open_price", price)
        fb_failed, fb_reason = detect_failed_breakout(bars_1min, breakout_price)
        if fb_failed:
            print(f"   [SKIP] {symbol}: failed breakout — {fb_reason}")
            _reject(symbol, score, setup_type, "breakout_gate", "failed_breakout",
                    observed=fb_reason, is_experimental=is_experimental)
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
                _reject(symbol, score, setup_type, "pullback_gate", "pullback_rejected",
                        observed=pullback_result["pullback_reason"], threshold=tier,
                        is_experimental=is_experimental)
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

        # ORB/pullback mandatory gate — risky setups require ORB or pullback confirmation
        if not avoid_override_pending:
            orb_pb_confirmed  = orb_breakout or pullback_result.get("pullback_detected", False)
            needs_confirmation = score_below_min or is_experimental or regime == CHOPPY or low_vol_mode
            if needs_confirmation and not orb_pb_confirmed:
                print(f"   [SKIP] {symbol}: risky setup requires ORB/pullback confirmation "
                      f"(score={score}, regime={regime}, experimental={is_experimental})")
                _reject(symbol, score, setup_type, "orb_pullback_gate", "orb_pullback_required",
                        observed=f"orb={orb_breakout},pb={pullback_result.get('pullback_detected',False)}",
                        is_experimental=is_experimental)
                continue

        # ATR-aware stop (spec point 7)
        atr_stop = atr_aware_stop_pct(bars_1min, price)
        stop_pct = atr_stop if bars_1min else suggested_stop_pct(mom["strength"], regime)
        # Take profit = max(TAKE_PROFIT_PCT, 2x stop) — minimum 2:1 target-to-stop ratio
        tp_pct   = max(TAKE_PROFIT_PCT, 2.0 * stop_pct)

        # HIGH_VOL severity — moderate if stock ATR >= HIGH_VOL_MODERATE_ATR_PCT
        high_vol_moderate = high_vol_mode and (atr_stop * 100 >= HIGH_VOL_MODERATE_ATR_PCT)
        if high_vol_moderate:
            hv_mod_required = effective_min + HIGH_VOL_MIN_SCORE_EXTRA + HIGH_VOL_MODERATE_EXTRA_PTS
            if score < hv_mod_required:
                print(f"   [SKIP] {symbol}: HIGH_VOL MODERATE requires score>={hv_mod_required} "
                      f"(ATR={atr_stop*100:.2f}%, got {score})")
                _reject(symbol, score, setup_type, "high_vol_gate", "high_vol_moderate_score",
                        observed=score, threshold=hv_mod_required, is_experimental=is_experimental)
                continue

        shares, size_pct, size_note = dynamic_position_size(
            portfolio, price, score, vol_pct, spread_pct, regime, recent_wr
        )

        # PAPER mode: setup-based sizing bands (experimental → smaller, elite → larger)
        if TRADING_MODE == "PAPER":
            if score >= 90:
                band_min, band_max, band_label = 0.15, 0.25, "PAPER_ELITE"
            elif score >= 85:
                band_min, band_max, band_label = 0.15, 0.20, "PAPER_HIGH"
            elif score >= 72:
                band_min, band_max, band_label = 0.10, 0.15, "PAPER_NORMAL"
            else:
                band_min, band_max, band_label = 0.05, 0.10, "PAPER_EXP"
            banded_pct = min(band_max, max(band_min, size_pct))
            if banded_pct != size_pct:
                shares    = max(1, int(portfolio * banded_pct / price))
                size_pct  = banded_pct
                size_note += f" | {band_label}"

        # HIGH_VOL: reduce size (moderate = deeper cut)
        if high_vol_mode:
            actual_cut = HIGH_VOL_MODERATE_SIZE_CUT if high_vol_moderate else HIGH_VOL_SIZE_CUT
            hv_pct    = max(MIN_POSITION_SIZE_PCT, size_pct * (1 - actual_cut))
            shares    = max(1, int(portfolio * hv_pct / price))
            size_pct  = hv_pct
            size_note += f" | HV-{actual_cut:.0%}"

        # ELITE size boost (spec point 5) — only if spread tight and market aligned
        if tier == "ELITE" and not high_vol_mode and spread_pct < 0.15 and alignment in ("BULLISH", "STRONG_BULLISH", "ALIGNED"):
            boosted_pct = min(MAX_POSITION_SIZE_PCT, size_pct * (1 + ELITE_SIZE_BOOST))
            if boosted_pct > size_pct:
                shares   = max(1, int(portfolio * boosted_pct / price))
                size_pct = boosted_pct
                size_note += f" | ELITE+{ELITE_SIZE_BOOST:.0%}"

        # Preferred spread penalty — reduce size when spread is wide
        if spread_pct > SPREAD_PENALTY_ABOVE:
            penalty_pct = max(MIN_POSITION_SIZE_PCT, size_pct * (1 - SPREAD_SIZE_PENALTY_PCT))
            shares    = max(1, int(portfolio * penalty_pct / price))
            size_note += f" | SPREAD-{SPREAD_SIZE_PENALTY_PCT:.0%}"
            size_pct   = penalty_pct

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
            log_feed_event("TRADE_REJECTED_DATA", {"symbol": symbol, "reason": f"quality_score={iq['score']}: {iq['reason']}"})
            _reject(symbol, score, setup_type, "quality_gate", "intraday_quality_low",
                    observed=iq["score"], threshold=40, is_experimental=is_experimental)
            continue

        top_news_impact = pick.get("_top_news_impact", 0)

        # LOW_VOLUME stock-level RVOL gate
        if low_vol_mode and iq["rvol"] > 0:
            is_exceptional = (score >= LOW_VOLUME_EXCEPTIONAL_SCORE or top_news_impact >= LOW_VOLUME_EXCEPTIONAL_NEWS)
            if not is_exceptional and iq["rvol"] < LOW_VOLUME_STOCK_RVOL:
                print(f"   [SKIP] {symbol}: LOW_VOLUME stock RVOL {iq['rvol']:.1f}x "
                      f"< {LOW_VOLUME_STOCK_RVOL}x required")
                _reject(symbol, score, setup_type, "low_vol_gate", "low_volume_stock_rvol",
                        observed=round(iq["rvol"], 2), threshold=LOW_VOLUME_STOCK_RVOL,
                        is_experimental=is_experimental)
                continue
            if is_exceptional and iq["rvol"] < LOW_VOLUME_STOCK_RVOL:
                print(f"   [LOW_VOLUME] {symbol}: RVOL {iq['rvol']:.1f}x below threshold "
                      f"but EXCEPTIONAL (score={score}, news={top_news_impact}) — allowing")

        # HIGH_VOL stock-level RVOL gate
        if high_vol_mode and iq["rvol"] > 0 and iq["rvol"] < HIGH_VOL_STOCK_RVOL:
            print(f"   [SKIP] {symbol}: HIGH_VOL stock RVOL {iq['rvol']:.1f}x < {HIGH_VOL_STOCK_RVOL}x required")
            _reject(symbol, score, setup_type, "high_vol_gate", "high_vol_stock_rvol",
                    observed=round(iq["rvol"], 2), threshold=HIGH_VOL_STOCK_RVOL,
                    is_experimental=is_experimental)
            continue

        # Cross-provider quote validation — compare Alpaca vs Massive/Polygon before executing
        quote_ok, quote_reason, poly_quote = validate_cross_provider(
            symbol, price, spread_pct, pick.get("today_volume", 0)
        )
        if not quote_ok:
            print(f"   [DATA QUALITY] {symbol}: {quote_reason}")
            log_feed_event("TRADE_REJECTED_DATA", {"symbol": symbol, "reason": quote_reason})
            _reject(symbol, score, setup_type, "data_quality_gate", "quote_validation_failed",
                    observed=quote_reason, is_experimental=is_experimental)
            continue
        if "volume_divergence" in quote_reason:
            log_feed_event("VOLUME_DIVERGENCE", {"symbol": symbol, "detail": quote_reason})

        # Quality override check — now that we have ORB, pullback, and quality data
        qo_allowed, qo_reason = _quality_override(
            score, effective_min,
            rvol=iq.get("rvol", 0.0),
            spread_pct=spread_pct,
            news_impact=pick.get("_top_news_impact", 0),
            orb_breakout=orb_breakout,
            pullback_ok=pullback_result.get("pullback_detected", False),
            alignment=alignment,
            mom_ok=mom["strength"] in MIN_MOMENTUM_TO_TRADE,
            no_failed_bo=not fb_failed,
        )
        # PAPER AVOID override: finalize after all quality checks
        research_avoid_override = False
        if avoid_override_pending and TRADING_MODE == "PAPER":
            mom_ok_for_override = mom["strength"] in MIN_MOMENTUM_TO_TRADE
            orb_pb_ok           = orb_breakout or pullback_result.get("pullback_detected", False)
            if mom_ok_for_override and orb_pb_ok:
                research_avoid_override = True
                print(f"   [PAPER_AVOID_OVERRIDE] {symbol}: experimental AVOID override activated "
                      f"(RVOL/spread/catalyst/momentum/ORB all confirmed)")
                log_audit("AVOID_OVERRIDE", symbol, {
                    "score": score, "setup_type": setup_type,
                    "rvol": round(iq.get("rvol", 0.0), 2),
                    "spread_pct": spread_pct, "news_impact": pick.get("_top_news_impact", 0),
                    "trading_mode": TRADING_MODE,
                })
            else:
                print(f"   [SKIP] {symbol}: PAPER AVOID override failed "
                      f"(mom={mom['strength']}, orb={orb_breakout})")
                _reject(symbol, score, setup_type, "avoid_override_gate", "avoid_override_conditions_failed",
                        observed=f"mom={mom['strength']}, orb={orb_breakout}",
                        is_experimental=True)
                continue

        quality_override_applied = False
        if score_below_min and not research_avoid_override:
            if qo_allowed:
                quality_override_applied = True
                print(f"   [QUALITY OVERRIDE] {symbol}: {qo_reason}")
                log_audit("QUALITY_OVERRIDE", symbol, {
                    "score": score, "effective_min": effective_min,
                    "reason": qo_reason, "setup_type": setup_type,
                    "trading_mode": TRADING_MODE,
                })
            else:
                print(f"   [SKIP] {symbol}: {qo_reason}")
                _reject(symbol, score, setup_type, "quality_override_gate", "quality_override_failed",
                        observed=qo_reason, threshold=effective_min,
                        is_experimental=is_experimental)
                continue

        reason_allowed  = (
            "avoid_override"       if research_avoid_override else
            "quality_override"     if quality_override_applied else
            "regime_relaxed"       if effective_min < LIVE_BASE_SCORE else
            "threshold_met"
        )

        audit_details = {
            "score":                  score,
            "tier":                   tier,
            "shares":                 shares,
            "price":                  price,
            "cost":                   round(cost, 2),
            "size_pct":               round(size_pct, 3),
            "sizing":                 size_note,
            "regime":                 regime,
            "alignment":              alignment,
            "momentum":               mom["strength"],
            "setup_type":             setup_type,
            "stop_pct":               round(stop_pct, 4),
            "atr_stop_pct":           round(atr_stop, 5),
            "orb_breakout":           orb_breakout,
            "pullback_quality":       pullback_result.get("pullback_quality", "N/A"),
            "vwap":                   vwap,
            "spread_pct":             spread_pct,
            "trading_mode":           TRADING_MODE,
            "effective_min":          effective_min,
            "quality_override":       quality_override_applied,
            "experimental":           is_experimental,
            "reason_allowed":         reason_allowed,
            "research_avoid_override": research_avoid_override,
            "decay_points":           pick.get("_decay_points", 0),
            "age_mins":               round(age_mins, 1),
            "stale":                  is_stale,
            "high_vol_severity":      "moderate" if high_vol_moderate else ("mild" if high_vol_mode else "none"),
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

        # Pre-submit — live: block on failure; paper: tag would_reject_live
        ps_ok, ps_reason = _pre_submit_check(symbol, price, spread_pct, portfolio)
        would_reject_live        = not ps_ok
        would_reject_live_reason = ps_reason if not ps_ok else ""
        audit_details["would_reject_live"]        = would_reject_live
        audit_details["would_reject_live_reason"] = would_reject_live_reason
        if not paper_mode and not ps_ok:
            print(f"   [PRE_SUBMIT_REJECT] {symbol}: {ps_reason}")
            log_audit("PRE_SUBMIT_REJECT", symbol, {
                "reason": ps_reason, "score": score, "setup_type": setup_type,
                "trading_mode": TRADING_MODE, "is_experimental": is_experimental,
            })
            continue

        # LIVE promoted-setup logging (informational, LIVE_REQUIRE_PROMOTED_SETUPS gates this)
        if LIVE_REQUIRE_PROMOTED_SETUPS and setup_type not in LIVE_PROMOTED_SETUPS:
            log_audit("LIVE_UNPROMOTED_SETUP", symbol, {
                "setup_type": setup_type, "score": score, "regime": regime,
            })

        if paper_mode:
            exp_tag = " [EXPERIMENTAL]" if is_experimental else ""
            qo_tag  = " [QUALITY_OVERRIDE]" if quality_override_applied else ""
            ao_tag  = " [AVOID_OVERRIDE]" if research_avoid_override else ""
            print(f"   [PAPER] WOULD BUY {symbol}  score={score}/100  tier={tier}  "
                  f"{size_note}  stop={stop_pct:.1%}  setup={setup_type}  "
                  f"orb={'Y' if orb_breakout else 'n'}  pullback={pullback_result.get('pullback_quality','N/A')}"
                  f"{exp_tag}{qo_tag}{ao_tag}")
            print(f"   [PAPER] {shares}sh @ ${price:.2f} = ${cost:,.0f} | {reasoning}")
            log_paper_trade(symbol, shares, price, "BUY", score, reasoning)
            log_audit("PAPER_TRADE", symbol, audit_details)
            log_audit("PAPER_TRADE_TAGS", symbol, {
                "setup_type":              setup_type,
                "regime":                  regime,
                "score_band":              _score_band(score),
                "score":                   score,
                "effective_min":           effective_min,
                "experimental":            is_experimental,
                "quality_override":        quality_override_applied,
                "research_avoid_override": research_avoid_override,
                "reason_allowed":          reason_allowed,
                "orb_status":              orb_breakout,
                "pullback_status":         pullback_result.get("pullback_quality", "N/A"),
                "catalyst_quality":        pick.get("_top_news_impact", 0),
                "vwap_distance_pct":       round((price - vwap) / vwap * 100, 3) if vwap else None,
                "rvol":                    round(iq.get("rvol", 0.0), 2),
                "spread_pct":              spread_pct,
                "decay_points":            pick.get("_decay_points", 0),
                "age_mins":                round(age_mins, 1),
                "stale":                   is_stale,
                "trading_mode":            TRADING_MODE,
                "claude_involved":         not pick.get("_local_only", False),
                "would_reject_live":       would_reject_live,
                "would_reject_live_reason": would_reject_live_reason,
                "high_vol_moderate":       high_vol_moderate,
                "no_trade_hypothetical":   no_trade_hypothetical,
            })
            # Paper trade outcome classification (Item 1)
            if no_trade_hypothetical:
                log_audit("NO_TRADE_HYPOTHETICAL", symbol, {
                    "regime": regime, "score": score, "setup_type": setup_type,
                    "would_reject_live": True,
                })
                log_audit("PAPER_EXPLORATORY_ONLY", symbol, {
                    "reason": f"no_trade_regime:{regime}",
                    "would_reject_live": True,
                    "score": score, "setup_type": setup_type,
                })
            elif lv_exploratory_allowed:
                log_audit("PAPER_EXPLORATORY_ONLY", symbol, {
                    "reason":                "lv_exploratory_threshold",
                    "score":                 score,
                    "normal_threshold":      PAPER_LIVE_REALISTIC_LOW_VOLUME_MIN_SCORE,
                    "exploratory_threshold": PAPER_EXPLORATORY_LOW_VOLUME_MIN_SCORE,
                    "regime":                regime,
                    "setup_type":            setup_type,
                    "claude_involved":       not pick.get("_local_only", False),
                    "rvol":                  round(iq.get("rvol", 0.0), 2),
                    "spread":                spread_pct,
                    "catalyst_quality":      pick.get("_top_news_impact", 0),
                    "orb_status":            orb_breakout,
                    "pullback_status":       pullback_result.get("pullback_quality", "N/A"),
                    "momentum_state":        mom["strength"],
                    "would_pass_live_rules": not would_reject_live,
                    "would_reject_live_reason": would_reject_live_reason,
                    "would_reject_live":     True,
                })
            elif would_reject_live:
                log_audit("PAPER_EXPLORATORY_ONLY", symbol, {
                    "would_reject_live": True,
                    "would_reject_live_reason": would_reject_live_reason,
                    "score": score, "setup_type": setup_type, "regime": regime,
                })
            else:
                log_audit("PAPER_LIVE_REALISTIC", symbol, {
                    "score": score, "setup_type": setup_type, "regime": regime,
                    "spread_pct": spread_pct, "tier": tier,
                    "orb_breakout": orb_breakout,
                })
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
                "submitted_limit_price": round(price * (1 + min(_limit_offset(spread_pct), MAX_LIMIT_SLIPPAGE_PCT)), 4),
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

    log_audit("SCAN_DONE", details={"trades": trades_this_scan, "regime": regime, "candidates": len(candidates)})


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

    _paper_category_report()
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
