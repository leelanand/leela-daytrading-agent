"""
Business Process Monitoring (BPM) — end-to-end operational observability
for the Leela live trading platform (Alpaca + IBKR agents).

Exposes collect_bpm() → dict, called by dashboard.py /api/bpm every 60s.
Each section is independent and fails gracefully.
"""
from __future__ import annotations
import json
import re
import sqlite3
import time
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET  = ZoneInfo("America/New_York")

ALPACA_DIR = Path(__file__).parent
IBKR_DIR   = Path(r"C:\Users\leela\leela-ibkr-agent")

_LOG_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+(\S+)\s*(\S*)\s*(.*)")

# ── Cache (missed opportunities is expensive) ─────────────────────────────────
_bpm_cache: dict   = {}
_bpm_cache_ts: float = 0.0
_BPM_TTL = 55  # seconds — slightly under dashboard 60s poll interval


# ── Helpers ───────────────────────────────────────────────────────────────────

def _today() -> str:
    return date.today().isoformat()


def _now_et() -> datetime:
    return datetime.now(ET)


def _mins_ago(ts_str: str) -> float:
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60
    except Exception:
        return 9999.0


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}


def _read_audit(directory: Path) -> list[dict]:
    today = _today()
    entries = []
    try:
        for line in (directory / "audit.log").read_text(encoding="utf-8", errors="replace").splitlines():
            if today not in line:
                continue
            m = _LOG_RE.match(line)
            if m:
                entries.append({
                    "ts":     m.group(1),
                    "action": m.group(2),
                    "symbol": m.group(3).strip(),
                    "detail": m.group(4)[:500],
                    "raw":    line,
                })
    except Exception:
        pass
    return entries


def _parse_detail(entry: dict) -> dict:
    try:
        raw = entry.get("raw", "")
        idx = raw.index("{")
        return json.loads(raw[idx:])
    except Exception:
        try:
            return json.loads(entry.get("detail", "{}") or "{}")
        except Exception:
            return {}


def _db_query(db_path: Path, sql: str, params: tuple = ()) -> list:
    try:
        if not db_path.exists():
            return []
        con = sqlite3.connect(db_path)
        rows = con.execute(sql, params).fetchall()
        con.close()
        return rows
    except Exception:
        return []


def _combined_audit() -> tuple[list[dict], list[dict]]:
    """Returns (alpaca_audit, ibkr_audit) for today."""
    return _read_audit(ALPACA_DIR), _read_audit(IBKR_DIR)


# ── 1. Research Pipeline ──────────────────────────────────────────────────────

def _research_pipeline() -> dict:
    today  = _today()
    issues = []
    rc = {}
    for d in (ALPACA_DIR, IBKR_DIR):
        data = _read_json(d / "research_cache.json")
        if data.get("generated_at", "")[:10] == today:
            rc = data
            break

    if not rc:
        return {
            "status": "missing", "ran_today": False,
            "generated_at": None, "cache_age_mins": None,
            "symbols_researched": 0, "bullish": 0, "bearish": 0, "avoid": 0,
            "score_distribution": {}, "macro": {}, "research_consumed": False,
            "issues": ["WARNING: No research cache for today"],
        }

    gen_at   = rc.get("generated_at", "")
    age_mins = _mins_ago(gen_at) if gen_at else None
    symbols  = rc.get("symbols", {})

    bullish = bearish = avoid = 0
    scores  = []
    for info in symbols.values():
        bias = (info.get("pre_market_bias") or "").lower()
        if "bullish" in bias:   bullish += 1
        elif "bearish" in bias: bearish += 1
        elif "avoid" in bias:   avoid   += 1
        s = info.get("research_score")
        if s is not None:
            scores.append(int(s))

    dist = {}
    if scores:
        dist = {
            ">=80": sum(1 for s in scores if s >= 80),
            "60-79": sum(1 for s in scores if 60 <= s < 80),
            "40-59": sum(1 for s in scores if 40 <= s < 60),
            "<40":   sum(1 for s in scores if s < 40),
            "avg":   round(sum(scores) / len(scores), 1),
        }

    if age_mins and age_mins > 300:
        issues.append(f"WARNING: Research cache is {age_mins:.0f} min old")

    # Was research consumed? Check if any prescan ran after research was written
    a_audit, i_audit = _combined_audit()
    prescan_done = any(e["action"] == "PRESCAN_DONE" for e in a_audit + i_audit)

    return {
        "status":            "ok" if not issues else "degraded",
        "ran_today":         True,
        "generated_at":      gen_at[:19] if gen_at else None,
        "cache_age_mins":    round(age_mins, 1) if age_mins is not None else None,
        "symbols_researched": len(symbols),
        "bullish":           bullish,
        "bearish":           bearish,
        "avoid":             avoid,
        "score_distribution": dist,
        "macro":             rc.get("macro", {}),
        "research_consumed": prescan_done,
        "issues":            issues,
    }


# ── 2. Scan Pipeline ─────────────────────────────────────────────────────────

def _scan_pipeline() -> dict:
    issues  = []
    now_et  = _now_et()
    mins_et = now_et.hour * 60 + now_et.minute
    in_session = 9 * 60 + 45 <= mins_et < 15 * 60 + 30 and now_et.weekday() < 5
    midday     = 12 * 60 <= mins_et < 13 * 60

    a_audit, i_audit = _combined_audit()
    all_audit = a_audit + i_audit

    scans  = [e for e in all_audit if e["action"] in ("SCAN_DONE", "CONTINUOUS_SCAN", "SCAN_START")]
    skips  = [e for e in all_audit if e["action"] == "SCAN_SKIPPED"]
    prescan_done    = any(e["action"] == "PRESCAN_DONE"    for e in all_audit)
    prescan_skipped = any(e["action"] == "PRESCAN_SKIPPED" for e in all_audit)

    last_scan_ts  = scans[-1]["ts"] if scans else None
    last_scan_age = _mins_ago(last_scan_ts) if last_scan_ts else None

    if in_session and not midday and last_scan_age is not None and last_scan_age > 10:
        issues.append(f"WARNING: Last scan was {last_scan_age:.1f} min ago (expected ≤5 min)")
    if in_session and not midday and not scans:
        issues.append("WARNING: No scans recorded during active session")

    # Pool size
    pool = tradeable = watchlist = 0
    for d in (ALPACA_DIR, IBKR_DIR):
        try:
            raw   = json.loads((d / "candidates.json").read_text())
            cands = raw.get("candidates", raw) if isinstance(raw, dict) else raw
            if not isinstance(cands, list):
                continue
            pool      = len(cands)
            tradeable = sum(1 for c in cands if isinstance(c, dict) and c.get("tradeable"))
            watchlist = sum(1 for c in cands if isinstance(c, dict) and c.get("watchlist"))
            break
        except Exception:
            pass

    # Skip reasons
    skip_reasons = []
    for e in skips[-5:]:
        det = _parse_detail(e)
        skip_reasons.append(det.get("reason", det.get("regime", ""))[:60])

    return {
        "status":              "ok" if not issues else "degraded",
        "prescan_done":        prescan_done,
        "prescan_skipped":     prescan_skipped,
        "total_scans":         len(scans),
        "skipped_scans":       len(skips),
        "last_scan_at":        last_scan_ts[:19] if last_scan_ts else None,
        "last_scan_age_mins":  round(last_scan_age, 1) if last_scan_age is not None else None,
        "candidates_in_pool":  pool,
        "tradeable_count":     tradeable,
        "watchlist_count":     watchlist,
        "scan_timeline":       [{"ts": s["ts"][11:16]} for s in scans[-10:]],
        "skip_reasons":        skip_reasons,
        "issues":              issues,
    }


# ── 3. Research + Scan Integration ───────────────────────────────────────────

def _integration() -> dict:
    today  = _today()
    issues = []

    research_syms: set[str] = set()
    for d in (ALPACA_DIR, IBKR_DIR):
        data = _read_json(d / "research_cache.json")
        if data.get("generated_at", "")[:10] == today:
            research_syms = set(data.get("symbols", {}).keys())
            break

    candidates: list[dict] = []
    for d in (ALPACA_DIR, IBKR_DIR):
        try:
            raw   = json.loads((d / "candidates.json").read_text())
            cands = raw.get("candidates", raw) if isinstance(raw, dict) else raw
            if isinstance(cands, list) and cands:
                candidates = [c for c in cands if isinstance(c, dict)]
                break
        except Exception:
            pass

    if not candidates:
        return {"status": "unknown", "candidates_with_research": 0,
                "candidates_without_research": 0, "coverage_pct": 0,
                "missing_research": [], "issues": ["No candidates loaded"]}

    with_res    = sum(1 for c in candidates if c.get("symbol", "") in research_syms)
    without_res = len(candidates) - with_res
    missing     = [c["symbol"] for c in candidates if c.get("symbol", "") not in research_syms]

    if missing:
        issues.append(f"WARNING: {len(missing)} candidate(s) without research: {', '.join(missing[:5])}")

    return {
        "status":                       "ok" if not issues else "degraded",
        "candidates_with_research":     with_res,
        "candidates_without_research":  without_res,
        "coverage_pct":                 round(with_res / len(candidates) * 100) if candidates else 0,
        "missing_research":             missing[:8],
        "issues":                       issues,
    }


# ── 4. Scoring Engine Health ──────────────────────────────────────────────────

def _scoring_engine() -> dict:
    today  = _today()
    issues = []

    claude_total = cache_hits = local_rejects = to_claude = changed = 0
    for d in (ALPACA_DIR, IBKR_DIR):
        f = d / "claude_effectiveness.jsonl"
        if not f.exists():
            continue
        try:
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get("date") != today:
                        continue
                    claude_total += 1
                    if rec.get("cache_hit"):        cache_hits += 1
                    if rec.get("local_only"):       local_rejects += 1
                    if not rec.get("cache_hit") and not rec.get("local_only"):
                        to_claude += 1
                    if rec.get("claude_changed_decision"):
                        changed += 1
                except Exception:
                    pass
        except Exception:
            pass

    rejection_reasons: dict[str, int] = {}
    a_audit, i_audit = _combined_audit()
    for e in a_audit + i_audit:
        if e["action"] == "TRADE_REJECTED":
            det  = _parse_detail(e)
            rule = det.get("rule_failed") or det.get("reason") or "unknown"
            rule = rule[:50]
            rejection_reasons[rule] = rejection_reasons.get(rule, 0) + 1

    eligible   = max(claude_total - local_rejects, 1)
    cache_rate = round(cache_hits / eligible * 100) if claude_total else 0
    change_rate = round(changed / max(to_claude, 1) * 100) if to_claude else 0

    if claude_total == 0 and _scan_pipeline().get("total_scans", 0) > 5:
        issues.append("WARNING: No scoring activity after 5+ scan cycles")

    return {
        "status":                   "ok" if not issues else "degraded",
        "candidates_seen":          claude_total,
        "sent_to_claude":           to_claude,
        "cache_hits":               cache_hits,
        "local_rejects":            local_rejects,
        "cache_hit_rate_pct":       cache_rate,
        "decision_change_rate_pct": change_rate,
        "top_rejection_reasons":    dict(sorted(rejection_reasons.items(), key=lambda x: -x[1])[:8]),
        "issues":                   issues,
    }


# ── 5. Missed Opportunity Report ──────────────────────────────────────────────

def _missed_opportunities() -> dict:
    now_et = _now_et()
    mins   = now_et.hour * 60 + now_et.minute
    if mins < 10 * 60 + 30:
        return {"status": "too_early", "message": "Available after 10:30 ET", "movers": [],
                "missed_count": 0, "rejected_missed": 0}

    today = _today()

    scanned_syms: set[str]  = set()
    scored_syms: set[str]   = set()
    traded_syms: set[str]   = set()
    rejection_map: dict[str, str] = {}

    _TICKER_RE = re.compile(r'^[A-Z]{1,5}$')

    a_audit, i_audit = _combined_audit()
    for e in a_audit + i_audit:
        sym = e.get("symbol", "").strip()
        if not sym or not _TICKER_RE.match(sym):
            continue
        if e["action"] in ("SCAN_DONE", "PRESCAN_DONE", "TRADE_REJECTED",
                            "SHORTLISTED", "ORDER_PLACED", "NO_ENTRY"):
            scanned_syms.add(sym)
        if e["action"] == "TRADE_REJECTED":
            det = _parse_detail(e)
            rejection_map[sym] = (det.get("rule_failed") or det.get("reason") or "rejected")[:50]
        if e["action"] == "ORDER_PLACED":
            traded_syms.add(sym)

    for d in (ALPACA_DIR, IBKR_DIR):
        try:
            raw   = json.loads((d / "candidates.json").read_text())
            cands = raw.get("candidates", raw) if isinstance(raw, dict) else raw
            for c in (cands if isinstance(cands, list) else []):
                if not isinstance(c, dict):
                    continue
                s = c.get("symbol", "")
                if s:
                    scanned_syms.add(s)
                    scored_syms.add(s)
        except Exception:
            pass

    # Gappers
    gapper_syms: list[str] = []
    for d in (ALPACA_DIR, IBKR_DIR):
        g = _read_json(d / "gappers_today.json")
        if g.get("date") == today:
            gapper_syms = [x["symbol"] for x in g.get("details", [])]
            break

    universe = list((scanned_syms | set(gapper_syms)) - traded_syms)[:25]
    movers: list[dict] = []

    if universe:
        try:
            import yfinance as yf
            tickers = yf.download(universe, period="1d", interval="5m",
                                  progress=False, threads=True, auto_adjust=True)
            closes = tickers.get("Close")
            opens  = tickers.get("Open")
            if closes is not None and not closes.empty:
                cols = closes.columns if hasattr(closes, "columns") else []
                for sym in cols:
                    try:
                        c_series = closes[sym].dropna()
                        o_series = opens[sym].dropna() if opens is not None else None
                        if len(c_series) < 2:
                            continue
                        open_px = float(o_series.iloc[0]) if o_series is not None and len(o_series) else float(c_series.iloc[0])
                        last_px = float(c_series.iloc[-1])
                        high_px = float(c_series.max())
                        if open_px <= 0:
                            continue
                        move_pct = (last_px - open_px) / open_px * 100
                        high_pct = (high_px - open_px) / open_px * 100
                        if abs(move_pct) < 2.0 and abs(high_pct) < 3.0:
                            continue
                        status = ("traded"              if sym in traded_syms    else
                                  "rejected"            if sym in rejection_map  else
                                  "scored_not_traded"   if sym in scored_syms    else
                                  "scanned_not_scored"  if sym in scanned_syms   else
                                  "missed_entirely")
                        movers.append({
                            "symbol":           sym,
                            "move_pct":         round(move_pct, 2),
                            "high_pct":         round(high_pct, 2),
                            "price":            round(last_px, 2),
                            "status":           status,
                            "rejection_reason": rejection_map.get(sym, ""),
                        })
                    except Exception:
                        pass
        except Exception:
            pass

    movers.sort(key=lambda x: -abs(x.get("high_pct", 0)))
    missed_entirely = [m for m in movers if m["status"] == "missed_entirely"]
    rejected_missed = [m for m in movers if m["status"] == "rejected"]

    return {
        "status":          "ok",
        "generated_at":    now_et.strftime("%H:%M ET"),
        "universe_checked": len(universe),
        "movers":          movers[:15],
        "missed_count":    len(missed_entirely),
        "rejected_missed": len(rejected_missed),
        "top_missed":      missed_entirely[:5],
    }


# ── 6. Execution Pipeline ────────────────────────────────────────────────────

def _execution_pipeline() -> dict:
    today  = _today()
    issues = []
    orders = fills = rejected = 0
    slippages: list[float] = []

    a_audit, i_audit = _combined_audit()
    for e in a_audit + i_audit:
        if e["action"] == "ORDER_PLACED":
            orders += 1
        elif e["action"] in ("TRADE_REJECTED", "TRADE_BLOCKED"):
            rejected += 1

    # Fills from DB
    for d in (ALPACA_DIR, IBKR_DIR):
        rows = _db_query(d / "daytrades.db",
                         "SELECT COUNT(*) FROM trades WHERE date=?", (today,))
        if rows:
            fills += rows[0][0] or 0

    # Slippage from execution_telemetry.jsonl
    for d in (ALPACA_DIR, IBKR_DIR):
        f = d / "execution_telemetry.jsonl"
        if not f.exists():
            continue
        try:
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                if today not in line:
                    continue
                try:
                    rec = json.loads(line)
                    s = rec.get("slippage")
                    if s is not None:
                        slippages.append(float(s))
                except Exception:
                    pass
        except Exception:
            pass

    now_et = _now_et()
    past_close = now_et.hour >= 16 and now_et.weekday() < 5

    force_closed   = any(e["action"] == "FORCE_CLOSE" for e in a_audit + i_audit)
    verified_flat  = any(e["action"] in ("VERIFIED", "EOD_VERIFIED") for e in a_audit + i_audit)
    cutoff_done    = any(e["action"] in ("CUTOFF_DONE", "CUTOFF") for e in a_audit + i_audit)

    if past_close and not force_closed:
        issues.append("WARNING: No FORCE_CLOSE event recorded after market close")
    if past_close and not verified_flat:
        issues.append("WARNING: No VERIFIED flatness event recorded after market close")

    avg_slip = round(sum(slippages) / len(slippages) * 100, 3) if slippages else None

    return {
        "status":        "ok" if not issues else "degraded",
        "orders_today":  orders,
        "fills_today":   fills,
        "rejected_today": rejected,
        "avg_slippage_pct": avg_slip,
        "force_closed":  force_closed,
        "verified_flat": verified_flat,
        "cutoff_done":   cutoff_done,
        "issues":        issues,
    }


# ── 7. Market Data Health ────────────────────────────────────────────────────

def _market_data() -> dict:
    issues = []
    fh  = _read_json(IBKR_DIR / "feed_health.json")
    fq  = _read_json(IBKR_DIR / "feed_quality.json")
    if not fq:
        fq = _read_json(ALPACA_DIR / "feed_quality.json")

    connected        = fh.get("connected", False)
    ibkr_status      = fh.get("status", "unknown")
    quote_age        = fh.get("quote_age_secs")
    market_data_type = fh.get("market_data_type")
    stale_events     = fq.get("stale_quote_events", 0)
    spike_events     = fq.get("price_spike_events", 0)
    mismatches       = fq.get("quote_mismatches", 0)
    validations      = max(fq.get("quote_validations", 1), 1)
    trades_rejected_data = fq.get("trades_rejected_data", 0)

    mismatch_pct = round(mismatches / validations * 100, 1)

    confidence = 100
    if not connected:
        confidence -= 50
        issues.append("CRITICAL: IB Gateway disconnected")
    if quote_age and quote_age > 60:
        confidence -= 20
        issues.append(f"WARNING: Quote age {quote_age:.0f}s — stale feed")
    if market_data_type and market_data_type != 1:
        confidence -= 10
        issues.append(f"WARNING: Data type {market_data_type} (not live real-time)")
    if stale_events > 2:
        confidence -= min(20, stale_events * 3)
    if spike_events > 0:
        confidence -= min(15, spike_events * 5)
    if mismatch_pct > 15:
        confidence -= 10
    if trades_rejected_data > 0:
        issues.append(f"WARNING: {trades_rejected_data} trade(s) rejected due to data quality")

    return {
        "status":               "ok" if not issues else ("critical" if not connected else "degraded"),
        "ibkr_connected":       connected,
        "market_data_type":     market_data_type,
        "quote_age_secs":       quote_age,
        "stale_quote_events":   stale_events,
        "price_spike_events":   spike_events,
        "mismatch_pct":         mismatch_pct,
        "trades_rejected_data": trades_rejected_data,
        "data_confidence_score": max(0, confidence),
        "checked_at":           fh.get("checked_at", ""),
        "issues":               issues,
    }


# ── 8. Regime Engine ─────────────────────────────────────────────────────────

def _regime_engine() -> dict:
    issues       = []
    current      = "UNKNOWN"
    regime_reason = ""
    metrics      = {}

    for d in (ALPACA_DIR, IBKR_DIR):
        rc = _read_json(d / "regime_cache.json")
        if rc:
            current       = rc.get("regime", "UNKNOWN")
            regime_reason = rc.get("reason", "")
            metrics       = rc.get("metrics", {})
            break

    a_audit, i_audit = _combined_audit()
    transitions: list[dict] = []
    time_in: dict[str, int] = {}
    prev_ts = prev_regime = None

    for e in sorted(a_audit, key=lambda x: x["ts"]):
        if e["action"] != "REGIME_DETECTED":
            continue
        det = _parse_detail(e)
        r   = det.get("regime", "")
        if not r or r == prev_regime:
            continue
        transitions.append({"ts": e["ts"][11:16], "regime": r, "reason": det.get("reason", "")[:60]})
        if prev_regime and prev_ts:
            try:
                dt1 = datetime.strptime(prev_ts, "%Y-%m-%d %H:%M:%S")
                dt2 = datetime.strptime(e["ts"], "%Y-%m-%d %H:%M:%S")
                mins = max(0, int((dt2 - dt1).total_seconds() / 60))
                time_in[prev_regime] = time_in.get(prev_regime, 0) + mins
            except Exception:
                pass
        prev_ts, prev_regime = e["ts"], r

    vol   = metrics.get("effective_vol_ratio") or metrics.get("spy_intraday_ratio")
    vix   = metrics.get("vix")

    if current == "NO_TRADE":
        issues.append("WARNING: NO_TRADE regime — all trading blocked")

    return {
        "status":             "ok" if not issues else "degraded",
        "current":            current,
        "reason":             regime_reason[:100],
        "effective_vol":      round(vol, 2) if vol else None,
        "vix":                round(vix, 1) if vix else None,
        "low_vol_blocking":   current == "LOW_VOLUME",
        "choppy_blocking":    current == "CHOPPY",
        "no_trade_blocking":  current == "NO_TRADE",
        "transitions_today":  len(transitions),
        "regime_history":     transitions[-6:],
        "time_in_regime":     time_in,
        "issues":             issues,
    }


# ── 9. Risk & PDT ────────────────────────────────────────────────────────────

def _risk_pdt() -> dict:
    today  = _today()
    issues = []
    trades_today = 0
    loss_limit_hit = False
    pdt_remaining  = -1  # -1 = unlimited

    for d in (ALPACA_DIR, IBKR_DIR):
        rows = _db_query(d / "daytrades.db",
                         "SELECT COUNT(*) FROM trades WHERE date=?", (today,))
        if rows:
            trades_today += rows[0][0] or 0

    a_audit, i_audit = _combined_audit()
    for e in a_audit + i_audit:
        if e["action"] in ("TRADE_BLOCKED", "TRADE_REJECTED"):
            det = _parse_detail(e)
            reason = (det.get("rule_failed") or det.get("reason") or "").lower()
            if "daily loss" in reason:
                loss_limit_hit = True

    # PDT from live_gate_state.json
    for d in (ALPACA_DIR, IBKR_DIR):
        lg = _read_json(d / "live_gate_state.json")
        if lg.get("date") == today:
            tests = lg.get("tests", {})
            for key in ("pdt_remaining", "day_trades_remaining", "DayTradesRemaining"):
                val = tests.get(key)
                if val is not None:
                    try:
                        pdt_remaining = int(float(val))
                    except Exception:
                        pass
                    break
            break

    if loss_limit_hit:
        issues.append("CRITICAL: Daily loss limit hit — all new entries blocked")
    if pdt_remaining == 0:
        issues.append("CRITICAL: PDT exhausted — 0 day trades remaining")
    elif 0 < pdt_remaining <= 1:
        issues.append(f"WARNING: PDT low — only {pdt_remaining} day trade(s) remaining")

    return {
        "status":          "ok" if not issues else ("critical" if loss_limit_hit or pdt_remaining == 0 else "degraded"),
        "trades_today":    trades_today,
        "pdt_remaining":   pdt_remaining,
        "loss_limit_hit":  loss_limit_hit,
        "issues":          issues,
    }


# ── 10. Agent Coordination ───────────────────────────────────────────────────

def _coordination() -> dict:
    today  = _today()
    issues = []

    alpaca_syms: set[str] = set()
    ibkr_syms:   set[str] = set()

    for name, d, target in [("alpaca", ALPACA_DIR, alpaca_syms), ("ibkr", IBKR_DIR, ibkr_syms)]:
        rows = _db_query(d / "daytrades.db",
                         "SELECT symbol FROM trades WHERE date=?", (today,))
        for r in rows:
            target.add(r[0])

    dupes = alpaca_syms & ibkr_syms
    if dupes:
        issues.append(f"WARNING: Same symbol traded by both agents: {', '.join(sorted(dupes))}")

    # Agent alive check
    alive = {}
    for name, d in [("alpaca", ALPACA_DIR), ("ibkr", IBKR_DIR)]:
        lp = d / "trading_day.log"
        if lp.exists():
            age = (datetime.now() - datetime.fromtimestamp(lp.stat().st_mtime)).total_seconds()
            alive[name] = age < 300
        else:
            alive[name] = False

    if not alive.get("alpaca"):
        issues.append("WARNING: Alpaca agent log stale (>5 min) — may be down")
    if not alive.get("ibkr"):
        issues.append("WARNING: IBKR agent log stale (>5 min) — may be down")

    # Shortlist overlap
    sl_syms: dict[str, list[str]] = {}
    for name, d in [("alpaca", ALPACA_DIR), ("ibkr", IBKR_DIR)]:
        try:
            sl = json.loads((d / "shortlist_state.json").read_text())
            sl_syms[name] = [e.get("symbol", "") for e in sl if e.get("symbol")]
        except Exception:
            sl_syms[name] = []

    return {
        "status":           "ok" if not issues else "degraded",
        "alpaca_alive":     alive.get("alpaca", False),
        "ibkr_alive":       alive.get("ibkr", False),
        "alpaca_trades":    len(alpaca_syms),
        "ibkr_trades":      len(ibkr_syms),
        "duplicate_trades": sorted(dupes),
        "alpaca_shortlist": sl_syms.get("alpaca", []),
        "ibkr_shortlist":   sl_syms.get("ibkr", []),
        "issues":           issues,
    }


# ── 11. Strategy Effectiveness ───────────────────────────────────────────────

def _strategy() -> dict:
    today = _today()
    for d in (ALPACA_DIR, IBKR_DIR):
        p = _read_json(d / "performance.json")
        if p.get("date") == today and p.get("trades", 0) > 0:
            return {
                "status":       "ok",
                "source":       "performance.json",
                "trades":       p.get("trades", 0),
                "win_rate":     p.get("win_rate"),
                "expectancy":   p.get("expectancy"),
                "profit_factor": p.get("profit_factor"),
                "avg_win":      p.get("avg_win"),
                "avg_loss":     p.get("avg_loss"),
                "avg_slippage": p.get("avg_slippage"),
                "time_windows": p.get("time_windows", {}),
                "rolling_10d":  p.get("rolling_10d", {}),
            }

    # Fallback: aggregate from DB
    all_pnl: list[float] = []
    for d in (ALPACA_DIR, IBKR_DIR):
        rows = _db_query(d / "daytrades.db",
                         "SELECT pnl FROM trades WHERE date=?", (today,))
        all_pnl.extend(r[0] for r in rows if r[0] is not None)

    if not all_pnl:
        return {"status": "no_trades", "trades": 0}

    wins   = [p for p in all_pnl if p > 0]
    losses = [p for p in all_pnl if p <= 0]
    win_r  = round(len(wins) / len(all_pnl) * 100, 1) if all_pnl else None
    pf     = round(-sum(wins) / sum(losses), 2) if losses and sum(losses) != 0 else None
    exp    = round(
        (len(wins) / len(all_pnl) * (sum(wins) / len(wins) if wins else 0) +
         len(losses) / len(all_pnl) * (sum(losses) / len(losses) if losses else 0)), 2
    ) if all_pnl else None

    return {
        "status":       "ok",
        "source":       "db",
        "trades":       len(all_pnl),
        "win_rate":     win_r,
        "profit_factor": pf,
        "expectancy":   exp,
        "avg_win":      round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss":     round(sum(losses) / len(losses), 2) if losses else None,
    }


# ── 12. Why Not Trading ──────────────────────────────────────────────────────

def _why_not_trading(scan_st: dict, regime_st: dict, risk_st: dict,
                     data_st: dict) -> dict:
    now_et = _now_et()
    mins   = now_et.hour * 60 + now_et.minute
    today  = _today()
    reasons: list[dict] = []

    def add(sev: str, cat: str, msg: str):
        reasons.append({"severity": sev, "category": cat, "message": msg})

    # Time gate
    if now_et.weekday() >= 5:
        add("INFO", "time_gate", "Weekend — market closed")
    elif mins < 9 * 60 + 45:
        add("INFO", "time_gate", f"Pre-market — scan starts at 09:45 ET (now {now_et.strftime('%H:%M')} ET)")
    elif 12 * 60 <= mins < 13 * 60:
        add("INFO", "time_gate", "Midday block 12:00–13:00 ET — no new entries")
    elif mins >= 15 * 60 + 30:
        add("INFO", "time_gate", "After 15:30 ET — no new entries")

    # Regime
    regime = regime_st.get("current", "UNKNOWN")
    if regime == "NO_TRADE":
        add("CRITICAL", "regime", f"NO_TRADE regime — {regime_st.get('reason', '')[:80]}")
    elif regime == "CHOPPY":
        add("WARNING", "regime", "CHOPPY regime — min score raised to 73; tight spreads only")
    elif regime == "LOW_VOLUME":
        add("WARNING", "regime", "LOW_VOLUME regime — min score raised to 85; RVOL ≥1.5× required")
    elif regime == "HIGH_VOL":
        add("INFO", "regime", "HIGH_VOL regime — RVOL ≥2.0× required; tighter entry criteria")

    # Live gate
    for d in (ALPACA_DIR, IBKR_DIR):
        lg = _read_json(d / "live_gate_state.json")
        if lg.get("date") == today:
            enabled = lg.get("live_enabled") or lg.get("LIVE_ENABLED")
            if enabled is False:
                add("CRITICAL", "live_gate", f"Live gate BLOCKED — {lg.get('reason', '')[:80]}")
            break

    # Risk
    if risk_st.get("loss_limit_hit"):
        add("CRITICAL", "risk", "Daily loss limit hit — all new entries blocked for today")
    pdt = risk_st.get("pdt_remaining", -1)
    if pdt == 0:
        add("CRITICAL", "risk", "PDT budget exhausted — 0 day trades remaining")
    elif 0 < pdt <= 1:
        add("WARNING", "risk", f"PDT budget low — {pdt} day trade remaining (reserved as buffer)")

    # Data
    if not data_st.get("ibkr_connected"):
        add("CRITICAL", "data", "IB Gateway disconnected — no live quotes")
    elif data_st.get("data_confidence_score", 100) < 60:
        add("WARNING", "data", f"Low data confidence ({data_st['data_confidence_score']}%) — stale feed")

    # Prescan / candidates
    if not scan_st.get("prescan_done"):
        add("WARNING", "prescan", "Prescan not yet run today — no candidates scored")
    tradeable = scan_st.get("tradeable_count", 0)
    watchlist = scan_st.get("watchlist_count", 0)
    if tradeable == 0 and watchlist == 0:
        add("INFO", "candidates", "No prescan candidates above threshold — waiting for setup")
    elif tradeable == 0 and watchlist > 0:
        add("INFO", "candidates", f"{watchlist} watchlist candidate(s) being monitored — none yet tradeable")
    else:
        add("INFO", "candidates", f"{tradeable} tradeable + {watchlist} watchlist candidates in scoring queue")

    in_window = (9 * 60 + 45 <= mins < 12 * 60) or (13 * 60 <= mins < 15 * 60 + 30)
    primary = reasons[0]["message"] if reasons else "All systems normal — scanning for setups"

    return {
        "in_trading_window":  in_window and now_et.weekday() < 5,
        "scanning":           scan_st.get("total_scans", 0) > 0,
        "primary_reason":     primary,
        "reasons":            reasons,
        "regime":             regime,
        "tradeable_candidates": tradeable,
    }


# ── 13. Business KPIs ────────────────────────────────────────────────────────

def _kpis(scan_st: dict, scoring_st: dict, exec_st: dict,
          strat_st: dict, data_st: dict, missed_st: dict) -> dict:
    return {
        "scans_today":              scan_st.get("total_scans", 0),
        "candidates_scored":        scoring_st.get("candidates_seen", 0),
        "candidates_rejected":      scoring_st.get("local_rejects", 0) + exec_st.get("rejected_today", 0),
        "orders_placed":            exec_st.get("orders_today", 0),
        "fills_confirmed":          exec_st.get("fills_today", 0),
        "missed_movers":            missed_st.get("missed_count", 0),
        "win_rate":                 strat_st.get("win_rate"),
        "expectancy":               strat_st.get("expectancy"),
        "profit_factor":            strat_st.get("profit_factor"),
        "avg_slippage_pct":         exec_st.get("avg_slippage_pct"),
        "data_confidence":          data_st.get("data_confidence_score", 100),
        "cache_hit_rate_pct":       scoring_st.get("cache_hit_rate_pct", 0),
        "claude_change_rate_pct":   scoring_st.get("decision_change_rate_pct", 0),
    }


# ── 14. Alerts ───────────────────────────────────────────────────────────────

def _alerts(bpm: dict) -> list[dict]:
    alerts: list[dict] = []
    ts = datetime.now(ET).strftime("%H:%M ET")

    def alert(sev: str, cat: str, msg: str):
        alerts.append({"severity": sev, "category": cat.upper(), "message": msg, "ts": ts})

    for section_key in ("research", "scan", "integration", "scoring",
                        "execution", "data", "regime", "risk", "coordination"):
        for issue in bpm.get(section_key, {}).get("issues", []):
            sev = ("CRITICAL" if issue.startswith("CRITICAL") else
                   "WARNING"  if issue.startswith("WARNING")  else "INFO")
            msg = issue.replace("CRITICAL: ", "").replace("WARNING: ", "").replace("INFO: ", "")
            alert(sev, section_key, msg)

    # Composite alerts
    scan_st    = bpm.get("scan", {})
    scoring_st = bpm.get("scoring", {})
    why_st     = bpm.get("why", {})

    if scan_st.get("skipped_scans", 0) > max(scan_st.get("total_scans", 0), 1) * 2:
        alert("WARNING", "scan", f"Most scans are being skipped "
              f"({scan_st['skipped_scans']} skipped vs {scan_st['total_scans']} executed)")

    if (scoring_st.get("candidates_seen", 0) == 0
            and scan_st.get("total_scans", 0) > 5
            and why_st.get("in_trading_window")):
        alert("WARNING", "scoring", "No scoring activity after 5+ scan cycles during trading window")

    if (bpm.get("execution", {}).get("orders_today", 0) > 0
            and bpm.get("execution", {}).get("fills_today", 0) == 0):
        alert("WARNING", "execution", "Orders placed but no fills recorded — check broker connection")

    SEV_ORDER = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}
    alerts.sort(key=lambda a: SEV_ORDER.get(a["severity"], 3))
    return alerts


# ── Main collector ────────────────────────────────────────────────────────────

def collect_bpm(force: bool = False) -> dict:
    global _bpm_cache, _bpm_cache_ts

    if not force and _bpm_cache and time.time() - _bpm_cache_ts < _BPM_TTL:
        return _bpm_cache

    now_et = _now_et()
    mins   = now_et.hour * 60 + now_et.minute
    run_missed = mins >= 10 * 60 + 30 and now_et.weekday() < 5

    research  = _research_pipeline()
    scan      = _scan_pipeline()
    integ     = _integration()
    scoring   = _scoring_engine()
    missed    = _missed_opportunities() if run_missed else {"status": "too_early", "movers": [], "missed_count": 0, "rejected_missed": 0}
    execution = _execution_pipeline()
    data      = _market_data()
    regime    = _regime_engine()
    risk      = _risk_pdt()
    coord     = _coordination()
    strategy  = _strategy()
    why       = _why_not_trading(scan, regime, risk, data)
    kpis      = _kpis(scan, scoring, execution, strategy, data, missed)

    bpm = {
        "generated_at": now_et.strftime("%H:%M:%S ET"),
        "research":     research,
        "scan":         scan,
        "integration":  integ,
        "scoring":      scoring,
        "missed":       missed,
        "execution":    execution,
        "data":         data,
        "regime":       regime,
        "risk":         risk,
        "coordination": coord,
        "strategy":     strategy,
        "why":          why,
        "kpis":         kpis,
    }
    bpm["alerts"] = _alerts(bpm)

    _bpm_cache    = bpm
    _bpm_cache_ts = time.time()
    return bpm
