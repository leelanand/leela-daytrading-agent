"""
End-of-day performance analytics and adaptive learning.

Calculates: expectancy, profit factor, win rate, drawdown, slippage,
            per-time-window stats, rejection breakdown.

Outputs non-binding improvement suggestions — never auto-modifies hard limits.
"""
import json
import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from config import (
    DB_PATH, PERFORMANCE_FILE, PERF_HISTORY_FILE,
    PERF_LOOKBACK_DAYS, MIN_WIN_RATE_TO_TRADE,
    WINDOW_BLOCK_MIN_TRADES, WINDOW_BLOCK_AVG_PNL,
)

ET = ZoneInfo("America/New_York")


# ── Time-window helper ─────────────────────────────────────────────────────────

def _window(ts_str: str) -> str:
    try:
        dt   = datetime.fromisoformat(ts_str).astimezone(ET)
        mins = dt.hour * 60 + dt.minute
        if mins < 10 * 60 + 30:  return "open"
        if mins < 12 * 60:        return "late_morning"
        if mins < 13 * 60:        return "midday"
        if mins < 15 * 60:        return "afternoon"
        return "power_hour"
    except Exception:
        return "unknown"


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    return bool(con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone())


# ── Rolling performance (used by sizing.py for adaptive sizing) ────────────────

def get_recent_performance(days: int | None = None) -> dict:
    """Lightweight rolling stats used at trade time for adaptive sizing."""
    days  = days or PERF_LOOKBACK_DAYS
    since = (date.today() - timedelta(days=days)).isoformat()
    con   = sqlite3.connect(DB_PATH)
    pnls  = [r[0] for r in con.execute(
        "SELECT pnl FROM trades WHERE date >= ?", (since,)
    ).fetchall()]
    con.close()

    if not pnls:
        return {"win_rate": 0.50, "profit_factor": 1.0, "expectancy": 0.0, "trades": 0}

    wins      = [p for p in pnls if p > 0]
    losses    = [p for p in pnls if p <= 0]
    win_rate  = len(wins) / len(pnls)
    avg_win   = sum(wins)   / len(wins)   if wins   else 0.0
    avg_loss  = abs(sum(losses) / len(losses)) if losses else 0.0
    gp        = sum(wins)
    gl        = abs(sum(losses))
    pf        = gp / gl if gl > 0 else (999.0 if gp > 0 else 0.0)
    exp       = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)

    return {
        "trades":        len(pnls),
        "win_rate":      round(win_rate, 3),
        "avg_win":       round(avg_win, 2),
        "avg_loss":      round(avg_loss, 2),
        "profit_factor": round(pf, 2),
        "expectancy":    round(exp, 2),
    }


# ── Full EOD report ────────────────────────────────────────────────────────────

def generate_daily_performance() -> dict:
    """
    Runs at EOD. Saves to performance.json and appends to history.
    """
    today = date.today().isoformat()
    con   = sqlite3.connect(DB_PATH)

    trades = con.execute(
        "SELECT symbol,shares,entry,exit_price,pnl,pnl_pct FROM trades WHERE date=?",
        (today,),
    ).fetchall()

    audit = con.execute(
        "SELECT action,symbol,details,ts FROM audit_log WHERE date=?", (today,)
    ).fetchall()

    slippages = []
    if _table_exists(con, "execution_log"):
        rows = con.execute(
            "SELECT slippage FROM execution_log WHERE date=? AND slippage IS NOT NULL",
            (today,),
        ).fetchall()
        slippages = [r[0] for r in rows]

    con.close()

    # Core metrics
    wins   = [t for t in trades if t[4] > 0]
    losses = [t for t in trades if t[4] <= 0]
    pnls   = [t[4] for t in trades]

    total_pnl    = sum(pnls)
    gross_profit = sum(t[4] for t in wins)
    gross_loss   = abs(sum(t[4] for t in losses))
    win_rate     = len(wins) / len(trades) if trades else 0.0
    avg_win      = gross_profit / len(wins)   if wins   else 0.0
    avg_loss     = gross_loss   / len(losses) if losses else 0.0
    pf           = gross_profit / gross_loss  if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
    expectancy   = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)

    # Max intraday drawdown
    peak = cumul = max_dd = 0.0
    for p in pnls:
        cumul += p
        peak   = max(peak, cumul)
        max_dd = max(max_dd, peak - cumul)

    # Time-window breakdown (keyed by ORDER_PLACED events)
    trade_map    = {t[0]: t[4] for t in trades}
    window_pnl: dict[str, list[float]] = {}
    for action, sym, _, ts in audit:
        if action == "ORDER_PLACED" and ts:
            w = _window(ts)
            window_pnl.setdefault(w, []).append(trade_map.get(sym, 0.0))

    window_stats = {
        w: {
            "trades": len(v),
            "total":  round(sum(v), 2),
            "avg":    round(sum(v) / len(v), 2),
        }
        for w, v in window_pnl.items() if v
    }

    # Rejection reasons + top-scoring rejected candidates
    rejections: dict[str, int] = {}
    top_rejected: list[dict] = []
    for action, sym, details_str, _ in audit:
        if action in ("TRADE_REJECTED", "TRADE_BLOCKED"):
            try:
                d      = json.loads(details_str)
                reason = d.get("reason", "unknown")
                score  = d.get("score", 0)
            except Exception:
                reason, score = "unknown", 0
            rejections[reason] = rejections.get(reason, 0) + 1
            if sym and score > 0:
                top_rejected.append({"symbol": sym, "score": score, "reason": reason})
    top_rejected.sort(key=lambda x: -x["score"])
    top_rejected = top_rejected[:5]

    # Slippage
    avg_slip = round(sum(slippages) / len(slippages), 4) if slippages else 0.0

    # Rolling window
    rolling = get_recent_performance()
    lb      = PERF_LOOKBACK_DAYS

    perf = {
        "date":                today,
        "trades":              len(trades),
        "wins":                len(wins),
        "losses":              len(losses),
        "win_rate":            round(win_rate, 3),
        "total_pnl":           round(total_pnl, 2),
        "gross_profit":        round(gross_profit, 2),
        "gross_loss":          round(gross_loss, 2),
        "profit_factor":       round(pf, 2),
        "avg_win":             round(avg_win, 2),
        "avg_loss":            round(avg_loss, 2),
        "expectancy":          round(expectancy, 2),
        "max_drawdown":        round(max_dd, 2),
        "avg_slippage":        avg_slip,
        "rejections":          rejections,
        "top_rejected":        top_rejected,
        "time_windows":        window_stats,
        f"rolling_{lb}d": {
            "trades":        rolling.get("trades", 0),
            "win_rate":      rolling.get("win_rate", 0.0),
            "profit_factor": rolling.get("profit_factor", 0.0),
            "expectancy":    rolling.get("expectancy", 0.0),
            "avg_win":       rolling.get("avg_win", 0.0),
            "avg_loss":      rolling.get("avg_loss", 0.0),
        },
    }

    # High-score loser review
    losers = []
    try:
        losers = high_score_loser_review()
    except Exception:
        pass
    perf["high_score_losers"] = losers

    PERFORMANCE_FILE.write_text(json.dumps(perf, indent=2))
    with open(PERF_HISTORY_FILE, "a") as f:
        f.write(json.dumps({k: v for k, v in perf.items() if k != "rejections"}) + "\n")

    return perf


def print_performance_report(perf: dict):
    lb = PERF_LOOKBACK_DAYS
    r  = perf.get(f"rolling_{lb}d", {})

    print(f"\n{'='*64}")
    print(f"  PERFORMANCE DASHBOARD — {perf['date']}")
    print(f"{'='*64}")
    print(f"  Today   : {perf['trades']} trades  {perf['wins']}W / {perf['losses']}L  "
          f"WR {perf['win_rate']:.0%}")
    print(f"  P&L     : ${perf['total_pnl']:+.2f}  |  "
          f"Avg Win ${perf['avg_win']:+.2f}  |  Avg Loss -${perf['avg_loss']:.2f}")
    print(f"  PF      : {perf['profit_factor']:.2f}  "
          f"Expectancy ${perf['expectancy']:+.2f}/trade  "
          f"MaxDD ${perf['max_drawdown']:.2f}")
    if perf.get("avg_slippage"):
        print(f"  Slippage: avg ${perf['avg_slippage']:+.4f}/share")

    if r:
        print(f"\n  {lb}-Day Rolling:")
        print(f"  Win Rate : {r.get('win_rate', 0):.0%}   "
              f"PF: {r.get('profit_factor', 0):.2f}   "
              f"Expectancy: ${r.get('expectancy', 0):+.2f}/trade")
        print(f"  Avg Win  : ${r.get('avg_win', 0):+.2f}   "
              f"Avg Loss: -${r.get('avg_loss', 0):.2f}   "
              f"Trades: {r.get('trades', 0)}")

    if perf.get("time_windows"):
        print(f"\n  By Time Window:")
        order = ["open", "late_morning", "midday", "afternoon", "power_hour", "unknown"]
        for w in order:
            s = perf["time_windows"].get(w)
            if s:
                print(f"    {w:14s}  {s['trades']}T  ${s['total']:+.2f}  "
                      f"avg ${s['avg']:+.2f}")

    if perf.get("rejections"):
        print(f"\n  Rejection Reasons:")
        for reason, cnt in sorted(perf["rejections"].items(), key=lambda x: -x[1]):
            print(f"    {reason[:52]:52s}  {cnt}×")

    if perf.get("top_rejected"):
        print(f"\n  Top Rejected Candidates (by score):")
        for r in perf["top_rejected"]:
            print(f"    {r['symbol']:6s}  score={r['score']:3d}  {r['reason'][:52]}")

    # Expectancy by dimension (only if data available)
    try:
        dims = get_expectancy_by_dimension()
        if dims.get("by_setup"):
            print(f"\n  Expectancy by Setup Type ({PERF_LOOKBACK_DAYS}d):")
            for s, st in sorted(dims["by_setup"].items(), key=lambda x: -x[1]["expectancy"]):
                print(f"    {s:22s}  n={st['n']:3d}  WR={st['win_rate']:.0%}  "
                      f"E=${st['expectancy']:+.2f}")
        if dims.get("by_regime"):
            print(f"\n  Expectancy by Regime ({PERF_LOOKBACK_DAYS}d):")
            for r, st in sorted(dims["by_regime"].items(), key=lambda x: -x[1]["expectancy"]):
                print(f"    {r:16s}  n={st['n']:3d}  WR={st['win_rate']:.0%}  "
                      f"E=${st['expectancy']:+.2f}")
    except Exception:
        pass

    # Non-binding adaptive suggestions
    wr  = r.get("win_rate", 0.5) if r else 0.5
    pf2 = r.get("profit_factor", 1.0) if r else 1.0
    exp = r.get("expectancy", 0.0) if r else 0.0
    n   = r.get("trades", 0) if r else 0

    print(f"\n  Adaptive Suggestions (informational — not applied automatically):")
    if n < 5:
        print(f"  — Insufficient data ({n} trades) — no suggestions yet")
    else:
        if wr < 0.40:
            print(f"  ⚠  Win rate {wr:.0%} < 40% — consider raising MIN_SCORE_TO_TRADE")
        if pf2 < 1.0:
            print(f"  ⚠  Profit factor {pf2:.2f} < 1.0 — review setup criteria")
        if pf2 > 2.5 and wr >= 0.55:
            print(f"  ✓  Strong edge (PF={pf2:.2f}, WR={wr:.0%}) — thresholds working well")
        if exp > 0 and wr >= 0.40:
            print(f"  ✓  Positive expectancy ${exp:+.2f}/trade")
        if exp < 0:
            print(f"  ⚠  Negative expectancy ${exp:+.2f} — strategy losing edge, pause and review")

    # High-score loser review
    try:
        losers = perf.get("high_score_losers") or high_score_loser_review()
        print_high_score_loser_review(losers)
    except Exception:
        pass

    print(f"{'='*64}\n")


def get_weak_windows() -> set[str]:
    """
    Returns time windows to avoid based on rolling performance history.
    A window is blocked if average P&L per trade < WINDOW_BLOCK_AVG_PNL
    across at least WINDOW_BLOCK_MIN_TRADES samples.
    """
    if not PERF_HISTORY_FILE.exists():
        return set()
    try:
        lines = [l for l in PERF_HISTORY_FILE.read_text().strip().split("\n") if l]
        lines = lines[-PERF_LOOKBACK_DAYS:]

        window_avgs: dict[str, list[float]] = {}
        for line in lines:
            data = json.loads(line)
            for w, stats in data.get("time_windows", {}).items():
                if stats.get("trades", 0) > 0:
                    window_avgs.setdefault(w, []).append(stats["avg"])

        blocked = set()
        for w, avgs in window_avgs.items():
            if len(avgs) >= WINDOW_BLOCK_MIN_TRADES:
                mean_avg = sum(avgs) / len(avgs)
                if mean_avg < WINDOW_BLOCK_AVG_PNL:
                    blocked.add(w)
        return blocked
    except Exception:
        return set()


def get_expectancy_by_dimension() -> dict:
    """
    Returns expectancy broken down by time-window, market regime, and setup type.
    Reads ORDER_PLACED events from audit_log (which carry regime + setup_type in details).
    """
    since = (date.today() - timedelta(days=PERF_LOOKBACK_DAYS)).isoformat()
    con   = sqlite3.connect(DB_PATH)

    placements = con.execute(
        "SELECT symbol,details,ts FROM audit_log WHERE action='ORDER_PLACED' AND date >= ?",
        (since,),
    ).fetchall()

    trade_pnl = {
        r[0]: r[1]
        for r in con.execute(
            "SELECT symbol,pnl FROM trades WHERE date >= ?", (since,)
        ).fetchall()
    }
    con.close()

    by_window: dict[str, list[float]] = {}
    by_regime: dict[str, list[float]] = {}
    by_setup:  dict[str, list[float]] = {}

    for sym, details_str, ts in placements:
        pnl = trade_pnl.get(sym)
        if pnl is None:
            continue
        try:
            details = json.loads(details_str)
        except Exception:
            details = {}

        w  = _window(ts) if ts else "unknown"
        rg = details.get("regime", "unknown")
        st = details.get("setup_type", "unknown")

        by_window.setdefault(w,  []).append(pnl)
        by_regime.setdefault(rg, []).append(pnl)
        by_setup.setdefault(st,  []).append(pnl)

    def _stats(pnls: list[float]) -> dict:
        if not pnls:
            return {"n": 0, "win_rate": 0.0, "expectancy": 0.0, "avg_pnl": 0.0}
        wins  = [p for p in pnls if p > 0]
        losses= [p for p in pnls if p <= 0]
        wr    = len(wins) / len(pnls)
        aw    = sum(wins)   / len(wins)   if wins   else 0.0
        al    = abs(sum(losses) / len(losses)) if losses else 0.0
        exp   = (wr * aw) - ((1 - wr) * al)
        return {
            "n":          len(pnls),
            "win_rate":   round(wr, 2),
            "expectancy": round(exp, 2),
            "avg_pnl":    round(sum(pnls) / len(pnls), 2),
        }

    return {
        "by_window": {k: _stats(v) for k, v in by_window.items()},
        "by_regime": {k: _stats(v) for k, v in by_regime.items()},
        "by_setup":  {k: _stats(v) for k, v in by_setup.items()},
    }


def should_pause_trading() -> tuple[bool, str]:
    perf = get_recent_performance()
    if perf["trades"] < 5:
        return False, "insufficient data"
    if perf["win_rate"] < MIN_WIN_RATE_TO_TRADE:
        return True, (f"rolling win rate {perf['win_rate']:.0%} below "
                      f"{MIN_WIN_RATE_TO_TRADE:.0%} threshold")
    return False, "performance ok"


# ── High-Score Loser Review ────────────────────────────────────────────────────

def high_score_loser_review(conn=None) -> list[dict]:
    """
    Find all losing trades with score >= MIN_SCORE_TO_TRADE (78).
    Enriches each record with audit log details where available.
    Returns list sorted by score descending.
    """
    from config import MIN_SCORE_TO_TRADE
    since = (date.today() - timedelta(days=PERF_LOOKBACK_DAYS)).isoformat()
    _conn = conn or sqlite3.connect(DB_PATH)
    own   = conn is None

    try:
        # Losing trades
        trades = _conn.execute(
            "SELECT symbol, shares, entry, exit_price, pnl, pnl_pct, ts "
            "FROM trades WHERE date >= ? AND pnl <= 0",
            (since,),
        ).fetchall()

        if not trades:
            return []

        # Fetch corresponding ORDER_PLACED audit records for metadata
        placed = _conn.execute(
            "SELECT symbol, details, ts FROM audit_log "
            "WHERE action='ORDER_PLACED' AND date >= ?",
            (since,),
        ).fetchall()
    finally:
        if own:
            _conn.close()

    # Build a lookup from symbol→latest ORDER_PLACED details
    placed_map: dict[str, dict] = {}
    for sym, details_str, ts in placed:
        try:
            d = json.loads(details_str)
        except Exception:
            d = {}
        placed_map[sym] = {"details": d, "ts": ts}

    losers = []
    for symbol, shares, entry, exit_price, pnl, pnl_pct, ts in trades:
        meta    = placed_map.get(symbol, {})
        details = meta.get("details", {})
        score   = details.get("score", 0)

        if score < MIN_SCORE_TO_TRADE:
            continue

        # Derive tier
        from config import TIER_ELITE_MIN, TIER_HIGH_MIN
        if score >= TIER_ELITE_MIN:
            tier = "ELITE"
        elif score >= TIER_HIGH_MIN:
            tier = "HIGH"
        else:
            tier = "NORMAL"

        # Hold time
        hold_mins = 0
        try:
            entry_ts = datetime.fromisoformat(meta.get("ts") or ts)
            exit_ts  = datetime.fromisoformat(ts)
            hold_mins = round((exit_ts - entry_ts).total_seconds() / 60, 1)
        except Exception:
            pass

        # VWAP distance at entry
        vwap_dist = None
        vwap  = details.get("vwap", 0)
        if vwap and entry:
            vwap_dist = round((entry - vwap) / vwap * 100, 3)

        losers.append({
            "symbol":            symbol,
            "score":             score,
            "tier":              tier,
            "pnl":               round(pnl, 2),
            "pnl_pct":           round(pnl_pct, 3),
            "entry_time_et":     meta.get("ts", ts),
            "hold_mins":         hold_mins,
            "vwap_distance_pct": vwap_dist,
            "orb_status":        details.get("orb_breakout", None),
            "pullback_status":   details.get("pullback_quality", None),
            "momentum_at_entry": details.get("momentum", None),
            "news_impact":       details.get("top_news_impact", None),
            "spread_at_entry":   details.get("spread_pct", None),
            "exit_reason":       details.get("exit_reason", "unknown"),
            "catalyst_quality":  details.get("setup_type", "unknown"),
        })

    losers.sort(key=lambda x: -x["score"])
    return losers


def print_high_score_loser_review(losers: list[dict]):
    """Format and print the high-score loser review section."""
    if not losers:
        print(f"\n  High-Score Loser Review: no losing trades with score >= 78")
        return

    print(f"\n  High-Score Loser Review ({len(losers)} trade(s)):")
    print(f"  {'Sym':<6} {'Score':>5} {'Tier':<7} {'PnL':>8} {'HoldM':>6} "
          f"{'VwapD%':>7} {'Momentum':<14} {'Exit Reason'}")
    print(f"  {'-'*90}")
    for t in losers:
        vd  = f"{t['vwap_distance_pct']:+.2f}%" if t["vwap_distance_pct"] is not None else "  n/a"
        mom = (t["momentum_at_entry"] or "?")[:13]
        ex  = (t["exit_reason"] or "?")[:30]
        print(f"  {t['symbol']:<6} {t['score']:>5} {t['tier']:<7} "
              f"${t['pnl']:>+7.2f} {t['hold_mins']:>5.0f}m "
              f"{vd:>7} {mom:<14} {ex}")

    # Pattern summary
    if len(losers) >= 2:
        print(f"\n  Common patterns among high-score losers:")

        # Group by exit reason
        by_exit: dict[str, list[dict]] = {}
        for t in losers:
            key = t.get("exit_reason") or "unknown"
            by_exit.setdefault(key, []).append(t)

        # Group by momentum at entry
        by_mom: dict[str, list[dict]] = {}
        for t in losers:
            key = t.get("momentum_at_entry") or "unknown"
            by_mom.setdefault(key, []).append(t)

        n = len(losers)
        for reason, group in sorted(by_exit.items(), key=lambda x: -len(x[1])):
            cnt  = len(group)
            pct  = cnt / n * 100
            moms = [t.get("momentum_at_entry", "?") for t in group]
            mom_counts = {m: moms.count(m) for m in set(moms)}
            dominant_mom = max(mom_counts, key=mom_counts.get) if mom_counts else "?"

            suggestion = ""
            if reason == "rapid_invalidation" and dominant_mom in ("WEAKENING", "EXHAUSTED"):
                suggestion = "consider raising MIN_MOMENTUM_TO_TRADE threshold"
            elif reason == "time_exit" and cnt >= 2:
                suggestion = "consider tightening TIME_EXIT_MINS or raising WATCHLIST_SCORE"
            elif reason == "trailing_stop" and cnt >= 2:
                suggestion = "review TRAILING_STOP_TRIGGER_PCT — setups may be entering too late"
            elif "momentum" in reason.lower():
                suggestion = "consider requiring momentum=STRENGTHENING for this tier"

            if cnt > 1:
                print(f"    {cnt}/{n} exited on '{reason}' with momentum={dominant_mom}"
                      + (f"\n      -> {suggestion}" if suggestion else ""))

        for mom, group in sorted(by_mom.items(), key=lambda x: -len(x[1])):
            cnt = len(group)
            if cnt >= 2 and mom in ("WEAKENING", "EXHAUSTED"):
                print(f"    {cnt}/{n} entries had momentum={mom} — "
                      f"consider blocking {mom} for HIGH/ELITE tiers")
