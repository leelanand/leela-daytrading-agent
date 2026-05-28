"""
Trading Day Dashboard — http://localhost:8765
Serves a live status page for the Alpaca and IBKR trading agents.
Auto-refreshes every 30 seconds via JavaScript fetch.

Run:  python dashboard.py
Open: http://localhost:8765
"""
import json
import re
import sqlite3
import time
import urllib.request
from datetime import date, datetime
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from zoneinfo import ZoneInfo

BST  = ZoneInfo("Europe/London")
ET   = ZoneInfo("America/New_York")
PORT = 8765

ALPACA_DIR = Path(__file__).parent
IBKR_DIR   = Path(r"C:\Users\leela\leela-ibkr-agent")

# BST schedule: (hhmm, label, event_key, window)
SCHEDULE = [
    (1330, "Research — fundamentals + Claude brief", "RESEARCH",     "pre"),
    (1440, "Precheck — live gate TEST_01/02/14/20",  "PRECHECK",     "pre"),
    (1445, "Prescan — score candidates, no orders",  "PRESCAN",      "pre"),
    (1450, "Continuous scan (5/15/10 min cadence)",  "CONTINUOUS",   "trading"),
    (1500, "Monitor loop (45s cadence)",             "MONITOR",      "trading"),
    (1700, "Midday block — no new entries",          "MIDDAY_START", "blocked"),
    (1800, "Afternoon session resumes",              "MIDDAY_END",   "trading"),
    (2030, "Cutoff — cancel unfilled limit orders",  "CUTOFF",       "closing"),
    (2044, "Force close all positions",              "FORCE_CLOSE",  "closing"),
    (2055, "Verify flat",                            "VERIFIED",     "eod"),
    (2115, "EOD report",                             "REPORT",       "eod"),
    (2130, "Performance dashboard",                  "PERFORMANCE",  "eod"),
]

LOG_RE = re.compile(
    r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+(\S+)\s*(\S*)\s*(.*)"
)


# ── Data collection ───────────────────────────────────────────────────────────

def _now_bst() -> datetime:
    return datetime.now(BST)


def _hhmm(dt: datetime) -> int:
    return dt.hour * 100 + dt.minute


def _read_audit(directory: Path) -> list[dict]:
    today   = date.today().isoformat()
    entries = []
    try:
        for line in (directory / "audit.log").read_text(encoding="utf-8", errors="replace").splitlines():
            if today not in line:
                continue
            m = LOG_RE.match(line)
            if m:
                entries.append({
                    "ts":     m.group(1),
                    "action": m.group(2),
                    "symbol": m.group(3),
                    "detail": m.group(4)[:200],
                    "raw":    line[:200],
                })
    except Exception:
        pass
    return entries


def _action_set(audit: list[dict]) -> set[str]:
    return {e["action"] for e in audit}


def _last_scan_time(audit: list[dict]) -> str:
    scans = [e["ts"] for e in audit if e["action"] in
             ("SCAN_START", "SCAN_DONE", "PRESCAN_DONE")]
    return scans[-1][11:16] if scans else ""


def _trades_today(db_path: Path) -> list[dict]:
    today = date.today().isoformat()
    try:
        con  = sqlite3.connect(db_path)
        rows = con.execute(
            "SELECT symbol,shares,entry,exit_price,pnl,pnl_pct,reason,ts "
            "FROM trades WHERE date=? ORDER BY ts",
            (today,),
        ).fetchall()
        con.close()
        return [
            {"symbol": r[0], "shares": r[1], "entry": r[2], "exit": r[3],
             "pnl": r[4], "pnl_pct": r[5], "reason": r[6], "ts": r[7]}
            for r in rows
        ]
    except Exception:
        return []


def _research_done() -> bool:
    today = date.today().isoformat()
    for d in (ALPACA_DIR, IBKR_DIR):
        try:
            rc = json.loads((d / "research_cache.json").read_text())
            if rc.get("generated_at", "")[:10] == today:
                return True
        except Exception:
            pass
    return False


def _research_symbols() -> tuple[int, int]:
    """Returns (total, claude_scored)."""
    for d in (ALPACA_DIR, IBKR_DIR):
        try:
            rc  = json.loads((d / "research_cache.json").read_text())
            sym = rc.get("symbols", {})
            total  = len(sym)
            scored = sum(1 for v in sym.values()
                         if v.get("research_brief", "").startswith("Not") is False
                         and v.get("research_brief"))
            return total, scored
        except Exception:
            pass
    return 0, 0


def _claude_eff() -> dict:
    today = date.today().isoformat()
    for d in (ALPACA_DIR, IBKR_DIR):
        try:
            f = d / "claude_effectiveness.jsonl"
            if not f.exists():
                continue
            records = [
                json.loads(ln) for ln in f.read_text(encoding="utf-8").splitlines()
                if ln.strip() and json.loads(ln).get("date") == today
            ]
            if not records:
                continue
            total      = len(records)
            rejects    = sum(1 for r in records if r.get("local_only"))
            hits       = sum(1 for r in records if r.get("cache_hit"))
            scored     = sum(1 for r in records if not r.get("local_only") and not r.get("cache_hit"))
            changed    = sum(1 for r in records if r.get("claude_changed_decision"))
            return {
                "total": total, "rejects": rejects,
                "hits": hits, "scored": scored, "changed": changed,
            }
        except Exception:
            pass
    return {}


def _perf_today() -> dict:
    today = date.today().isoformat()
    for d in (ALPACA_DIR, IBKR_DIR):
        try:
            p = json.loads((d / "performance.json").read_text())
            if p.get("date") == today:
                return p
        except Exception:
            pass
    return {}


def _candidates_today() -> tuple[int, int]:
    """Returns (tradeable, watchlist) from candidates.json."""
    today = date.today().isoformat()
    for d in (ALPACA_DIR, IBKR_DIR):
        try:
            f = d / "candidates.json"
            if f.stat().st_mtime and date.fromtimestamp(f.stat().st_mtime).isoformat() == today:
                cands = json.loads(f.read_text())
                t = sum(1 for c in cands if c.get("tradeable"))
                w = sum(1 for c in cands if c.get("watchlist"))
                return t, w
        except Exception:
            pass
    return 0, 0


def _ibkr_log_tail() -> list[str]:
    try:
        lines = (IBKR_DIR / "trading_day.log").read_text(encoding="utf-8", errors="replace").splitlines()
        return [l for l in lines if l.strip()][-8:]
    except Exception:
        return []


def _env_dict(directory: Path) -> dict:
    result = {}
    try:
        for line in (directory / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                result[k.strip()] = v.strip()
    except Exception:
        pass
    return result


_balance_cache: dict = {}
_balance_cache_ts: float = 0.0
_BALANCE_TTL = 60  # seconds


def _fetch_alpaca_balance() -> str:
    env     = _env_dict(ALPACA_DIR)
    api_key = env.get("ALPACA_API_KEY", "")
    secret  = env.get("ALPACA_SECRET_KEY", "")
    paper   = env.get("PAPER_TRADING", "false").lower() == "true"
    if not api_key:
        return ""
    try:
        base = "https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets"
        req  = urllib.request.Request(
            f"{base}/v2/account",
            headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            val  = data.get("portfolio_value") or data.get("equity", "")
            return f"{float(val):,.2f}" if val else ""
    except Exception:
        return ""


def _fetch_ibkr_balance() -> str:
    try:
        import asyncio
        from ib_insync import IB
        env  = _env_dict(IBKR_DIR)
        port = int(env.get("IBKR_PORT", "4001"))

        async def _get() -> str:
            ib = IB()
            await ib.connectAsync("127.0.0.1", port, clientId=9, timeout=5)
            await asyncio.sleep(0.5)
            for v in ib.accountValues():
                if v.tag == "NetLiquidation" and v.currency == "USD":
                    val = v.value
                    ib.disconnect()
                    return f"{float(val):,.2f}"
            ib.disconnect()
            return ""

        return asyncio.run(_get())
    except Exception:
        return ""


def _live_balances() -> dict:
    global _balance_cache, _balance_cache_ts
    if time.time() - _balance_cache_ts < _BALANCE_TTL:
        return _balance_cache
    _balance_cache = {
        "alpaca": _fetch_alpaca_balance(),
        "ibkr":   _fetch_ibkr_balance(),
    }
    _balance_cache_ts = time.time()
    return _balance_cache


def _read_env_mode(directory: Path) -> str:
    """Read PAPER_TRADING from agent's .env file. Returns 'LIVE' or 'PAPER'."""
    try:
        for line in (directory / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("PAPER_TRADING"):
                val = line.split("=", 1)[-1].strip().lower()
                return "PAPER" if val in ("true", "1", "yes") else "LIVE"
    except Exception:
        pass
    return "PAPER"


def _agent_status() -> dict:
    agents = {}
    for name, d in [("alpaca", ALPACA_DIR), ("ibkr", IBKR_DIR)]:
        s = {"running": False, "mode": _read_env_mode(d), "regime": "—", "regime_note": "",
             "portfolio": "", "pnl": 0.0, "last_ts": ""}
        log_path = d / "trading_day.log"
        if log_path.exists():
            age_s = (datetime.now() - datetime.fromtimestamp(log_path.stat().st_mtime)).total_seconds()
            s["running"] = age_s < 300
            lines = [l for l in log_path.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
            # Last timestamped line
            for l in reversed(lines):
                if l.startswith("[20"):
                    s["last_ts"] = l[1:17]
                    break
            # Override mode from log if agent is actively running
            head = "\n".join(lines[:30])
            if "[LIVE]" in head:
                s["mode"] = "LIVE"
            elif "[PAPER]" in head:
                s["mode"] = "PAPER"
            # Portfolio balance — today's lines only to avoid stale log
            today_str = date.today().isoformat()
            for l in reversed(lines):
                if today_str not in l:
                    continue
                if "Portfolio" in l and "$" in l:
                    try:
                        s["portfolio"] = l.split("$")[1].split()[0].replace(",", "")
                    except Exception:
                        pass
                    break
        # Regime
        rc_path = d / "regime_cache.json"
        if rc_path.exists():
            try:
                rc = json.loads(rc_path.read_text())
                s["regime"]      = rc.get("regime", "—")
                s["regime_note"] = rc.get("note", "")[:80]
            except Exception:
                pass
        # P&L
        pf_path = d / "performance.json"
        if pf_path.exists():
            try:
                p = json.loads(pf_path.read_text())
                if p.get("date") == date.today().isoformat():
                    s["pnl"] = p.get("total_pnl", 0.0)
            except Exception:
                pass
        agents[name] = s

    # Override portfolio with live API balance (cached 60s)
    balances = _live_balances()
    for name in agents:
        live_bal = balances.get(name, "")
        if live_bal:
            agents[name]["portfolio"] = live_bal

    return agents


def _gappers_today() -> list[dict]:
    today = date.today().isoformat()
    seen: set[str] = set()
    merged: list[dict] = []
    # Both agents share the same cache file (IBKR points to Alpaca's), but try both paths
    for d in (ALPACA_DIR, IBKR_DIR):
        try:
            f = d / "gappers_today.json"
            if not f.exists():
                continue
            data = json.loads(f.read_text())
            if data.get("date") != today:
                continue
            for g in data.get("details", []):
                sym = g.get("symbol", "")
                if sym and sym not in seen:
                    seen.add(sym)
                    merged.append(g)
        except Exception:
            pass
    merged.sort(key=lambda x: -x.get("score", 0))
    return merged


def _live_gate_status() -> dict:
    """Read live_gate_state.json from both agents. Returns first fresh one found."""
    today = date.today().isoformat()
    result = {}
    for name, d in [("alpaca", ALPACA_DIR), ("ibkr", IBKR_DIR)]:
        try:
            f = d / "live_gate_state.json"
            if not f.exists():
                continue
            data = json.loads(f.read_text())
            if data.get("date") == today:
                result[name] = data
        except Exception:
            pass
    return result


def _feed_health() -> dict:
    """Read IBKR feed_health.json for IB Gateway status and quote age."""
    try:
        f = IBKR_DIR / "feed_health.json"
        if f.exists():
            return json.loads(f.read_text())
    except Exception:
        pass
    return {}


def _ibkr_trades_today() -> list[dict]:
    return _trades_today(IBKR_DIR / "daytrades.db")


# ── Schedule status ───────────────────────────────────────────────────────────

def _schedule_rows(audit: list[dict]) -> list[dict]:
    now       = _now_bst()
    hhmm_now  = _hhmm(now)
    actions   = _action_set(audit)
    today     = date.today().isoformat()

    # Derive booleans
    research_ok     = _research_done()
    precheck_ok     = "LIVE_GATE_PASS"  in actions
    precheck_fail   = "LIVE_GATE_FAIL"  in actions
    prescan_ok      = "PRESCAN_DONE"    in actions
    prescan_skipped = "PRESCAN_SKIPPED" in actions
    force_ok        = "FORCE_CLOSE"     in actions
    cutoff_ok       = "CUTOFF_DONE"     in actions or "CUTOFF" in actions
    verified_ok     = "VERIFIED" in actions or "EOD_VERIFIED" in actions
    perf_ok         = bool(_perf_today())
    scan_count      = sum(1 for e in audit if e["action"] in ("SCAN_DONE", "SCAN_START", "CONTINUOUS_SCAN"))
    last_scan       = _last_scan_time(audit)
    any_order       = "ORDER_PLACED" in actions
    monitor_active  = "MONITOR_START"   in actions
    regime_entry    = next((e for e in audit if e["action"] == "REGIME_DETECTED"), None)
    regime_note     = ""
    if regime_entry:
        try:
            raw = regime_entry["raw"]
            d = json.loads(raw[raw.index("{"):])
            regime_note = d.get("regime", "")
        except Exception:
            pass

    t_count, w_count = _candidates_today()

    rows = []
    for hhmm, label, key, window in SCHEDULE:
        past = hhmm_now >= hhmm
        if key == "RESEARCH":
            if research_ok:
                status, note = "done", "cache fresh"
            elif past:
                status, note = "warn", "expected by now"
            else:
                status, note = "pending", ""

        elif key == "PRECHECK":
            if precheck_ok:
                status, note = "done", "LIVE_ENABLED=true"
            elif precheck_fail:
                status, note = "warn", "LIVE_ENABLED=false"
            elif past:
                status, note = "warn", "expected by now"
            else:
                status, note = "pending", ""

        elif key == "PRESCAN":
            if prescan_ok:
                status, note = "done", f"{t_count} candidates"
            elif prescan_skipped:
                status, note = "skip", f"regime={regime_note}"
            elif past:
                status, note = "warn", "expected by now"
            else:
                status, note = "pending", ""

        elif key == "CONTINUOUS":
            in_midday = 1700 <= hhmm_now < 1800
            if force_ok:
                status, note = "done", "closed"
            elif scan_count > 0 and not in_midday and hhmm_now < 2030:
                status, note = "active", f"{scan_count} scans{'  order placed' if any_order else ''}"
            elif in_midday:
                status, note = "active", "midday block"
            elif past:
                status, note = "pending", ""
            else:
                status, note = "pending", ""

        elif key == "MONITOR":
            if force_ok:
                status, note = "done", "flat"
            elif monitor_active and hhmm_now < 2030:
                status, note = "active", "45s cadence"
            elif past:
                status, note = "pending", ""
            else:
                status, note = "pending", ""

        elif key == "MIDDAY_START":
            status = "active" if 1700 <= hhmm_now < 1800 else ("done" if past else "pending")
            note   = ""

        elif key == "MIDDAY_END":
            status = "done" if past else ("active" if hhmm_now >= 1700 else "pending")
            note   = ""

        elif key == "CUTOFF":
            if cutoff_ok:
                status, note = "done", "orders cancelled"
            elif past:
                status, note = "warn", "expected by now"
            else:
                status, note = "pending", ""

        elif key == "FORCE_CLOSE":
            if force_ok:
                status, note = "done", "all flat"
            elif past:
                status, note = "warn", "expected by now"
            else:
                status, note = "pending", ""

        elif key == "VERIFIED":
            if verified_ok:
                status, note = "done", "positions=0"
            elif past:
                status, note = "warn", ""
            else:
                status, note = "pending", ""

        elif key == "REPORT":
            if perf_ok:
                p = _perf_today()
                pnl = p.get("total_pnl", 0)
                status = "done"
                note   = f"P&L ${pnl:+.2f}"
            elif past:
                status, note = "warn", ""
            else:
                status, note = "pending", ""

        elif key == "PERFORMANCE":
            if perf_ok:
                status, note = "done", "dashboard written"
            elif past:
                status, note = "warn", ""
            else:
                status, note = "pending", ""

        else:
            status, note = "pending", ""

        # ET time
        h, m  = divmod(hhmm, 100)
        et_h  = (h - 5) % 24
        et    = f"{et_h:02d}:{m:02d}"

        rows.append({
            "bst":    f"{h:02d}:{m:02d}",
            "et":     et,
            "label":  label,
            "status": status,
            "note":   note,
            "window": window,
        })

    return rows


# ── JSON API ──────────────────────────────────────────────────────────────────

def _build_status() -> dict:
    now_bst = _now_bst()
    now_et  = datetime.now(ET)
    audit   = _read_audit(ALPACA_DIR)
    trades  = _trades_today(ALPACA_DIR / "daytrades.db")
    ibkr_trades = _ibkr_trades_today()
    eff     = _claude_eff()
    perf    = _perf_today()
    ibkr_log = _ibkr_log_tail()
    res_total, res_scored = _research_symbols()
    live_gate = _live_gate_status()
    feed_health = _feed_health()

    total_pnl = sum(t["pnl"] for t in trades) if trades else 0.0
    ibkr_pnl  = sum(t["pnl"] for t in ibkr_trades) if ibkr_trades else 0.0
    wins      = sum(1 for t in trades if t["pnl"] > 0)
    losses    = sum(1 for t in trades if t["pnl"] <= 0)

    # Recent audit events (last 12 distinct actions, skip noise)
    SKIP = {"LOW_VOLUME_MODE", "MARKET_REGIME"}
    recent = [e for e in audit if e["action"] not in SKIP][-12:]

    return {
        "now_bst":    now_bst.strftime("%H:%M:%S BST"),
        "now_et":     now_et.strftime("%H:%M:%S ET"),
        "today":      date.today().isoformat(),
        "schedule":   _schedule_rows(audit),
        "trades":     trades,
        "ibkr_trades": ibkr_trades,
        "total_pnl":  round(total_pnl, 2),
        "ibkr_pnl":   round(ibkr_pnl, 2),
        "wins":       wins,
        "losses":     losses,
        "claude_eff": eff,
        "research":   {"total": res_total, "claude_scored": res_scored},
        "recent_log": recent,
        "ibkr_log":   ibkr_log,
        "agents":     _agent_status(),
        "gappers":    _gappers_today(),
        "perf":       {k: perf.get(k) for k in
                       ("win_rate", "profit_factor", "expectancy", "trades")
                       if k in perf},
        "live_gate":  live_gate,
        "feed_health": feed_health,
    }


# ── HTTP handler ──────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Leela Trading Dashboard</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d1117; color: #e6edf3; font-family: 'Cascadia Code', 'Fira Mono', monospace; font-size: 13px; padding: 16px; }
  h1 { font-size: 18px; color: #58a6ff; margin-bottom: 4px; }
  .subtitle { color: #8b949e; font-size: 12px; margin-bottom: 16px; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 12px; }
  .card h2 { font-size: 13px; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 10px; border-bottom: 1px solid #21262d; padding-bottom: 6px; }
  table { width: 100%; border-collapse: collapse; }
  th { color: #8b949e; font-weight: normal; text-align: left; padding: 4px 8px; font-size: 11px; }
  td { padding: 5px 8px; border-top: 1px solid #21262d; }
  tr:first-child td { border-top: none; }
  .done    { color: #3fb950; }
  .active  { color: #58a6ff; }
  .warn    { color: #d29922; }
  .skip    { color: #a371f7; }
  .pending { color: #484f58; }
  .dot-done    { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #3fb950; margin-right: 6px; }
  .dot-active  { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #58a6ff; margin-right: 6px; animation: pulse 1.5s infinite; }
  .dot-warn    { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #d29922; margin-right: 6px; }
  .dot-skip    { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #a371f7; margin-right: 6px; }
  .dot-pending { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #30363d; margin-right: 6px; }
  .win-trading { color: #3fb950; font-size: 10px; font-weight: bold; letter-spacing: .5px; }
  .win-blocked { color: #f85149; font-size: 10px; font-weight: bold; letter-spacing: .5px; }
  .win-closing { color: #d29922; font-size: 10px; font-weight: bold; letter-spacing: .5px; }
  .win-pre     { color: #8b949e; font-size: 10px; letter-spacing: .5px; }
  .win-eod     { color: #8b949e; font-size: 10px; letter-spacing: .5px; }
  tr.row-blocked td { background: #1a1010; }
  tr.row-trading td { background: #0d1a0d; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
  .pnl-pos { color: #3fb950; }
  .pnl-neg { color: #f85149; }
  .pnl-zero { color: #8b949e; }
  .log-entry { font-size: 11px; color: #8b949e; padding: 2px 0; border-top: 1px solid #21262d; }
  .log-entry:first-child { border-top: none; }
  .log-action { color: #e6edf3; }
  .log-sym { color: #79c0ff; }
  .header-bar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
  .time-display { font-size: 22px; color: #e6edf3; }
  .time-et { font-size: 14px; color: #8b949e; margin-left: 12px; }
  .refresh-info { font-size: 11px; color: #484f58; }
  #last-refresh { color: #58a6ff; }
  .metric { display: inline-block; margin-right: 20px; }
  .metric-val { font-size: 20px; }
  .metric-label { font-size: 10px; color: #8b949e; text-transform: uppercase; }
  .current-row td { background: #1c2128; }
  .full-width { grid-column: 1 / -1; }
  .agent-card { display: flex; gap: 20px; flex-wrap: wrap; }
  .agent-block { flex: 1; min-width: 260px; background: #0d1117; border: 1px solid #30363d; border-radius: 6px; padding: 10px 14px; }
  .agent-name  { font-size: 13px; font-weight: bold; color: #e6edf3; margin-bottom: 6px; display: flex; align-items: center; gap: 8px; }
  .badge-paper { background:#1f4068; color:#58a6ff; border-radius:4px; padding:1px 7px; font-size:10px; font-weight:bold; letter-spacing:.5px; }
  .badge-live  { background:#3d1a1a; color:#f85149; border-radius:4px; padding:1px 7px; font-size:10px; font-weight:bold; letter-spacing:.5px; }
  .badge-run   { background:#1a3d1a; color:#3fb950; border-radius:4px; padding:1px 7px; font-size:10px; }
  .badge-stop  { background:#3d1a1a; color:#f85149; border-radius:4px; padding:1px 7px; font-size:10px; }
  .agent-row   { display: flex; justify-content: space-between; padding: 3px 0; border-top: 1px solid #21262d; font-size: 12px; }
  .agent-row:first-of-type { border-top: none; }
  .agent-key   { color: #8b949e; }
  .gapper-chip { display:inline-block; background:#1f2d1f; border:1px solid #2d4a2d; border-radius:12px; padding:2px 10px; margin:3px; font-size:11px; }
  .gapper-sym  { color:#3fb950; font-weight:bold; }
  .gapper-pct  { color:#8b949e; margin-left:4px; }
  .gapper-vol  { color:#484f58; margin-left:4px; font-size:10px; }
  .gate-row    { display:flex; justify-content:space-between; padding:4px 0; border-top:1px solid #21262d; font-size:12px; }
  .gate-row:first-of-type { border-top:none; }
  .gate-key    { color:#8b949e; }
  .gate-pass   { color:#3fb950; font-weight:bold; }
  .gate-fail   { color:#f85149; font-weight:bold; }
  .gate-none   { color:#484f58; }
  .feed-ok     { color:#3fb950; }
  .feed-warn   { color:#d29922; }
  .feed-err    { color:#f85149; }
  .feed-row    { display:flex; justify-content:space-between; padding:4px 0; border-top:1px solid #21262d; font-size:12px; }
  .feed-row:first-of-type { border-top:none; }
</style>
</head>
<body>
<div class="header-bar">
  <div>
    <h1>Leela Trading Dashboard</h1>
    <div class="subtitle">Alpaca LIVE + IBKR LIVE — IBKR real-time market data — auto-refresh every 30s</div>
  </div>
  <div style="text-align:right">
    <div class="time-display" id="clock-bst">--:-- BST</div>
    <div class="time-et" id="clock-et">--:-- ET</div>
    <div class="refresh-info">Last update: <span id="last-refresh">—</span></div>
  </div>
</div>

<div class="card" style="margin-bottom:16px">
  <h2>Agent Status</h2>
  <div class="agent-card" id="agents-block"></div>
</div>

<div class="grid">

  <!-- Schedule -->
  <div class="card full-width">
    <h2>Trading Day Schedule</h2>
    <table>
      <tr><th>BST</th><th>ET</th><th>Window</th><th>Event</th><th>Status</th><th>Notes</th></tr>
      <tbody id="schedule-body"></tbody>
    </table>
  </div>

  <!-- Live Gate Status -->
  <div class="card">
    <h2>Live Gate</h2>
    <div id="live-gate-block"><span style="color:#484f58">Waiting for precheck...</span></div>
  </div>

  <!-- IB Gateway / Feed Health -->
  <div class="card">
    <h2>IB Gateway &amp; Feed</h2>
    <div id="feed-health-block"><span style="color:#484f58">No feed health data yet.</span></div>
  </div>

  <!-- Alpaca P&L -->
  <div class="card">
    <h2>Alpaca — Today's Trades</h2>
    <div id="pnl-summary" style="margin-bottom:10px"></div>
    <table>
      <tr><th>Symbol</th><th>Shares</th><th>Entry</th><th>Exit</th><th>P&L</th><th>Reason</th></tr>
      <tbody id="trades-body"></tbody>
    </table>
    <div id="no-trades" style="color:#484f58;font-size:12px;display:none">No trades today yet.</div>
  </div>

  <!-- IBKR P&L -->
  <div class="card">
    <h2>IBKR — Today's Trades</h2>
    <div id="ibkr-pnl-summary" style="margin-bottom:10px"></div>
    <table>
      <tr><th>Symbol</th><th>Shares</th><th>Entry</th><th>Exit</th><th>P&L</th><th>Reason</th></tr>
      <tbody id="ibkr-trades-body"></tbody>
    </table>
    <div id="no-ibkr-trades" style="color:#484f58;font-size:12px;display:none">No IBKR trades today yet.</div>
  </div>

  <!-- Claude Effectiveness -->
  <div class="card">
    <h2>Claude Effectiveness</h2>
    <div id="claude-stats"></div>
  </div>

  <!-- Gappers -->
  <div class="card">
    <h2>Today's Gappers</h2>
    <div id="gappers-block"><span style="color:#484f58">Scanning...</span></div>
  </div>

  <!-- Recent log -->
  <div class="card full-width">
    <h2>Alpaca — Recent Activity</h2>
    <div id="log-entries"></div>
  </div>

  <!-- IBKR log -->
  <div class="card full-width">
    <h2>IBKR — Recent Activity</h2>
    <div id="ibkr-log-entries"></div>
  </div>

</div>

<script>
let currentBSThhmm = 0;

function hhmm(dt) { return dt.getHours() * 100 + dt.getMinutes(); }

function tickClock() {
  const now = new Date();
  // BST = UTC+1
  const bst = new Date(now.getTime() + 60*60*1000);
  const et  = new Date(now.getTime() - 4*60*60*1000); // EDT UTC-4
  document.getElementById('clock-bst').textContent =
    bst.toISOString().substring(11,19) + ' BST';
  document.getElementById('clock-et').textContent =
    et.toISOString().substring(11,19) + ' ET';
  currentBSThhmm = bst.getUTCHours() * 100 + bst.getUTCMinutes();
}

function statusIcon(s) {
  return `<span class="dot-${s}"></span>`;
}

function statusText(s, note) {
  const labels = {done:'Done', active:'Active', warn:'⚠ Late', skip:'⏭ Skipped', pending:'Pending'};
  return `<span class="${s}">${statusIcon(s)}${labels[s]||s}</span>${note ? ' <span style="color:#8b949e">'+note+'</span>' : ''}`;
}

function pnlClass(v) {
  return v > 0 ? 'pnl-pos' : v < 0 ? 'pnl-neg' : 'pnl-zero';
}

function render(data) {
  // Schedule
  const sched = document.getElementById('schedule-body');
  sched.innerHTML = '';
  const SCHED_HHMM = [1330,1440,1445,1450,1500,1700,1800,2030,2044,2055,2115,2130];
  data.schedule.forEach((row, i) => {
    const tr = document.createElement('tr');
    const isCurrent = (i < data.schedule.length - 1)
      ? (currentBSThhmm >= SCHED_HHMM[i] && currentBSThhmm < SCHED_HHMM[i+1])
      : (currentBSThhmm >= SCHED_HHMM[i]);
    const WIN_LABELS = {
      trading: '<span class="win-trading">&#9654; TRADING</span>',
      blocked: '<span class="win-blocked">&#9940; NO TRADES</span>',
      closing: '<span class="win-closing">&#9209; CLOSING</span>',
      pre:     '<span class="win-pre">PRE-MKT</span>',
      eod:     '<span class="win-eod">EOD</span>',
    };
    if (isCurrent) tr.className = 'current-row';
    else if (row.window === 'blocked') tr.className = 'row-blocked';
    else if (row.window === 'trading') tr.className = 'row-trading';
    const winBadge = WIN_LABELS[row.window] || '';
    tr.innerHTML = '<td>' + row.bst + '</td>'
      + '<td style="color:#8b949e">' + row.et + '</td>'
      + '<td>' + winBadge + '</td>'
      + '<td>' + row.label + '</td>'
      + '<td>' + statusText(row.status, '') + '</td>'
      + '<td style="color:#8b949e">' + (row.note || '') + '</td>';
    sched.appendChild(tr);
  });

  // P&L summary
  const pnlCls = pnlClass(data.total_pnl);
  document.getElementById('pnl-summary').innerHTML = `
    <span class="metric"><span class="metric-val ${pnlCls}">$${data.total_pnl >= 0 ? '+' : ''}${data.total_pnl.toFixed(2)}</span><div class="metric-label">P&amp;L</div></span>
    <span class="metric"><span class="metric-val">${data.wins}W / ${data.losses}L</span><div class="metric-label">Trades</div></span>
  `;

  // Trades table
  const tb = document.getElementById('trades-body');
  const noTrades = document.getElementById('no-trades');
  tb.innerHTML = '';
  if (data.trades.length === 0) {
    noTrades.style.display = 'block';
  } else {
    noTrades.style.display = 'none';
    data.trades.forEach(t => {
      const cls = pnlClass(t.pnl);
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td class="log-sym">${t.symbol}</td>
        <td>${t.shares}</td>
        <td>$${t.entry.toFixed(2)}</td>
        <td>${t.exit ? '$'+t.exit.toFixed(2) : '—'}</td>
        <td class="${cls}">$${t.pnl >= 0?'+':''}${t.pnl.toFixed(2)}</td>
        <td style="color:#8b949e;font-size:11px">${t.reason||''}</td>`;
      tb.appendChild(tr);
    });
  }

  // Claude effectiveness
  const eff = data.claude_eff;
  const res = data.research;
  let claude_html = '';
  if (!eff || !eff.total) {
    claude_html = '<div style="color:#484f58">No data yet — starts after first scan.</div>';
  } else {
    const eligible = eff.total - eff.rejects;
    const cacheRate = eligible > 0 ? Math.round(eff.hits / eligible * 100) : 0;
    const changeRate = eff.scored > 0 ? Math.round(eff.changed / eff.scored * 100) : 0;
    claude_html = `
      <table>
        <tr><td>Candidates seen</td><td style="color:#e6edf3">${eff.total}</td></tr>
        <tr><td>Local rejects</td><td class="pending">${eff.rejects} (no Claude)</td></tr>
        <tr><td>Cache hits</td><td class="done">${eff.hits}</td></tr>
        <tr><td>Claude scored</td><td class="active">${eff.scored}</td></tr>
        <tr><td>Cache hit rate</td><td class="done">${cacheRate}%</td></tr>
        <tr><td>Decision changes</td><td class="${changeRate>20?'active':'pending'}">${eff.changed} (${changeRate}%)</td></tr>
      </table>
      <div style="margin-top:8px;color:#8b949e;font-size:11px">Research: ${res.claude_scored} of ${res.total} symbols sent to Claude</div>`;
  }
  document.getElementById('claude-stats').innerHTML = claude_html;

  // Live Gate
  const gateDiv = document.getElementById('live-gate-block');
  const lg = data.live_gate || {};
  if (Object.keys(lg).length === 0) {
    gateDiv.innerHTML = '<div class="gate-none">Waiting for precheck (14:40 BST)…</div>';
  } else {
    let html = '';
    for (const [agName, g] of Object.entries(lg)) {
      const enabled = g.live_enabled === true || g.LIVE_ENABLED === true;
      const tests   = g.tests || {};
      const passAll = enabled ? 'gate-pass' : 'gate-fail';
      const label   = agName === 'alpaca' ? 'Alpaca' : 'IBKR';
      html += `<div style="margin-bottom:8px"><strong style="color:#e6edf3">${label}</strong> <span class="${passAll}">${enabled ? '✓ LIVE_ENABLED' : '✗ BLOCKED'}</span>`;
      for (const [k, v] of Object.entries(tests)) {
        const cls = v === 'pass' || v === true ? 'gate-pass' : v === 'skip' ? 'gate-none' : 'gate-fail';
        html += `<div class="gate-row"><span class="gate-key">${k}</span><span class="${cls}">${v}</span></div>`;
      }
      if (g.reason) html += `<div style="color:#8b949e;font-size:11px;margin-top:4px">${g.reason}</div>`;
      html += '</div>';
    }
    gateDiv.innerHTML = html;
  }

  // Feed Health
  const feedDiv = document.getElementById('feed-health-block');
  const fh = data.feed_health || {};
  if (!fh.checked_at) {
    feedDiv.innerHTML = '<div class="gate-none">IB Gateway status unknown — no feed_health.json yet.</div>';
  } else {
    const connected = fh.connected === true;
    const dataType  = fh.market_data_type || '?';
    const dtLabel   = dataType == 1 ? 'Live real-time' : dataType == 2 ? 'Frozen' : dataType == 3 ? 'Delayed 15–20 min' : dataType == 4 ? 'Delayed frozen' : 'Unknown';
    const dtClass   = dataType == 1 ? 'feed-ok' : dataType <= 2 ? 'feed-warn' : 'feed-err';
    const connClass = connected ? 'feed-ok' : 'feed-err';
    let fhtml = `<div class="feed-row"><span class="gate-key">IB Gateway</span><span class="${connClass}">${connected ? '● Connected' : '● Disconnected'}</span></div>`;
    fhtml += `<div class="feed-row"><span class="gate-key">Data type</span><span class="${dtClass}">${dtLabel} (${dataType})</span></div>`;
    if (fh.port) fhtml += `<div class="feed-row"><span class="gate-key">Port</span><span style="color:#8b949e">${fh.port}</span></div>`;
    if (fh.symbols) {
      for (const [sym, q] of Object.entries(fh.symbols)) {
        const age  = q.age_s != null ? `${q.age_s}s ago` : '—';
        const bid  = q.bid != null ? `$${q.bid}` : '—';
        const ask  = q.ask != null ? `$${q.ask}` : '—';
        const ageClass = q.age_s != null && q.age_s < 10 ? 'feed-ok' : q.age_s != null && q.age_s < 60 ? 'feed-warn' : 'feed-err';
        fhtml += `<div class="feed-row"><span class="gate-key">${sym}</span><span><span class="${ageClass}">${age}</span> <span style="color:#8b949e">${bid}/${ask}</span></span></div>`;
      }
    }
    if (fh.checked_at) fhtml += `<div style="color:#484f58;font-size:10px;margin-top:4px">Checked ${fh.checked_at.substring(11,19)}</div>`;
    feedDiv.innerHTML = fhtml;
  }

  // IBKR Trades
  const ibkrTb = document.getElementById('ibkr-trades-body');
  const noIbkrTrades = document.getElementById('no-ibkr-trades');
  const ibkrPnlSum = document.getElementById('ibkr-pnl-summary');
  const ibkrTrades = data.ibkr_trades || [];
  const ibkrPnl = data.ibkr_pnl || 0;
  const ibkrWins = ibkrTrades.filter(t => t.pnl > 0).length;
  const ibkrLosses = ibkrTrades.filter(t => t.pnl <= 0).length;
  ibkrPnlSum.innerHTML = `
    <span class="metric"><span class="metric-val ${pnlClass(ibkrPnl)}">$${ibkrPnl >= 0 ? '+' : ''}${ibkrPnl.toFixed(2)}</span><div class="metric-label">P&amp;L</div></span>
    <span class="metric"><span class="metric-val">${ibkrWins}W / ${ibkrLosses}L</span><div class="metric-label">Trades</div></span>
  `;
  ibkrTb.innerHTML = '';
  if (ibkrTrades.length === 0) {
    noIbkrTrades.style.display = 'block';
  } else {
    noIbkrTrades.style.display = 'none';
    ibkrTrades.forEach(t => {
      const cls = pnlClass(t.pnl);
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td class="log-sym">${t.symbol}</td>
        <td>${t.shares}</td>
        <td>$${t.entry.toFixed(2)}</td>
        <td>${t.exit ? '$'+t.exit.toFixed(2) : '—'}</td>
        <td class="${cls}">$${t.pnl >= 0?'+':''}${t.pnl.toFixed(2)}</td>
        <td style="color:#8b949e;font-size:11px">${t.reason||''}</td>`;
      ibkrTb.appendChild(tr);
    });
  }

  // Recent log
  const logDiv = document.getElementById('log-entries');
  const SKIP = new Set(['LOW_VOLUME_MODE','MARKET_REGIME']);
  logDiv.innerHTML = '';
  data.recent_log.forEach(e => {
    const d = document.createElement('div');
    d.className = 'log-entry';
    d.innerHTML = `<span style="color:#484f58">${e.ts.substring(11,16)}</span>  <span class="log-action">${e.action}</span>  <span class="log-sym">${e.symbol||''}</span>  <span>${e.detail.substring(0,120)}</span>`;
    logDiv.appendChild(d);
  });
  if (data.recent_log.length === 0) {
    logDiv.innerHTML = '<div style="color:#484f58">No activity yet today.</div>';
  }

  // Agent status
  const agBlock = document.getElementById('agents-block');
  agBlock.innerHTML = '';
  const agNames = {alpaca: 'Alpaca (Daytrading)', ibkr: 'IBKR'};
  for (const [key, ag] of Object.entries(data.agents || {})) {
    const modeBadge = ag.mode === 'LIVE'
      ? '<span class="badge-live">LIVE</span>'
      : '<span class="badge-paper">PAPER</span>';
    const runBadge = ag.running
      ? '<span class="badge-run">&#9679; RUNNING</span>'
      : '<span class="badge-stop">&#9679; STOPPED</span>';
    const regimeColor = ag.regime === 'NO_TRADE' ? '#d29922' : ag.regime === 'TRENDING_UP' ? '#3fb950' : '#8b949e';
    const pnlCls = pnlClass(ag.pnl || 0);
    const pnlStr = (ag.pnl >= 0 ? '+' : '') + (ag.pnl || 0).toFixed(2);
    const div = document.createElement('div');
    div.className = 'agent-block';
    div.innerHTML = `
      <div class="agent-name">${agNames[key] || key} ${modeBadge} ${runBadge}</div>
      <div class="agent-row"><span class="agent-key">Portfolio</span><span>${ag.portfolio ? '$'+ag.portfolio : '—'}</span></div>
      <div class="agent-row"><span class="agent-key">Today P&amp;L</span><span class="${pnlCls}">$${pnlStr}</span></div>
      <div class="agent-row"><span class="agent-key">Regime</span><span style="color:${regimeColor}">${ag.regime}</span></div>
      <div class="agent-row"><span class="agent-key">Note</span><span style="color:#484f58;font-size:11px">${ag.regime_note || '—'}</span></div>
      <div class="agent-row"><span class="agent-key">Last seen</span><span style="color:#484f58;font-size:11px">${ag.last_ts || '—'}</span></div>`;
    agBlock.appendChild(div);
  }

  // Gappers
  const gDiv = document.getElementById('gappers-block');
  if (!data.gappers || data.gappers.length === 0) {
    gDiv.innerHTML = '<span style="color:#484f58;font-size:12px">No gappers above threshold today.</span>';
  } else {
    gDiv.innerHTML = data.gappers.map(g =>
      `<span class="gapper-chip"><span class="gapper-sym">${g.symbol}</span><span class="gapper-pct">${g.gap_pct > 0 ? '+' : ''}${g.gap_pct.toFixed(1)}%</span><span class="gapper-vol">${g.vol_ratio.toFixed(1)}x vol</span></span>`
    ).join('');
  }

  // IBKR log
  const ibkrDiv = document.getElementById('ibkr-log-entries');
  if (!data.ibkr_log || data.ibkr_log.length === 0) {
    ibkrDiv.innerHTML = '<div style="color:#484f58">IBKR agent not running.</div>';
  } else {
    ibkrDiv.innerHTML = data.ibkr_log.map(l => {
      const ts  = l.match(/\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})/);
      const body = l.replace(/\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]\s*/, '');
      return `<div class="log-entry"><span style="color:#484f58">${ts ? ts[1].substring(11) : ''}</span>  <span>${body.substring(0,160)}</span></div>`;
    }).join('');
  }

  document.getElementById('last-refresh').textContent = new Date().toLocaleTimeString();
}

async function refresh() {
  try {
    const r = await fetch('/api/status');
    const data = await r.json();
    render(data);
  } catch(e) {
    document.getElementById('last-refresh').textContent = 'error — retrying';
  }
}

setInterval(tickClock, 1000);
tickClock();
refresh();
setInterval(refresh, 30000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/status":
            try:
                data = _build_status()
                body = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", len(body))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                body = json.dumps({"error": str(e)}).encode()
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", len(body))
                self.end_headers()
                self.wfile.write(body)
        else:
            body = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # suppress access log noise


if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Dashboard running → http://localhost:{PORT}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
