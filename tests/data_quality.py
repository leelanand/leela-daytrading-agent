"""
Data Quality Tests — Alpaca Agent
Validates feed health, candidate field completeness, numeric bounds,
cross-field consistency, cache freshness, and historical drift.

Run: python tests/data_quality.py
Exit: 0 = all pass/warn, 1 = hard failures found
"""
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

AGENT_DIR = Path(r"C:\Users\leela\leela-daytrading-agent")
IBKR_DIR  = Path(r"C:\Users\leela\leela-ibkr-agent")
REPORTS   = AGENT_DIR / "tests" / "reports"
REPORTS.mkdir(parents=True, exist_ok=True)

now       = datetime.now()
today_str = now.strftime("%Y-%m-%d")
is_weekend = now.weekday() >= 5
# Market hours: 09:30–16:00 ET (UTC-4 summer / UTC-5 winter)
et_offset  = timedelta(hours=4)   # rough BST→ET offset (adjust if needed)
et_hour    = (now - et_offset).hour
in_market_hours = (not is_weekend) and (9 <= et_hour < 16)

passed = failed = warned = 0
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

def warn(name: str, detail: str = ""):
    global warned
    warned += 1
    details.append({"case": name, "status": "WARN", "detail": detail})
    print(f"  [WARN] {name}" + (f" -- {detail}" if detail else ""))

def check(name: str, cond: bool, detail: str = "", warn_only: bool = False):
    if cond:
        ok(name, detail)
    elif warn_only:
        warn(name, detail)
    else:
        fail(name, detail)

def _load(path: Path) -> dict | list | None:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except Exception as e:
        return None

def _age_mins(path: Path) -> float:
    try:
        return (now - datetime.fromtimestamp(path.stat().st_mtime)).total_seconds() / 60
    except Exception:
        return 9999.0


# ── DQ-01: Feed Health ────────────────────────────────────────────────────────

print("\n[DQ-01] Feed Health — feed_health.json")

fh = _load(AGENT_DIR / "feed_health.json")
if fh is None:
    warn("feed_health_file_exists", "feed_health.json missing — not yet generated (ok before first run)")
else:
    age = _age_mins(AGENT_DIR / "feed_health.json")
    check("feed_health_fresh",      age < 10,     f"age={age:.1f}min (max 10)", warn_only=not in_market_hours)
    check("feed_health_not_critical", fh.get("status") != "critical", f"status={fh.get('status')}")
    check("feed_not_blocking",      not fh.get("block_live_trading", False),
          f"block_live_trading={fh.get('block_live_trading')}", warn_only=True)

    qa = fh.get("quote_age_secs", 9999)
    SLA = 30
    check("quote_age_sla",          qa <= SLA,    f"quote_age={qa}s (SLA={SLA}s)",
          warn_only=not in_market_hours)

    alpaca_ok = fh.get("alpaca", "ok") == "ok"
    check("alpaca_feed_ok",         alpaca_ok,    f"alpaca={fh.get('alpaca')}", warn_only=True)

    issues = fh.get("issues", [])
    check("no_feed_issues",         len(issues) == 0,
          f"{len(issues)} issue(s): {issues[:2]}", warn_only=True)


# ── DQ-02: Candidate Field Completeness ──────────────────────────────────────

print("\n[DQ-02] Candidate Field Completeness")

REQUIRED_FIELDS = ["symbol", "price", "gap_pct", "rel_volume", "spread_pct",
                   "today_volume", "prev_close", "score"]
NUMERIC_FIELDS  = ["price", "gap_pct", "rel_volume", "spread_pct", "today_volume", "prev_close", "score"]

cand_raw = _load(AGENT_DIR / "candidates.json")
if cand_raw is None:
    warn("candidates_file_exists", "candidates.json missing — ok if no scan has run yet")
    candidates = []
elif isinstance(cand_raw, dict):
    candidates = cand_raw.get("candidates", [])
else:
    candidates = cand_raw if isinstance(cand_raw, list) else []

check("candidates_file_exists",    cand_raw is not None, warn_only=True)

if candidates:
    age_c = _age_mins(AGENT_DIR / "candidates.json")
    check("candidates_cache_fresh", age_c < 60, f"age={age_c:.1f}min", warn_only=not in_market_hours)

    missing_fields_any = False
    null_fields_any    = False
    for i, c in enumerate(candidates):
        for f in REQUIRED_FIELDS:
            if f not in c:
                fail(f"candidate_has_{f}", f"candidate[{i}] ({c.get('symbol','?')}) missing field '{f}'")
                missing_fields_any = True
            elif f in NUMERIC_FIELDS and c[f] is None:
                warn(f"candidate_{f}_not_null", f"candidate[{i}] ({c.get('symbol','?')}) field '{f}' is null")
                null_fields_any = True
    if not missing_fields_any:
        ok("all_candidates_have_required_fields", f"{len(candidates)} candidates checked")
    if not null_fields_any:
        ok("no_null_numeric_fields", f"{len(candidates)} candidates checked")
else:
    warn("candidates_not_empty", "no candidates — ok outside market hours")


# ── DQ-03: Numeric Bounds / Price Sanity ─────────────────────────────────────

print("\n[DQ-03] Numeric Bounds & Price Sanity")

if candidates:
    price_ok   = all(c.get("price", 0) > 0           for c in candidates)
    prev_ok    = all(c.get("prev_close", 0) > 0       for c in candidates)
    gap_ok     = all(-50 < c.get("gap_pct", 0) < 200  for c in candidates)
    rvol_ok    = all(c.get("rel_volume", 0) >= 0       for c in candidates)
    spread_ok  = all(0 <= c.get("spread_pct", 0) < 10  for c in candidates)
    vol_ok     = all(c.get("today_volume", 0) >= 0     for c in candidates)
    score_ok   = all(0 <= c.get("score", 50) <= 100    for c in candidates)

    check("price_positive",         price_ok,   f"{len(candidates)} candidates")
    check("prev_close_positive",    prev_ok,    f"{len(candidates)} candidates")
    check("gap_pct_in_range",       gap_ok,     "gap_pct in (-50, 200)")
    check("rel_volume_non_negative",rvol_ok,    "rel_volume >= 0")
    check("spread_pct_sane",        spread_ok,  "spread_pct in [0, 10)")
    check("today_volume_non_neg",   vol_ok,     "today_volume >= 0")
    check("score_in_range",         score_ok,   "score in [0, 100]")

    # Bid/ask sanity where present
    has_bidask = [c for c in candidates if c.get("bid") and c.get("ask")]
    if has_bidask:
        bidask_ok = all(c["bid"] < c["ask"] for c in has_bidask)
        check("bid_lt_ask",         bidask_ok,  f"{len(has_bidask)} candidates with bid/ask")
    else:
        warn("bid_ask_present",     "bid/ask not populated in candidates — ok in paper mode")
else:
    warn("numeric_bounds_skipped",  "no candidates to check")


# ── DQ-04: Cross-Field Consistency ───────────────────────────────────────────

print("\n[DQ-04] Cross-Field Consistency")

if candidates:
    inconsistent_gap = []
    inconsistent_spread = []
    for c in candidates:
        price, prev, gap = c.get("price", 0), c.get("prev_close", 0), c.get("gap_pct", 0)
        if price > 0 and prev > 0:
            implied_gap = (price / prev - 1) * 100
            if abs(implied_gap - gap) > 3.0:   # allow 3% tolerance (intraday drift)
                inconsistent_gap.append(c.get("symbol"))

        bid, ask, spread = c.get("bid", 0), c.get("ask", 0), c.get("spread_pct", 0)
        if bid and ask and price > 0:
            implied_spread = (ask - bid) / price * 100
            if abs(implied_spread - spread) > 0.5:
                inconsistent_spread.append(c.get("symbol"))

    check("gap_pct_consistent_with_price",
          len(inconsistent_gap) == 0,
          f"{len(inconsistent_gap)} inconsistent: {inconsistent_gap[:3]}", warn_only=True)
    check("spread_pct_consistent_with_bidask",
          len(inconsistent_spread) == 0,
          f"{len(inconsistent_spread)} inconsistent: {inconsistent_spread[:3]}", warn_only=True)

    # Score sub-component sum check (total should be ≈ sum of components)
    bad_score_sum = []
    for c in candidates:
        sub = sum(c.get(f, 0) or 0 for f in
                  ["momentum_score","volume_score","news_score","market_trend_score","volatility_score","liquidity_score"])
        total = c.get("score", 0) or 0
        if sub > 0 and total > 0 and abs(sub - total) > 5:
            bad_score_sum.append(f"{c.get('symbol')}(sub={sub:.0f},total={total:.0f})")
    check("score_components_sum_to_total",
          len(bad_score_sum) == 0,
          f"{len(bad_score_sum)} mismatch: {bad_score_sum[:2]}", warn_only=True)
else:
    warn("cross_field_skipped", "no candidates to check")


# ── DQ-05: Gapper Cache Integrity ────────────────────────────────────────────

print("\n[DQ-05] Gapper Cache Integrity")

gf = _load(AGENT_DIR / "gappers_today.json")
if gf is None:
    warn("gapper_cache_exists", "gappers_today.json missing — ok if prescan hasn't run")
else:
    gdate = gf.get("date", "")
    check("gapper_cache_date_matches_today", gdate == today_str,
          f"cache_date={gdate} today={today_str}", warn_only=is_weekend)

    symbols = gf.get("symbols", [])
    check("gapper_cache_not_empty",   len(symbols) > 0,
          f"{len(symbols)} symbols", warn_only=not in_market_hours)

    details_raw = gf.get("details", [])
    detail_list = details_raw if isinstance(details_raw, list) else list(details_raw.values())
    if detail_list:
        # gappers_today details use vol_ratio (not rel_volume) and gap_pct + price
        detail_fields = ["price", "gap_pct"]
        bad = [d.get("symbol", f"entry[{i}]") for i, d in enumerate(detail_list[:10])
               if isinstance(d, dict) and any(d.get(f) is None for f in detail_fields)]
        check("gapper_details_have_required_fields",
              len(bad) == 0, f"missing fields in: {bad[:3]}", warn_only=True)

    age_g = _age_mins(AGENT_DIR / "gappers_today.json")
    check("gapper_cache_fresh",       age_g < 90,
          f"age={age_g:.1f}min (max 90)", warn_only=not in_market_hours)


# ── DQ-06: Research Cache Freshness ──────────────────────────────────────────

print("\n[DQ-06] Research Cache Freshness & Completeness")

rc = _load(AGENT_DIR / "research_cache.json")
if rc is None:
    warn("research_cache_exists", "research_cache.json missing — ok before first run")
else:
    MAX_RESEARCH_AGE_HOURS = 8
    age_r = _age_mins(AGENT_DIR / "research_cache.json") / 60  # hours
    check("research_cache_fresh",
          age_r < MAX_RESEARCH_AGE_HOURS,
          f"age={age_r:.1f}h (max {MAX_RESEARCH_AGE_HOURS}h)", warn_only=is_weekend)

    check("research_has_symbols",    bool(rc.get("symbols")),    "symbols section present")
    check("research_has_macro",      bool(rc.get("macro")),      "macro section present")
    check("research_has_timestamp",  bool(rc.get("generated_at")), "generated_at present")

    sym_count = len(rc.get("symbols", {}))
    check("research_symbols_non_empty", sym_count > 0,
          f"{sym_count} symbols researched", warn_only=not in_market_hours)


# ── DQ-07: Audit Log Field Integrity ─────────────────────────────────────────

print("\n[DQ-07] Audit Log Field Integrity")

REQUIRED_AUDIT_FIELDS = ["date", "action", "symbol"]
KNOWN_ACTION_PREFIXES = {
    "SCAN_COMPLETE", "TRADE_PLACED", "TRADE_REJECTED", "ORDER_PLACED",
    "ORDER_FILLED", "EXIT_TRIGGERED", "FORCE_CLOSE", "SCORE_COMPUTED",
    "REGIME_CHANGE", "RISK_BLOCKED", "STALE_QUOTE_REJECT", "QUOTE_REFRESHED",
    "SESSION_START", "SESSION_END", "MARKET_CLOSED", "PDT_LIMIT",
    "LOSS_LIMIT_HIT", "LOG_AUDIT", "PRESCAN_COMPLETE", "RESEARCH_COMPLETE",
    "PRESCAN_START", "POWER_HOUR_GATE_REJECT", "MIDDAY_BLOCK",
    "EOD_VERIFY", "FORCE_CLOSE_SENT", "CANDIDATE_SCORED", "WATCHLIST_ADD",
    "DAILY_SUMMARY", "PRESCAN_SKIP", "SCAN_SKIP", "BYPASS_TRIGGERED",
}

try:
    db_path = AGENT_DIR / "daytrades.db"
    if not db_path.exists():
        warn("audit_db_exists", "daytrades.db not found — ok on first run")
    else:
        conn = sqlite3.connect(str(db_path))
        cur  = conn.cursor()
        # Check audit_log table
        tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "audit_log" in tables:
            rows = cur.execute(
                "SELECT date, action, symbol, details FROM audit_log ORDER BY rowid DESC LIMIT 50"
            ).fetchall()
            if rows:
                missing_fields = [r for r in rows if not r[0] or not r[1] or r[2] is None]
                check("audit_log_fields_present",
                      len(missing_fields) == 0,
                      f"{len(missing_fields)}/{len(rows)} entries missing required fields")

                unknown_actions = [r[1] for r in rows
                                   if not any(r[1] == p or r[1].startswith(p + "_") or r[1].startswith(p)
                                              for p in KNOWN_ACTION_PREFIXES)]
                check("audit_log_known_actions",
                      len(unknown_actions) == 0,
                      f"{len(unknown_actions)} unknown: {list(set(unknown_actions))[:3]}",
                      warn_only=True)

                ok("audit_log_has_recent_entries", f"{len(rows)} recent entries found")
            else:
                warn("audit_log_has_entries", "audit_log empty — ok on first run")
        else:
            warn("audit_log_table_exists", "audit_log table missing from DB — ok on first run")

        # Check trades table schema
        if "trades" in tables:
            cols = {r[1] for r in cur.execute("PRAGMA table_info(trades)").fetchall()}
            required_cols = {"symbol", "entry", "ts"}
            missing_cols  = required_cols - cols
            check("trades_table_schema",
                  len(missing_cols) == 0,
                  f"missing columns: {missing_cols}")
        conn.close()
except Exception as e:
    warn("audit_db_check", f"DB check error: {e}")


# ── DQ-08: Historical Score / Gap Distribution Drift ─────────────────────────

print("\n[DQ-08] Historical Data Distribution Baseline")

try:
    db_path = AGENT_DIR / "daytrades.db"
    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        cur  = conn.cursor()
        tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

        if "trades" in tables:
            rows = cur.execute(
                "SELECT pnl, pnl_pct FROM trades WHERE pnl IS NOT NULL ORDER BY rowid DESC LIMIT 100"
            ).fetchall()
            if len(rows) >= 5:
                pnl_pcts = [r[1] for r in rows if r[1] is not None]
                avg_pnl  = sum(pnl_pcts) / len(pnl_pcts)
                wins     = sum(1 for p in pnl_pcts if p > 0)
                win_rate = wins / len(pnl_pcts)

                check("win_rate_not_zero",       win_rate > 0,
                      f"win_rate={win_rate:.1%} over {len(pnl_pcts)} trades", warn_only=True)
                check("avg_pnl_not_extreme",     -0.20 < avg_pnl < 0.20,
                      f"avg_pnl_pct={avg_pnl:.3f}", warn_only=True)
                ok("historical_trade_sample", f"{len(pnl_pcts)} trades analysed")
            else:
                warn("historical_trade_sample", f"only {len(rows)} trades — need 5+ for drift check")

        conn.close()
    else:
        warn("historical_db", "daytrades.db missing")
except Exception as e:
    warn("historical_check", f"error: {e}")


# ── DQ-09: Schema Parity Between Agents ──────────────────────────────────────

print("\n[DQ-09] Cross-Agent Candidate Schema Parity")

ibkr_cand_raw = _load(IBKR_DIR / "candidates.json")
if ibkr_cand_raw is not None:
    ibkr_cands = ibkr_cand_raw.get("candidates", ibkr_cand_raw) if isinstance(ibkr_cand_raw, dict) else ibkr_cand_raw
    if candidates and ibkr_cands:
        alpa_keys = set(candidates[0].keys())
        ibkr_keys = set(ibkr_cands[0].keys())
        core = {"symbol", "price", "gap_pct", "rel_volume", "spread_pct", "score"}
        alpa_missing_core = core - alpa_keys
        ibkr_missing_core = core - ibkr_keys
        check("alpaca_has_core_fields", len(alpa_missing_core) == 0,
              f"missing: {alpa_missing_core}")
        check("ibkr_has_core_fields",   len(ibkr_missing_core) == 0,
              f"missing: {ibkr_missing_core}")
        extra_alpa = alpa_keys - ibkr_keys - {"_normalize_alpaca"}
        extra_ibkr = ibkr_keys - alpa_keys
        if extra_alpa or extra_ibkr:
            warn("schema_parity",
                 f"alpaca-only={sorted(extra_alpa)[:5]}  ibkr-only={sorted(extra_ibkr)[:5]}")
        else:
            ok("schema_parity", "both agents have identical candidate schemas")
    else:
        warn("schema_parity_skipped", "one or both agents have no candidates yet")
else:
    warn("ibkr_candidates_accessible", "IBKR candidates.json not found")


# ── DQ-10: Feed Health Source Code Completeness ───────────────────────────────

print("\n[DQ-10] Feed Validation in Source Code")

scanner_src = (AGENT_DIR / "scanner.py").read_text(encoding="utf-8")
risk_src    = (AGENT_DIR / "risk.py").read_text(encoding="utf-8")
agent_src   = (AGENT_DIR / "agent.py").read_text(encoding="utf-8")

config_src = (AGENT_DIR / "config.py").read_text(encoding="utf-8")
feed_src   = (AGENT_DIR / "feed_logger.py").read_text(encoding="utf-8") if (AGENT_DIR / "feed_logger.py").exists() else ""
check("feed_health_written_in_scanner",  "feed_health" in scanner_src or "feed_health" in agent_src,
      "feed_health.json written by scanner or agent")
check("quote_age_tracked",               "STALE_QUOTE" in agent_src or "FEED_HEALTH_STALE_QUOTE_SECS" in config_src or "quote_age" in feed_src,
      "quote staleness tracked (STALE_QUOTE / FEED_HEALTH_STALE_QUOTE_SECS)")
check("spread_validated_in_risk",        "spread_pct" in risk_src,
      "spread_pct checked in risk.py")
check("rel_volume_gated",                "MIN_REL_VOLUME" in scanner_src or "MIN_REL_VOLUME" in agent_src or "rel_volume" in scanner_src,
      "rel_volume gate in scanner or agent")
check("price_validated_in_risk",         "price" in risk_src and ("price <= 0" in risk_src or "price > 0" in risk_src or "price <" in risk_src),
      "price > 0 check in risk.py")
check("gap_pct_validated_in_scanner",    "gap_pct" in scanner_src or "MIN_GAP_PCT" in scanner_src,
      "gap_pct filtered in scanner.py")
check("data_src_tagged_in_candidate",    "_data_src" in scanner_src,
      "_data_src provenance tag in scanner.py")


# ── Summary ───────────────────────────────────────────────────────────────────

total  = passed + failed + warned
status = "ALL_PASS" if failed == 0 else "FAILURES"

print(f"\n{'='*60}")
print(f"Data Quality Tests (Alpaca): {passed} pass, {warned} warn, {failed} fail -- {status}")
print(f"{'='*60}")

report = {
    "agent":     "alpaca",
    "timestamp": now.isoformat(),
    "passed":    passed,
    "warned":    warned,
    "failed":    failed,
    "total":     total,
    "status":    status,
    "in_market_hours": in_market_hours,
    "details":   details,
}
(REPORTS / "data_quality.json").write_text(json.dumps(report, indent=2))
print(f"Report: {REPORTS / 'data_quality.json'}")
sys.exit(0 if failed == 0 else 1)
