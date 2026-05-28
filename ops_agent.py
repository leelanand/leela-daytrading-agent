"""
Autonomous OPS Agent — Leela Live Trading System.

Fetches full BPM dashboard, reasons over all 14 pipeline sections using Claude,
takes corrective actions autonomously, and returns what needs human attention.

Called by the ops monitor cron every 8 minutes during trading hours.
Output: JSON {fixed, notify_human, observations} written to stdout.
All actions logged to ops_fixes.log.
"""
from __future__ import annotations
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import anthropic

# ── Paths ─────────────────────────────────────────────────────────────────────

ALPACA_DIR = Path(r"C:\Users\leela\leela-daytrading-agent")
IBKR_DIR   = Path(r"C:\Users\leela\leela-ibkr-agent")
PYTHON     = r"C:\Users\leela\AppData\Local\Programs\Python\Python312\python.exe"
OPS_LOG    = IBKR_DIR / "ops_fixes.log"
BPM_URL    = "http://localhost:8765/api/bpm"
RESTART_LOCK = IBKR_DIR / "ops_restart_lock.json"
RESTART_COOLDOWN_MINS = 12


# ── Logging ───────────────────────────────────────────────────────────────────

def _log(msg: str, severity: str = "INFO"):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{severity:7s}] {msg}"
    print(line, file=sys.stderr)
    try:
        with open(OPS_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_env() -> str:
    for d in (ALPACA_DIR, IBKR_DIR):
        try:
            for line in (d / ".env").read_text().splitlines():
                if line.startswith("ANTHROPIC_API_KEY="):
                    return line.split("=", 1)[1].strip()
        except Exception:
            pass
    return os.environ.get("ANTHROPIC_API_KEY", "")


def _log_age_mins(path: Path) -> float:
    try:
        return (datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)).total_seconds() / 60
    except Exception:
        return 9999.0


def _recent_restart(agent: str) -> bool:
    try:
        if RESTART_LOCK.exists():
            locks = json.loads(RESTART_LOCK.read_text())
            ts_str = locks.get(agent)
            if ts_str:
                age_mins = (datetime.now() - datetime.fromisoformat(ts_str)).total_seconds() / 60
                return age_mins < RESTART_COOLDOWN_MINS
    except Exception:
        pass
    return False


def _record_restart(agent: str):
    try:
        locks = {}
        if RESTART_LOCK.exists():
            locks = json.loads(RESTART_LOCK.read_text())
        locks[agent] = datetime.now().isoformat()
        RESTART_LOCK.write_text(json.dumps(locks))
    except Exception:
        pass


def _port_open(port: int) -> bool:
    s = socket.socket()
    s.settimeout(2)
    try:
        s.connect(("127.0.0.1", port))
        s.close()
        return True
    except Exception:
        return False


def _launch_ps1(script: Path, cwd: Path):
    subprocess.Popen(
        ["powershell", "-NoExit", "-File", str(script)],
        cwd=str(cwd),
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )


# ── Constants ─────────────────────────────────────────────────────────────────

MISSED_FEEDBACK_FILE  = ALPACA_DIR / "missed_feedback.jsonl"
LATE_ADDITIONS_EXPIRY = 45   # minutes — late addition symbols expire after this


# ── Tool implementations ──────────────────────────────────────────────────────

def tool_get_bpm_status(_: dict) -> dict:
    import urllib.request
    try:
        with urllib.request.urlopen(BPM_URL, timeout=8) as resp:
            bpm = json.loads(resp.read())
    except Exception as e:
        return {"error": str(e), "dashboard_down": True,
                "fallback_note": "Dashboard unreachable — reading files directly",
                "alpaca_log_age_mins": round(_log_age_mins(ALPACA_DIR / "trading_day.log"), 1),
                "ibkr_log_age_mins":   round(_log_age_mins(IBKR_DIR   / "trading_day.log"), 1),
                "gateway_up":          _port_open(4001)}

    # Distil to a token-efficient summary
    r = bpm.get("research",     {})
    s = bpm.get("scan",         {})
    sc = bpm.get("scoring",     {})
    d = bpm.get("data",         {})
    re = bpm.get("regime",      {})
    ri = bpm.get("risk",        {})
    ex = bpm.get("execution",   {})
    co = bpm.get("coordination",{})
    it = bpm.get("integration", {})
    st = bpm.get("strategy",    {})
    wh = bpm.get("why",         {})
    ms = bpm.get("missed",      {})
    kp = bpm.get("kpis",        {})
    al = bpm.get("alerts",      [])

    return {
        "generated_at":           bpm.get("generated_at"),
        "active_alerts":          [{"sev": a["severity"], "cat": a["category"], "msg": a["message"]} for a in al],
        "research": {
            "status": r.get("status"), "ran_today": r.get("ran_today"),
            "age_mins": r.get("cache_age_mins"), "symbols": r.get("symbols_researched"),
            "consumed_by_prescan": r.get("research_consumed"),
            "bias": {"bullish": r.get("bullish"), "bearish": r.get("bearish"), "avoid": r.get("avoid")},
        },
        "scan": {
            "status": s.get("status"), "prescan_done": s.get("prescan_done"),
            "total_scans": s.get("total_scans"), "skipped_scans": s.get("skipped_scans"),
            "last_age_mins": s.get("last_scan_age_mins"),
            "pool": {"tradeable": s.get("tradeable_count"), "watchlist": s.get("watchlist_count")},
            "skip_reasons": s.get("skip_reasons", []),
        },
        "scoring": {
            "status": sc.get("status"), "candidates_seen": sc.get("candidates_seen"),
            "sent_to_claude": sc.get("sent_to_claude"), "cache_hit_rate_pct": sc.get("cache_hit_rate_pct"),
            "decision_change_rate_pct": sc.get("decision_change_rate_pct"),
            "top_rejections": sc.get("top_rejection_reasons", {}),
        },
        "data": {
            "status": d.get("status"), "ibkr_connected": d.get("ibkr_connected"),
            "market_data_type": d.get("market_data_type"), "quote_age_secs": d.get("quote_age_secs"),
            "data_confidence": d.get("data_confidence_score"), "stale_events": d.get("stale_quote_events"),
        },
        "regime": {
            "current": re.get("current"), "reason": re.get("reason"),
            "effective_vol": re.get("effective_vol"), "vix": re.get("vix"),
            "blocking": {"no_trade": re.get("no_trade_blocking"), "choppy": re.get("choppy_blocking"), "low_vol": re.get("low_vol_blocking")},
            "transitions_today": re.get("transitions_today"),
        },
        "risk": {
            "status": ri.get("status"), "pdt_remaining": ri.get("pdt_remaining"),
            "loss_limit_hit": ri.get("loss_limit_hit"), "trades_today": ri.get("trades_today"),
        },
        "execution": {
            "status": ex.get("status"), "orders": ex.get("orders_today"), "fills": ex.get("fills_today"),
            "rejected": ex.get("rejected_today"), "avg_slippage_pct": ex.get("avg_slippage_pct"),
            "force_closed": ex.get("force_closed"), "verified_flat": ex.get("verified_flat"),
        },
        "coordination": {
            "alpaca_alive": co.get("alpaca_alive"), "ibkr_alive": co.get("ibkr_alive"),
            "alpaca_trades": co.get("alpaca_trades"), "ibkr_trades": co.get("ibkr_trades"),
            "duplicate_trades": co.get("duplicate_trades", []),
        },
        "integration": {
            "status": it.get("status"), "coverage_pct": it.get("coverage_pct"),
            "missing_research": it.get("missing_research", []),
        },
        "strategy": {
            "trades": st.get("trades"), "win_rate": st.get("win_rate"),
            "expectancy": st.get("expectancy"), "profit_factor": st.get("profit_factor"),
        },
        "missed": {
            "missed_count": ms.get("missed_count", 0),
            "rejected_missed": ms.get("rejected_missed", 0),
            "top_missed": ms.get("top_missed", []),
        },
        "why_not_trading":  {"primary": wh.get("primary_reason"), "in_window": wh.get("in_trading_window"), "reasons": wh.get("reasons", [])},
        "kpis": kp,
        "log_ages": {
            "alpaca_mins": round(_log_age_mins(ALPACA_DIR / "trading_day.log"), 1),
            "ibkr_mins":   round(_log_age_mins(IBKR_DIR   / "trading_day.log"), 1),
        },
    }


def tool_check_gateway(_: dict) -> dict:
    up = _port_open(4001)
    _log(f"Gateway check: {'UP' if up else 'DOWN'}", "INFO")
    return {"reachable": up, "port": 4001}


def tool_get_process_info(_: dict) -> dict:
    try:
        r = subprocess.run(
            ["powershell", "-Command",
             "Get-Process python,powershell -EA SilentlyContinue | "
             "Select-Object Id,ProcessName,"
             "@{N='AgeMins';E={[math]::Round(((Get-Date)-$_.StartTime).TotalMinutes,1)}},CPU | "
             "Sort-Object AgeMins | ConvertTo-Json -Depth 2"],
            capture_output=True, text=True, timeout=10,
        )
        procs = json.loads(r.stdout) if r.stdout.strip().startswith("[") or r.stdout.strip().startswith("{") else []
        if isinstance(procs, dict):
            procs = [procs]
        return {"count": len(procs), "processes": [
            {"id": p.get("Id"), "name": p.get("ProcessName"),
             "age_mins": p.get("AgeMins"), "cpu": p.get("CPU")}
            for p in procs
        ]}
    except Exception as e:
        return {"error": str(e)}


def tool_restart_pipeline(args: dict) -> dict:
    agent  = args["agent"]
    reason = args["reason"]

    if _recent_restart(agent):
        msg = f"{agent} in restart cooldown ({RESTART_COOLDOWN_MINS} min) — skipping"
        _log(msg, "INFO")
        return {"skipped": True, "reason": msg}

    agent_dir = ALPACA_DIR if agent == "alpaca" else IBKR_DIR
    log_path  = agent_dir / "trading_day.log"
    age       = _log_age_mins(log_path)

    if age < 3:
        msg = f"{agent} log is only {age:.1f} min old — pipeline alive, skipping restart"
        _log(msg, "INFO")
        return {"skipped": True, "reason": msg}

    _log(f"Restarting {agent} pipeline. Reason: {reason}", "ACTION")
    try:
        _launch_ps1(agent_dir / "run_trading_day.ps1", agent_dir)
        _record_restart(agent)
        time.sleep(15)
        new_age = _log_age_mins(log_path)
        if new_age < 3:
            _log(f"{agent} pipeline restarted OK (log age now {new_age:.1f} min)", "FIXED")
            return {"restarted": True, "confirmed": True, "new_log_age_mins": round(new_age, 1)}
        else:
            _log(f"{agent} restart attempted — log still {new_age:.1f} min old", "WARNING")
            return {"restarted": True, "confirmed": False, "new_log_age_mins": round(new_age, 1)}
    except Exception as e:
        _log(f"Restart failed for {agent}: {e}", "WARNING")
        return {"error": str(e)}


def tool_restart_dashboard(_: dict) -> dict:
    _log("Restarting dashboard", "ACTION")
    try:
        # Kill existing
        r = subprocess.run(
            ["powershell", "-Command",
             "$p = Get-NetTCPConnection -LocalPort 8765 -EA SilentlyContinue; "
             "if ($p) { Stop-Process -Id $p.OwningProcess -Force -EA SilentlyContinue }"],
            capture_output=True, timeout=8,
        )
        time.sleep(2)
        _launch_ps1(ALPACA_DIR / "start_dashboard.ps1", ALPACA_DIR)
        time.sleep(7)
        up = _port_open(8765)
        if up:
            _log("Dashboard restarted OK", "FIXED")
        else:
            _log("Dashboard restart attempted — port 8765 still closed", "WARNING")
        return {"restarted": True, "confirmed": up}
    except Exception as e:
        _log(f"Dashboard restart failed: {e}", "WARNING")
        return {"error": str(e)}


def tool_clear_cache(args: dict) -> dict:
    agent = args["agent"]
    cache = args["cache"]
    file_map = {
        "score_cache":    "analyst_score_cache.json",
        "candidates":     "candidates.json",
        "regime_cache":   "regime_cache.json",
        "research_cache": "research_cache.json",
        "intraday_cache": "intraday_cache.json",
    }
    fname = file_map.get(cache)
    if not fname:
        return {"error": f"Unknown cache: {cache}"}

    dirs = []
    if agent in ("alpaca", "both"): dirs.append(ALPACA_DIR)
    if agent in ("ibkr",   "both"): dirs.append(IBKR_DIR)

    cleared = []
    for d in dirs:
        p = d / fname
        if p.exists():
            p.unlink()
            cleared.append(f"{d.name}/{fname}")
            _log(f"Cleared {cache} ({d.name})", "ACTION")

    return {"cleared": cleared}


def tool_run_agent_command(args: dict) -> dict:
    agent   = args["agent"]
    command = args["command"]
    agent_dir = ALPACA_DIR if agent == "alpaca" else IBKR_DIR
    age = _log_age_mins(agent_dir / "trading_day.log")

    if age < 5:
        return {"skipped": True,
                "reason": f"Pipeline alive ({age:.1f} min log) — let it schedule its own {command}"}

    _log(f"Running {agent} {command}", "ACTION")
    try:
        result = subprocess.run(
            [PYTHON, str(agent_dir / "agent.py"), command],
            cwd=str(agent_dir), capture_output=True, text=True, timeout=120,
        )
        _log(f"{agent} {command} rc={result.returncode}", "INFO")
        return {"completed": True, "rc": result.returncode,
                "tail": (result.stdout or "")[-400:]}
    except Exception as e:
        _log(f"{agent} {command} failed: {e}", "WARNING")
        return {"error": str(e)}


def tool_log_missed_opportunities(args: dict) -> dict:
    """
    Logs significant missed movers to missed_feedback.jsonl and, when still in the
    trading window, injects them as late additions for immediate rescoring by both agents.

    Threshold: high_pct >= threshold_pct (default 4.0).
    Late additions are written only when:
      - time < 15:00 ET
      - regime is not NO_TRADE
      - loss_limit_hit is False
      - pdt_remaining != 0
    """
    import urllib.request
    threshold = args.get("threshold_pct", 4.0)

    # Fetch fresh BPM to get current missed movers and risk state
    try:
        with urllib.request.urlopen(BPM_URL, timeout=8) as resp:
            bpm = json.loads(resp.read())
    except Exception as e:
        return {"error": f"BPM unreachable: {e}"}

    missed_section = bpm.get("missed", {})
    risk_section   = bpm.get("risk",   {})
    regime_section = bpm.get("regime", {})
    movers         = missed_section.get("movers", [])

    # Guard: conditions that block late additions
    now_et       = datetime.now()  # local time on Windows — adjust if needed
    try:
        from zoneinfo import ZoneInfo
        now_et = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        pass
    mins_et       = now_et.hour * 60 + now_et.minute
    in_window     = 9 * 60 + 45 <= mins_et < 15 * 60  # before 15:00 ET
    regime_ok     = regime_section.get("current", "UNKNOWN") not in ("NO_TRADE",)
    loss_ok       = not risk_section.get("loss_limit_hit", False)
    pdt_ok        = risk_section.get("pdt_remaining", -1) != 0

    significant   = [
        m for m in movers
        if isinstance(m, dict) and m.get("high_pct", 0) >= threshold
    ]

    if not significant:
        return {"logged": [], "late_added": [], "message": "No significant misses above threshold"}

    today_str  = datetime.now().strftime("%Y-%m-%d")
    logged_at  = datetime.now().isoformat()
    feedback_lines = []
    late_syms  = []

    for m in significant:
        sym    = m.get("symbol", "")
        status = m.get("status", "unknown")
        if not sym:
            continue
        record = {
            "date":             today_str,
            "symbol":           sym,
            "move_pct":         m.get("move_pct", 0),
            "high_pct":         m.get("high_pct", 0),
            "status":           status,
            "rejection_reason": m.get("rejection_reason", ""),
            "logged_at":        logged_at,
        }
        feedback_lines.append(record)

        if status == "missed_entirely" and in_window and regime_ok and loss_ok and pdt_ok:
            late_syms.append(sym)

    # Append to missed_feedback.jsonl in both agent dirs
    for d in (ALPACA_DIR, IBKR_DIR):
        fb_file = d / "missed_feedback.jsonl"
        try:
            with open(fb_file, "a", encoding="utf-8") as f:
                for rec in feedback_lines:
                    f.write(json.dumps(rec) + "\n")
        except Exception as e:
            _log(f"Could not write missed_feedback.jsonl ({d.name}): {e}", "WARNING")

    _log(f"Logged {len(feedback_lines)} missed movers to feedback file "
         f"({', '.join(r['symbol'] for r in feedback_lines)})", "ACTION")

    # Write late_additions.json if applicable
    if late_syms:
        payload = {
            "saved_at": datetime.now(ZoneInfo("America/New_York")).isoformat()
                        if 'ZoneInfo' in dir() else datetime.now().isoformat(),
            "symbols": late_syms,
            "source":  "ops_agent_missed_opportunities",
        }
        for d in (ALPACA_DIR, IBKR_DIR):
            try:
                (d / "late_additions.json").write_text(json.dumps(payload, indent=2))
            except Exception as e:
                _log(f"Could not write late_additions.json ({d.name}): {e}", "WARNING")
        _log(f"Injected {len(late_syms)} late addition(s) for next scan: "
             f"{', '.join(late_syms)}", "ACTION")
    elif significant and not in_window:
        _log("Trading window closed — missed movers logged for next-day research only", "INFO")
    elif significant and not (regime_ok and loss_ok and pdt_ok):
        _log("Late additions blocked: regime/loss/PDT constraint active", "INFO")

    return {
        "logged":     [r["symbol"] for r in feedback_lines],
        "late_added": late_syms,
        "in_window":  in_window,
        "details":    feedback_lines,
    }


def tool_log_action(args: dict) -> dict:
    _log(args["message"], args.get("severity", "INFO"))
    return {"logged": True}


def tool_complete(args: dict) -> dict:
    return args


# ── Tool registry ─────────────────────────────────────────────────────────────

TOOL_FNS = {
    "get_bpm_status":            tool_get_bpm_status,
    "check_gateway":             tool_check_gateway,
    "get_process_info":          tool_get_process_info,
    "restart_pipeline":          tool_restart_pipeline,
    "restart_dashboard":         tool_restart_dashboard,
    "clear_cache":               tool_clear_cache,
    "run_agent_command":         tool_run_agent_command,
    "log_missed_opportunities":  tool_log_missed_opportunities,
    "log_action":                tool_log_action,
    "complete":                  tool_complete,
}

TOOLS = [
    {
        "name": "get_bpm_status",
        "description": "Fetch the full BPM dashboard — all 14 pipeline sections, active alerts, KPIs, log ages. Always call this first.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "check_gateway",
        "description": "Test whether IB Gateway is reachable on port 4001.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_process_info",
        "description": "List running Python and PowerShell processes with age and CPU. Use to diagnose zombie processes or confirm a restart worked.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "restart_pipeline",
        "description": "Restart a stalled trading pipeline. Has 12-min anti-loop cooldown and will not restart if log was updated within 3 min.",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent":  {"type": "string", "enum": ["alpaca", "ibkr"], "description": "Which pipeline to restart"},
                "reason": {"type": "string", "description": "Why you are restarting — logged to ops_fixes.log"},
            },
            "required": ["agent", "reason"],
        },
    },
    {
        "name": "restart_dashboard",
        "description": "Restart the trading dashboard (port 8765).",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "clear_cache",
        "description": "Delete a stale cache file so the pipeline regenerates it on next cycle. Safe while pipeline is running.",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent":  {"type": "string", "enum": ["alpaca", "ibkr", "both"]},
                "cache":  {"type": "string", "enum": ["score_cache", "candidates", "regime_cache", "research_cache", "intraday_cache"]},
            },
            "required": ["agent", "cache"],
        },
    },
    {
        "name": "run_agent_command",
        "description": "Run --prescan or --research manually. ONLY safe when the pipeline is confirmed stalled (log > 5 min). Do not call while the continuous loop is running.",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent":   {"type": "string", "enum": ["alpaca", "ibkr"]},
                "command": {"type": "string", "enum": ["--prescan", "--research"]},
            },
            "required": ["agent", "command"],
        },
    },
    {
        "name": "log_missed_opportunities",
        "description": (
            "Log significant missed movers (high_pct >= threshold_pct) to missed_feedback.jsonl "
            "for next-day research enrichment, and — if still in the trading window before 15:00 ET "
            "with no blocking risk conditions — inject missed_entirely symbols into late_additions.json "
            "so both agents rescore them at the next scan cycle. "
            "Call this whenever missed.missed_count > 0 or missed.rejected_missed > 0."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "threshold_pct": {
                    "type": "number",
                    "description": "Minimum high_pct to consider a significant miss (default 4.0)",
                },
            },
            "required": [],
        },
    },
    {
        "name": "log_action",
        "description": "Write an observation or action to the ops_fixes.log audit trail.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message":  {"type": "string"},
                "severity": {"type": "string", "enum": ["INFO", "WARNING", "ACTION", "FIXED"]},
            },
            "required": ["message", "severity"],
        },
    },
    {
        "name": "complete",
        "description": "End the monitoring cycle with a structured summary.",
        "input_schema": {
            "type": "object",
            "properties": {
                "fixed":        {"type": "array", "items": {"type": "string"}, "description": "Issues auto-fixed this cycle"},
                "notify_human": {"type": "array", "items": {"type": "string"}, "description": "Issues needing human attention — each becomes a push notification"},
                "observations": {"type": "array", "items": {"type": "string"}, "description": "Noteworthy observations that need no action"},
            },
            "required": ["fixed", "notify_human"],
        },
    },
]

SYSTEM = """You are the autonomous OPS agent for the Leela live trading platform.
Two live agents trade simultaneously: Alpaca (US stocks via Alpaca API) and IBKR (US stocks via Interactive Brokers).
Your role: monitor the full Business Process Monitor every 8 minutes, reason about every pipeline section, fix what you can autonomously, and surface only what genuinely needs the human trader.

═══ TRADING SYSTEM OVERVIEW ═══

Pipeline flow per agent:
  Pre-market research → Prescan (score candidates) → Scan loop (entry decisions, every 5 min) →
  Monitor loop (exits, every 30s) → Midday block (12-13 ET, no new entries) →
  Afternoon prescan (12:45 ET) → Scan resumes 13:00 ET → Force close 15:44 ET → EOD reports

Key thresholds:
  LIVE min score: 78 | CHOPPY: 73 | LOW_VOLUME: 85 | HIGH_VOL: 80
  PDT: 3 day trades / 5 days for accounts < $25k. DayTradesRemaining=-1 means unlimited.
  Daily loss limit: 3% of account

═══ HOW TO REASON OVER EACH BPM SECTION ═══

RESEARCH: Is cache from today? Age < 4h = fresh, > 6h = stale. Was it consumed by prescan?
  If stale and pipeline alive → clear_cache research_cache (pipeline will regenerate).
  If not consumed → normal if prescan hasn't run yet; flag if prescan is done but research unused.

SCAN: Is prescan done? Scan count healthy for time of day?
  Before 14:33 BST → prescan not expected yet, normal.
  After 14:48 BST with 0 scans → investigate. Midday 17-18 BST → normal pause.
  Skipped > executed scans → only a problem if reason is NOT regime/time-gate.
  Stale candidates (>60 min old, pool not refreshed) → clear_cache candidates.

SCORING: No Claude calls after 5+ scans in trading window → score cache may be fully stale.
  Clear score_cache if zero to_claude calls and zero cache_hits after multiple scans.
  High local_rejects normal for weak markets. Decision change rate 10-30% is healthy.

DATA: ibkr_connected=False → CRITICAL — check gateway, notify if unreachable.
  Quote age > 60s → degraded. Market data type != 1 (live) → flag but don't block.
  Data confidence < 60 during active session → concerning.

REGIME: NO_TRADE → all trading blocked, normal if market is genuinely bad.
  CHOPPY → score gate raised, expected in choppy markets.
  LOW_VOLUME → score gate raised, early session or low-activity day.
  Regime cache stale (> 30 min with no updates) → clear_cache regime_cache.

RISK: pdt_remaining=0 → CRITICAL, notify human. loss_limit_hit → CRITICAL, notify human.
  pdt_remaining=1 → WARNING, notify human (last trade reserved as buffer).
  pdt_remaining=-1 → unlimited, all good.

EXECUTION: orders > 0 but fills = 0 → potential broker issue, notify human.
  After 20:44 BST: force_closed should be True. If False → notify human.
  After 20:55 BST: verified_flat should be True. If False → notify human.
  Avg slippage > 0.15% → flag as observation.

COORDINATION: duplicate_trades → notify human (both agents in same position).
  Both agents stalled simultaneously → notify human.
  One stalled → restart it.

INTEGRATION: Missing research coverage < 80% → normal if gappers appeared after research ran.
  0% coverage → prescan ran before research; clear candidates, trigger prescan if pipeline stalled.

STRATEGY: < 5 trades today → insufficient data, don't judge.
  Win rate < 30% with 5+ trades → flag as observation.
  Consecutive losses pattern in today_trades → observation only, human decides.

MISSED OPPORTUNITIES: Always call log_missed_opportunities when missed_count > 0 or rejected_missed > 0.
  threshold_pct=4.0 is standard; use 3.0 in LOW_VOLUME/CHOPPY regimes where fewer big movers appear.
  The tool handles three outcomes automatically:
    a) Feedback log (always): appends to missed_feedback.jsonl for next-morning research enrichment
    b) Late injection (if window open): writes late_additions.json so both agents rescore at next scan
    c) Blocked injection (if window closed / NO_TRADE / loss_limit / PDT=0): feedback-only, note in observations
  After calling: report what was logged and whether late additions were injected.
  If all misses are from CHOPPY/LOW_VOLUME regime → note "regime correctly filtered these; logged for pattern analysis".
  If misses were rejected_missed (had rejection reasons) → note the rejection reasons in observations.

WHY_NOT_TRADING: Synthesises the above. If in_window=True and primary reason is "No candidates"
  with 0 tradeable → normal, just no setups. Don't over-act on a quiet market day.

═══ AUTONOMOUS ACTIONS ALLOWED ═══

restart_pipeline:  log stale > 7 min AND gateway up (not during cooldown)
restart_dashboard: port 8765 down
clear_cache:       score_cache (scoring stuck), candidates (stale >2h in active session),
                   regime_cache (stale >30 min), intraday_cache (always safe to clear)
run_agent_command: --prescan or --research ONLY when pipeline stalled (log >5 min)
log_action:        always — document observations

═══ ALWAYS NOTIFY HUMAN ═══

- IB Gateway unreachable (needs manual 2FA login — nothing we can do)
- Daily loss limit hit
- PDT = 0 (exhausted) or PDT = 1 (last trade)
- Pipeline restart attempted but log still stale after 15s
- Both agents stalled simultaneously
- Orders placed but fills = 0 (broker execution failure)
- Duplicate symbols traded by both agents on same day
- Force close not confirmed after 20:50 BST
- Dashboard restart attempted but port still down

═══ DO NOT ACT ON (normal behaviour) ═══

- Scans skipped because regime = CHOPPY / LOW_VOLUME / NO_TRADE → regime is working correctly
- No trades when market is FLAT / LOW_VOLUME → correct, waiting for quality setups
- Midday block 17:00-18:00 BST → no new entries, monitors still run
- research_consumed=False before 14:45 BST → prescan hasn't run yet
- Low win rate with < 5 trades → statistically meaningless
- Error 300 "Can't find EId" → harmless IBKR message
- Watchlist candidates not trading → they need score upgrade, monitoring correctly
- Alpaca feed shows IEX → correct, Alpaca subscription is IEX (no real-time SIP)

═══ PROCESS ═══

1. Call get_bpm_status — read everything carefully
2. Work through each section systematically
3. For unclear situations, call check_gateway or get_process_info
4. Take the minimum intervention that resolves the issue
5. Log every significant action and observation
6. Call complete() with a clear summary"""


# ── Agent loop ────────────────────────────────────────────────────────────────

def run() -> dict:
    api_key = _load_env()
    if not api_key:
        _log("No ANTHROPIC_API_KEY found", "WARNING")
        return {"fixed": [], "notify_human": ["OPS agent: no API key"], "observations": []}

    client   = anthropic.Anthropic(api_key=api_key)
    messages = [{"role": "user", "content": "Run your monitoring cycle now."}]
    result   = {"fixed": [], "notify_human": [], "observations": []}

    for _ in range(15):  # safety iteration cap
        response = client.messages.create(
            model       = "claude-sonnet-4-6",
            max_tokens  = 4096,
            system      = SYSTEM,
            tools       = TOOLS,
            messages    = messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            break
        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            fn  = TOOL_FNS.get(block.name)
            out = fn(block.input or {}) if fn else {"error": f"unknown tool {block.name}"}

            if block.name == "complete":
                result = out
                tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                      "content": json.dumps(out)})
                messages.append({"role": "user", "content": tool_results})
                return result

            tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                  "content": json.dumps(out)})

        messages.append({"role": "user", "content": tool_results})

    return result


if __name__ == "__main__":
    _log("=== OPS agent cycle start ===", "INFO")
    try:
        result = run()
        fixed   = result.get("fixed", [])
        notify  = result.get("notify_human", [])
        obs     = result.get("observations", [])
        _log(f"Cycle done — fixed:{len(fixed)}  notify:{len(notify)}  obs:{len(obs)}", "INFO")
        if fixed:
            _log("Fixed: " + "; ".join(fixed), "FIXED")
        if obs:
            _log("Observed: " + "; ".join(obs), "INFO")
        print(json.dumps(result))
    except Exception as e:
        import traceback
        _log(f"OPS agent crashed: {e}", "WARNING")
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"fixed": [], "notify_human": [f"OPS agent crashed: {e}"], "observations": []}))
