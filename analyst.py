"""
Claude scores intraday momentum candidates 0-100 with component breakdown.

Token-optimised flow:
  - Local pre-scoring gates Claude calls: candidates below CLAUDE_MIN_LOCAL_SCORE
    are rejected deterministically without any API call
  - File-based score cache (keyed by symbol + date + input hash) avoids re-scoring
    candidates whose inputs have not materially changed since the last scan
  - Compact prompt: only the fields Claude actually needs, short field names
  - Compact output: top_reasons + red_flags arrays (max 2 each) instead of
    free-text reasoning — reasoning string is derived locally for downstream use
"""
import anthropic
import json
import hashlib
import yfinance as yf
from datetime import date
from config import (
    ANTHROPIC_API_KEY, MIN_SCORE_TO_TRADE, WATCHLIST_SCORE,
    SECTOR_ETFS, NEWS_MIN_IMPACT_SCORE,
    CLAUDE_MIN_LOCAL_SCORE, ENABLE_CLAUDE_RESCORING,
    MAX_SYMBOLS_PER_CLAUDE_BATCH, ANALYST_SCORE_CACHE_FILE,
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
    if bias == "AVOID":     score = min(score, 45)
    elif bias == "BEARISH": score -= 6
    elif bias == "BULLISH": score += 5
    if rscore < 30:         score -= 5

    return max(0, min(100, score))


def _candidate_hash(c: dict, top_news: list, research: dict) -> str:
    """Short hash of the fields that materially affect Claude's score.
    Rounded to coarse buckets so minor price drift doesn't bust the cache."""
    key = {
        "gap":      round(c.get("gap_pct", 0) * 2) / 2,       # 0.5% buckets
        "rvol":     round(c.get("rel_volume", 0) * 4) / 4,    # 0.25 buckets
        "spread":   round(c.get("spread_pct", 0), 2),
        "bvwap":    c.get("below_vwap", False),
        "vtrend":   round(c.get("vol_trend_ratio", 1.0), 1),
        "impact":   top_news[0]["impact"] if top_news else 0,
        "headline": top_news[0]["headline"][:60] if top_news else "",
        "bias":     research.get("pre_market_bias", "NEUTRAL"),
        "rscore":   research.get("research_score", 50),
    }
    return hashlib.md5(json.dumps(key, sort_keys=True).encode()).hexdigest()[:12]


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

    # Compact market context — single line, only what Claude needs
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

    # Compact candidate objects — short keys, only needed fields
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
        research  = get_research(sym)
        local     = _local_score(c, research)
        h         = _candidate_hash(c, top_news, research)
        top_impact = next((n["impact_score"] for n in all_news if n["ticker"] == sym), 0)
        candidate_ctx.append({
            "c":           c,
            "sym":         sym,
            "top_news":    top_news,
            "research":    research,
            "local_score": local,
            "hash":        h,
            "top_impact":  top_impact,
        })

    score_cache = _load_score_cache()
    budget      = {"calls": 0, "cache_hits": 0, "local_rejects": 0}
    to_claude   = []
    results     = {}

    for ctx in candidate_ctx:
        sym   = ctx["sym"]
        local = ctx["local_score"]

        if local < CLAUDE_MIN_LOCAL_SCORE:
            budget["local_rejects"] += 1
            results[sym] = _make_local_result(ctx, local, news_stats)
            continue

        cache_key = f"{sym}:{ctx['hash']}"
        if cache_key in score_cache:
            budget["cache_hits"] += 1
            cached = score_cache[cache_key]
            results[sym] = {**cached, "_news_stats": news_stats, "_top_news_impact": ctx["top_impact"]}
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
                    sym = ctx["sym"]
                    p   = batch_results.get(sym, _make_local_result(ctx, ctx["local_score"], news_stats))
                    # Derive reasoning string for backward compatibility
                    reasons = p.get("top_reasons", [])
                    flags   = p.get("red_flags", [])
                    p["reasoning"] = "; ".join(reasons + [f"⚠ {f}" for f in flags]) or "no detail"
                    cache_key = f"{sym}:{ctx['hash']}"
                    score_cache[cache_key] = {k: v for k, v in p.items() if not k.startswith("_")}
                    results[sym] = {**p, "_news_stats": news_stats, "_top_news_impact": ctx["top_impact"]}
            except Exception as exc:
                print(f"   [ANALYST] Claude batch error: {exc} — using local scores")
                for ctx in batch:
                    sym = ctx["sym"]
                    results[sym] = _make_local_result(ctx, ctx["local_score"], news_stats)
    else:
        for ctx in to_claude:
            results[ctx["sym"]] = _make_local_result(ctx, ctx["local_score"], news_stats)

    _save_score_cache(score_cache)

    total = len(candidates)
    print(
        f"   [ANALYST] calls:{budget['calls']}  cache_hits:{budget['cache_hits']}  "
        f"local_rejects:{budget['local_rejects']}  to_claude:{len(to_claude)}  total:{total}"
    )

    picks = list(results.values())
    for p in picks:
        score = p.get("score", 0)
        p["tradeable"] = score >= MIN_SCORE_TO_TRADE
        p["watchlist"] = WATCHLIST_SCORE <= score < MIN_SCORE_TO_TRADE

    picks.sort(key=lambda x: x.get("score", 0), reverse=True)
    return picks


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
