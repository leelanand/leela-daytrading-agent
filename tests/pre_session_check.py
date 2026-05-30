"""
Pre-Session Gate — Alpaca Agent
Runs fast P0 checks before the agent is allowed to trade.
Called by run_trading_day.ps1 at ~08:30 ET. Exits 0=OK, 1=BLOCKED.

Run: python tests/pre_session_check.py
"""
import sys, os, json, subprocess
from pathlib import Path
from datetime import date, datetime
from zoneinfo import ZoneInfo

ALPA_DIR  = Path(r"C:\Users\leela\leela-daytrading-agent")
IBKR_DIR  = Path(r"C:\Users\leela\leela-ibkr-agent")
TESTS_DIR = ALPA_DIR / "tests"
REPORTS   = TESTS_DIR / "reports"
REPORTS.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(ALPA_DIR))
ET = ZoneInfo("America/New_York")

_passed = _failed = _warnings = 0
_blocks: list[str] = []
_warns:  list[str] = []

def p0(name: str, ok: bool, detail: str = "", fix: str = ""):
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  [OK]   {name}" + (f" -- {detail}" if detail else ""))
    else:
        _failed += 1
        _blocks.append(name + (f": {detail}" if detail else ""))
        print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))
        if fix:
            print(f"         FIX: {fix}")

def warn(name: str, detail: str = ""):
    global _warnings
    _warnings += 1
    _warns.append(name)
    print(f"  [WARN] {name}" + (f" -- {detail}" if detail else ""))

# ── 1. NYSE holiday guard ──────────────────────────────────────────────────────

print("\n[PRE] NYSE holiday guard")

NYSE_HOLIDAYS = {
    (2026,  1,  1), (2026,  1, 19), (2026,  2, 16), (2026,  4,  3),
    (2026,  5, 25), (2026,  7,  3), (2026,  9,  7), (2026, 11, 26),
    (2026, 12, 25),
    (2027,  1,  1), (2027,  1, 18), (2027,  2, 15), (2027,  4, 26),
    (2027,  5, 31), (2027,  7,  5), (2027,  9,  6), (2027, 11, 25),
    (2027, 12, 24),
}

today = date.today()
is_weekend = today.weekday() >= 5
is_holiday = (today.year, today.month, today.day) in NYSE_HOLIDAYS

p0("not_weekend",  not is_weekend, f"weekday={today.weekday()}")
p0("not_holiday",  not is_holiday, str(today),
   "NYSE holiday — disable or skip the scheduled task for today")

# ── 2. Config validation ───────────────────────────────────────────────────────

print("\n[PRE] Config validation")
try:
    import config as cfg
    p0("force_close_min_45",    cfg.FORCE_CLOSE_MIN == 45,  f"got={cfg.FORCE_CLOSE_MIN}")
    p0("paper_trading_true",    getattr(cfg, "PAPER_TRADING", False) is True)
    p0("stop_loss_sane",        0 < cfg.STOP_LOSS_PCT < 0.05, f"got={cfg.STOP_LOSS_PCT:.3f}")
    p0("take_profit_sane",      0 < cfg.TAKE_PROFIT_PCT < 0.10)
    p0("position_size_sane",    0 < cfg.POSITION_SIZE_PCT <= 0.30)
    p0("alpaca_key_present",    bool(getattr(cfg, "ALPACA_API_KEY", "")))
    p0("alpaca_secret_present", bool(getattr(cfg, "ALPACA_SECRET_KEY", "")))
    is_paper_key = getattr(cfg, "ALPACA_API_KEY", "").startswith("PK")
    p0("alpaca_key_is_paper",   is_paper_key,
       f"key prefix={getattr(cfg,'ALPACA_API_KEY','')[:4]}...",
       "Use Alpaca paper key (starts with PK) while PAPER_TRADING=True")
except Exception as e:
    p0("config_import", False, str(e))

# ── 3. Config parity (Alpaca == IBKR for critical params) ─────────────────────

print("\n[PRE] Cross-agent config parity")
try:
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("ibkr_cfg", IBKR_DIR / "config.py")
    ibkr  = _ilu.module_from_spec(_spec); _spec.loader.exec_module(ibkr)
    alpa  = cfg
    for attr in ("FORCE_CLOSE_MIN", "FORCE_CLOSE_HOUR", "STOP_LOSS_PCT",
                 "TAKE_PROFIT_PCT", "MIN_GAP_PCT", "MAX_SPREAD_PCT", "MIN_REL_VOLUME"):
        iv = getattr(ibkr, attr, None)
        av = getattr(alpa, attr, None)
        p0(f"parity_{attr.lower()}", iv == av,
           f"alpaca={av} ibkr={iv}", f"Sync {attr} between both config.py files")
except Exception as e:
    warn("config_parity_check", str(e))

# ── 4. Critical files present ─────────────────────────────────────────────────

print("\n[PRE] Critical files present")
for f in [ALPA_DIR / n for n in ("agent.py","scanner.py","executor.py","risk.py",".env")]:
    p0(f"file_{f.name}", f.exists(), str(f))

# ── 5. Unit tests pass ────────────────────────────────────────────────────────

print("\n[PRE] Unit tests")
try:
    result = subprocess.run(
        [sys.executable, str(TESTS_DIR / "test_phase1_unit.py")],
        capture_output=True, text=True, timeout=60
    )
    unit_ok   = result.returncode == 0
    last_line = result.stdout.strip().split("\n")[-1] if result.stdout.strip() else "no output"
    p0("unit_tests_pass", unit_ok, last_line,
       "Run: python tests\\test_phase1_unit.py  and fix failures")
except Exception as e:
    p0("unit_tests_pass", False, str(e))

# ── 6. Alpaca paper key safety ────────────────────────────────────────────────

print("\n[PRE] Alpaca paper vs live key safety")
try:
    env_path = ALPA_DIR / ".env"
    if env_path.exists():
        env_text = env_path.read_text()
        has_pk   = "ALPACA_API_KEY=PK" in env_text
        p0("env_has_paper_key", has_pk, ".env ALPACA_API_KEY starts with PK",
           "Replace live key with paper key in .env")
    else:
        warn("env_file_missing", ".env not found")
except Exception as e:
    warn("paper_key_check", str(e))

# ── 7. Orphan position check ──────────────────────────────────────────────────

print("\n[PRE] Orphan position check")
try:
    import sqlite3
    db_path = ALPA_DIR / "daytrades.db"
    if not db_path.exists():
        warn("orphan_positions", "daytrades.db not found -- first run?")
    else:
        conn    = sqlite3.connect(db_path)
        cur     = conn.cursor()
        cur.execute("""
            SELECT symbol, ts FROM trades
            WHERE exit_price IS NULL AND DATE(ts) < ?
        """, (today.isoformat(),))
        orphans = cur.fetchall()
        conn.close()
        if orphans:
            for sym, ts2 in orphans:
                warn("orphan_position", f"{sym} opened {ts2} — exit_price NULL")
            p0("no_orphan_positions", False,
               f"{len(orphans)} trade(s) with no exit from prior sessions",
               "Run agent.py --close to flatten before today's session")
        else:
            p0("no_orphan_positions", True, "no prior-session open trades")
except Exception as e:
    warn("orphan_check", str(e))

# ── 8. Score drift check ──────────────────────────────────────────────────────

print("\n[PRE] Score drift check")
try:
    eff_path = ALPA_DIR / "claude_effectiveness.jsonl"
    if not eff_path.exists():
        warn("score_drift", "claude_effectiveness.jsonl not found")
    else:
        lines  = [l for l in eff_path.read_text().splitlines() if l.strip()]
        recent = [json.loads(l) for l in lines[-20:]]
        deltas = [abs(r.get("delta", 0)) for r in recent if "delta" in r]
        if deltas:
            avg_drift = sum(deltas) / len(deltas)
            bad = [d for d in deltas if d > 15]
            if len(bad) > len(deltas) * 0.5:
                warn("score_drift_high",
                     f"avg={avg_drift:.1f}pts, {len(bad)}/{len(deltas)} entries >15pts")
            else:
                p0("score_drift_acceptable", True,
                   f"avg drift={avg_drift:.1f}pts over last {len(deltas)} scores")
        else:
            warn("score_drift", "no delta entries found")
except Exception as e:
    warn("score_drift_check", str(e))

# ── 9. PDT day-trade counter ──────────────────────────────────────────────────

print("\n[PRE] PDT tracking")
try:
    # With new rule (June 4 2026): $2K minimum, 3 day trades in 5 days before PDT flag
    # Alpaca tracks this server-side. We verify the agent has PDT logic.
    agent_src = (ALPA_DIR / "agent.py").read_text()
    p0("pdt_logic_present", "_pdt_budget" in agent_src or "pdt" in agent_src.lower(),
       "PDT budget function present in agent.py")

    # Check daytrades.db for today's trade count
    db_path = ALPA_DIR / "daytrades.db"
    if db_path.exists():
        import sqlite3
        conn = sqlite3.connect(db_path)
        cur  = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM trades
            WHERE DATE(ts) = ?
        """, (today.isoformat(),))
        count = cur.fetchone()[0]
        conn.close()
        if count >= 3:
            warn("pdt_approaching_limit", f"{count} day trades already today (PDT limit = 3 in 5 days)")
        else:
            p0("pdt_count_ok", True, f"{count} day trades so far today")
except Exception as e:
    warn("pdt_check", str(e))

# ── Summary ───────────────────────────────────────────────────────────────────

total  = _passed + _failed
status = "TRADING_ALLOWED" if _failed == 0 else "TRADING_BLOCKED"

print(f"\n{'='*60}")
print(f"Pre-Session Gate (Alpaca): {_passed}/{total} P0 checks passed")
print(f"Status: {status}")
if _blocks:
    print(f"\nBLOCKERS ({len(_blocks)}):")
    for b in _blocks: print(f"  - {b}")
if _warns:
    print(f"\nWARNINGS ({len(_warns)}):")
    for w in _warns: print(f"  ~ {w}")
print(f"{'='*60}")

report = {
    "agent":     "alpaca",
    "date":      today.isoformat(),
    "timestamp": datetime.now(ET).isoformat(),
    "status":    status,
    "p0_passed": _passed, "p0_failed": _failed, "warnings": _warnings,
    "blockers":  _blocks, "warn_list": _warns,
}
(REPORTS / "pre_session_check.json").write_text(json.dumps(report, indent=2))
print(f"Report: {REPORTS / 'pre_session_check.json'}")
sys.exit(0 if _failed == 0 else 1)
