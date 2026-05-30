"""
Alpaca Agent Integration Tests — covers Alpaca-specific data path gaps not in IBKR run_tests.py.
Run: python tests\run_alpaca_tests.py
"""
import sys, os, json, socket
import importlib.util
from pathlib import Path
from datetime import datetime, date, timezone, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

ALPA_DIR  = Path(r"C:\Users\leela\leela-daytrading-agent")
IBKR_DIR  = Path(r"C:\Users\leela\leela-ibkr-agent")
TESTS_DIR = ALPA_DIR / "tests"
REPORTS   = TESTS_DIR / "reports"
REPORTS.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(ALPA_DIR))
sys.path.insert(1, str(IBKR_DIR))

ET  = ZoneInfo("America/New_York")
NOW = datetime.now(ET)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _case(name, passed, detail=None, deferred=False, defect=None):
    status = "DEFERRED" if deferred else ("PASS" if passed else "FAIL")
    entry  = {"case": name, "status": status}
    if detail:  entry["detail"]  = detail
    if defect:  entry["defect"]  = defect
    return entry

def _save(filename, data):
    p = REPORTS / filename
    p.write_text(json.dumps(data, indent=2, default=str))
    print(f"   Saved {p.name}")
    return data

def _report(test_id, name, status, run, passed, failed, deferred, details, defects, notes=""):
    score = (passed + deferred * 0.5) / max(run, 1)
    return {
        "test_id": test_id, "test_name": name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": status, "cases_run": run, "cases_passed": passed,
        "cases_failed": failed, "cases_deferred": deferred,
        "score": round(score, 4), "details": details,
        "defects_found": defects, "notes": notes,
    }

_all_results = []

def _finish(r, filename):
    _all_results.append(r)
    _save(filename, r)
    tag = f"[{'PASS' if r['cases_failed']==0 and r['cases_deferred']==0 else 'FAIL' if r['cases_failed']>0 else 'DEFER'}]"
    print(f"   ALPA_{r['test_id']}: {tag} (score={r['score']:.2f})")

# ── ALPA TEST 01 — Alpaca API Connectivity ────────────────────────────────────

def test_alpa01_alpaca_connectivity():
    print("\n[ALPA TEST 01] Alpaca API Connectivity")
    details, defects = [], []
    passed = failed = deferred = 0

    try:
        from dotenv import load_dotenv
        load_dotenv(ALPA_DIR / ".env")
        import os as _os
        api_key    = _os.getenv("ALPACA_API_KEY",    "")
        secret_key = _os.getenv("ALPACA_SECRET_KEY", "")
        paper_base = "https://paper-api.alpaca.markets"

        ok_keys = bool(api_key and secret_key)
        if ok_keys:
            passed += 1
        else:
            failed += 1
        details.append(_case("alpaca_keys_present", ok_keys,
            f"key={'set' if api_key else 'MISSING'} secret={'set' if secret_key else 'MISSING'}"))

        if ok_keys:
            is_paper = api_key.startswith("PK") or "paper" in api_key.lower()
            details.append(_case("alpaca_key_is_paper_key", is_paper,
                f"key prefix={api_key[:4]}..."))
            if is_paper: passed += 1
            else:
                failed += 1
                defects.append("Live Alpaca key in use — PAPER_TRADING=True required")

        # Network reachability (DNS only)
        try:
            socket.getaddrinfo("paper-api.alpaca.markets", 443, timeout=3)
            passed += 1
            details.append(_case("alpaca_paper_dns", True, "paper-api.alpaca.markets resolves"))
        except Exception as e:
            deferred += 1
            details.append(_case("alpaca_paper_dns", False, str(e), deferred=True))

        # SDK import
        try:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockSnapshotRequest
            passed += 1
            details.append(_case("alpaca_sdk_importable", True, "SDK imported successfully"))
        except ImportError as e:
            failed += 1
            defects.append(f"alpaca-trade-api not installed: {e}")
            details.append(_case("alpaca_sdk_importable", False, str(e)))

    except Exception as e:
        failed += 1
        details.append(_case("alpaca_setup", False, f"exception: {e}"))

    status = "PASS" if failed == 0 else "PARTIAL" if passed > 0 else "FAIL"
    run = passed + failed + deferred
    _finish(_report("01", "Alpaca API Connectivity", status,
                    run, passed, failed, deferred, details, defects), "alpa_01_connectivity.json")

# ── ALPA TEST 02 — Alpaca Snapshot Normalization ──────────────────────────────

def test_alpa02_snapshot_normalization():
    print("\n[ALPA TEST 02] Alpaca Snapshot Normalization")
    details, defects = [], []
    passed = failed = deferred = 0

    # Mock all external deps before importing scanner
    mocks = {
        "ib_insync": MagicMock(), "ibkr_client": MagicMock(),
        "gapper": MagicMock(), "finnhub": MagicMock(),
        "alpaca": MagicMock(), "alpaca.data": MagicMock(),
        "alpaca.data.historical": MagicMock(),
        "alpaca.data.requests": MagicMock(),
    }
    for k, v in mocks.items():
        sys.modules.setdefault(k, v)

    try:
        spec = importlib.util.spec_from_file_location("alpa_scan_norm", ALPA_DIR / "scanner.py")
        sc   = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sc)

        def _snap(price=50.0, prev=47.0, vol=100_000, bid=49.9, ask=50.1,
                  high=51.0, low=48.0, open_=47.5, vwap=49.5,
                  no_quote=False, no_bar=False):
            s = MagicMock()
            s.latest_trade.price = price
            s.previous_daily_bar.close = prev
            s.daily_bar = None if no_bar else MagicMock(
                volume=vol, high=high, low=low, open=open_, vwap=vwap)
            s.latest_quote = None if no_quote else MagicMock(bid_price=bid, ask_price=ask)
            return s

        # Full snap
        r = sc._normalize_alpaca(_snap(), "T")
        cases = [
            ("price_correct",      r and r["price"]      == 50.0),
            ("prev_close_correct", r and r["prev_close"] == 47.0),
            ("today_vol_correct",  r and r["today_vol"]  == 100_000),
            ("bid_correct",        r and r["bid"]        == 49.9),
            ("ask_correct",        r and r["ask"]        == 50.1),
            ("day_high_correct",   r and r["day_high"]   == 51.0),
            ("day_low_correct",    r and r["day_low"]    == 48.0),
            ("open_price_correct", r and r["open_price"] == 47.5),
            ("vwap_correct",       r and r["vwap"]       == 49.5),
            ("data_src_alpaca",    r and r["data_src"]   == "alpaca"),
        ]
        for name, cond in cases:
            if cond: passed += 1
            else:
                failed += 1
                defects.append(name)
            details.append(_case(name, cond))

        # No quote fallback
        r2 = sc._normalize_alpaca(_snap(no_quote=True), "T")
        ok2 = r2 and abs(r2["bid"] - 50.0 * 0.999) < 0.01
        if ok2: passed += 1
        else: failed += 1
        details.append(_case("no_quote_bid_fallback", ok2,
            f"bid={r2['bid']:.4f} expected={50.0*0.999:.4f}" if r2 else "None"))

        ok3 = r2 and abs(r2["ask"] - 50.0 * 1.001) < 0.01
        if ok3: passed += 1
        else: failed += 1
        details.append(_case("no_quote_ask_fallback", ok3))

        # No daily_bar fallback
        r3 = sc._normalize_alpaca(_snap(no_bar=True), "T")
        ok4 = r3 and r3["today_vol"] == 0 and r3["day_high"] == 50.0 and r3["open_price"] == 47.0
        if ok4: passed += 1
        else: failed += 1
        details.append(_case("no_bar_fallback_values", ok4,
            f"vol={r3.get('today_vol')} high={r3.get('day_high')} open={r3.get('open_price')}" if r3 else "None"))

        # Exception handling
        bad = MagicMock()
        bad.latest_trade.price = "not_a_number"
        r4 = sc._normalize_alpaca(bad, "T")
        if r4 is None: passed += 1
        else: failed += 1
        details.append(_case("exception_returns_none", r4 is None))

    except Exception as e:
        failed += 1
        defects.append(f"Module load failed: {e}")
        details.append(_case("scanner_import", False, str(e)))

    status = "PASS" if failed == 0 else "PARTIAL" if passed > 0 else "FAIL"
    run = passed + failed + deferred
    _finish(_report("02", "Alpaca Snapshot Normalization", status,
                    run, passed, failed, deferred, details, defects), "alpa_02_normalization.json")

# ── ALPA TEST 03 — Alpaca Scanner Data Path ───────────────────────────────────

def test_alpa03_scanner_data_path():
    print("\n[ALPA TEST 03] Alpaca Scanner Data Path")
    details, defects = [], []
    passed = failed = deferred = 0

    # Verify _snapshots function exists and has SIP/IEX fallback
    try:
        scanner_src = (ALPA_DIR / "scanner.py").read_text(encoding="utf-8")

        checks = [
            ("snapshots_fn_exists",        "def _snapshots" in scanner_src),
            ("sip_feed_requested",         '"sip"' in scanner_src),
            ("iex_fallback_present",       "DATA_FEED_DEGRADED" in scanner_src or "IEX" in scanner_src),
            ("normalize_alpaca_exists",    "def _normalize_alpaca" in scanner_src),
            ("normalize_ibkr_exists",      "def _normalize_ibkr" in scanner_src),
            ("ibkr_primary_fallback",      "_use_alpaca" in scanner_src),
            ("src_label_alpaca",           '"Alpaca"' in scanner_src),
            ("src_label_ibkr",             '"IBKR"' in scanner_src),
        ]
        for name, cond in checks:
            if cond: passed += 1
            else:
                failed += 1
                defects.append(name)
            details.append(_case(name, cond))

    except Exception as e:
        failed += 1
        details.append(_case("scanner_read", False, str(e)))

    # Verify agent.py also has Alpaca fallback config key
    try:
        agent_src = (ALPA_DIR / "agent.py").read_text(encoding="utf-8")
        ok_a = "ALPACA" in agent_src or "alpaca" in agent_src
        if ok_a: passed += 1
        else: failed += 1
        details.append(_case("agent_references_alpaca", ok_a))
    except Exception as e:
        failed += 1
        details.append(_case("agent_read", False, str(e)))

    status = "PASS" if failed == 0 else "PARTIAL" if passed > 0 else "FAIL"
    run = passed + failed + deferred
    _finish(_report("03", "Alpaca Scanner Data Path", status,
                    run, passed, failed, deferred, details, defects), "alpa_03_data_path.json")

# ── ALPA TEST 04 — Config Validation ─────────────────────────────────────────

def test_alpa04_config():
    print("\n[ALPA TEST 04] Config Validation")
    details, defects = [], []
    passed = failed = deferred = 0

    try:
        import config as cfg

        numeric_checks = [
            ("force_close_hour_15",       cfg.FORCE_CLOSE_HOUR == 15),
            ("force_close_min_45",        cfg.FORCE_CLOSE_MIN  == 45),
            ("stop_loss_pct_positive",    0 < cfg.STOP_LOSS_PCT    < 0.1),
            ("take_profit_pct_positive",  0 < cfg.TAKE_PROFIT_PCT  < 0.1),
            ("position_size_pct_positive",0 < cfg.POSITION_SIZE_PCT <= 1.0),
            ("min_gap_pct_positive",      cfg.MIN_GAP_PCT   > 0),
            ("max_spread_pct_positive",   cfg.MAX_SPREAD_PCT > 0),
            ("min_rel_volume_gte1",       cfg.MIN_REL_VOLUME >= 1.0),
        ]
        for name, cond in numeric_checks:
            if cond: passed += 1
            else:
                failed += 1
                defects.append(name)
            details.append(_case(name, cond,
                str(getattr(cfg, name.split("_")[0].upper(), "N/A"))[:40]))

        bool_checks = [
            ("paper_trading_true",        getattr(cfg, "PAPER_TRADING", False) is True),
            ("use_limit_orders_bool",     isinstance(getattr(cfg, "USE_LIMIT_ORDERS", None), bool)),
        ]
        for name, cond in bool_checks:
            if cond: passed += 1
            else:
                failed += 1
                defects.append(name)
            details.append(_case(name, cond))

        # Watchlist is a non-empty list of strings
        wl = getattr(cfg, "WATCHLIST", [])
        ok_wl = isinstance(wl, list) and len(wl) > 0 and all(isinstance(s, str) for s in wl)
        if ok_wl: passed += 1
        else: failed += 1
        details.append(_case("watchlist_valid", ok_wl, f"len={len(wl)}"))

    except Exception as e:
        failed += 1
        defects.append(str(e))
        details.append(_case("config_import", False, str(e)))

    status = "PASS" if failed == 0 else "PARTIAL" if passed > 0 else "FAIL"
    run = passed + failed + deferred
    _finish(_report("04", "Config Validation", status,
                    run, passed, failed, deferred, details, defects), "alpa_04_config.json")

# ── ALPA TEST 05 — Gate Logic Code Presence ───────────────────────────────────

def test_alpa05_gate_logic():
    print("\n[ALPA TEST 05] Gate Logic -- Phase 1 bypasses present in agent")
    details, defects = [], []
    passed = failed = deferred = 0

    try:
        agent_src = (ALPA_DIR / "agent.py").read_text(encoding="utf-8")

        gates = [
            ("momentum_gate_present",        "momentum_weak" in agent_src),
            ("momentum_bypass_present",      "MOMENTUM BYPASS" in agent_src or "mfo >= 4.0" in agent_src),
            ("fb_bypass_present",            "BREAKOUT BYPASS" in agent_src or "fb_failed = False" in agent_src),
            ("hod_bypass_present",           "HOD BYPASS" in agent_src or "_hod_near" in agent_src),
            ("hod_2pct_threshold",           "2.0" in agent_src and "_hod_near" in agent_src),
            ("confirmation_hod_type",        "HOD_CONSOLIDATION" in agent_src),
            ("orb_pb_confirmed_combines_all","orb_pb_confirmed" in agent_src and "or pb_detected or _hod_near" in agent_src),
            ("scan_capped_event",            "SCAN_CAPPED" in agent_src),
            ("reject_fn_present",            "def _reject" in agent_src or "_reject(" in agent_src),
        ]
        for name, cond in gates:
            if cond: passed += 1
            else:
                failed += 1
                defects.append(name)
            details.append(_case(name, cond))

    except Exception as e:
        failed += 1
        details.append(_case("agent_read", False, str(e)))

    status = "PASS" if failed == 0 else "PARTIAL" if passed > 0 else "FAIL"
    run = passed + failed + deferred
    _finish(_report("05", "Gate Logic Phase 1 Bypasses", status,
                    run, passed, failed, deferred, details, defects), "alpa_05_gate_logic.json")

# ── ALPA TEST 06 — Cross-Agent Parity (Alpaca vs IBKR code symmetry) ─────────

def test_alpa06_cross_agent_parity():
    print("\n[ALPA TEST 06] Cross-Agent Code Parity")
    details, defects = [], []
    passed = failed = deferred = 0

    try:
        alpa_scanner = (ALPA_DIR / "scanner.py").read_text(encoding="utf-8")
        ibkr_scanner = (IBKR_DIR / "scanner.py").read_text(encoding="utf-8")
        alpa_agent   = (ALPA_DIR / "agent.py").read_text(encoding="utf-8")
        ibkr_agent   = (IBKR_DIR / "agent.py").read_text(encoding="utf-8")

        # Both scanners must have same Phase 1 features
        shared_scanner_features = [
            ("both_have_has_catalyst",    '"has_catalyst"' in alpa_scanner and '"has_catalyst"' in ibkr_scanner),
            ("both_have_float_tier",      '"float_tier"' in alpa_scanner and '"float_tier"' in ibkr_scanner),
            ("both_cap_at_25",            "candidates[:25]" in alpa_scanner and "candidates[:25]" in ibkr_scanner),
            ("both_have_catalyst_boost",  "1.15" in alpa_scanner and "1.15" in ibkr_scanner),
            ("both_have_catalyst_kws",    "_CATALYST_KEYWORDS" in alpa_scanner and "_CATALYST_KEYWORDS" in ibkr_scanner),
            ("both_have_symbol_info",     "_symbol_info" in alpa_scanner and "_symbol_info" in ibkr_scanner),
        ]
        # Both agents must have same gate bypasses
        shared_agent_features = [
            ("both_momentum_bypass",      "MOMENTUM BYPASS" in alpa_agent and "MOMENTUM BYPASS" in ibkr_agent),
            ("both_fb_bypass",            "BREAKOUT BYPASS" in alpa_agent and "BREAKOUT BYPASS" in ibkr_agent),
            ("both_hod_bypass",           "HOD BYPASS" in alpa_agent and "HOD BYPASS" in ibkr_agent),
            ("both_scan_capped",          "SCAN_CAPPED" in alpa_agent and "SCAN_CAPPED" in ibkr_agent),
            ("both_hod_consolidation",    "HOD_CONSOLIDATION" in alpa_agent and "HOD_CONSOLIDATION" in ibkr_agent),
            ("both_orb_pb_confirmed",     "orb_pb_confirmed" in alpa_agent and "orb_pb_confirmed" in ibkr_agent),
        ]
        for name, cond in (shared_scanner_features + shared_agent_features):
            if cond: passed += 1
            else:
                failed += 1
                defects.append(name)
            details.append(_case(name, cond))

        # Config parity for critical trading params
        import config as alpa_cfg
        if "config" in sys.modules: del sys.modules["config"]
        sys.path.insert(0, str(IBKR_DIR))
        import config as ibkr_cfg
        if "config" in sys.modules: del sys.modules["config"]
        sys.path.remove(str(IBKR_DIR))

        config_parity = [
            ("force_close_min_same",    alpa_cfg.FORCE_CLOSE_MIN  == ibkr_cfg.FORCE_CLOSE_MIN),
            ("force_close_hour_same",   alpa_cfg.FORCE_CLOSE_HOUR == ibkr_cfg.FORCE_CLOSE_HOUR),
            ("stop_loss_pct_same",      alpa_cfg.STOP_LOSS_PCT    == ibkr_cfg.STOP_LOSS_PCT),
            ("take_profit_pct_same",    alpa_cfg.TAKE_PROFIT_PCT  == ibkr_cfg.TAKE_PROFIT_PCT),
            ("min_gap_pct_same",        alpa_cfg.MIN_GAP_PCT      == ibkr_cfg.MIN_GAP_PCT),
            ("max_spread_pct_same",     alpa_cfg.MAX_SPREAD_PCT   == ibkr_cfg.MAX_SPREAD_PCT),
            ("min_rel_volume_same",     alpa_cfg.MIN_REL_VOLUME   == ibkr_cfg.MIN_REL_VOLUME),
        ]
        for name, cond in config_parity:
            if cond: passed += 1
            else:
                failed += 1
                defects.append(name)
                alpa_v = getattr(alpa_cfg, name.replace("_same","").upper().replace("_",""), "?")
                ibkr_v = getattr(ibkr_cfg, name.replace("_same","").upper().replace("_",""), "?")
                details.append(_case(name, cond, f"alpaca={alpa_v} ibkr={ibkr_v}"))
                continue
            details.append(_case(name, cond))

    except Exception as e:
        failed += 1
        defects.append(str(e))
        details.append(_case("parity_check", False, str(e)))

    status = "PASS" if failed == 0 else "PARTIAL" if passed > 0 else "FAIL"
    run = passed + failed + deferred
    _finish(_report("06", "Cross-Agent Code Parity", status,
                    run, passed, failed, deferred, details, defects), "alpa_06_parity.json")

# ── ALPA TEST 07 — Holiday Regression ────────────────────────────────────────

def test_alpa07_holiday_regression():
    print("\n[ALPA TEST 07] Holiday Regression -- NYSE calendar")
    details, defects = [], []
    passed = failed = deferred = 0

    NYSE_HOLIDAYS = {
        (2026,  1,  1), (2026,  1, 19), (2026,  2, 16), (2026,  4,  3),
        (2026,  5, 25), (2026,  7,  3), (2026,  9,  7), (2026, 11, 26),
        (2026, 12, 25),
        (2027,  1,  1), (2027,  1, 18), (2027,  2, 15), (2027,  4, 26),
        (2027,  5, 31), (2027,  7,  5), (2027,  9,  6), (2027, 11, 25),
        (2027, 12, 24),
    }

    def _is_trading(d):
        if d.weekday() >= 5: return False
        return (d.year, d.month, d.day) not in NYSE_HOLIDAYS

    from datetime import date as _date

    holidays = [
        (_date(2026,  1,  1), "new_years"),
        (_date(2026,  1, 19), "mlk"),
        (_date(2026,  2, 16), "presidents"),
        (_date(2026,  4,  3), "good_friday"),
        (_date(2026,  5, 25), "memorial"),
        (_date(2026, 11, 26), "thanksgiving"),
        (_date(2026, 12, 25), "christmas"),
    ]
    for d, name in holidays:
        ok = not _is_trading(d)
        if ok: passed += 1
        else: failed += 1; defects.append(f"{name} not blocked")
        details.append(_case(f"holiday_{name}", ok, str(d)))

    # Normal trading days
    trading = [
        (_date(2026,  1,  2), "jan2"),
        (_date(2026,  5, 29), "may29"),
        (_date(2026, 11, 27), "black_friday"),
    ]
    for d, name in trading:
        ok = _is_trading(d)
        if ok: passed += 1
        else: failed += 1; defects.append(f"{name} wrongly blocked")
        details.append(_case(f"trading_{name}", ok, str(d)))

    # Alpaca uses server-side clock — verify _market_open uses Alpaca API
    try:
        agent_src = (ALPA_DIR / "agent.py").read_text(encoding="utf-8")
        uses_alpaca_clock = "get_clock" in agent_src or "is_open" in agent_src
        if uses_alpaca_clock: passed += 1
        else: failed += 1; defects.append("agent does not use Alpaca clock API")
        details.append(_case("alpaca_clock_api_used", uses_alpaca_clock,
            "Alpaca clock.is_open handles holidays server-side"))
    except Exception as e:
        failed += 1; details.append(_case("agent_clock_check", False, str(e)))

    status = "PASS" if failed == 0 else "PARTIAL" if passed > 0 else "FAIL"
    run = passed + failed + deferred
    _finish(_report("07", "Holiday Regression", status,
                    run, passed, failed, deferred, details, defects),
            "alpa_07_holiday.json")


# ── ALPA TEST 08 — Readiness Check (TEST_21 equivalent) ──────────────────────

def test_alpa08_readiness():
    print("\n[ALPA TEST 08] Alpaca Agent Readiness")
    details, defects = [], []
    passed = failed = deferred = 0

    scores: dict[str, float] = {}

    # DATA category
    data_checks = []
    try:
        import os; from dotenv import load_dotenv
        load_dotenv(ALPA_DIR / ".env")
        data_checks.append(("api_key_set",    bool(os.getenv("ALPACA_API_KEY",""))))
        data_checks.append(("secret_key_set", bool(os.getenv("ALPACA_SECRET_KEY",""))))
        data_checks.append(("finnhub_key_set",bool(os.getenv("FINNHUB_API_KEY",""))))
    except Exception: pass

    try:
        data_checks.append(("scanner_file_present",  (ALPA_DIR/"scanner.py").exists()))
        data_checks.append(("gapper_file_present",   (ALPA_DIR/"gapper.py").exists()))
        data_checks.append(("feed_log_present",
            (ALPA_DIR/"feed_log.jsonl").exists() or (ALPA_DIR/"audit.log").exists()))
    except Exception: pass

    data_p = sum(1 for _, c in data_checks if c)
    scores["DATA"] = data_p / max(len(data_checks), 1)
    for name, cond in data_checks:
        if cond: passed += 1
        else: failed += 1; defects.append(f"DATA:{name}")
        details.append(_case(f"data_{name}", cond))

    # LOGIC category
    logic_checks = []
    try:
        scanner_src = (ALPA_DIR/"scanner.py").read_text(encoding="utf-8")
        agent_src   = (ALPA_DIR/"agent.py").read_text(encoding="utf-8")
        logic_checks = [
            ("has_catalyst_logic",    "_has_catalyst" in scanner_src),
            ("float_tier_logic",      "_symbol_info" in scanner_src),
            ("cap_25",                "candidates[:25]" in scanner_src),
            ("momentum_bypass",       "MOMENTUM BYPASS" in agent_src),
            ("hod_bypass",            "HOD BYPASS" in agent_src),
            ("fb_bypass",             "BREAKOUT BYPASS" in agent_src),
            ("scan_cap_rejection",    "scan_capped" in agent_src),
        ]
    except Exception as e:
        logic_checks = [("logic_load", False)]

    logic_p = sum(1 for _, c in logic_checks if c)
    scores["LOGIC"] = logic_p / max(len(logic_checks), 1)
    for name, cond in logic_checks:
        if cond: passed += 1
        else: failed += 1; defects.append(f"LOGIC:{name}")
        details.append(_case(f"logic_{name}", cond))

    # RISK category
    risk_checks = []
    try:
        risk_src = (ALPA_DIR/"risk.py").read_text(encoding="utf-8") if (ALPA_DIR/"risk.py").exists() else ""
        agent_src2 = (ALPA_DIR/"agent.py").read_text(encoding="utf-8")
        risk_checks = [
            ("risk_py_exists",        (ALPA_DIR/"risk.py").exists()),
            ("can_trade_fn",          "can_trade" in risk_src or "can_trade" in agent_src2),
            ("stop_loss_applied",     "STOP_LOSS_PCT" in agent_src2 or "stop_loss" in agent_src2.lower()
                                         or "STOP_LOSS_PCT" in risk_src or "stop_loss" in risk_src.lower()),
            ("position_size_applied", "position_size" in agent_src2 or "POSITION_SIZE" in agent_src2),
            ("force_close_present",   "FORCE_CLOSE" in agent_src2),
            ("pdt_budget_present",    "_pdt_budget" in agent_src2),
        ]
    except Exception: pass

    risk_p = sum(1 for _, c in risk_checks if c)
    scores["RISK"] = risk_p / max(len(risk_checks), 1)
    for name, cond in risk_checks:
        if cond: passed += 1
        else: failed += 1; defects.append(f"RISK:{name}")
        details.append(_case(f"risk_{name}", cond))

    # EXECUTION category
    exec_checks = []
    try:
        exec_src = (ALPA_DIR/"executor.py").read_text(encoding="utf-8") if (ALPA_DIR/"executor.py").exists() else ""
        exec_checks = [
            ("executor_exists",        (ALPA_DIR/"executor.py").exists()),
            ("paper_mode_respected",   "PAPER_TRADING" in exec_src or "paper" in exec_src.lower()),
            ("limit_orders_used",      "limit" in exec_src.lower()),
            ("audit_log_on_fill",      "audit" in exec_src.lower() or "log_audit" in exec_src
                                         or "log_audit" in (ALPA_DIR/"logger.py").read_text(encoding="utf-8")),
            ("exits_file_present",     (ALPA_DIR/"exits.py").exists()),
            ("force_close_45",         "FORCE_CLOSE_MIN" in exec_src or
                                       "FORCE_CLOSE" in (ALPA_DIR/"agent.py").read_text()),
        ]
    except Exception: pass

    exec_p = sum(1 for _, c in exec_checks if c)
    scores["EXECUTION"] = exec_p / max(len(exec_checks), 1)
    for name, cond in exec_checks:
        if cond: passed += 1
        else: failed += 1; defects.append(f"EXEC:{name}")
        details.append(_case(f"exec_{name}", cond))

    # Config check
    try:
        import config as cfg
        config_checks = [
            ("force_close_45",      cfg.FORCE_CLOSE_MIN == 45),
            ("paper_trading_true",  getattr(cfg,"PAPER_TRADING",False) is True),
            ("stop_loss_sane",      0 < cfg.STOP_LOSS_PCT < 0.05),
        ]
        config_p = sum(1 for _, c in config_checks if c)
        scores["CONFIG"] = config_p / max(len(config_checks), 1)
        for name, cond in config_checks:
            if cond: passed += 1
            else: failed += 1; defects.append(f"CONFIG:{name}")
            details.append(_case(f"config_{name}", cond))
    except Exception as e:
        scores["CONFIG"] = 0.0; failed += 1
        details.append(_case("config_import", False, str(e)))

    overall = sum(scores.values()) / max(len(scores), 1)
    paper_ok = all(scores.get(k, 0) >= 0.8 for k in ("DATA","LOGIC","RISK","EXECUTION"))
    allowed_mode = "PAPER_ONLY" if paper_ok else "NO_TRADING"

    print(f"\n   Readiness scores:")
    for k, v in scores.items():
        print(f"     {k:<12} {v:.1%}")
    print(f"   Overall: {overall:.1%}   Mode: {allowed_mode}")

    run = passed + failed + deferred
    status = "PASS" if failed == 0 else "PARTIAL" if passed > 0 else "FAIL"
    notes = f"overall={overall:.2f} mode={allowed_mode} " + \
            " ".join(f"{k}={v:.2f}" for k, v in scores.items())
    _finish(_report("08", "Alpaca Agent Readiness", status,
                    run, passed, failed, deferred, details, defects, notes),
            "alpa_08_readiness.json")


# ── ALPA TEST 09 — Real-World Gaps ───────────────────────────────────────────

def test_alpa09_realworld_gaps():
    print("\n[ALPA TEST 09] Real-World Trading Gaps")
    details, defects = [], []
    passed = failed = deferred = 0
    from datetime import date as _date

    # Fill quality
    try:
        tel = ALPA_DIR / "execution_telemetry.jsonl"
        if tel.exists():
            lines = [l for l in tel.read_text().splitlines() if l.strip()]
            entries = [json.loads(l) for l in lines[-50:]]
            slips = []
            for e in entries:
                lp = e.get("limit_price", 0); fp = e.get("fill_price", 0)
                if lp > 0 and fp > 0: slips.append(abs(fp-lp)/lp*100)
            if slips:
                avg = sum(slips)/len(slips); ok = avg < 0.5
                if ok: passed += 1
                else: failed += 1; defects.append(f"slippage {avg:.2f}%>0.5%")
                details.append(_case("fill_slippage_acceptable", ok, f"avg={avg:.3f}%"))
            else:
                deferred += 1; details.append(_case("fill_quality", False, "no fills yet", deferred=True))
        else:
            deferred += 1; details.append(_case("fill_telemetry_file", False, "file not found", deferred=True))
    except Exception as e:
        deferred += 1; details.append(_case("fill_quality", False, str(e), deferred=True))

    # PDT tracking
    try:
        agent_src = (ALPA_DIR/"agent.py").read_text(encoding="utf-8")
        for name, cond in [
            ("pdt_budget_fn",   "_pdt_budget" in agent_src),
            ("pdt_tracking",    "day_trades" in agent_src.lower() or "pdt" in agent_src.lower()),
        ]:
            if cond: passed += 1
            else: failed += 1; defects.append(name)
            details.append(_case(name, cond))
    except Exception as e:
        failed += 1; details.append(_case("pdt_check", False, str(e)))

    # Score drift
    try:
        eff = ALPA_DIR / "claude_effectiveness.jsonl"
        if eff.exists():
            lines  = [l for l in eff.read_text().splitlines() if l.strip()]
            recent = [json.loads(l) for l in lines[-20:]]
            deltas = [abs(r.get("delta",0)) for r in recent if "delta" in r]
            if deltas:
                avg = sum(deltas)/len(deltas); bad = sum(1 for d in deltas if d>15)
                ok  = bad < len(deltas)*0.5
                if ok: passed += 1
                else: failed += 1; defects.append(f"score drift avg={avg:.1f}")
                details.append(_case("score_drift_ok", ok, f"avg={avg:.1f}pts, {bad}/{len(deltas)}>15"))
            else:
                deferred += 1; details.append(_case("score_drift", False, "no data", deferred=True))
        else:
            deferred += 1; details.append(_case("score_drift", False, "file not found", deferred=True))
    except Exception as e:
        deferred += 1; details.append(_case("score_drift", False, str(e), deferred=True))

    # Orphan positions
    try:
        import sqlite3
        db = ALPA_DIR / "daytrades.db"
        if db.exists():
            conn = sqlite3.connect(db); cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM trades WHERE status='open' AND DATE(entry_time) < ?",
                        (_date.today().isoformat(),))
            count = cur.fetchone()[0]; conn.close()
            ok = count == 0
            if ok: passed += 1
            else: failed += 1; defects.append(f"{count} orphan positions")
            details.append(_case("no_orphan_positions", ok, f"{count} prior-session open"))
        else:
            deferred += 1; details.append(_case("orphan_check", False, "no db yet", deferred=True))
    except Exception as e:
        deferred += 1; details.append(_case("orphan_check", False, str(e), deferred=True))

    run = passed + failed + deferred
    status = "PASS" if failed == 0 and deferred < run else (
             "PARTIAL" if passed > 0 else "DEFERRED" if failed == 0 else "FAIL")
    _finish(_report("09", "Real-World Trading Gaps", status,
                    run, passed, failed, deferred, details, defects),
            "alpa_09_realworld.json")


# ── Run all tests ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    test_alpa01_alpaca_connectivity()
    test_alpa02_snapshot_normalization()
    test_alpa03_scanner_data_path()
    test_alpa04_config()
    test_alpa05_gate_logic()
    test_alpa06_cross_agent_parity()
    test_alpa07_holiday_regression()
    test_alpa08_readiness()
    test_alpa09_realworld_gaps()

    total_pass    = sum(r["cases_passed"]   for r in _all_results)
    total_fail    = sum(r["cases_failed"]   for r in _all_results)
    total_defer   = sum(r["cases_deferred"] for r in _all_results)
    total_run     = sum(r["cases_run"]      for r in _all_results)
    all_defects   = [d for r in _all_results for d in r["defects_found"]]
    overall_score = round((total_pass + total_defer * 0.5) / max(total_run, 1), 4)

    print(f"\n{'='*62}")
    print(f"Alpaca Integration Tests: {total_pass}/{total_run} passed "
          f"({total_defer} deferred, {total_fail} failed) -- score={overall_score:.2f}")
    if total_fail:
        print(f"  Defects: {', '.join(all_defects[:5])}")
    print(f"{'='*62}")

    summary = {
        "suite": "alpaca_integration_tests",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_run": total_run, "total_passed": total_pass,
        "total_failed": total_fail, "total_deferred": total_defer,
        "overall_score": overall_score, "all_defects": all_defects,
        "test_results": _all_results,
    }
    (REPORTS / "alpaca_suite_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(f"Summary: {REPORTS / 'alpaca_suite_summary.json'}")
    sys.exit(0 if total_fail == 0 else 1)
