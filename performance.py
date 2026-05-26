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

    # Rejection reasons
    rejections: dict[str, int] = {}
    for action, sym, details_str, _ in audit:
        if action in ("TRADE_REJECTED", "TRADE_BLOCKED"):
            try:
                reason = json.loads(details_str).get("reason", "unknown")
            except Exception:
                reason = "unknown"
            rejections[reason] = rejections.get(reason, 0) + 1

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
        "time_windows":        window_stats,
        f"rolling_{lb}d": {
            "trades":        rolling["trades"],
            "win_rate":      rolling["win_rate"],
            "profit_factor": rolling["profit_factor"],
            "expectancy":    rolling["expectancy"],
            "avg_win":       rolling["avg_win"],
            "avg_loss":      rolling["avg_loss"],
        },
    }

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
          f"Avg Win ${perf['avg_win']:+.2f}  |  Avg Loss −${perf['avg_loss']:.2f}")
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
              f"Avg Loss: −${r.get('avg_loss', 0):.2f}   "
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

    print(f"{'='*64}\n")


def should_pause_trading() -> tuple[bool, str]:
    perf = get_recent_performance()
    if perf["trades"] < 5:
        return False, "insufficient data"
    if perf["win_rate"] < MIN_WIN_RATE_TO_TRADE:
        return True, (f"rolling win rate {perf['win_rate']:.0%} below "
                      f"{MIN_WIN_RATE_TO_TRADE:.0%} threshold")
    return False, "performance ok"
