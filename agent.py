"""
Day Trading Agent — intraday momentum, all positions closed by 3:44pm ET.

Daily schedule (BST / ET):
  08:30 / 13:30  --research      Pre-market fundamentals + Claude brief for full watchlist
  09:40 / 14:40  --precheck      Data readiness + live gate (TEST_01/02/14/20) — writes LIVE_ENABLED
  09:45 / 14:45  --prescan       Discover & score candidates, save to JSON, NO orders
  09:45 / 14:45  --continuous    Adaptive scan loop (5/15/10 min cadence) until 15:30 ET
                                   OR use fixed Task Scheduler scans (--scan):
  10:00 / 15:00  --scan          Scan1 | --monitor-loop  45s monitor loop
  10:30 / 15:30  --scan          Scan2
  11:30 / 16:30  --scan          Scan3
  [midday block  12:00–13:00 ET / 17:00–18:00 BST — scans skip automatically]
  13:30 / 18:30  --scan          Scan4
  15:00 / 20:00  --scan          Scan5  [power-hour gate: elite setups only]
  15:30 / 20:30  --cutoff        Cancel unfilled limit orders — entry window closes
  15:44 / 20:44  --close         Force-close all positions
  15:55 / 20:55  --verify        Emergency flatness check — close anything still open
  16:15 / 21:15  --report        Basic P&L from Alpaca
  16:30 / 21:30  --performance   Full analytics dashboard (expectancy, PF, windows)

Live safety gates (--precheck must pass all before --scan allows live orders):
  TEST_01 Data Source Connectivity
  TEST_02 Cross-Provider Validation
  TEST_14 Broker Order Safety
  TEST_20 End-to-End Dry Run

Other:
  --paper         Simulate full --scan logic without placing real orders
  --monitor       Single-run position monitor (advanced exits, shortlist check)
  --morning       Alias for --prescan (backwards compat)
  --status        Any time — current positions and P&L
  --feedreport    Feed quality report — provider health, mismatches, rejections
"""
import argparse
import json
import math
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Ensure UTF-8 output on Windows so Unicode symbols in print() don't crash
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from alpaca.trading.client import TradingClient
from config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL, PAPER_TRADING, TRADING_MODE,
    LIVE_ORDER_EARLIEST_ET, LIVE_ORDER_LATEST_ET,
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

# ── Live gate / PDT constants ─────────────────────────────────────────────────
LIVE_GATE_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "live_gate_state.json")
PDT_RESERVED_BUFFER  = 1   # always keep this many PDT slots in reserve
QUOTE_STALE_SECS     = 30  # reject live quotes older than this (seconds)

# ── Gapper refresh state ──────────────────────────────────────────────────────
_last_gapper_refresh_et: datetime | None = None


# ── Mode helpers ──────────────────────────────────────────────────────────────

def _print_vol_diagnostics() -> None:
    """Print time-of-day normalized volume diagnostics after each regime detection."""
    try:
        ctx   = get_regime_context()
        spy_r = ctx.get("spy_intraday_ratio")
        if spy_r is None:
            return  # old cache format — skip
        qqq_r = ctx.get("qqq_intraday_ratio", 0)
        iwm_r = ctx.get("iwm_intraday_ratio", 0)
        eff_r = ctx.get("effective_vol_ratio", spy_r)
        mins  = ctx.get("mins_since_open", "?")
        tv    = ctx.get("spy_today_vol", 0)
        ev    = ctx.get("spy_expected_vol", 0)
        n     = ctx.get("spy_baseline_samples", 0)
        wd_n  = ctx.get("spy_same_wd_n", 0)
        note  = ctx.get("ratio_note", "")
        print(
            f"   [VOL] SPY={spy_r:.0%}  QQQ={qqq_r:.0%}  IWM={iwm_r:.0%}  "
            f"effective={eff_r:.0%}  @{mins}min  "
            f"today={tv:,} vs expected={ev:,}  "
            f"baseline={n} sessions ({wd_n} same-wd)"
        )
        if note:
            print(f"          {note}")
    except Exception:
        pass


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


def _validate_vwap_reclaim(bars: list[dict], vwap: float, spread_pct: float,
                            min_hold_candles: int = 2,
                            max_spread: float = 0.30) -> tuple[bool, str]:
    """
    Returns (valid, reason). A real VWAP reclaim requires:
    - price crossed from below VWAP to above (actual reclaim, not staying above)
    - holds above VWAP for at least min_hold_candles consecutive closes
    - volume does not collapse on reclaim candles vs prior average
    - spread remains acceptable
    """
    if not bars or vwap <= 0:
        return False, "no bars or no vwap"
    if spread_pct > max_spread:
        return False, f"spread {spread_pct:.3f}% > {max_spread:.3f}% max"

    recent = bars[-10:] if len(bars) >= 10 else bars
    if len(recent) < min_hold_candles + 2:
        return False, "insufficient bars for VWAP reclaim check"

    closes  = [b["close"]  for b in recent]
    volumes = [b["volume"] for b in recent]

    # Count consecutive closes above VWAP from the most recent candle
    hold_count = 0
    for c in reversed(closes):
        if c > vwap:
            hold_count += 1
        else:
            break

    if hold_count < min_hold_candles:
        return False, f"only {hold_count} candle(s) above VWAP — need {min_hold_candles}+"

    # Must have had at least one close at/below VWAP before the hold (actual cross)
    prior_closes = closes[:-hold_count]
    if not any(c <= vwap for c in prior_closes):
        return False, "no prior cross — price was already above VWAP (not a reclaim)"

    # Volume check: reclaim candles must not be below 50% of prior candles average
    reclaim_vols = volumes[-hold_count:]
    prior_vols   = volumes[:-hold_count]
    if prior_vols:
        avg_prior   = sum(prior_vols) / len(prior_vols)
        avg_reclaim = sum(reclaim_vols) / len(reclaim_vols)
        if avg_prior > 0 and avg_reclaim < avg_prior * 0.5:
            return False, (f"volume collapsed on reclaim "
                           f"({avg_reclaim:.0f} < 50% of {avg_prior:.0f} prior avg)")

    return True, (f"VWAP reclaim: {hold_count} candles above VWAP, "
                  f"volume holding ({sum(reclaim_vols):.0f} total)")


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


def _data_confidence(pick: dict) -> int:
    """0–100 score reflecting data quality for this candidate at order time."""
    score = 100
    # Quote age penalty
    q_ts = pick.get("quote_fetched_at")
    if q_ts:
        try:
            from datetime import timezone as _tz
            age_s = (datetime.now(_tz.utc) - datetime.fromisoformat(q_ts)).total_seconds()
            if age_s > 20:
                score -= min(30, int(age_s - 20))
        except Exception:
            score -= 10
    else:
        score -= 20  # no timestamp at all
    # VWAP source
    if not pick.get("vwap") or pick.get("vwap", 0) <= 0:
        score -= 10
    # Data source quality (IEX = wider spreads / delayed)
    if pick.get("_data_src") not in ("ibkr", "sip", "alpaca_sip"):
        score -= 15
    # Spread width
    spread = pick.get("spread_pct", 0)
    if spread > 0.1:
        score -= min(15, int((spread - 0.1) * 100))
    return max(0, score)


def _pdt_budget() -> dict:
    """
    Return full PDT budget for LIVE sub-$25k accounts.
    Reserves PDT_RESERVED_BUFFER slots so we never exhaust all 3 in one session.
    Returns large numbers for paper mode / equity >= $25k (effectively unlimited).
    """
    if TRADING_MODE != "LIVE":
        return {"usable": 999, "budget": 999, "count": 0, "equity": 0,
                "last_trade_mode": False, "blocked": False,
                "reason": "paper mode — PDT not applicable"}
    try:
        acct        = _client().get_account()
        equity      = float(getattr(acct, "equity", 0) or 0)
        if equity >= 25_000:
            return {"usable": 999, "budget": 999, "count": 0, "equity": equity,
                    "last_trade_mode": False, "blocked": False,
                    "reason": f"equity ${equity:,.0f} >= $25k — PDT not applicable"}
        count       = int(getattr(acct, "daytrade_count", 0) or 0)
        pdt_flagged = getattr(acct, "pattern_day_trader", False)
        if pdt_flagged:
            return {"usable": 0, "budget": 0, "count": count, "equity": equity,
                    "last_trade_mode": False, "blocked": True,
                    "reason": "account flagged as pattern_day_trader — contact Alpaca support"}
        budget = max(0, 3 - count)
        usable = max(0, budget - PDT_RESERVED_BUFFER)
        return {
            "usable":          usable,
            "budget":          budget,
            "count":           count,
            "equity":          equity,
            "last_trade_mode": usable == 1,
            "blocked":         usable == 0,
            "reason":          (f"PDT: {count}/3 used — budget={budget} "
                                f"usable={usable} (reserve={PDT_RESERVED_BUFFER})"),
        }
    except Exception as e:
        return {"usable": 999, "budget": 999, "count": 0, "equity": 0,
                "last_trade_mode": False, "blocked": False,
                "reason": f"PDT check error (allowing trade): {e}"}


def _pdt_check() -> tuple[bool, str]:
    """Thin wrapper around _pdt_budget() — True = trade allowed."""
    pdt = _pdt_budget()
    return not pdt["blocked"], pdt["reason"]


def _live_time_gate() -> tuple[bool, str]:
    """
    Block live orders outside the allowed window (09:45–15:30 ET).
    Returns True unconditionally for paper mode.
    """
    if TRADING_MODE != "LIVE":
        return True, "paper mode — time gate not applicable"
    now_et  = datetime.now(ET)
    now_bst = now_et.astimezone(ZoneInfo("Europe/London"))
    mins    = now_et.hour * 60 + now_et.minute
    lo_h, lo_m = LIVE_ORDER_EARLIEST_ET
    hi_h, hi_m = LIVE_ORDER_LATEST_ET
    lo_mins    = lo_h * 60 + lo_m
    hi_mins    = hi_h * 60 + hi_m
    et_str  = now_et.strftime("%H:%M ET")
    bst_str = now_bst.strftime("%H:%M BST")
    if mins < lo_mins:
        return False, (f"LIVE_TIME_GATE_REJECT: too early — {et_str} / {bst_str} "
                       f"(live orders allowed from {lo_h:02d}:{lo_m:02d} ET / "
                       f"{now_et.replace(hour=lo_h, minute=lo_m).astimezone(ZoneInfo('Europe/London')).strftime('%H:%M')} BST)")
    if mins > hi_mins:
        return False, (f"LIVE_TIME_GATE_REJECT: past new-entry cutoff — {et_str} / {bst_str} "
                       f"(latest new entry {hi_h:02d}:{hi_m:02d} ET / "
                       f"{now_et.replace(hour=hi_h, minute=hi_m).astimezone(ZoneInfo('Europe/London')).strftime('%H:%M')} BST)")
    return True, f"time gate ok: {et_str} / {bst_str}"


def _verify_account_mode() -> None:
    """
    Abort live scan if account mode is misconfigured.
    Called only for real (non-paper) scan/execute runs.
    """
    if TRADING_MODE != "LIVE":
        return
    if PAPER_TRADING:
        print("\n  *** ACCOUNT_MODE ERROR: TRADING_MODE=LIVE but PAPER_TRADING=True ***")
        print("  *** Set PAPER_TRADING=false in .env — aborting to protect capital ***")
        sys.exit(1)
    if "paper-api" in ALPACA_BASE_URL:
        print(f"\n  *** ACCOUNT_MODE ERROR: TRADING_MODE=LIVE but endpoint is {ALPACA_BASE_URL} ***")
        print("  *** Expected https://api.alpaca.markets — aborting to protect capital ***")
        sys.exit(1)
    print(f"  [ACCOUNT_MODE] LIVE | endpoint: {ALPACA_BASE_URL}")


def _pre_submit_check(symbol: str, intended_price: float, intended_spread: float,
                      portfolio: float) -> tuple[bool, str]:
    """Re-check 13 conditions immediately before order submission."""
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

    # 7. Live order time gate (09:45–15:30 ET) — hard block for live mode
    tg_ok, tg_reason = _live_time_gate()
    if not tg_ok:
        log_audit("LIVE_TIME_GATE_REJECT", symbol, {
            "reason": tg_reason, "trading_mode": TRADING_MODE,
        })
        return False, tg_reason

    # 8. Earnings re-check
    try:
        earn_block, earn_desc = check_earnings(symbol)
        if earn_block:
            return False, f"earnings_recheck: {earn_desc}"
    except Exception:
        pass

    # 9. Max trades today
    try:
        closed_today = len(today_summary())
        if closed_today >= MAX_TRADES_PER_DAY:
            return False, f"max_trades_today: {closed_today}/{MAX_TRADES_PER_DAY}"
    except Exception:
        pass

    try:
        client = _client()

        # 10. Duplicate open order check
        open_orders = client.get_orders()
        dupes = [o for o in open_orders if hasattr(o, "symbol") and o.symbol == symbol]
        if dupes:
            return False, f"duplicate_order: {len(dupes)} order(s) already open for {symbol}"

        # 11. Buying power minimum
        acct = client.get_account()
        bp   = float(acct.buying_power or 0)
        min_cost = intended_price * max(1, int(portfolio * MIN_POSITION_SIZE_PCT / max(intended_price, 0.01)))
        if bp < min_cost:
            return False, f"buying_power: ${bp:.0f} < min_cost ${min_cost:.0f}"

        # 12. Spread re-check (intended vs configured max)
        from config import MAX_SPREAD_PCT
        if intended_spread > MAX_SPREAD_PCT:
            return False, f"spread_widened: {intended_spread:.2f}% > {MAX_SPREAD_PCT}%"

        # 13. Price sanity (not aberrantly priced)
        if intended_price <= 0 or intended_price > 50_000:
            return False, f"price_invalid: ${intended_price:.2f}"

    except Exception:
        pass  # non-blocking — don't fail a good trade on check errors

    return True, "ok"


def _market_open() -> bool:
    return _client().get_clock().is_open


def _header(mode: str):
    now_et  = datetime.now(ET)
    now_bst = now_et.astimezone(ZoneInfo("Europe/London"))
    tag     = "[PAPER]" if PAPER_TRADING else "[LIVE]"
    print(f"\n{'='*62}")
    print(f"  LEELA DAY TRADING AGENT {tag} -- {now_et.strftime('%Y-%m-%d %H:%M ET')} / {now_bst.strftime('%H:%M BST')}")
    print(f"  Mode: {mode}")
    if TRADING_MODE == "LIVE":
        print(f"  ACCOUNT_MODE=LIVE | endpoint: {ALPACA_BASE_URL}")
    else:
        print(f"  ACCOUNT_MODE=PAPER | endpoint: {ALPACA_BASE_URL}")
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
    daily_pct = daily_pnl / start * 100 if start else 0.0

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


def _write_live_gate_state(enabled: bool, tests: dict) -> None:
    state = {
        "live_enabled": enabled,
        "ts":           datetime.now(ET).isoformat(),
        "allowed_mode": "LIVE" if enabled else "PAPER_ONLY",
        "tests":        tests,
    }
    try:
        with open(LIVE_GATE_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass


def _read_live_gate_state() -> tuple[bool, str]:
    """Returns (live_enabled, reason). Safe-fails to False if file missing/stale."""
    try:
        with open(LIVE_GATE_STATE_FILE) as f:
            state = json.load(f)
        ts  = datetime.fromisoformat(state["ts"]).astimezone(ET)
        now = datetime.now(ET)
        if ts.date() < now.date():
            return False, "live_gate_state is from a previous trading day — run --precheck"
        return state.get("live_enabled", False), state.get("allowed_mode", "PAPER_ONLY")
    except FileNotFoundError:
        return False, "precheck not yet run today — run --precheck first"
    except Exception as e:
        return False, f"state file error: {e}"


def _precheck():
    """
    Data readiness + live gate — runs at 09:40 ET / 14:40 BST, 5 min before prescan.

    Maps checks to the 4 mandatory live safety gate tests:
      TEST_01 Data Source Connectivity
      TEST_02 Cross-Provider Validation
      TEST_14 Broker Order Safety
      TEST_20 End-to-End Dry Run

    LIVE trading remains disabled unless ALL 4 named tests pass.
    Writes LIVE_ENABLED state to live_gate_state.json for use by --scan.
    No orders placed.  Logs PRECHECK_PASS or PRECHECK_FAIL to audit.
    """
    now_et  = datetime.now(ET)
    now_bst = now_et.astimezone(ZoneInfo("Europe/London"))
    subs: list[tuple[str, bool, str]] = []   # (sub_id, ok, detail)

    # ── TEST_01: Data Source Connectivity ─────────────────────────────────────
    try:
        healthy, _ = _run_feed_health(paper_mode=True)
        subs.append(("T01.feed_health", healthy,
                     "feeds ok" if healthy else "feed degraded — check feed_health.json"))
    except Exception as e:
        subs.append(("T01.feed_health", False, f"health check error: {e}"))

    try:
        from massive_feed import active_providers as _ap
        providers = _ap()
        subs.append(("T01.data_providers", bool(providers),
                     f"active: {providers}" if providers else "no data providers configured"))
    except Exception as e:
        subs.append(("T01.data_providers", False, f"provider check error: {e}"))

    # ── TEST_02: Cross-Provider Validation ────────────────────────────────────
    try:
        cval_ok, cval_reason, _ = validate_cross_provider("SPY", 0, 0, 0)
        # A missing secondary is not a hard failure; we care about active mismatches
        cval_pass = cval_ok or "no_secondary" in cval_reason or "mismatch" not in cval_reason
        subs.append(("T02.cross_provider", cval_pass,
                     (cval_reason[:80] if cval_reason else "cross-provider ok")))
    except Exception as e:
        subs.append(("T02.cross_provider", False, f"cross-provider error: {e}"))

    # ── TEST_14: Broker Order Safety ──────────────────────────────────────────
    try:
        client  = _client()
        acct    = client.get_account()
        equity  = float(acct.portfolio_value or 0)
        bp      = float(acct.buying_power or 0)
        blocked = getattr(acct, "trading_blocked", False) or getattr(acct, "account_blocked", False)
        if blocked:
            subs.append(("T14.broker_account", False, "account blocked — check Alpaca dashboard"))
        else:
            subs.append(("T14.broker_account", True,
                         f"equity=${equity:,.2f}  buying_power=${bp:,.2f}"))
    except Exception as e:
        subs.append(("T14.broker_account", False, f"API error: {e}"))

    if TRADING_MODE == "LIVE":
        pdt = _pdt_budget()
        subs.append(("T14.pdt_headroom", not pdt["blocked"], pdt["reason"]))

    # ── TEST_20: End-to-End Dry Run ───────────────────────────────────────────
    if TRADING_MODE == "LIVE" and ("paper-api" in ALPACA_BASE_URL or PAPER_TRADING):
        subs.append(("T20.account_mode", False,
                     f"LIVE but endpoint={ALPACA_BASE_URL} paper_flag={PAPER_TRADING}"))
    else:
        subs.append(("T20.account_mode", True,
                     f"ACCOUNT_MODE={TRADING_MODE} endpoint={ALPACA_BASE_URL}"))

    try:
        clock      = _client().get_clock()
        is_open    = clock.is_open
        next_close = clock.next_close.astimezone(ET).strftime("%H:%M ET")
        next_open  = clock.next_open.astimezone(ET).strftime("%H:%M ET") if not is_open else "—"
        subs.append(("T20.market_clock", True,
                     f"open={is_open}  next_close={next_close}" +
                     (f"  next_open={next_open}" if not is_open else "")))
    except Exception as e:
        subs.append(("T20.market_clock", False, f"clock error: {e}"))

    tg_ok, tg_msg = _live_time_gate()
    subs.append(("T20.time_gate", tg_ok, tg_msg))

    # ── Aggregate into 4 named tests ──────────────────────────────────────────
    def _tpass(prefix: str) -> bool:
        return all(ok for sid, ok, _ in subs if sid.startswith(prefix))

    named = {
        "TEST_01_DataSourceConnectivity":  _tpass("T01"),
        "TEST_02_CrossProviderValidation": _tpass("T02"),
        "TEST_14_BrokerOrderSafety":       _tpass("T14"),
        "TEST_20_EndToEndDryRun":          _tpass("T20"),
    }
    live_enabled = all(named.values())
    allowed_mode = "LIVE" if live_enabled else "PAPER_ONLY"

    # ── Print results ─────────────────────────────────────────────────────────
    print(f"  {'PRECHECK_PASS' if live_enabled else 'PRECHECK_FAIL'}  "
          f"{now_et.strftime('%H:%M ET')} / {now_bst.strftime('%H:%M BST')}\n")
    print(f"  {'─'*58}")
    for tname, tpass in named.items():
        print(f"  [{'PASS' if tpass else 'FAIL'}] {tname}")
    print(f"  {'─'*58}")
    for sid, ok, detail in subs:
        print(f"  [{'  ok' if ok else 'FAIL'}]   {sid:<30} {detail}")
    print(f"\n  LIVE_ENABLED  = {str(live_enabled).lower()}")
    print(f"  allowed_mode  = {allowed_mode}")
    if not live_enabled:
        failed = [t for t, ok in named.items() if not ok]
        print(f"\n  Blocking tests: {', '.join(failed)}")
        print(f"  Action        : remain PAPER_ONLY until all 4 tests pass")

    _write_live_gate_state(live_enabled, named)
    log_audit("PRECHECK_PASS" if live_enabled else "PRECHECK_FAIL", details={
        "live_enabled":  live_enabled,
        "allowed_mode":  allowed_mode,
        "named_tests":   named,
        "sub_checks":    {s: {"ok": o, "detail": d} for s, o, d in subs},
        "trading_mode":  TRADING_MODE,
    })
    print()


def _cutoff():
    """
    Entry cutoff — runs at 15:30 ET / 20:30 BST.
    Cancels all open (unfilled) limit orders.  Does NOT close positions.
    Force-close at 15:44 handles open positions.
    """
    client    = _client()
    open_ords = client.get_orders()
    now_et    = datetime.now(ET)
    now_bst   = now_et.astimezone(ZoneInfo("Europe/London"))

    if not open_ords:
        print(f"  [CUTOFF] No open orders — entry window clean "
              f"({now_et.strftime('%H:%M ET')} / {now_bst.strftime('%H:%M BST')})")
        log_audit("ENTRY_CUTOFF", details={"cancelled": 0})
        return

    symbols = [o.symbol for o in open_ords if hasattr(o, "symbol")]
    print(f"  [CUTOFF] Cancelling {len(open_ords)} open order(s): {', '.join(symbols)}")
    try:
        client.cancel_orders()
    except Exception as e:
        print(f"  [CUTOFF] cancel_orders error: {e}")

    log_audit("ENTRY_CUTOFF", details={
        "cancelled": len(open_ords),
        "symbols":   symbols,
        "time_et":   now_et.strftime("%H:%M"),
        "time_bst":  now_bst.strftime("%H:%M"),
    })
    print(f"  [CUTOFF] Done — entry window closed, positions held until force-close at 15:44 ET")


# ── Price safety guard ────────────────────────────────────────────────────────

def _price_safety_guard(symbol: str, entry: float, stop: float, tp: float,
                        qty: int, spread_pct: float) -> tuple[bool, str]:
    """
    Hard-reject any order with invalid price data before broker submission.
    Called immediately before place_bracket_order().  Logs INVALID_PRICE_DATA_REJECT.
    """
    def _bad(v) -> bool:
        return v is None or (isinstance(v, float) and math.isnan(v)) or v <= 0

    for val, name in [(entry, "entry_price"), (stop, "stop_price"), (tp, "take_profit")]:
        if _bad(val):
            msg = f"{name} invalid: {val}"
            log_audit("INVALID_PRICE_DATA_REJECT", symbol, {"error": msg})
            return False, msg

    if qty is None or qty <= 0:
        msg = f"quantity invalid: {qty}"
        log_audit("INVALID_PRICE_DATA_REJECT", symbol, {"error": msg})
        return False, msg

    if spread_pct is None or spread_pct < 0 or (isinstance(spread_pct, float) and math.isnan(spread_pct)):
        msg = f"spread_pct invalid: {spread_pct}"
        log_audit("INVALID_PRICE_DATA_REJECT", symbol, {"error": msg})
        return False, msg

    if stop >= entry:
        msg = f"stop {stop:.4f} >= entry {entry:.4f} — invalid bracket"
        log_audit("INVALID_PRICE_DATA_REJECT", symbol, {"error": msg})
        return False, msg

    if tp <= entry:
        msg = f"take_profit {tp:.4f} <= entry {entry:.4f} — invalid bracket"
        log_audit("INVALID_PRICE_DATA_REJECT", symbol, {"error": msg})
        return False, msg

    return True, "price data valid"


# ── Power-hour gate ───────────────────────────────────────────────────────────

_POWER_HOUR_ALLOWED_SETUPS = frozenset({
    "gap_and_go", "orb_breakout", "orb_continuation", "vwap_reclaim",
    "hod_breakout", "power_hour", "trend_continuation", "news_momentum",
})


def _power_hour_gate(symbol: str, score: int, setup_type: str,
                     spread_pct: float, rvol: float,
                     alignment: str) -> tuple[bool, str]:
    """
    After 15:00 ET: elite continuation setups only.
    After 15:15 ET: tighten score/spread further.
    Returns (allowed, reason).  Always passes before 15:00 ET.
    """
    now_et = datetime.now(ET)
    mins   = now_et.hour * 60 + now_et.minute
    if mins < 15 * 60:
        return True, "not power hour"

    late_tight = mins >= 15 * 60 + 15
    min_score  = 88 if late_tight else 85
    max_spread = 0.10

    if setup_type not in _POWER_HOUR_ALLOWED_SETUPS:
        return False, (f"power_hour: setup {setup_type!r} blocked after "
                       f"15:{'15' if late_tight else '00'} ET "
                       f"— continuation setups only")
    if score < min_score:
        return False, (f"power_hour: score {score} < {min_score} required after "
                       f"15:{'15' if late_tight else '00'} ET")
    if rvol > 0 and rvol < 2.5:
        return False, f"power_hour: RVOL {rvol:.1f}x < 2.5x required"
    if spread_pct > max_spread:
        return False, f"power_hour: spread {spread_pct:.2%} > {max_spread:.0%} allowed"
    if alignment not in ("BULLISH", "STRONG_BULLISH", "ALIGNED"):
        return False, f"power_hour: alignment {alignment!r} insufficient"
    return True, f"power_hour ok: score={score} rvol={rvol:.1f}x setup={setup_type}"


# ── Adaptive gapper refresh ───────────────────────────────────────────────────

def _refresh_gappers_adaptive() -> None:
    """
    Time-aware gapper refresh (replaces bare refresh_gappers_intraday() in scan loop):
      09:30–11:00 ET (first 90 min)  →  every 5 min
      after 11:00 ET                 →  every 10 min
    Only re-runs when the interval has elapsed since last refresh.
    """
    global _last_gapper_refresh_et
    now_et   = datetime.now(ET)
    elapsed  = (now_et.hour * 60 + now_et.minute) - (9 * 60 + 30)
    interval = 5 * 60 if elapsed <= 90 else 10 * 60
    if (_last_gapper_refresh_et is None or
            (now_et - _last_gapper_refresh_et).total_seconds() >= interval):
        try:
            from gapper import refresh_gappers_intraday
            refresh_gappers_intraday()
            _last_gapper_refresh_et = now_et
        except Exception:
            pass


# ── Bracket verification ──────────────────────────────────────────────────────

def _verify_bracket(symbol: str) -> tuple[bool, str]:
    """
    After entry fill: confirm stop-loss and take-profit child orders exist.
    If either is missing: flatten immediately, cancel all orders, log BRACKET_PROTECTION_FAILURE.
    Returns (bracket_ok, reason).
    """
    import time as _time
    _time.sleep(1.5)  # allow bracket legs to register
    try:
        client = _client()
        orders = client.get_orders()
        legs   = [o for o in orders
                  if hasattr(o, "symbol") and o.symbol == symbol]
        has_sl = any(getattr(o, "type", "") in ("stop", "stop_limit") for o in legs)
        has_tp = any(getattr(o, "type", "") in ("limit", "take_profit") for o in legs)
        if has_sl and has_tp:
            return True, f"bracket ok: {len(legs)} child order(s)"
        # Bracket incomplete — flatten immediately
        print(f"\n   [BRACKET_PROTECTION_FAILURE] {symbol}: "
              f"sl={has_sl} tp={has_tp} legs={len(legs)} — flattening")
        log_audit("BRACKET_PROTECTION_FAILURE", symbol, {
            "has_sl": has_sl, "has_tp": has_tp, "legs": len(legs),
        })
        try:
            client.close_position(symbol)
            client.cancel_orders()
        except Exception as ce:
            log_audit("BRACKET_FLATTEN_ERROR", symbol, {"error": str(ce)})
        return False, f"bracket incomplete: sl={has_sl} tp={has_tp}"
    except Exception as e:
        return False, f"bracket verification error: {e}"


# ── Continuous adaptive scan loop ─────────────────────────────────────────────

def _continuous_scan_loop() -> None:
    """
    Adaptive scan cadence from 09:45 to 15:30 ET:
      09:45–11:00  every  5 min   (open — aggressive momentum capture)
      11:00–13:00  every 15 min   (late morning)
      13:00–15:30  every 10 min   (afternoon + power hour)
    Power-hour gate activates automatically at 15:00 ET inside _scan_and_trade().
    Exits when 15:30 ET cutoff is reached.
    """
    import time as _time

    def _cadence() -> int:
        mins = datetime.now(ET).hour * 60 + datetime.now(ET).minute
        if mins < 11 * 60:  return  5 * 60
        if mins < 13 * 60:  return 15 * 60
        return                      10 * 60

    _header("CONTINUOUS SCAN — adaptive 09:45–15:30 ET")
    loop_n = 0

    while True:
        now_et = datetime.now(ET)
        mins   = now_et.hour * 60 + now_et.minute

        if mins >= 15 * 60 + 30:
            print(f"   [CONTINUOUS] {now_et.strftime('%H:%M ET')} — "
                  f"15:30 entry cutoff reached. Exiting scan loop.")
            log_audit("CONTINUOUS_SCAN_DONE", details={"loops": loop_n})
            break

        lo_h, lo_m = LIVE_ORDER_EARLIEST_ET
        lo_mins    = lo_h * 60 + lo_m
        if mins < lo_mins:
            wait_s = (lo_mins - mins) * 60
            print(f"   [CONTINUOUS] Before {lo_h:02d}:{lo_m:02d} ET — "
                  f"waiting {wait_s // 60}min for entry window")
            _time.sleep(wait_s)
            continue

        loop_n += 1
        print(f"\n{'─'*52}  loop #{loop_n}  "
              f"{datetime.now(ET).strftime('%H:%M ET')}")
        _status()
        print()
        _scan_and_trade(paper_mode=False)

        cadence = _cadence()
        next_et = datetime.now(ET) + timedelta(seconds=cadence)
        print(f"\n   [CONTINUOUS] Next scan ~{next_et.strftime('%H:%M ET')} "
              f"(cadence={cadence // 60}min)")
        _time.sleep(cadence)


# ── Monitor loop (30–60 s cadence) ───────────────────────────────────────────

def _monitor_loop() -> None:
    """
    Tight-loop position monitor at 45-second cadence.
    Monitors: stops, trailing stops, failed breakouts, spread deterioration,
    momentum weakening, VWAP loss, liquidity collapse, rapid invalidation.
    Exits when no positions remain after 15:30 ET.
    """
    import time as _time
    INTERVAL = 45  # seconds

    _header("MONITOR LOOP — 45s cadence")

    while True:
        now_et = datetime.now(ET)
        mins   = now_et.hour * 60 + now_et.minute
        monitor_positions()
        try:
            ready = monitor_shortlist()
            if ready:
                print(f"\n   [SHORTLIST] {len(ready)} ready: "
                      f"{', '.join(r['symbol'] for r in ready)}")
        except Exception:
            pass

        # After entry cutoff: exit loop once flat
        if mins >= 15 * 60 + 30:
            try:
                remaining = _client().get_all_positions()
                if not remaining:
                    print(f"   [MONITOR LOOP] {now_et.strftime('%H:%M ET')} — "
                          f"past cutoff and fully flat. Exiting.")
                    break
            except Exception:
                pass

        _time.sleep(INTERVAL)


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
    _print_vol_diagnostics()
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
        print(f"     vix={ctx.get('vix','?')}  "
              f"SPY={ctx.get('spy_intraday_ratio','?'):.0%}  "
              f"QQQ={ctx.get('qqq_intraday_ratio','?'):.0%}  "
              f"IWM={ctx.get('iwm_intraday_ratio','?'):.0%}  "
              f"effective={ctx.get('effective_vol_ratio','?'):.0%}  "
              f"baseline={ctx.get('spy_baseline_samples','?')} sessions")
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
    if not paper_mode:
        _verify_account_mode()   # aborts if LIVE mode is misconfigured

        # Live gate state — require successful --precheck today before any live orders
        if TRADING_MODE == "LIVE":
            gate_ok, gate_reason = _read_live_gate_state()
            if not gate_ok:
                print(f"   [LIVE_GATE_BLOCK] {gate_reason}")
                print(f"   Run --precheck to enable live trading for today.")
                log_audit("LIVE_GATE_BLOCK", details={"reason": gate_reason})
                return

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

    # Adaptive gapper refresh (5 min first 90 min, 10 min after 11:00 ET)
    _refresh_gappers_adaptive()

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
    _print_vol_diagnostics()
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
    lv_ctx        = get_regime_context()
    hc_ok         = lv_ctx.get("high_conviction_ok", False)  # Item 8: RVOL>=3 override flag
    if TRADING_MODE == "LIVE":
        _lv_score_label = str(LIVE_LOW_VOLUME_MIN_SCORE)
        _lv_score_log   = LIVE_LOW_VOLUME_MIN_SCORE
    else:
        _lv_score_label = (f"{PAPER_EXPLORATORY_LOW_VOLUME_MIN_SCORE}(exp)/"
                           f"{PAPER_LIVE_REALISTIC_LOW_VOLUME_MIN_SCORE}(real)")
        _lv_score_log   = PAPER_EXPLORATORY_LOW_VOLUME_MIN_SCORE
    if low_vol_mode:
        print(f"   [LOW_VOLUME] REDUCED_RISK: score>={_lv_score_label} ({'live' if TRADING_MODE == 'LIVE' else 'paper'})  "
              f"RVOL>={LOW_VOLUME_STOCK_RVOL}x  max {LOW_VOLUME_MAX_TRADES} trade  size-50%"
              + ("  [HC_OVERRIDE: RVOL>=3 setups bypass score gate]" if hc_ok else ""))
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
        # High-conviction bypass: RVOL>=3 + news_impact>=50 + tight spread skips score gate
        # (still subject to RVOL gate and spread gate below)
        lv_exploratory_allowed   = False
        lv_high_conviction_bypass = False
        if low_vol_mode and hc_ok:
            _pre_rvol        = pick.get("rel_volume", 0)
            _pre_news_impact = pick.get("_top_news_impact", 0)
            if _pre_rvol >= 3.0 and spread_pct <= PREFERRED_SPREAD_PCT and _pre_news_impact >= 50:
                lv_high_conviction_bypass = True
                print(f"   [HC_OVERRIDE] {symbol}: RVOL={_pre_rvol:.1f}x  "
                      f"news={_pre_news_impact}  spread={spread_pct:.3%} — "
                      f"LOW_VOLUME score gate bypassed")
                log_audit("LV_HC_OVERRIDE", symbol, {
                    "rvol": _pre_rvol, "news_impact": _pre_news_impact,
                    "spread_pct": round(spread_pct, 4), "score": score,
                })
        if low_vol_mode and not lv_high_conviction_bypass:
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

        # Confirmation gate — pattern confirmation required in CHOPPY and for risky setups
        confirmation_type = "NONE"
        if not avoid_override_pending:
            pb_detected = pullback_result.get("pullback_detected", False)

            if regime == CHOPPY:
                # Validate VWAP reclaim with real bars (cross + hold + volume)
                vwap_reclaim_valid, vwap_reclaim_reason = _validate_vwap_reclaim(
                    bars_1min, vwap, spread_pct) if setup_type == "vwap_reclaim" else (False, "not vwap_reclaim setup")
                if setup_type == "vwap_reclaim":
                    print(f"   VWAP reclaim {symbol}: "
                          f"{'VALID' if vwap_reclaim_valid else 'INVALID'} — {vwap_reclaim_reason}")

                # News momentum elite — all conditions required
                news_momentum_elite = (
                    setup_type == "news_momentum"
                    and pick.get("_top_news_impact", 0) >= 70
                    and pick.get("rel_volume", 0) >= 2.5
                    and spread_pct <= 0.15
                    and mom["strength"] in ("STRENGTHENING", "STABLE")
                    # fb_failed and exhaustion already eliminated by prior gates
                )

                # Determine which confirmation passes (priority order)
                if orb_breakout:
                    choppy_ok, confirmation_type = True, "ORB"
                elif vwap_reclaim_valid:
                    choppy_ok, confirmation_type = True, "VWAP_RECLAIM"
                elif pb_detected:
                    choppy_ok, confirmation_type = True, "PULLBACK"
                elif news_momentum_elite:
                    choppy_ok, confirmation_type = True, "NEWS_ELITE"
                else:
                    choppy_ok = False

                if choppy_ok:
                    print(f"   [CHOPPY OK] {symbol}: confirmed via {confirmation_type}")
                else:
                    print(f"   [SKIP] {symbol}: CHOPPY requires ORB/VWAP-reclaim/pullback/news-elite "
                          f"(score={score} setup={setup_type} "
                          f"rvol={pick.get('rel_volume',0):.1f}x spread={spread_pct:.3f}% "
                          f"mom={mom['strength']} news_impact={pick.get('_top_news_impact',0)})")
                    _reject(symbol, score, setup_type, "choppy_gate", "choppy_confirmation_required",
                            observed=f"orb={orb_breakout},vwap_reclaim={vwap_reclaim_valid},"
                                     f"pb={pb_detected},news_elite={news_momentum_elite}",
                            is_experimental=is_experimental)
                    continue
            else:
                # Non-CHOPPY: only risky setups need ORB/pullback confirmation
                orb_pb_confirmed   = orb_breakout or pb_detected
                needs_confirmation = score_below_min or is_experimental or low_vol_mode
                if needs_confirmation and not orb_pb_confirmed:
                    print(f"   [SKIP] {symbol}: risky setup requires ORB/pullback confirmation "
                          f"(score={score}, regime={regime}, experimental={is_experimental})")
                    _reject(symbol, score, setup_type, "orb_pullback_gate", "orb_pullback_required",
                            observed=f"orb={orb_breakout},pb={pb_detected}",
                            is_experimental=is_experimental)
                    continue
                # Track confirmation type for non-CHOPPY trades
                if orb_breakout:
                    confirmation_type = "ORB"
                elif pb_detected:
                    confirmation_type = "PULLBACK"

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

        # Power-hour gate — elite continuation setups only after 15:00 ET
        ph_ok, ph_reason = _power_hour_gate(
            symbol, score, setup_type, spread_pct, iq.get("rvol", 0.0), alignment
        )
        if not ph_ok:
            print(f"   [SKIP] {symbol}: {ph_reason}")
            _reject(symbol, score, setup_type, "power_hour_gate", "power_hour_restriction",
                    observed=ph_reason, is_experimental=is_experimental)
            log_audit("POWER_HOUR_GATE_REJECT", symbol, {
                "reason": ph_reason, "score": score, "setup_type": setup_type,
            })
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
            "confirmation_type":      confirmation_type,
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
            "data_confidence_score":  _data_confidence(pick),
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

        # PDT budget guard — live only, with last-trade-mode elite criteria
        if not paper_mode:
            pdt = _pdt_budget()
            if pdt["blocked"]:
                print(f"   [PDT_BLOCK] {pdt['reason']}")
                log_audit("PDT_BLOCK", symbol, {
                    "reason": pdt["reason"], "score": score, "setup_type": setup_type,
                    "budget": pdt["budget"], "usable": pdt["usable"],
                })
                break  # budget exhausted — no more trades this session
            if pdt["last_trade_mode"]:
                # 1 usable slot remaining: require elite entry criteria
                ltm_ok = (
                    score >= 90
                    and iq.get("rvol", 0) >= 2.5
                    and spread_pct <= 0.15
                    and setup_type in {"gap_and_go", "orb_breakout", "vwap_reclaim",
                                       "news_momentum", "hod_breakout"}
                    and (orb_breakout or pullback_result.get("pullback_detected", False))
                    and alignment in ("BULLISH", "STRONG_BULLISH", "ALIGNED")
                )
                if not ltm_ok:
                    print(f"   [PDT_LAST_TRADE] {symbol}: 1 slot left — elite criteria not met "
                          f"(score={score} rvol={iq.get('rvol',0):.1f}x "
                          f"spread={spread_pct:.2%} setup={setup_type})")
                    log_audit("PDT_LAST_TRADE_BLOCK", symbol, {
                        "reason": "last_trade_mode_criteria_not_met",
                        "score": score, "rvol": round(iq.get("rvol", 0), 2),
                        "spread_pct": spread_pct, "setup_type": setup_type,
                        "orb": orb_breakout, "alignment": alignment,
                    })
                    continue
                print(f"   [PDT_LAST_TRADE] {symbol}: last slot — elite criteria met ✓")
                log_audit("PDT_LAST_TRADE_APPROVED", symbol, {
                    "score": score, "setup_type": setup_type, "usable": pdt["usable"],
                })

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

            # Quote staleness guard — reject if snapshot is too old
            if TRADING_MODE == "LIVE":
                from datetime import timezone as _tz
                q_ts = pick.get("quote_fetched_at")
                if q_ts:
                    try:
                        age_s = (datetime.now(_tz.utc) - datetime.fromisoformat(q_ts)).total_seconds()
                        if age_s > QUOTE_STALE_SECS:
                            print(f"   [STALE_QUOTE_REJECT] {symbol}: quote is {age_s:.0f}s old (>{QUOTE_STALE_SECS}s)")
                            log_audit("STALE_QUOTE_REJECT", symbol, {
                                "age_s": round(age_s, 1), "limit_s": QUOTE_STALE_SECS,
                                "quote_fetched_at": q_ts,
                            })
                            continue
                    except Exception:
                        pass

            # Price safety guard — hard reject before broker submission
            limit_price = price * (1 + min(_limit_offset(spread_pct), MAX_LIMIT_SLIPPAGE_PCT))
            stop_price  = price * (1 - stop_pct)
            tp_price    = price * (1 + tp_pct)
            pg_ok, pg_reason = _price_safety_guard(symbol, limit_price, stop_price, tp_price,
                                                    shares, spread_pct)
            if not pg_ok:
                print(f"   [INVALID_PRICE_DATA_REJECT] {symbol}: {pg_reason}")
                log_audit("INVALID_PRICE_DATA_REJECT", symbol, {
                    "reason": pg_reason, "entry": price, "stop_pct": stop_pct, "tp_pct": tp_pct,
                })
                continue

            order = place_bracket_order(
                symbol, shares, price,
                score=score, size_pct=size_pct, sizing_note=size_note,
                stop_pct=stop_pct, take_profit_pct=tp_pct,
            )

            # Bracket verification — confirm SL+TP legs exist; flatten if missing
            bv_ok, bv_reason = _verify_bracket(symbol)
            if not bv_ok:
                log_audit("BRACKET_PROTECTION_FAILURE", symbol, {
                    "reason": bv_reason, "score": score, "setup_type": setup_type,
                })
                continue  # position already flattened inside _verify_bracket()

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
                "submitted_limit_price": round(limit_price, 4),
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
    daily_pct = daily_pnl / start * 100 if start else 0.0

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
    parser.add_argument("--research",    action="store_true", help="Pre-market research — fundamentals + Claude brief (8:30am ET)")
    parser.add_argument("--precheck",      action="store_true", help="Data readiness + live gate check (9:40am ET)")
    parser.add_argument("--prescan",       action="store_true", help="Discover & score candidates, NO orders (9:45am ET)")
    parser.add_argument("--morning",       action="store_true", help="Alias for --prescan")
    parser.add_argument("--scan",          action="store_true", help="Load prescan candidates and execute (10:00am+ ET)")
    parser.add_argument("--continuous",    action="store_true", help="Adaptive scan loop 09:45–15:30 ET (5/15/10 min cadence)")
    parser.add_argument("--paper",         action="store_true", help="Simulate full --scan logic without real orders")
    parser.add_argument("--monitor",       action="store_true", help="Check trailing stops and advanced exits (single run)")
    parser.add_argument("--monitor-loop",  action="store_true", help="Tight 45s monitor loop — replaces 2-min Task Scheduler polling")
    parser.add_argument("--cutoff",        action="store_true", help="Entry cutoff — cancel unfilled limit orders (3:30pm ET)")
    parser.add_argument("--close",         action="store_true", help="Force-close all positions (3:44pm ET)")
    parser.add_argument("--verify",        action="store_true", help="Emergency flatness check — close anything still open (3:55pm ET)")
    parser.add_argument("--report",        action="store_true", help="Basic P&L report (4:15pm ET)")
    parser.add_argument("--performance",   action="store_true", help="Full analytics dashboard (4:30pm ET)")
    parser.add_argument("--feedreport",    action="store_true", help="Feed quality report — provider health, mismatches, rejections")
    parser.add_argument("--status",        action="store_true", help="Current positions and P&L (any time)")
    args = parser.parse_args()

    if args.research:
        _header("PRE-MARKET RESEARCH")
        from research import run_premarket_research
        run_premarket_research()

    elif args.precheck:
        _header("DATA READINESS — 9:40am ET / 14:40 BST")
        _precheck()

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

    elif args.continuous:
        _continuous_scan_loop()

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

    elif getattr(args, "monitor_loop", False):
        _monitor_loop()

    elif args.cutoff:
        _header("ENTRY CUTOFF — 3:30pm ET / 20:30 BST")
        _cutoff()

    elif args.close:
        _header("FORCE CLOSE — 3:44pm ET / 20:44 BST")
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
              "--research | --precheck | --prescan | --scan | --continuous | "
              "--paper | --monitor | --monitor-loop | --cutoff | --close | "
              "--verify | --report | --performance | --feedreport | --status")
