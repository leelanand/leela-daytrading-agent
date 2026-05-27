"""
Claude scores intraday momentum candidates 0-100 with component breakdown.

Token-optimised flow:
  - Local pre-scoring gates Claude calls: candidates below CLAUDE_MIN_LOCAL_SCORE
    are rejected deterministically without any API call
  - File-based score cache with material-change invalidation: only invalidates when
    move_from_open, RVOL, headline, or regime shift beyond configured thresholds
  - Compact prompt (short field names, only needed fields) + compact output schema
  - Effectiveness tracking: logs local vs Claude decision per candidate per scan
"""
import anthropic
import json
import hashlib
import math
import yfinance as yf
from datetime import date, datetime
from config import (
    ANTHROPIC_API_KEY, MIN_SCORE_TO_TRADE, WATCHLIST_SCORE,
    SECTOR_ETFS, NEWS_MIN_IMPACT_SCORE,
    CLAUDE_MIN_LOCAL_SCORE, ENABLE_CLAUDE_RESCORING,
    MAX_SYMBOLS_PER_CLAUDE_BATCH, ANALYST_SCORE_CACHE_FILE,
    CLAUDE_CACHE_PRICE_MOVE_INVALIDATE_PCT,
    CLAUDE_CACHE_RVOL_CHANGE_INVALIDATE_PCT,
    CLAUDE_CACHE_REGIME_CHANGE_INVALIDATE,
    CLAUDE_CACHE_NEW_CATALYST_INVALIDATE,
    TRACK_CLAUDE_DECISION_DELTA, CLAUDE_EFFECTIVENESS_LOG_FILE,
    REGIME_CACHE_FILE,
    TRADING_MODE,
    get_min_score,
)
from news_feed import get_all_news, news_for_symbol
from event_risk import get_risk_summary
from research import get_research

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM = """You are an expert intraday day trader. Evaluate stocks for same-day momentum trades.

Score each candidate 0-100 using these weighted components:
- momentum_score      (max 25): gap strength, direction, sustainability, move from open
- volume_score        (max 20): relative volume, confirmation, absolute daily volume, volume trend
- news_score          (max 20): catalyst quality (use impact_score), recency, sentiment, type
- market_trend_score  (max 15): sector ETF alignment, broad market regime, IWM/VIX context
- volatility_score    (max 10): intraday range suitability — not too quiet, not too wild
- liquidity_score     (max 10): bid/ask spread tightness, VWAP position

Key rules:
- BELOW-VWAP entries are lower quality — reduce market_trend_score and liquidity_score slightly
- FALLING volume trend (vol_trend_ratio < 1) is a red flag — reduce volume_score
- If sector ETF is down, reduce market_trend_score for stocks in that sector
- VIX > 25: reduce all size expectations
- IWM underperforming: risk-off environment, raise bar for small/mid-cap names
- Earnings within 1d is already blocked upstream; if within 3d in warns, reduce news_score
- Economic events (FOMC, CPI, NFP): reduce market_trend_score for all candidates
- news.impact >= 60 is strong catalyst; < 30 is weak; weight news_score accordingly
- pre_market_bias AVOID: hard-cap total score at 50
- pre_market_bias BEARISH: reduce market_trend_score by up to 5
- pre_market_bias BULLISH: add up to 5 to market_trend_score if fundamentals support
- research_score < 30: flag as fundamentally weak
- red_flags present: include in red_flags array

Respond with a JSON array only — no markdown, no explanation outside the array.
Each element must have exactly: {"symbol":"X","score":85,"momentum_score":22,"volume_score":18,"news_score":16,"market_trend_score":12,"volatility_score":8,"liquidity_score":9,"top_reasons":["up to 2 short phrases"],"red_flags":["up to 2 flags, empty if none"]}
top_reasons: max 2 items, each ≤8 words. red_flags: max 2 items, empty array if none."""


# ── Market context helpers ────────────────────────────────────────────────────

def _sector_etf_strength(sectors: set[str]) -> dict[str, float]:
    tickers = [SECTOR_ETFS[s] for s in sectors if s in SECTOR_ETFS]
    if not tickers:
        return {}
    try:
        result = {}
        for ticker in tickers:
            hist = yf.Ticker(ticker).history(period="2d", interval="1d")
            if len(hist) >= 2:
                prev  = float(hist["Close"].iloc[-2])
                today = float(hist["Close"].iloc[-1])
                result[ticker] = round((today - prev) / prev * 100, 2)
        return result
    except Exception:
        return {}


def _broad_market_context() -> dict:
    try:
        gaps = {}
        for ticker in ("SPY", "QQQ", "IWM"):
            hist = yf.Ticker(ticker).history(period="2d", interval="1d")
            if len(hist) >= 2:
                prev  = float(hist["Close"].iloc[-2])
                today = float(hist["Close"].iloc[-1])
                gaps[f"{ticker}_gap"] = round((today - prev) / prev * 100, 2)
        vix = yf.Ticker("^VIX").history(period="1d")
        if not vix.empty:
            gaps["vix"] = round(float(vix["Close"].iloc[-1]), 2)
        return gaps
    except Exception:
        return {}


def _get_current_regime() -> str:
    try:
        data = json.loads(REGIME_CACHE_FILE.read_text())
        return data.get("regime", "UNKNOWN")
    except Exception:
        return "UNKNOWN"


# ── Local pre-scoring (gate only — not used for trading decisions) ─────────────

def _local_score(c: dict, research: dict) -> int:
    """Rough deterministic estimate used only to decide whether to call Claude."""
    score = 50

    gap = abs(c.get("gap_pct", 0))
    if gap >= 5:      score += 20
    elif gap >= 3:    score += 15
    elif gap >= 2:    score += 10
    elif gap >= 1.5:  score += 5

    rvol = c.get("rel_volume", 0)
    if rvol >= 3.0:   score += 18
    elif rvol >= 2.0: score += 13
    elif rvol >= 1.5: score += 7
    elif rvol >= 1.3: score += 2

    if c.get("below_vwap", False):           score -= 6
    if c.get("vol_trend_ratio", 1.0) < 0.8: score -= 6
    if c.get("spread_pct", 0) > 0.20:       score -= 7
    if c.get("halted", False):               return 0

    bias   = research.get("pre_market_bias", "NEUTRAL")
    rscore = research.get("research_score", 50)
    if bias == "AVOID":
        if TRADING_MODE == "PAPER":
            score -= 15   # PAPER: penalise but allow strong setups to reach Claude threshold
        else:
            score = min(score, 45)   # LIVE: hard cap — Claude will also cap at 50
    elif bias == "BEARISH": score -= 6
    elif bias == "BULLISH": score += 5
    if rscore < 30:         score -= 5

    return max(0, min(100, score))


# ── Cache key + invalidation ──────────────────────────────────────────────────

def _candidate_hash(c: dict, top_news: list, research: dict, regime: str) -> str:
    """Cache key using config-driven buckets.

    Price is bucketed on a log scale so that a move of CLAUDE_CACHE_PRICE_MOVE_INVALIDATE_PCT%
    from the last-scored price lands in the next bucket and triggers a cache miss.
    """
    rvol = c.get("rel_volume", 0)
    rvol_bucket = max(round(rvol * CLAUDE_CACHE_RVOL_CHANGE_INVALIDATE_PCT / 100 * 2) / 2, 0.25)

    # Log-scale price bucket: each bucket ≈ CLAUDE_CACHE_PRICE_MOVE_INVALIDATE_PCT% wide
    price = c.get("price", 0)
    log_step = math.log(1 + CLAUDE_CACHE_PRICE_MOVE_INVALIDATE_PCT / 100)
    price_bucket = int(math.log(price) / log_step) if price > 0 else 0

    key = {
        "gap":          round(c.get("gap_pct", 0) / 0.5) * 0.5,
        "price_bucket": price_bucket,
        "rvol":         round(rvol / rvol_bucket) * rvol_bucket,
        "spread":       round(c.get("spread_pct", 0), 2),
        "bvwap":        c.get("below_vwap", False),
        "vtrend":       round(c.get("vol_trend_ratio", 1.0), 1),
        "impact":       top_news[0]["impact"] if top_news else 0,
        "headline":     (top_news[0]["headline"][:60] if top_news else "")
                        if CLAUDE_CACHE_NEW_CATALYST_INVALIDATE else "",
        "bias":         research.get("pre_market_bias", "NEUTRAL"),
        "rscore":       research.get("research_score", 50),
        "regime":       regime if CLAUDE_CACHE_REGIME_CHANGE_INVALIDATE else "",
    }
    return hashlib.md5(json.dumps(key, sort_keys=True).encode()).hexdigest()[:12]


def _build_snap(c: dict, top_news: list, regime: str) -> dict:
    """Snapshot of values stored in cache entry for secondary invalidation check."""
    return {
        "price":    c.get("price", 0),      # actual price at scoring time (not move_from_open)
        "rvol":     c.get("rel_volume", 0),
        "spread":   c.get("spread_pct", 0),
        "headline": top_news[0]["headline"][:60] if top_news else "",
        "regime":   regime,
    }


def _should_invalidate(snap: dict, c: dict, top_news: list, regime: str) -> bool:
    """Secondary check: bust cache if values shifted materially since last scoring."""
    cur_price  = c.get("price", 0)
    cur_rvol   = c.get("rel_volume", 0)
    cur_spread = c.get("spread_pct", 0)
    cur_head   = top_news[0]["headline"][:60] if top_news else ""

    # Price change from last-scored price (not move_from_open)
    snap_price = snap.get("price", 0)
    if cur_price > 0 and snap_price > 0:
        if abs(cur_price - snap_price) / snap_price * 100 > CLAUDE_CACHE_PRICE_MOVE_INVALIDATE_PCT:
            return True

    old_rvol = snap.get("rvol", cur_rvol)
    if old_rvol > 0:
        if abs(cur_rvol - old_rvol) / old_rvol * 100 > CLAUDE_CACHE_RVOL_CHANGE_INVALIDATE_PCT:
            return True

    if CLAUDE_CACHE_NEW_CATALYST_INVALIDATE and cur_head != snap.get("headline", cur_head):
        return True

    if CLAUDE_CACHE_REGIME_CHANGE_INVALIDATE and regime != snap.get("regime", regime):
        return True

    old_spread = snap.get("spread", 0)
    if old_spread > 0 and cur_spread > old_spread * 1.5:  # spread widened 50%+
        return True

    return False


# ── Score cache ───────────────────────────────────────────────────────────────

def _load_score_cache() -> dict:
    try:
        if ANALYST_SCORE_CACHE_FILE.exists():
            data = json.loads(ANALYST_SCORE_CACHE_FILE.read_text())
            if data.get("date") == date.today().isoformat():
                return data.get("scores", {})
    except Exception:
        pass
    return {}


def _save_score_cache(scores: dict):
    try:
        ANALYST_SCORE_CACHE_FILE.write_text(json.dumps({
            "date":   date.today().isoformat(),
            "scores": scores,
        }, indent=2))
    except Exception:
        pass


# ── Effectiveness logging ─────────────────────────────────────────────────────

def _log_effectiveness(entries: list[dict]):
    if not TRACK_CLAUDE_DECISION_DELTA or not entries:
        return
    try:
        with open(CLAUDE_EFFECTIVENESS_LOG_FILE, "a", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
    except Exception:
        pass


# ── Claude batch call ─────────────────────────────────────────────────────────

def _call_claude(
    batch_ctx:  list[dict],
    broad:      dict,
    etf_data:   dict,
    earn_warns: dict,
    risk:       dict,
    vix:        float,
) -> dict[str, dict]:
    """Call Claude for a batch. Returns {symbol: result_dict}."""
    bm = (
        f"SPY:{broad.get('SPY_gap', 0):+.2f}% "
        f"QQQ:{broad.get('QQQ_gap', 0):+.2f}% "
        f"IWM:{broad.get('IWM_gap', 0):+.2f}% "
        f"VIX:{vix:.1f}" + (" [HIGH]" if vix > 25 else "")
    )
    context_lines = [bm]
    if etf_data:
        context_lines.append("ETFs: " + " ".join(f"{e}:{g:+.2f}%" for e, g in etf_data.items()))
    econ = risk.get("economic_events", [])
    if econ:
        context_lines.append("EVENTS: " + ", ".join(econ[:3]))
    earn_str = "; ".join(f"{s}:{d}" for s, d in earn_warns.items())
    if earn_str:
        context_lines.append("EARN_WARN: " + earn_str)

    compact = []
    for ctx in batch_ctx:
        c  = ctx["c"]
        tn = ctx["top_news"]
        r  = ctx["research"]
        compact.append({
            "sym":    ctx["sym"],
            "gap":    c.get("gap_pct", 0),
            "rvol":   c.get("rel_volume", 0),
            "move":   c.get("move_from_open", 0),
            "vtrend": c.get("vol_trend_ratio", 1.0),
            "vol_k":  round(c.get("today_volume", 0) / 1000),
            "spread": c.get("spread_pct", 0),
            "bvwap":  c.get("below_vwap", False),
            "etf":    SECTOR_ETFS.get(c.get("sector", ""), ""),
            "news":   [{"h": n["headline"][:80], "imp": n["impact"],
                        "sent": n["sentiment"], "age": n["age_mins"]} for n in tn],
            "earn":   earn_warns.get(ctx["sym"], ""),
            "bias":   r.get("pre_market_bias", "NEUTRAL"),
            "rscore": r.get("research_score", 50),
            "rflags": r.get("red_flags", []),
        })

    # In PAPER mode, lift the AVOID hard-cap so experimental trades can be researched
    if TRADING_MODE == "PAPER" and any(ctx["research"].get("pre_market_bias") == "AVOID" for ctx in batch_ctx):
        context_lines.append("PAPER_RESEARCH: AVOID bias = caution flag only — score objectively, no hard score cap")

    prompt = (
        "\n".join(context_lines) + "\n\n"
        f"Score these {len(compact)} candidates:\n"
        + json.dumps(compact, separators=(",", ":"))
    )

    resp = _client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1200,
        system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": prompt}],
    )

    text = resp.content[0].text.strip()
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]

    picks = json.loads(text.strip())
    return {p["symbol"]: p for p in picks}


# ── Main scoring function ─────────────────────────────────────────────────────

def analyse_candidates(candidates: list[dict]) -> list[dict]:
    if not candidates:
        return []

    symbols    = [c["symbol"] for c in candidates]
    all_news, news_stats = get_all_news(symbols)
    risk       = get_risk_summary(symbols)
    broad      = _broad_market_context()
    vix        = broad.get("vix", 0)
    sectors    = {c.get("sector", "Unknown") for c in candidates if c.get("sector")}
    etf_data   = _sector_etf_strength(sectors)
    earn_warns = risk.get("earnings_warns", {})
    regime     = _get_current_regime()

    # Build per-candidate context dicts
    candidate_ctx = []
    for c in candidates:
        sym      = c["symbol"]
        sym_news = news_for_symbol(sym, all_news)
        top_news = [
            {
                "headline":  n["headline"][:120],
                "impact":    n["impact_score"],
                "type":      n["catalyst_type"],
                "sentiment": n["sentiment"],
                "age_mins":  n["age_mins"],
            }
            for n in sym_news[:3]
            if n["impact_score"] >= NEWS_MIN_IMPACT_SCORE
        ]
        research   = get_research(sym)
        local      = _local_score(c, research)
        h          = _candidate_hash(c, top_news, research, regime)
        snap       = _build_snap(c, top_news, regime)
        top_impact = next((n["impact_score"] for n in all_news if n["ticker"] == sym), 0)
        candidate_ctx.append({
            "c":           c,
            "sym":         sym,
            "top_news":    top_news,
            "research":    research,
            "local_score": local,
            "hash":        h,
            "snap":        snap,
            "top_impact":  top_impact,
        })

    score_cache = _load_score_cache()
    budget = {"calls": 0, "cache_hits": 0, "local_rejects": 0, "invalidated": 0}
    to_claude   = []
    results     = {}
    eff_log     = []  # effectiveness entries to write at end

    now_str = datetime.now().strftime("%H:%M")

    for ctx in candidate_ctx:
        sym   = ctx["sym"]
        local = ctx["local_score"]

        if local < CLAUDE_MIN_LOCAL_SCORE:
            budget["local_rejects"] += 1
            results[sym] = _make_local_result(ctx, local, news_stats)
            eff_log.append(_make_eff_entry(sym, local, None, now_str,
                                           cache_hit=False, local_only=True))
            continue

        cache_key = f"{sym}:{ctx['hash']}"
        if cache_key in score_cache:
            cached = score_cache[cache_key]
            snap_stored = cached.get("_snap", {})
            if snap_stored and _should_invalidate(snap_stored, ctx["c"], ctx["top_news"], regime):
                budget["invalidated"] += 1
                to_claude.append(ctx)
            else:
                budget["cache_hits"] += 1
                results[sym] = {**cached, "_news_stats": news_stats,
                                "_top_news_impact": ctx["top_impact"]}
                eff_log.append(_make_eff_entry(sym, local,
                                               cached.get("score"), now_str,
                                               cache_hit=True, local_only=False))
            continue

        to_claude.append(ctx)

    # Batch-call Claude for candidates that need scoring
    if to_claude and ENABLE_CLAUDE_RESCORING:
        for i in range(0, len(to_claude), MAX_SYMBOLS_PER_CLAUDE_BATCH):
            batch = to_claude[i:i + MAX_SYMBOLS_PER_CLAUDE_BATCH]
            budget["calls"] += 1
            try:
                batch_results = _call_claude(batch, broad, etf_data, earn_warns, risk, vix)
                for ctx in batch:
                    sym       = ctx["sym"]
                    p         = batch_results.get(sym,
                                    _make_local_result(ctx, ctx["local_score"], news_stats))
                    reasons   = p.get("top_reasons", [])
                    flags     = p.get("red_flags", [])
                    p["reasoning"] = "; ".join(reasons + [f"⚠ {f}" for f in flags]) or "no detail"
                    cache_key = f"{sym}:{ctx['hash']}"
                    cache_entry = {k: v for k, v in p.items() if not k.startswith("_")}
                    cache_entry["_snap"]         = ctx["snap"]
                    cache_entry["_local_score"]  = ctx["local_score"]
                    changed = (ctx["local_score"] >= MIN_SCORE_TO_TRADE) != (p.get("score", 0) >= MIN_SCORE_TO_TRADE)
                    cache_entry["_claude_changed_decision"] = changed
                    score_cache[cache_key] = cache_entry
                    result_entry = {**p, "_news_stats": news_stats, "_top_news_impact": ctx["top_impact"]}
                    # PAPER AVOID override: tag if quality conditions are met for experimental trading
                    c_orig = ctx["c"]
                    if (TRADING_MODE == "PAPER"
                            and ctx["research"].get("pre_market_bias") == "AVOID"
                            and c_orig.get("rel_volume", 0) >= 2.5
                            and c_orig.get("spread_pct", 0) <= 0.15
                            and ctx["top_impact"] >= 70):
                        result_entry["research_avoid_override_pending"] = True
                    results[sym] = result_entry
                    eff_log.append(_make_eff_entry(sym, ctx["local_score"],
                                                   p.get("score"), now_str,
                                                   cache_hit=False, local_only=False))
            except Exception as exc:
                print(f"   [ANALYST] Claude batch error: {exc} — using local scores")
                for ctx in batch:
                    sym = ctx["sym"]
                    results[sym] = _make_local_result(ctx, ctx["local_score"], news_stats)
                    eff_log.append(_make_eff_entry(sym, ctx["local_score"], None, now_str,
                                                   cache_hit=False, local_only=True))
    else:
        for ctx in to_claude:
            results[ctx["sym"]] = _make_local_result(ctx, ctx["local_score"], news_stats)
            eff_log.append(_make_eff_entry(ctx["sym"], ctx["local_score"], None, now_str,
                                           cache_hit=False, local_only=True))

    _save_score_cache(score_cache)
    _log_effectiveness(eff_log)

    total = len(candidates)
    print(
        f"   [ANALYST] calls:{budget['calls']}  cache_hits:{budget['cache_hits']}  "
        f"invalidated:{budget['invalidated']}  local_rejects:{budget['local_rejects']}  "
        f"to_claude:{len(to_claude)}  total:{total}"
    )

    # Preserve original candidate fields (setup_type, price, VWAP, etc.) not provided by scoring
    orig_map = {c["symbol"]: c for c in candidates}
    picks = list(results.values())
    for p in picks:
        orig = orig_map.get(p["symbol"], {})
        for k, v in orig.items():
            if k not in p:
                p[k] = v

    # Apply setup-type and regime-aware minimum thresholds
    for p in picks:
        score      = p.get("score", 0)
        setup_t    = p.get("setup_type")
        eff_min    = get_min_score(regime, setup_t)
        p["tradeable"]      = score >= eff_min
        p["watchlist"]      = WATCHLIST_SCORE <= score < eff_min
        p["_effective_min"] = eff_min

    picks.sort(key=lambda x: x.get("score", 0), reverse=True)
    return picks


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_local_result(ctx: dict, score: int, news_stats: dict) -> dict:
    return {
        "symbol":            ctx["sym"],
        "score":             score,
        "momentum_score":    0,
        "volume_score":      0,
        "news_score":        0,
        "market_trend_score": 0,
        "volatility_score":  0,
        "liquidity_score":   0,
        "top_reasons":       [],
        "red_flags":         [],
        "reasoning":         "below local threshold — not scored by Claude",
        "_local_only":       True,
        "_news_stats":       news_stats,
        "_top_news_impact":  ctx["top_impact"],
    }


def _make_eff_entry(
    sym: str, local: int, claude: int | None, time_str: str,
    cache_hit: bool, local_only: bool,
) -> dict:
    local_tradeable  = local >= MIN_SCORE_TO_TRADE
    claude_tradeable = (claude >= MIN_SCORE_TO_TRADE) if claude is not None else None
    changed = (
        (claude_tradeable != local_tradeable)
        if claude_tradeable is not None else False
    )
    return {
        "date":                   date.today().isoformat(),
        "time":                   time_str,
        "symbol":                 sym,
        "local_score":            local,
        "claude_score":           claude,
        "local_tradeable":        local_tradeable,
        "claude_tradeable":       claude_tradeable,
        "claude_changed_decision": changed,
        "cache_hit":              cache_hit,
        "local_only":             local_only,
    }
