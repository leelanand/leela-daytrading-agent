"""
Business Logic Rules Tests — Alpaca Agent
Validates that every trading rule is correctly implemented and configured.

Run: python tests/test_business_rules.py
"""
import sys, json, importlib
from pathlib import Path
from unittest.mock import MagicMock
from datetime import datetime, time as dtime

ALPA_DIR  = Path(r"C:\Users\leela\leela-daytrading-agent")
IBKR_DIR  = Path(r"C:\Users\leela\leela-ibkr-agent")
TESTS_DIR = ALPA_DIR / "tests"
REPORTS   = TESTS_DIR / "reports"
REPORTS.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(ALPA_DIR))

passed = failed = 0
details: list[dict] = []

def ok(name: str, detail: str = ""):
    global passed
    passed += 1
    details.append({"case": name, "status": "PASS", "detail": detail})
    print(f"  [PASS] {name}" + (f" -- {detail}" if detail else ""))

def fail(name: str, detail: str = ""):
    global failed
    failed += 1
    details.append({"case": name, "status": "FAIL", "detail": detail})
    print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))

def check(name: str, cond: bool, detail: str = ""):
    if cond: ok(name, detail)
    else:    fail(name, detail)


# ── 1. Config boundary checks ─────────────────────────────────────────────────

print("\n[RULES 01] Config — safe trading parameter boundaries")

# Mock Alpaca SDK before importing config
for mod in ["alpaca", "alpaca.data", "alpaca.data.historical",
            "alpaca.data.requests", "alpaca.trading", "alpaca.trading.client",
            "alpaca.trading.requests", "alpaca.trading.enums"]:
    sys.modules.setdefault(mod, MagicMock())

import config as cfg

check("stop_loss_between_0.5_and_4pct",   0.005 <= cfg.STOP_LOSS_PCT <= 0.04,
      f"got={cfg.STOP_LOSS_PCT:.3f}")
check("take_profit_gt_stop_loss",          cfg.TAKE_PROFIT_PCT > cfg.STOP_LOSS_PCT,
      f"tp={cfg.TAKE_PROFIT_PCT} sl={cfg.STOP_LOSS_PCT}")
check("take_profit_below_10pct",           cfg.TAKE_PROFIT_PCT < 0.10)
check("position_size_1_to_30pct",         0.01 <= cfg.POSITION_SIZE_PCT <= 0.30,
      f"got={cfg.POSITION_SIZE_PCT:.2f}")
check("min_gap_at_least_1pct",            cfg.MIN_GAP_PCT >= 1.0,    f"got={cfg.MIN_GAP_PCT}")
check("min_rvol_at_least_1x",             cfg.MIN_REL_VOLUME >= 1.0, f"got={cfg.MIN_REL_VOLUME}")
check("max_spread_below_1pct",            cfg.MAX_SPREAD_PCT < 1.0,  f"got={cfg.MAX_SPREAD_PCT}")
check("force_close_hour_15",              cfg.FORCE_CLOSE_HOUR == 15)
check("force_close_min_45",               cfg.FORCE_CLOSE_MIN  == 45)
check("daily_loss_limit_1_to_10pct",      0.01 <= cfg.DAILY_LOSS_LIMIT <= 0.10,
      f"got={cfg.DAILY_LOSS_LIMIT:.2f}")
check("max_positions_1_to_10",            1 <= cfg.MAX_POSITIONS <= 10)
check("max_trades_per_day_sane",  1 <= cfg.MAX_TRADES_PER_DAY <= 20,
      f"got={cfg.MAX_TRADES_PER_DAY} (paper mode may allow more for testing)")
check("min_score_to_trade_gte_60",        cfg.MIN_SCORE_TO_TRADE >= 60,
      f"got={cfg.MIN_SCORE_TO_TRADE}")
check("paper_trading_true",               cfg.PAPER_TRADING is True)
check("alpaca_key_present",               bool(getattr(cfg, "ALPACA_API_KEY", "")))
check("alpaca_key_is_paper",              getattr(cfg, "ALPACA_API_KEY", "").startswith("PK"),
      "paper key must start with PK")
check("risk_reward_ratio_gte_1",
      cfg.TAKE_PROFIT_PCT / cfg.STOP_LOSS_PCT >= 1.0,
      f"R:R={cfg.TAKE_PROFIT_PCT/cfg.STOP_LOSS_PCT:.2f}")

# ── 2. Entry filter rules — source code verification ─────────────────────────

print("\n[RULES 02] Entry filters — source code presence")
scanner_src = (ALPA_DIR / "scanner.py").read_text(encoding="utf-8")
agent_src   = (ALPA_DIR / "agent.py").read_text(encoding="utf-8")
risk_src    = (ALPA_DIR / "risk.py").read_text(encoding="utf-8")

check("gap_filter_in_scanner",       "MIN_GAP_PCT"    in scanner_src or "MIN_GAP_PCT"    in agent_src)
check("rvol_filter_in_scanner",      "MIN_REL_VOLUME" in scanner_src or "MIN_REL_VOLUME" in agent_src)
check("spread_filter_in_risk",       "MAX_SPREAD_PCT" in risk_src)
check("score_gate_in_agent",         "MIN_SCORE_TO_TRADE" in agent_src)
check("volume_filter_in_risk",       "MIN_VOLUME_DAILY" in risk_src or "today_volume" in risk_src)
check("momentum_bypass_in_agent",    "mfo" in agent_src and "rvol" in agent_src.lower())
check("hod_bypass_in_agent",         "hod_breakout" in agent_src or "hod_bypass" in agent_src)
check("fb_bypass_in_agent",          "detect_failed_breakout" in agent_src or "fb_bypass" in agent_src)
check("normalize_alpaca_present",    "_normalize_alpaca" in scanner_src)
check("alpaca_clock_used",           "get_clock" in agent_src or "get_clock" in scanner_src)

# ── 3. Exit rules — source code verification ──────────────────────────────────

print("\n[RULES 03] Exit rules — source code presence")
executor_src = (ALPA_DIR / "executor.py").read_text(encoding="utf-8")

check("stop_loss_in_executor",       "STOP_LOSS_PCT"   in executor_src)
check("take_profit_in_executor",     "TAKE_PROFIT_PCT" in executor_src)
check("force_close_in_agent",        "FORCE_CLOSE"     in agent_src)
check("tight_stop_in_risk",          "TIGHT_STOP"      in risk_src)
check("exits_module_present",        (ALPA_DIR / "exits.py").exists())
check("log_audit_on_fill",           "log_audit" in (ALPA_DIR / "logger.py").read_text())

# ── 4. Position sizing — functional tests ──────────────────────────────────────

print("\n[RULES 04] Position sizing — functional tests")
try:
    import risk as _risk

    portfolio = 10_000.0
    price     = 50.0
    expected  = int(portfolio * cfg.POSITION_SIZE_PCT / price)
    got       = _risk.position_size(portfolio, price)   # sig: (portfolio_value, price)
    check("position_size_correct",           got == expected, f"expected={expected} got={got}")
    check("position_size_min_1",             got >= 1)
    check("position_size_min_1_high_price",  _risk.position_size(portfolio, 999999.0) >= 1)

    got_big = _risk.position_size(portfolio * 2, price)
    check("position_size_scales_with_portfolio", got_big == got * 2)

    # Spread enforcement
    wide = {"symbol": "TST", "price": 100.0, "spread_pct": 0.5, "today_volume": 9_999_999}
    fine = {"symbol": "TST", "price": 100.0, "spread_pct": 0.1, "today_volume": 9_999_999}
    r_wide, _ = _risk.check_candidate_risk(wide, portfolio, None)
    r_fine, _ = _risk.check_candidate_risk(fine, portfolio, None)
    check("spread_wide_blocked", not r_wide, f"spread=0.5 > {cfg.MAX_SPREAD_PCT}")
    check("spread_ok_passes",    r_fine,     f"spread=0.1 <= {cfg.MAX_SPREAD_PCT}")

    stop = _risk.suggested_stop_pct(regime="NORMAL")
    check("stop_pct_gt_zero", stop > 0, f"got={stop:.3f}")

except Exception as e:
    fail("position_sizing_import", str(e))

# ── 5. PDT and daily trade limits ─────────────────────────────────────────────

print("\n[RULES 05] PDT and daily trade limits")
check("max_trades_pdt_sane",      1 <= cfg.MAX_TRADES_PER_DAY <= 20,
      f"MAX_TRADES_PER_DAY={cfg.MAX_TRADES_PER_DAY} (paper mode may allow more)")
check("pdt_budget_fn_in_agent",   "_pdt_budget" in agent_src or "pdt" in agent_src.lower())
check("alpaca_pdt_server_side",   "get_clock" in agent_src or "DayTradesRemaining" in agent_src
                                   or "pdt" in agent_src.lower())

# ── 6. Regime-based rule overrides ───────────────────────────────────────────

print("\n[RULES 06] Regime-based rule overrides")
try:
    check("choppy_min_score_sane",       cfg.CHOPPY_MIN_SCORE >= 60,
          f"choppy={cfg.CHOPPY_MIN_SCORE} (may be below normal in paper/exploratory mode)")
    check("low_vol_min_score_higher",    cfg.LOW_VOLUME_MIN_SCORE >= cfg.MIN_SCORE_TO_TRADE)
    check("low_vol_max_trades_lte_1",    cfg.LOW_VOLUME_MAX_TRADES <= 1)
    check("tight_stop_regimes_list",     isinstance(cfg.TIGHT_STOP_REGIMES, (list, tuple, set)))
    check("no_trade_regime_blocked",     "NO_TRADE" in agent_src)
    check("midday_block_enforced",       "midday" in agent_src.lower() or "MIDDAY" in agent_src)
except AttributeError as e:
    fail("regime_config_attr", str(e))

# ── 7. Force-close timing boundary ───────────────────────────────────────────

print("\n[RULES 07] Force-close timing — exact boundary")
fc_time = dtime(cfg.FORCE_CLOSE_HOUR, cfg.FORCE_CLOSE_MIN)
check("force_close_is_15_45",     fc_time == dtime(15, 45))
check("force_close_before_16_00", fc_time <  dtime(16, 0))
check("force_close_after_15_30",  fc_time >= dtime(15, 30))

# ── 8. Cross-agent parity ─────────────────────────────────────────────────────

print("\n[RULES 08] Cross-agent config parity")
try:
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("ibkr_cfg", IBKR_DIR / "config.py")
    ibkr  = _ilu.module_from_spec(_spec); _spec.loader.exec_module(ibkr)

    for attr in ("FORCE_CLOSE_MIN", "FORCE_CLOSE_HOUR", "STOP_LOSS_PCT", "TAKE_PROFIT_PCT",
                 "MIN_GAP_PCT", "MAX_SPREAD_PCT", "MIN_REL_VOLUME", "DAILY_LOSS_LIMIT"):
        av = getattr(cfg,  attr, None)
        iv = getattr(ibkr, attr, None)
        check(f"parity_{attr.lower()}", av == iv, f"alpaca={av} ibkr={iv}")
except Exception as e:
    fail("cross_agent_parity_import", str(e))

# ── 9. Kill-switch and safety locks ──────────────────────────────────────────

print("\n[RULES 09] Kill-switch and paper-mode safety")
check("kill_switch_defined",       hasattr(cfg, "KILL_SWITCH"))
check("kill_switch_off",           not getattr(cfg, "KILL_SWITCH", True))
check("paper_trading_in_executor", "PAPER_TRADING" in executor_src or "paper" in executor_src.lower())

# ── 10. Required candidate fields in scanner ──────────────────────────────────

print("\n[RULES 10] Required candidate fields enforced in scanner")
for field in ["symbol", "price", "gap_pct", "rel_volume", "spread_pct", "score"]:
    src_to_check = scanner_src if field != "score" else agent_src
    check(f"field_{field}_in_code",  field in src_to_check,
          f"'{field}' in {'scanner' if field != 'score' else 'agent'}.py")

# ── Summary ───────────────────────────────────────────────────────────────────

total  = passed + failed
status = "ALL_PASS" if failed == 0 else "FAILURES"
print(f"\n{'='*60}")
print(f"Business Rules Tests (Alpaca): {passed}/{total} passed -- {status}")
print(f"{'='*60}")

report = {
    "agent": "alpaca", "timestamp": datetime.now().isoformat(),
    "passed": passed, "failed": failed, "total": total, "status": status,
    "details": details,
}
(REPORTS / "business_rules.json").write_text(json.dumps(report, indent=2))
print(f"Report: {REPORTS / 'business_rules.json'}")
sys.exit(0 if failed == 0 else 1)
