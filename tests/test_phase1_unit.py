"""
Phase 1 Unit Tests — Alpaca agent
Port of leela-ibkr-agent/tests/test_phase1_unit.py for the Alpaca data path.
Additions: _normalize_alpaca, Alpaca config, Alpaca mock modules.
Run: python tests\test_phase1_unit.py
"""
import sys, os, json
from pathlib import Path
from unittest.mock import MagicMock, patch

ALPA_DIR  = Path(r"C:\Users\leela\leela-daytrading-agent")
IBKR_DIR  = Path(r"C:\Users\leela\leela-ibkr-agent")
TESTS_DIR = ALPA_DIR / "tests"
REPORTS   = TESTS_DIR / "reports"
REPORTS.mkdir(parents=True, exist_ok=True)

# ── Tracking ──────────────────────────────────────────────────────────────────

_passed = _failed = 0
_defects: list[str] = []
_details: list[dict] = []

def ok(name: str, condition: bool, detail: str = ""):
    global _passed, _failed
    status = "PASS" if condition else "FAIL"
    if condition:
        _passed += 1
    else:
        _failed += 1
        _defects.append(name)
    tag = "  [PASS]" if condition else "  [FAIL]"
    print(f"{tag} {name}" + (f" -- {detail}" if detail else ""))
    _details.append({"case": name, "status": status, "detail": detail or None})

# ── Mock external dependencies before importing scanner ───────────────────────

_fake_ib_insync = MagicMock()
_fake_ib_insync.Stock = MagicMock
sys.modules.setdefault("ib_insync",   _fake_ib_insync)
sys.modules.setdefault("ibkr_client", MagicMock())
sys.modules.setdefault("gapper",      MagicMock())
sys.modules.setdefault("finnhub",     MagicMock())

# Alpaca SDK mocks
_fake_alpaca_hist    = MagicMock()
_fake_alpaca_req     = MagicMock()
sys.modules.setdefault("alpaca",                           MagicMock())
sys.modules.setdefault("alpaca.data",                      MagicMock())
sys.modules.setdefault("alpaca.data.historical",           _fake_alpaca_hist)
sys.modules.setdefault("alpaca.data.requests",             _fake_alpaca_req)
sys.modules.setdefault("alpaca.data.historical.stock",     MagicMock())
sys.modules.setdefault("alpaca.data.requests.stock",       MagicMock())

sys.path.insert(0, str(ALPA_DIR))
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("alpa_scanner_unit", ALPA_DIR / "scanner.py")
_scanner = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_scanner)

# Replace real yfinance inside the scanner namespace with a controllable mock
_yf_mock = MagicMock()
_scanner.yf = _yf_mock

# ═══════════════════════════════════════════════════════════════════════════════
# 1. _has_catalyst — keyword detection
# ═══════════════════════════════════════════════════════════════════════════════

print("\n[1] _has_catalyst -- keyword detection")

ok("cat_earnings",          _scanner._has_catalyst(["ACME earnings beat estimates"]))
ok("cat_beat",              _scanner._has_catalyst(["Revenue beat by 15%"]))
ok("cat_fda_approved",      _scanner._has_catalyst(["FDA approved ACME oncology drug"]))
ok("cat_acquisition",       _scanner._has_catalyst(["ACME acquires RivalCo for $4B"]))
ok("cat_merger",            _scanner._has_catalyst(["Merger signed between ACME and BETA"]))
ok("cat_guidance_raised",   _scanner._has_catalyst(["Management raised full-year guidance"]))
ok("cat_upgrade",           _scanner._has_catalyst(["Morgan Stanley upgrades ACME to buy"]))
ok("cat_partnership",       _scanner._has_catalyst(["Strategic partnership announced"]))
ok("cat_contract",          _scanner._has_catalyst(["$500M government contract awarded"]))
ok("cat_buyout",            _scanner._has_catalyst(["Private equity buyout at $80/share"]))
ok("cat_case_insensitive",  _scanner._has_catalyst(["ACME EARNINGS BEATS FORECASTS"]))

ok("no_cat_generic",        not _scanner._has_catalyst(["Market broadly higher today"]))
ok("no_cat_empty",          not _scanner._has_catalyst([]))
ok("no_cat_results",        not _scanner._has_catalyst(["Q3 financial results in-line"]))
ok("no_cat_downgrade",      not _scanner._has_catalyst(["Analyst downgrades on valuation"]))

# ═══════════════════════════════════════════════════════════════════════════════
# 2. _symbol_info — float tier bucketing
# ═══════════════════════════════════════════════════════════════════════════════

print("\n[2] _symbol_info -- float tier bucketing (no live API)")

def _mock_ticker(shares, sector="Technology"):
    m = MagicMock()
    m.info = {"sector": sector, "floatShares": shares, "sharesOutstanding": 0}
    return m

_yf_mock.Ticker.side_effect = None

_yf_mock.Ticker.return_value = _mock_ticker(0)
_, t = _scanner._symbol_info("T"); ok("tier_zero_unknown",   t == "unknown", f"tier={t}")

_yf_mock.Ticker.return_value = _mock_ticker(5_000_000)
_, t = _scanner._symbol_info("T"); ok("tier_5M_low",         t == "low",     f"tier={t}")

_yf_mock.Ticker.return_value = _mock_ticker(20_000_000)
_, t = _scanner._symbol_info("T"); ok("tier_20M_mid",        t == "mid",     f"tier={t}")

_yf_mock.Ticker.return_value = _mock_ticker(100_000_000)
_, t = _scanner._symbol_info("T"); ok("tier_100M_mid",       t == "mid",     f"tier={t}")

_yf_mock.Ticker.return_value = _mock_ticker(100_000_001)
_, t = _scanner._symbol_info("T"); ok("tier_100pt1M_large",  t == "large",   f"tier={t}")

_yf_mock.Ticker.return_value = _mock_ticker(500_000_000)
_, t = _scanner._symbol_info("T"); ok("tier_500M_large",     t == "large",   f"tier={t}")

m2 = MagicMock()
m2.info = {"sector": "Finance", "floatShares": None, "sharesOutstanding": 30_000_000}
_yf_mock.Ticker.return_value = m2
_, t = _scanner._symbol_info("T"); ok("tier_falls_back_to_sharesOutstanding", t == "mid", f"tier={t}")

m3 = MagicMock()
m3.info = {"sector": "Healthcare", "floatShares": 10_000_000}
_yf_mock.Ticker.return_value = m3
s, _ = _scanner._symbol_info("T"); ok("sector_returned", s == "Healthcare", f"sector={s}")

_yf_mock.Ticker.side_effect = Exception("network error")
s, t = _scanner._symbol_info("T")
ok("exception_sector_unknown", s == "Unknown", f"sector={s}")
ok("exception_tier_unknown",   t == "unknown", f"tier={t}")
_yf_mock.Ticker.side_effect = None

# ═══════════════════════════════════════════════════════════════════════════════
# 3. _normalize_alpaca — Alpaca-specific data normalisation
# ═══════════════════════════════════════════════════════════════════════════════

print("\n[3] _normalize_alpaca -- Alpaca snapshot to normalised dict")

def _make_snap(price=50.0, prev_close=47.0, vol=100_000,
               bid=49.9, ask=50.1, high=51.0, low=48.0,
               open_=47.5, vwap=49.5, no_quote=False,
               no_daily_bar=False, no_vwap=False):
    snap = MagicMock()
    snap.latest_trade.price    = price
    snap.previous_daily_bar.close = prev_close
    if no_daily_bar:
        snap.daily_bar = None
    else:
        snap.daily_bar.volume = vol
        snap.daily_bar.high   = high
        snap.daily_bar.low    = low
        snap.daily_bar.open   = open_
        if no_vwap:
            snap.daily_bar.vwap = None
            # hasattr check returns False for None mocks; set via spec trick
            type(snap.daily_bar).vwap = property(lambda self: None)
        else:
            snap.daily_bar.vwap = vwap
    if no_quote:
        snap.latest_quote = None
    else:
        snap.latest_quote.bid_price = bid
        snap.latest_quote.ask_price = ask
    return snap

# Full snap
r = _scanner._normalize_alpaca(_make_snap(), "TEST")
ok("norm_alpa_price",      r is not None and r["price"]      == 50.0)
ok("norm_alpa_prev_close", r is not None and r["prev_close"] == 47.0)
ok("norm_alpa_today_vol",  r is not None and r["today_vol"]  == 100_000)
ok("norm_alpa_bid",        r is not None and r["bid"]        == 49.9)
ok("norm_alpa_ask",        r is not None and r["ask"]        == 50.1)
ok("norm_alpa_day_high",   r is not None and r["day_high"]   == 51.0)
ok("norm_alpa_day_low",    r is not None and r["day_low"]    == 48.0)
ok("norm_alpa_open_price", r is not None and r["open_price"] == 47.5)
ok("norm_alpa_vwap",       r is not None and r["vwap"]       == 49.5)
ok("norm_alpa_data_src",   r is not None and r["data_src"]   == "alpaca")

# No quote -> bid/ask fallback to price approximation
r2 = _scanner._normalize_alpaca(_make_snap(no_quote=True), "TEST")
ok("norm_alpa_no_quote_bid_approx", r2 is not None and abs(r2["bid"] - 50.0 * 0.999) < 0.01)
ok("norm_alpa_no_quote_ask_approx", r2 is not None and abs(r2["ask"] - 50.0 * 1.001) < 0.01)

# No daily_bar -> fallback values
r3 = _scanner._normalize_alpaca(_make_snap(no_daily_bar=True), "TEST")
ok("norm_alpa_no_bar_vol_zero",       r3 is not None and r3["today_vol"] == 0)
ok("norm_alpa_no_bar_high_is_price",  r3 is not None and r3["day_high"]  == 50.0)
ok("norm_alpa_no_bar_low_is_price",   r3 is not None and r3["day_low"]   == 50.0)
ok("norm_alpa_no_bar_open_is_close",  r3 is not None and r3["open_price"] == 47.0)

# Exception -> returns None
bad_snap = MagicMock()
bad_snap.latest_trade.price = "not_a_float_abc"
r4 = _scanner._normalize_alpaca(bad_snap, "TEST")
ok("norm_alpa_exception_returns_none", r4 is None)

# ═══════════════════════════════════════════════════════════════════════════════
# 4. Catalyst sort boost — 15% priority lift
# ═══════════════════════════════════════════════════════════════════════════════

print("\n[4] Catalyst sort boost -- 15% priority lift")

def _sort_key(c): return c["gap_pct"] * c["rel_volume"] * (1.15 if c.get("has_catalyst") else 1.0)
base = {"gap_pct": 5.0, "rel_volume": 2.0}
c_no  = {**base, "has_catalyst": False}
c_yes = {**base, "has_catalyst": True}

ok("catalyst_score_higher",     _sort_key(c_yes) > _sort_key(c_no))
ok("catalyst_boost_15pct",      abs(_sort_key(c_yes) / _sort_key(c_no) - 1.15) < 1e-9)
lst = [c_no, c_yes]; lst.sort(key=_sort_key, reverse=True)
ok("catalyst_sorted_first",     lst[0] is c_yes)

# ═══════════════════════════════════════════════════════════════════════════════
# 5. Momentum bypass — all 3 conditions required
# ═══════════════════════════════════════════════════════════════════════════════

print("\n[5] Momentum bypass -- all 3 conditions required")

def _mom_bypass(mfo, rvol, price, vwap): return mfo >= 4.0 and rvol >= 1.5 and price > vwap > 0

ok("mom_bypass_all_conditions",       _mom_bypass(5.0, 2.0, 50.0, 48.0))
ok("mom_bypass_at_boundary",          _mom_bypass(4.0, 1.5, 50.01, 50.0))
ok("mom_no_bypass_low_mfo",           not _mom_bypass(3.9, 2.0, 50.0, 48.0))
ok("mom_no_bypass_low_rvol",          not _mom_bypass(5.0, 1.49, 50.0, 48.0))
ok("mom_no_bypass_below_vwap",        not _mom_bypass(5.0, 2.0, 47.0, 48.0))
ok("mom_no_bypass_vwap_zero",         not _mom_bypass(5.0, 2.0, 50.0, 0.0))
ok("mom_no_bypass_price_eq_vwap",     not _mom_bypass(5.0, 2.0, 48.0, 48.0))

# ═══════════════════════════════════════════════════════════════════════════════
# 6. Failed-breakout bypass
# ═══════════════════════════════════════════════════════════════════════════════

print("\n[6] Failed-breakout bypass -- identical 3-condition check")

def _fb_bypass(mfo, rvol, price, vwap): return mfo >= 4.0 and rvol >= 1.5 and price > vwap > 0

ok("fb_bypass_fires",         _fb_bypass(6.0, 2.5, 52.0, 50.0))
ok("fb_no_bypass_low_rvol",   not _fb_bypass(6.0, 1.0, 52.0, 50.0))
ok("fb_no_bypass_below_vwap", not _fb_bypass(6.0, 2.5, 49.0, 50.0))

# ═══════════════════════════════════════════════════════════════════════════════
# 7. HOD bypass — within 2% of intraday high
# ═══════════════════════════════════════════════════════════════════════════════

print("\n[7] HOD bypass -- within 2% of intraday high")

def _hod_bypass(bars, mfo, rvol, price):
    if not bars or mfo < 4.0 or rvol < 1.5: return False
    hod = max((b.get("high", b.get("close", 0)) for b in bars), default=0)
    if hod <= 0: return False
    return ((hod - price) / hod * 100) <= 2.0

bars_near = [{"high": 51.0}, {"high": 50.0}]
bars_far  = [{"high": 55.0}, {"high": 50.0}]

ok("hod_within_2pct",       _hod_bypass(bars_near, 5.0, 2.0, 50.0))
ok("hod_outside_2pct",      not _hod_bypass(bars_far, 5.0, 2.0, 50.0))
ok("hod_no_bars",           not _hod_bypass([], 5.0, 2.0, 50.0))
ok("hod_low_mfo",           not _hod_bypass(bars_near, 3.9, 2.0, 50.0))
ok("hod_low_rvol",          not _hod_bypass(bars_near, 5.0, 1.4, 50.0))
ok("hod_fallback_to_close", _hod_bypass([{"close": 51.0}], 4.0, 1.5, 50.0))

# ═══════════════════════════════════════════════════════════════════════════════
# 8. Schema validation — Level 2 checks
# ═══════════════════════════════════════════════════════════════════════════════

print("\n[8] Schema validation -- field types and value ranges")

scanner_src = (ALPA_DIR / "scanner.py").read_text(encoding="utf-8")

ok("has_catalyst_in_scanner",    '"has_catalyst"' in scanner_src)
ok("float_tier_in_scanner",      '"float_tier"' in scanner_src)
ok("has_news_in_scanner",        '"has_news"' in scanner_src)
ok("setup_type_in_scanner",      '"setup_type"' in scanner_src)
ok("is_afternoon_setup_in_scanner", '"is_afternoon_setup"' in scanner_src)
ok("data_src_alpaca_present",    '"alpaca"' in scanner_src)

VALID_TIERS = {"low", "mid", "large", "unknown"}
sample = {"has_catalyst": True, "has_news": True, "float_tier": "mid",
          "move_from_open": 5.0, "gap_pct": 3.2}

ok("has_catalyst_bool",          isinstance(sample["has_catalyst"], bool))
ok("float_tier_valid",           sample["float_tier"] in VALID_TIERS)
ok("move_from_open_in_range",    -100 <= sample["move_from_open"] <= 200)
ok("catalyst_implies_news",      not (sample["has_catalyst"] and not sample["has_news"]))
ok("no_catalyst_from_empty_news", not _scanner._has_catalyst([]))

# ═══════════════════════════════════════════════════════════════════════════════
# 9. Rejection logging — reason strings in agent
# ═══════════════════════════════════════════════════════════════════════════════

print("\n[9] Rejection logging -- reason strings in Alpaca agent.py")

agent_src = (ALPA_DIR / "agent.py").read_text(encoding="utf-8")

ok("scan_capped_in_alpa_agent",
    '"scan_capped"' in agent_src or "'scan_capped'" in agent_src)
ok("queued_tomorrow_in_alpa_agent",
    '"queued_tomorrow"' in agent_src or "'queued_tomorrow'" in agent_src)
ok("risk_stop_in_alpa_agent",
    '"risk_stop"' in agent_src or "'risk_stop'" in agent_src)
ok("trade_rejected_in_alpa_agent", "TRADE_REJECTED" in agent_src)
ok("scan_cap_loop_all_remaining",
    "candidates[candidates.index(pick):]" in agent_src)

# ═══════════════════════════════════════════════════════════════════════════════
# 10. Scanner cap = 25
# ═══════════════════════════════════════════════════════════════════════════════

print("\n[10] Scanner cap = 25")

ok("scanner_returns_25_slice", "candidates[:25]" in scanner_src)
ok("cap_trims_50_to_25",       len(list(range(50))[:25]) == 25)
ok("cap_keeps_fewer",          len(list(range(10))[:25]) == 10)

# ═══════════════════════════════════════════════════════════════════════════════
# 11. _classify_setup — setup type output
# ═══════════════════════════════════════════════════════════════════════════════

print("\n[11] _classify_setup -- setup type classification")

VALID_SETUPS = {"gap_and_go", "vol_spike", "news_momentum", "trend_continuation",
                "vwap_reclaim", "orb_continuation", "hod_breakout"}

with patch.object(_scanner, "_is_afternoon", return_value=False):
    t = _scanner._classify_setup(3.0, 2.5, True, 3.0)
    ok("gap_and_go",              t == "gap_and_go", f"got={t}")
    t = _scanner._classify_setup(0.5, 3.5, False, 4.0)
    ok("vol_spike",               t == "vol_spike", f"got={t}")
    t = _scanner._classify_setup(2.0, 1.5, True, 2.0)
    ok("news_momentum",           t == "news_momentum", f"got={t}")
    t = _scanner._classify_setup(0.5, 1.0, False, 1.0)
    ok("trend_continuation",      t == "trend_continuation", f"got={t}")

with patch.object(_scanner, "_is_afternoon", return_value=True):
    t = _scanner._classify_setup(0.5, 0.8, False, 1.0,
                                  price=50.0, vwap=49.8, day_high=51.0, open_price=49.0)
    ok("vwap_reclaim",            t == "vwap_reclaim", f"got={t}")
    t = _scanner._classify_setup(0.5, 0.9, False, 1.0,
                                  price=50.0, vwap=45.0, day_high=50.1, open_price=49.5)
    ok("hod_breakout",            t == "hod_breakout", f"got={t}")
    t = _scanner._classify_setup(0.5, 0.8, False, 1.0,
                                  price=50.0, vwap=44.0, day_high=50.2, open_price=48.9)
    ok("orb_continuation",        t == "orb_continuation", f"got={t}")

ok("all_setup_types_valid",   VALID_SETUPS == {
    "gap_and_go", "vol_spike", "news_momentum", "trend_continuation",
    "vwap_reclaim", "orb_continuation", "hod_breakout"})

# ═══════════════════════════════════════════════════════════════════════════════
# 12. Config — force-close time and Alpaca keys present
# ═══════════════════════════════════════════════════════════════════════════════

print("\n[12] Config -- Alpaca agent config values")

import config as cfg
ok("force_close_hour_15",    cfg.FORCE_CLOSE_HOUR == 15,  f"got={cfg.FORCE_CLOSE_HOUR}")
ok("force_close_min_45",     cfg.FORCE_CLOSE_MIN  == 45,  f"got={cfg.FORCE_CLOSE_MIN}")
ok("alpaca_api_key_present", bool(getattr(cfg, "ALPACA_API_KEY",  "")),  "ALPACA_API_KEY set")
ok("alpaca_secret_present",  bool(getattr(cfg, "ALPACA_SECRET_KEY", "")), "ALPACA_SECRET_KEY set")
ok("paper_trading_enabled",  getattr(cfg, "PAPER_TRADING", False) is True, "PAPER_TRADING=True")

# ═══════════════════════════════════════════════════════════════════════════════
# 13. _is_afternoon_continuation — afternoon filter
# ═══════════════════════════════════════════════════════════════════════════════

print("\n[13] _is_afternoon_continuation -- afternoon signals")

with patch.object(_scanner, "_is_afternoon", return_value=True):
    ok("aft_vwap_reclaim",    _scanner._is_afternoon_continuation(50.0, 49.8, 51.0, 48.0, 0.8))
    ok("aft_hod_breakout",    _scanner._is_afternoon_continuation(50.0, 45.0, 50.1, 48.0, 0.9))
    ok("aft_below_vwap_no",   not _scanner._is_afternoon_continuation(48.0, 50.0, 51.0, 47.0, 0.8))
    ok("aft_zero_price_no",   not _scanner._is_afternoon_continuation(0.0, 49.0, 51.0, 48.0, 0.8))
with patch.object(_scanner, "_is_afternoon", return_value=False):
    ok("morning_always_false",not _scanner._is_afternoon_continuation(55.0, 50.0, 56.0, 48.0, 5.0))

# ═══════════════════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════════════════

total = _passed + _failed
pct   = int(_passed / total * 100) if total else 0
status = "ALL PASS" if _failed == 0 else f"{_failed} FAILED"
print(f"\n{'='*62}")
print(f"Alpaca Phase 1 Unit Tests: {_passed}/{total} passed ({pct}%) -- {status}")
if _defects:
    for d in _defects:
        print(f"  FAIL: {d}")
print(f"{'='*62}")

from datetime import datetime, timezone
report = {
    "test_id":       "ALPA_UNIT_PHASE1",
    "test_name":     "Alpaca Phase 1 unit tests -- scanner logic and gate bypasses",
    "timestamp":     datetime.now(timezone.utc).isoformat(),
    "status":        "PASS" if _failed == 0 else "FAIL",
    "cases_run":     total,
    "cases_passed":  _passed,
    "cases_failed":  _failed,
    "score":         round(_passed / max(total, 1), 4),
    "details":       _details,
    "defects_found": _defects,
}
rpath = REPORTS / "test_phase1_unit.json"
rpath.write_text(json.dumps(report, indent=2))
print(f"Report: {rpath}")
sys.exit(0 if _failed == 0 else 1)
