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

def _ibkr_port() -> int:
    """Read IBKR_PORT from the IBKR .env so gateway checks follow paper/live switches."""
    try:
        for line in (IBKR_DIR / ".env").read_text().splitlines():
            if line.startswith("IBKR_PORT="):
                return int(line.split("=", 1)[1].strip())
    except Exception:
        pass
    return 4001

IBKR_PORT = _ibkr_port()
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
        with urllib.request.urlopen(BPM_URL, timeout=8) as resp:  # nosec B310
            bpm = json.loads(resp.read())
    except Exception as e:
        return {"error": str(e), "dashboard_down": True,
                "fallback_note": "Dashboard unreachable — reading files directly",
                "alpaca_log_age_mins": round(_log_age_mins(ALPACA_DIR / "trading_day.log"), 1),
                "ibkr_log_age_mins":   round(_log_age_mins(IBKR_DIR   / "trading_day.log"), 1),
                "gateway_up":          _port_open(IBKR_PORT)}

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
    up = _port_open(IBKR_PORT)
    _log(f"Gateway check: {'UP' if up else 'DOWN'}", "INFO")
    return {"reachable": up, "port": IBKR_PORT}


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
        with urllib.request.urlopen(BPM_URL, timeout=8) as resp:  # nosec B310
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


def tool_read_trading_file(args: dict) -> dict:
    """
    Read the tail of a specific trading log or data file.
    Use this to investigate scored-not-traded, high decision change rate,
    validator anomalies, override blocks, or any pattern that needs evidence.
    """
    file_key = args["file"]
    n_lines  = min(int(args.get("lines", 50)), 200)
    today    = datetime.now().strftime("%Y-%m-%d")

    file_map = {
        "candidates":          ALPACA_DIR / "candidates.json",
        "score_trace":         ALPACA_DIR / "claude_score_trace.jsonl",
        "audit_log":           ALPACA_DIR / "audit.log",
        "validator_flags":     ALPACA_DIR / "validator_flags.jsonl",
        "score_overrides":     ALPACA_DIR / "score_overrides.jsonl",
        "validator_challenge": ALPACA_DIR / "validator_challenge.jsonl",
        "effectiveness_log":   ALPACA_DIR / "claude_effectiveness.jsonl",
        "review_log":          ALPACA_DIR / "ops_review_log.jsonl",
        "ibkr_review_log":     IBKR_DIR   / "ops_review_log.jsonl",
        "ibkr_candidates":     IBKR_DIR   / "candidates.json",
        "ibkr_score_trace":    IBKR_DIR   / "claude_score_trace.jsonl",
        "ibkr_audit_log":      IBKR_DIR   / "audit.log",
        "ibkr_validator_flags":     IBKR_DIR / "validator_flags.jsonl",
        "ibkr_score_overrides":     IBKR_DIR / "score_overrides.jsonl",
        "ibkr_validator_challenge": IBKR_DIR / "validator_challenge.jsonl",
    }

    path = file_map.get(file_key)
    if not path:
        return {"error": f"Unknown file key '{file_key}'. Available: {sorted(file_map.keys())}"}

    try:
        if not path.exists():
            return {"exists": False, "file": file_key, "path": str(path)}

        if path.suffix == ".json":
            raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            return {"exists": True, "file": file_key, "content": raw}

        # Text / JSONL — read tail
        text      = path.read_text(encoding="utf-8", errors="replace")
        all_lines = [l for l in text.splitlines() if l.strip()]
        tail      = all_lines[-n_lines:]

        if path.suffix == ".jsonl":
            # Filter to today's entries when possible; fall back to raw
            parsed_all = []
            for line in tail:
                try:
                    obj = json.loads(line)
                    # Only keep today's entries in the filtered set
                    if obj.get("date", today) == today or obj.get("ts", "")[:10] == today:
                        parsed_all.append(obj)
                except Exception:
                    parsed_all.append({"raw": line})
            return {
                "exists":       True,
                "file":         file_key,
                "today":        today,
                "entries":      parsed_all,
                "total_lines":  len(all_lines),
            }
        else:
            # Plain text (audit.log) — filter today's lines
            today_lines = [l for l in tail if today in l]
            return {
                "exists":      True,
                "file":        file_key,
                "today_lines": today_lines or tail[-30:],  # fallback to raw tail
                "total_lines": len(all_lines),
            }

    except Exception as e:
        return {"error": str(e), "file": file_key}


def tool_write_baseline_entry(args: dict) -> dict:
    """Write a trade learning or observation to paper_baseline_log.jsonl."""
    entry = {
        "ts":               datetime.now().isoformat(),
        "symbol":           args.get("symbol", "SYSTEM"),
        "action":           args.get("action", "observation"),
        "score":            args.get("score"),
        "outcome":          args.get("outcome"),
        "learning":         args.get("learning", ""),
        "correction_made":  args.get("correction_made"),
    }
    log_path = ALPACA_DIR / "paper_baseline_log.jsonl"
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        _log(f"Baseline entry written: {entry['action']} / {entry['symbol']}", "INFO")
        return {"written": True, "entry": entry}
    except Exception as e:
        return {"error": str(e)}


def tool_complete(args: dict) -> dict:
    return args


def tool_get_quality_status(_: dict) -> dict:
    """Read the latest quality/security JSON reports from both agents."""
    def _load(path: Path) -> dict:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _age_mins(path: Path) -> float:
        try:
            return (datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)).total_seconds() / 60
        except Exception:
            return 9999.0

    out = {}
    for agent, base in [("alpaca", ALPACA_DIR), ("ibkr", IBKR_DIR)]:
        r = base / "tests" / "reports"
        br  = _load(r / "business_rules.json")
        pre = _load(r / "pre_session_check.json")
        cq  = _load(r / "code_quality.json")
        sec = _load(r / "security_scan.json")
        out[agent] = {
            "business_rules": {
                "passed": br.get("passed"), "total": br.get("total"),
                "status": br.get("status"), "age_mins": round(_age_mins(r / "business_rules.json"), 1),
            },
            "pre_session": {
                "passed": pre.get("passed"), "status": pre.get("status"),
                "age_mins": round(_age_mins(r / "pre_session_check.json"), 1),
            },
            "code_quality": {
                "pylint": cq.get("pylint", {}).get("score"),
                "flake8_errors": cq.get("flake8", {}).get("errors"),
                "max_complexity": cq.get("radon", {}).get("max_complexity"),
                "status": cq.get("summary", {}).get("overall"),
                "age_mins": round(_age_mins(r / "code_quality.json"), 1),
            },
            "security": {
                "bandit_pass": sec.get("summary", {}).get("bandit_pass"),
                "pip_audit_pass": sec.get("summary", {}).get("pip_audit_pass"),
                "blocking_issues": sec.get("summary", {}).get("blocking_issues", 0),
                "high_cves": sec.get("summary", {}).get("high_cves", 0),
                "overall_pass": sec.get("summary", {}).get("overall_pass"),
                "age_mins": round(_age_mins(r / "security_scan.json"), 1),
            },
        }
    return out


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
    "read_trading_file":         tool_read_trading_file,
    "log_action":                tool_log_action,
    "write_baseline_entry":      tool_write_baseline_entry,
    "get_quality_status":        tool_get_quality_status,
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
        "description": "Test whether IB Gateway is reachable on the configured port (reads IBKR_PORT from .env — 4002 for paper, 4001 for live).",
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
        "name": "read_trading_file",
        "description": (
            "Read the tail of a specific trading log or data file to investigate a pattern. "
            "Use this BEFORE escalating any scored-not-traded, high decision-change-rate, "
            "validator anomaly, or override-blocked issue to the human. "
            "Available files: candidates, score_trace, audit_log, validator_flags, "
            "score_overrides, validator_challenge, effectiveness_log, "
            "ibkr_candidates, ibkr_score_trace, ibkr_audit_log, "
            "ibkr_validator_flags, ibkr_score_overrides, ibkr_validator_challenge."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file": {
                    "type": "string",
                    "description": "File key from the available list above",
                },
                "lines": {
                    "type": "integer",
                    "description": "Number of lines to read from the tail (default 50, max 200)",
                },
            },
            "required": ["file"],
        },
    },
    {
        "name": "write_baseline_entry",
        "description": (
            "Write a paper trading learning to paper_baseline_log.jsonl. "
            "Call after any completed trade (entry+exit), any correction made, or at EOD for the day summary. "
            "action: 'entry', 'exit', 'observation', 'correction', 'eod_summary'. "
            "outcome: 'win', 'loss', 'flat', or null for non-trade entries."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol":          {"type": "string"},
                "action":          {"type": "string", "enum": ["entry", "exit", "observation", "correction", "eod_summary"]},
                "score":           {"type": "number"},
                "outcome":         {"type": "string", "enum": ["win", "loss", "flat"]},
                "learning":        {"type": "string", "description": "What this trade teaches about model accuracy or gate behaviour"},
                "correction_made": {"type": "string", "description": "Description of any code/config correction made, or null"},
            },
            "required": ["action", "learning"],
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
        "name": "get_quality_status",
        "description": (
            "Read the latest quality and security gate reports for both agents from disk. "
            "Returns business_rules, pre_session, code_quality, and security results with age_mins. "
            "Call when you want to check if tests or security scans have regressed, or when age_mins is old."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
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

SYSTEM = """Autonomous OPS agent for the Leela live trading platform (Alpaca + IBKR agents running simultaneously).
Monitor every 8 min, fix autonomously, only escalate genuine blockers needing human action. Investigate before escalating.

PAPER TRADING MODE (2026-05-29): Both agents are in paper trading. IBKR uses port 4002. IBKR market data feed will likely return no prices — scanner auto-falls back to yfinance. Data section showing stale IBKR quotes is NORMAL and expected (STALE_QUOTE_PAPER). Do NOT escalate IBKR data issues. Alpaca uses paper API (paper-api.alpaca.markets). No live keys touched under any circumstances. Objective: test full pipeline end-to-end, create scoring baseline. User is away all day.

PAPER TRADING RULES:
- MAX_POSITIONS=10, POSITION_SIZE=8%(Alpaca)/5%(IBKR), MAX_TRADES=20, UTILISATION_CAP=95%
- ALLOW_REENTRY=True (agents can re-buy same symbol), CROSS_AGENT_GATE=False (both can hold same symbol)
- EOD close fires at 20:44 BST — both agents must be flat by 20:55
- NO LIVE TRADING — do not touch live keys, do not change PAPER_TRADING setting
- ALLOWED corrections: fix broken connections, restart stalled pipeline, fix import errors
- NOT ALLOWED: change score thresholds, business rules, stop-loss %, position sizing

BASELINE LOGGING: After each completed trade cycle visible in audit_log (ORDER_PLACED+EXIT or force-close), call write_baseline_entry with learning about whether the score was accurate and which gates fired. At EOD after verify completes, write one eod_summary entry covering: trades count, win rate, which gates fired most, score accuracy observations.

PENDING REVIEWS: At the start of each cycle, call read_trading_file(file="review_log") and check for entries where review_date matches today. For each pending review: check the expected_outcome against current audit_log and BPM data, answer the review_questions from evidence, summarise findings in observations. This is how logic changes are validated the next trading day.

PIPELINE: research(08:30ET) -> prescan(09:45ET) -> scan loop(5min) -> monitor(30s) -> midday block(12-13ET) -> afternoon prescan(12:45ET) -> force-close(15:44ET)
THRESHOLDS: TRENDING_UP=75 CHOPPY=75 LOW_VOL=82 HIGH_VOL=80 NO_TRADE=99 WATCHLIST=50. PDT=-1=unlimited. Loss limit=3%.

BPM SECTION RULES (act on deviations only):
RESEARCH: stale>6h+pipeline alive->clear research_cache. Not consumed before prescan ran->flag.
SCAN: after 14:48BST+0scans->investigate. Midday 17-18BST pause=normal. Stale candidates>60min->clear candidates cache.
SCORING: zero Claude calls after 5+scans->clear score_cache. Change rate 10-30%=healthy.
DATA: ibkr_connected=False->CRITICAL check gateway. Quote age>60s=degraded. data_confidence<60=concerning.
REGIME: NO_TRADE/CHOPPY/LOW_VOL blocking=correct behaviour. Regime cache stale>30min->clear regime_cache.
RISK: pdt=0->notify. loss_limit_hit->notify. pdt=1->notify. pdt=-1=fine.
EXECUTION: orders>0+fills=0->notify(broker issue). force_closed=False after 20:44BST->notify. verified_flat=False after 20:55BST->notify. slippage>0.15%=observation.
COORDINATION: duplicate_trades->notify. Both stalled->notify. One stalled->restart it.
INTEGRATION: coverage<80%=normal if gappers post-research. 0%=clear candidates+prescan if stalled.
STRATEGY: <5 trades=no data. win_rate<30% with 5+trades=observation.
MISSED: call log_missed_opportunities when missed_count>0 or rejected_missed>0. threshold_pct=4.0 (3.0 in CHOPPY/LOW_VOL).

INVESTIGATE BEFORE ESCALATING - never notify about things you can diagnose yourself:

scored_not_traded: read_trading_file(candidates) -> find score/tradeable. read_trading_file(audit_log) -> find TRADE_REJECTED. read_trading_file(score_trace,lines=100) -> check final score vs effective_min. read_trading_file(validator_flags)+read_trading_file(score_overrides) -> check override attempts. Diagnose: score<min=threshold gate(normal); score>=min+TRADE_REJECTED=execution gate(check stage_rejected); score>=min+no rejection=timing/decay; challenge blocked=gap>cap. Put in observations. Escalate ONLY if unexplainable after all files.

high_decision_change_rate(>30%): read_trading_file(effectiveness_log,lines=80) -> compare local vs claude scores. read_trading_file(score_trace,lines=60) -> check clustering near threshold. Diagnose: local>claude=local over-optimistic(normal weak market); claude 15+pts below systematically=calibration drift(note in obs); random=volatile day. Escalate ONLY if >80% for 3+ consecutive cycles with 15+pt systematic gap.

stale_quote: Agents auto-refresh stale quotes (QUOTE_REFRESHED in audit=working, silent). STALE_QUOTE_REJECT with refresh_attempted=True means refresh failed. read_trading_file(audit_log) to count. >3 today->notify(feed degraded). <=3=observation.

ALWAYS NOTIFY:
- IB Gateway unreachable (needs 2FA)
- loss_limit_hit or pdt=0 or pdt=1
- Pipeline restart attempted but log still stale
- Both agents stalled simultaneously
- orders>0 fills=0 (broker failure)
- duplicate_trades
- force_close not confirmed after 20:50BST
- STALE_QUOTE_REJECT refresh_attempted=True more than 3 times today
- Unexplainable scored_not_traded after reading all evidence files

NEVER NOTIFY (normal behaviour):
- Regime-gated scan skips. No trades in flat/low-vol. Midday block. IEX on Alpaca. research_consumed=False pre-prescan. <5 trades. IBKR error 300. Watchlist not trading. Elevated change rate alone. scored_not_traded alone.

QUALITY GATE RULES (call get_quality_status once per cycle — it's cheap):
- business_rules FAILURES -> ALWAYS NOTIFY (trading rules broken)
- security blocking_issues>0 -> ALWAYS NOTIFY (HIGH/MEDIUM bandit finding needs fixing)
- security high_cves>0 -> ALWAYS NOTIFY (CVE in dependency)
- pre_session TRADING_BLOCKED -> ALWAYS NOTIFY (gate is preventing today's run)
- code_quality WARN is NORMAL — do not notify, add to observations only if pylint<5.0
- Reports older than 48h -> add to observations (run_checks.ps1 may not have run)
- Reports missing (age_mins=9999) first cycle of week -> observation; after that escalate

ACTIONS AVAILABLE: restart_pipeline(log>7min+gateway up), restart_dashboard(port 8765 down), clear_cache(score/candidates/regime/intraday), run_agent_command(--prescan/--research only when stalled), read_trading_file(investigate patterns), log_action(document), complete(finish cycle).

PROCESS: 1.get_bpm_status 2.get_quality_status 3.check each BPM section 4.read_trading_file for patterns needing evidence 5.check_gateway/get_process_info for infra 6.minimum intervention 7.log observations 8.complete() with diagnoses in observations not notify_human"""


# ── Agent loop ────────────────────────────────────────────────────────────────

def run(prompt: str = "Run your monitoring cycle now.") -> dict:
    api_key = _load_env()
    if not api_key:
        _log("No ANTHROPIC_API_KEY found", "WARNING")
        return {"fixed": [], "notify_human": ["OPS agent: no API key"], "observations": []}

    client   = anthropic.Anthropic(api_key=api_key)
    messages = [{"role": "user", "content": prompt}]
    result   = {"fixed": [], "notify_human": [], "observations": []}

    for _ in range(15):  # safety iteration cap
        try:
            response = client.messages.create(
                model       = "claude-haiku-4-5-20251001",
                max_tokens  = 4096,
                system      = SYSTEM,
                tools       = TOOLS,
                messages    = messages,
            )
        except Exception as _exc:
            exc_str = str(_exc)
            if "rate_limit" in exc_str or "429" in exc_str:
                # Throttled — return silently; cron will retry in 8 minutes
                _log("Rate limited by Anthropic API — skipping cycle (will retry next cron)", "WARNING")
                return {"fixed": [], "notify_human": [], "observations": ["rate_limited_skipped"]}
            raise  # non-rate-limit errors propagate normally
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
    import argparse as _ap
    _parser = _ap.ArgumentParser()
    _parser.add_argument("--prompt", default="Run your monitoring cycle now.",
                         help="Custom investigation prompt for the ops agent")
    _args = _parser.parse_args()

    _log("=== OPS agent cycle start ===", "INFO")
    try:
        result = run(prompt=_args.prompt)
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
