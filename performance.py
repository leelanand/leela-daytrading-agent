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
    CLAUDE_EFFECTIVENESS_LOG_FILE,
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

    # Claude effectiveness
    perf["claude_effectiveness"] = get_claude_effectiveness(today)

    # Setup promotion candidates
    try:
        perf["setup_promotion"] = get_setup_promotion_candidates()
    except Exception:
        perf["setup_promotion"] = []

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

    # No-trade reason distribution
    try:
        print_no_trade_reason_distribution(perf)
    except Exception:
        pass

    # PAPER vs LIVE comparison
    try:
        from config import TRADING_MODE
        if TRADING_MODE == "PAPER":
            print_paper_vs_live_comparison(perf)
    except Exception:
        pass

    # Setup promotion framework
    try:
        promo = get_setup_promotion_candidates()
        if promo:
            print_promotion_report(promo)
    except Exception:
        pass

    # Claude effectiveness
    try:
        eff = perf.get("claude_effectiveness") or get_claude_effectiveness()
        print_claude_effectiveness(eff)
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


# ── Claude Effectiveness ──────────────────────────────────────────────────────

def get_claude_effectiveness(target_date: str | None = None) -> dict:
    """
    Summarise Claude decision-impact for a given date (default: today).
    Reads from claude_effectiveness.jsonl written by analyst.py.
    """
    td = target_date or date.today().isoformat()
    try:
        if not CLAUDE_EFFECTIVENESS_LOG_FILE.exists():
            return {}
        lines = [
            json.loads(ln)
            for ln in CLAUDE_EFFECTIVENESS_LOG_FILE.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        records = [r for r in lines if r.get("date") == td]
        if not records:
            return {}

        total         = len(records)
        local_rejects = sum(1 for r in records if r.get("local_only"))
        cache_hits    = sum(1 for r in records if r.get("cache_hit"))
        claude_scored = sum(1 for r in records if not r.get("local_only") and not r.get("cache_hit"))
        changed       = sum(1 for r in records if r.get("claude_changed_decision"))

        eligible      = total - local_rejects  # candidates that reached Claude or cache
        cache_rate    = round(cache_hits / eligible, 2) if eligible else 0.0
        change_rate   = round(changed / claude_scored, 2) if claude_scored else 0.0

        # Examples of decision changes
        changes = [
            {
                "symbol":       r["symbol"],
                "time":         r.get("time", ""),
                "local_score":  r["local_score"],
                "claude_score": r["claude_score"],
                "local_tradeable":  r.get("local_tradeable"),
                "claude_tradeable": r.get("claude_tradeable"),
            }
            for r in records
            if r.get("claude_changed_decision")
        ]

        return {
            "date":               td,
            "total_candidates":   total,
            "local_rejects":      local_rejects,
            "cache_hits":         cache_hits,
            "claude_scored":      claude_scored,
            "cache_hit_rate":     cache_rate,
            "decisions_changed":  changed,
            "decision_change_rate": change_rate,
            "decision_changes":   changes,
        }
    except Exception:
        return {}


def print_claude_effectiveness(eff: dict):
    if not eff:
        print("\n  Claude Effectiveness: no data for today")
        return
    print(f"\n  Claude Effectiveness — {eff['date']}")
    print(f"  {'─'*50}")
    print(f"  Candidates seen  : {eff['total_candidates']}")
    print(f"  Local rejects    : {eff['local_rejects']}  "
          f"(skipped Claude — below gate threshold)")
    print(f"  Cache hits       : {eff['cache_hits']}  "
          f"(reused score — inputs unchanged)")
    print(f"  Claude scored    : {eff['claude_scored']}  "
          f"(new API calls made)")
    print(f"  Cache hit rate   : {eff['cache_hit_rate']:.0%}  "
          f"(of eligible candidates)")
    print(f"  Decision changes : {eff['decisions_changed']}  "
          f"({eff['decision_change_rate']:.0%} of Claude-scored)")

    changes = eff.get("decision_changes", [])
    if changes:
        print(f"\n  Symbols where Claude changed tradeable decision:")
        for ch in changes:
            direction = ("↑ TRADEABLE" if ch.get("claude_tradeable")
                         else "↓ REJECTED")
            print(f"    {ch['symbol']:6s} {ch['time']:5s}  "
                  f"local={ch['local_score']:3d} → claude={ch['claude_score']:3d}  "
                  f"{direction}")
    else:
        print(f"  No tradeable decisions changed by Claude today.")

    if eff["claude_scored"] > 0 and eff["decision_change_rate"] < 0.10:
        print(f"\n  ⓘ  Claude changed <10% of its decisions — consider raising "
              f"CLAUDE_MIN_LOCAL_SCORE to reduce calls further.")
    elif eff["decision_change_rate"] > 0.40:
        print(f"\n  ⓘ  Claude changed >40% of decisions — it is adding material value; "
              f"keep current threshold.")


# ── No-Trade Reason Distribution ──────────────────────────────────────────────

_REJECTION_CATEGORIES = {
    "spread":           ["spread", "wide_spread"],
    "rvol_weak":        ["rvol", "rel_volume", "low_volume_stock_rvol"],
    "stale_candidate":  ["stale", "expired", "candidate_age", "decay"],
    "regime":           ["regime", "no_trade", "low_volume_mode"],
    "price_extension":  ["vol_extension", "move_from_open", "overextended"],
    "failed_momentum":  ["momentum", "weakening", "exhausted"],
    "failed_orb":       ["orb", "opening_range"],
    "failed_pullback":  ["pullback_reject"],
    "risk_cap":         ["daily_loss", "max_positions", "max_trades", "risk"],
    "duplicate":        ["duplicate_exposure", "symbol_taken"],
    "feed_quality":     ["intraday_quality", "data_quality", "feed"],
    "event_risk":       ["earnings", "halt"],
    "weak_window":      ["weak_window", "midday_block", "lockout"],
    "adaptive_pause":   ["adaptive_pause"],
    "failed_breakout":  ["failed_breakout"],
    "quality_override_failed": ["quality_override failed"],
}


def _categorize_rejection(reason_str: str) -> str:
    r = reason_str.lower()
    for category, keywords in _REJECTION_CATEGORIES.items():
        if any(kw in r for kw in keywords):
            return category
    return "other"


def print_no_trade_reason_distribution(perf: dict):
    """Print a categorized breakdown of why candidates were rejected today."""
    rejections = perf.get("rejections", {})
    if not rejections:
        return
    categorized: dict[str, int] = {}
    for reason, cnt in rejections.items():
        cat = _categorize_rejection(reason)
        categorized[cat] = categorized.get(cat, 0) + cnt

    total = sum(categorized.values())
    print(f"\n  No-Trade Reason Distribution ({total} rejections):")
    for cat, cnt in sorted(categorized.items(), key=lambda x: -x[1]):
        bar = "#" * min(20, cnt)
        pct = cnt / total * 100
        print(f"    {cat:<28s}  {cnt:3d}  {pct:4.0f}%  {bar}")


# ── Setup Promotion Framework ──────────────────────────────────────────────────

def get_setup_promotion_candidates(
    min_trades: int = 20,
    min_pf: float = 1.3,
    min_wr: float = 0.45,
    lookback_days: int = 60,
) -> list[dict]:
    """
    Identify PAPER setups ready for promotion to LIVE trading.
    Criteria: min_trades trades, positive expectancy, profit_factor >= min_pf,
              avg_win > avg_loss, works across multiple days.
    """
    since = (date.today() - timedelta(days=lookback_days)).isoformat()
    con   = sqlite3.connect(DB_PATH)

    # Get ORDER_PLACED events with setup_type
    placements = con.execute(
        "SELECT symbol, details, ts, date FROM audit_log "
        "WHERE action='ORDER_PLACED' AND date >= ?",
        (since,),
    ).fetchall()

    # Get P&L per symbol
    trade_pnl = {}
    for r in con.execute(
        "SELECT symbol, pnl, date FROM trades WHERE date >= ?", (since,)
    ).fetchall():
        key = (r[0], r[2])
        trade_pnl[key] = r[1]

    con.close()

    by_setup: dict[str, dict] = {}
    for sym, details_str, ts, trade_date in placements:
        try:
            d = json.loads(details_str)
        except Exception:
            d = {}
        setup = d.get("setup_type") or d.get("setup") or "unknown"
        if setup == "unknown":
            continue
        pnl = trade_pnl.get((sym, trade_date))
        if pnl is None:
            continue
        s = by_setup.setdefault(setup, {"pnls": [], "days": set(), "scores": []})
        s["pnls"].append(pnl)
        s["days"].add(trade_date)
        if d.get("score"):
            s["scores"].append(d["score"])

    promotable = []
    for setup, data in by_setup.items():
        pnls  = data["pnls"]
        n     = len(pnls)
        if n < min_trades:
            continue
        wins    = [p for p in pnls if p > 0]
        losses  = [p for p in pnls if p <= 0]
        wr      = len(wins) / n
        avg_win = sum(wins) / len(wins)   if wins   else 0.0
        avg_los = abs(sum(losses) / len(losses)) if losses else 0.0
        gp      = sum(wins)
        gl      = abs(sum(losses))
        pf      = gp / gl if gl > 0 else (999.0 if gp > 0 else 0.0)
        exp     = (wr * avg_win) - ((1 - wr) * avg_los)
        n_days  = len(data["days"])
        avg_sc  = round(sum(data["scores"]) / len(data["scores"]), 1) if data["scores"] else 0

        ready = (
            n >= min_trades
            and exp > 0
            and pf >= min_pf
            and wr >= min_wr
            and avg_win > avg_los
            and n_days >= 3
        )
        promotable.append({
            "setup_type":     setup,
            "trades":         n,
            "win_rate":       round(wr, 3),
            "profit_factor":  round(pf, 2),
            "expectancy":     round(exp, 2),
            "avg_win":        round(avg_win, 2),
            "avg_loss":       round(avg_los, 2),
            "days_traded":    n_days,
            "avg_score":      avg_sc,
            "promote_ready":  ready,
        })

    promotable.sort(key=lambda x: (-x["profit_factor"], -x["expectancy"]))
    return promotable


def print_promotion_report(candidates: list[dict]):
    """Print setup promotion status."""
    if not candidates:
        return
    print(f"\n  Setup Promotion Framework ({len(candidates)} setups tracked):")
    print(f"  {'Setup':<22s} {'N':>4} {'WR':>5} {'PF':>5} {'Exp':>7} {'AvgW':>7} {'AvgL':>7} {'Days':>4} {'Status'}")
    print(f"  {'-'*84}")
    for s in candidates:
        status = "PROMOTE" if s["promote_ready"] else "building"
        print(f"  {s['setup_type']:<22s} {s['trades']:>4} {s['win_rate']:>4.0%} "
              f"{s['profit_factor']:>5.2f} ${s['expectancy']:>+6.2f} "
              f"${s['avg_win']:>6.2f} ${s['avg_loss']:>6.2f} {s['days_traded']:>4}  {status}")


# ── PAPER vs LIVE Comparison ───────────────────────────────────────────────────

def print_paper_vs_live_comparison(perf: dict):
    """
    Compare all PAPER trades vs the subset that would have qualified as LIVE.
    Uses PAPER_TRADE_TAGS audit entries which store effective_min and experimental flags.
    """
    today = date.today().isoformat()
    con   = sqlite3.connect(DB_PATH)
    tags  = con.execute(
        "SELECT symbol, details FROM audit_log WHERE action='PAPER_TRADE_TAGS' AND date=?",
        (today,),
    ).fetchall()
    con.close()

    if not tags:
        return

    all_trades = []
    for sym, details_str in tags:
        try:
            d = json.loads(details_str)
            d["symbol"] = sym
            all_trades.append(d)
        except Exception:
            pass

    total      = len(all_trades)
    live_elig  = [t for t in all_trades if not t.get("experimental", True)]
    experim    = [t for t in all_trades if t.get("experimental", False)]
    qo_trades  = [t for t in all_trades if t.get("quality_override", False)]

    print(f"\n  PAPER vs LIVE Comparison ({today}):")
    print(f"  {'─'*52}")
    print(f"  Total PAPER trades          : {total}")
    print(f"  LIVE-eligible (score>=78)    : {len(live_elig)}")
    print(f"  Experimental (below LIVE bar): {len(experim)}")
    print(f"  Quality override applied    : {len(qo_trades)}")

    if all_trades:
        by_band: dict[str, int] = {}
        for t in all_trades:
            band = t.get("score_band", "unknown")
            by_band[band] = by_band.get(band, 0) + 1
        print(f"\n  By score band:")
        for band in ("elite", "high", "normal", "below_live", "experimental"):
            cnt = by_band.get(band, 0)
            if cnt:
                print(f"    {band:<16s}  {cnt}")

    if all_trades:
        by_setup: dict[str, int] = {}
        for t in all_trades:
            s = t.get("setup_type", "unknown")
            by_setup[s] = by_setup.get(s, 0) + 1
        print(f"\n  By setup type:")
        for s, cnt in sorted(by_setup.items(), key=lambda x: -x[1]):
            print(f"    {s:<22s}  {cnt}")


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
