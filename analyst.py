"""
Claude scores intraday momentum candidates 0-100 with component breakdown.

Feed enhancements vs original:
  - Enriched news: Finnhub + Benzinga, deduplicated, impact-scored
  - Event-risk context: earnings windows, halts, economic calendar events
  - Broad market context: SPY/QQQ/IWM gaps + VIX level + sector ETF gaps
  - News that fails the impact threshold is filtered before sending to Claude
"""
import anthropic
import json
import yfinance as yf
from config import (
    ANTHROPIC_API_KEY, MIN_SCORE_TO_TRADE, WATCHLIST_SCORE,
    SECTOR_ETFS, NEWS_MIN_IMPACT_SCORE,
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
- VIX > 25: reduce all size expectations — add note to reasoning
- IWM underperforming: risk-off environment, raise bar for small/mid-cap names
- Earnings within 1d is already blocked upstream; if flagged within 3d in warns, reduce news_score
- Economic calendar events (FOMC, CPI, NFP): reduce market_trend_score for all candidates
- Spike detection: if move_from_open >> gap_pct, the initial move may be a temporary spike
- news.impact_score >= 60 is strong catalyst; < 30 is weak; weight news_score accordingly
- pre_market_bias AVOID: hard-cap total score at 50, regardless of momentum or news
- pre_market_bias BEARISH: reduce market_trend_score by up to 5 points
- pre_market_bias BULLISH: add up to 5 points to market_trend_score if fundamentals support it
- research_score < 30: flag as fundamentally weak — reduce news_score if no strong catalyst
- red_flags present: note each flag in reasoning

Return ALL candidates with scores. Include low-scoring ones.
Respond with a JSON array only — no markdown, no explanation outside the array."""


# ── Market context helpers ────────────────────────────────────────────────────

def _sector_etf_strength(sectors: set[str]) -> dict[str, float]:
    """Gap pct for relevant sector ETFs. Quiet failure = empty dict."""
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
    """
    SPY, QQQ, IWM daily gaps and current VIX level.
    Returns empty dict on failure.
    """
    try:
        indices = {"SPY": "S&P 500", "QQQ": "Nasdaq", "IWM": "Russell 2000"}
        gaps    = {}
        for ticker, label in indices.items():
            hist = yf.Ticker(ticker).history(period="2d", interval="1d")
            if len(hist) >= 2:
                prev   = float(hist["Close"].iloc[-2])
                today  = float(hist["Close"].iloc[-1])
                gaps[f"{ticker}_gap"] = round((today - prev) / prev * 100, 2)

        vix = yf.Ticker("^VIX").history(period="1d")
        if not vix.empty:
            gaps["vix"] = round(float(vix["Close"].iloc[-1]), 2)

        return gaps
    except Exception:
        return {}


# ── Main scoring function ─────────────────────────────────────────────────────

def analyse_candidates(candidates: list[dict]) -> list[dict]:
    if not candidates:
        return []

    symbols = [c["symbol"] for c in candidates]

    # Fetch enriched news for all candidates at once (Finnhub + Benzinga)
    all_news, news_stats = get_all_news(symbols)

    # Event risk: earnings blocks/warns + halts + economic calendar
    risk = get_risk_summary(symbols)

    # Broad market context: SPY/QQQ/IWM gaps + VIX
    broad  = _broad_market_context()
    vix    = broad.get("vix", 0)

    # Sector ETF gaps
    sectors  = {c.get("sector", "Unknown") for c in candidates if c.get("sector")}
    etf_data = _sector_etf_strength(sectors)

    # ── Build prompt context ──────────────────────────────────────────────────

    # Broad market block
    bm_lines = [
        f"  SPY: {broad.get('SPY_gap', 0):+.2f}%",
        f"  QQQ: {broad.get('QQQ_gap', 0):+.2f}%",
        f"  IWM: {broad.get('IWM_gap', 0):+.2f}%",
        f"  VIX: {vix:.1f}" + (" [ELEVATED >25]" if vix > 25 else ""),
    ]
    broad_context = "Broad market today:\n" + "\n".join(bm_lines)

    # Sector ETF block
    etf_lines = [f"  {etf}: {gap:+.2f}%" for etf, gap in etf_data.items()]
    etf_context = ("Sector ETFs today:\n" + "\n".join(etf_lines)) if etf_lines else ""

    # Event risk block
    econ_events = risk.get("economic_events", [])
    econ_block  = ""
    if econ_events:
        econ_block = "TODAY'S HIGH-IMPACT ECONOMIC EVENTS:\n" + "\n".join(
            f"  - {e}" for e in econ_events
        )

    earn_warns = risk.get("earnings_warns", {})
    earn_block = ""
    if earn_warns:
        lines = [f"  {s}: {d}" for s, d in earn_warns.items()]
        earn_block = "Upcoming earnings (warn-only):\n" + "\n".join(lines)

    # ── Per-candidate summary for Claude ────────────────────────────────────

    summary = []
    for c in candidates:
        sym      = c["symbol"]
        sym_news = news_for_symbol(sym, all_news)
        # Send top 3 impactful news items, suppress noise below threshold
        top_news = [
            {
                "headline":  n["headline"][:120],
                "impact":    n["impact_score"],
                "type":      n["catalyst_type"],
                "sentiment": n["sentiment"],
                "age_mins":  n["age_mins"],
                "source":    n["source"],
            }
            for n in sym_news[:3]
            if n["impact_score"] >= NEWS_MIN_IMPACT_SCORE
        ]

        research = get_research(sym)

        summary.append({
            "symbol":            sym,
            "price":             c["price"],
            "gap_pct":           c["gap_pct"],
            "move_from_open":    c.get("move_from_open", 0),
            "rel_volume":        c["rel_volume"],
            "vol_trend_ratio":   c.get("vol_trend_ratio", 1.0),
            "today_volume":      c["today_volume"],
            "spread_pct":        c.get("spread_pct", 0),
            "volatility_pct":    c.get("volatility_pct", 0),
            "below_vwap":        c.get("below_vwap", False),
            "sector":            c.get("sector", "Unknown"),
            "sector_etf":        SECTOR_ETFS.get(c.get("sector", ""), ""),
            "news":              top_news,
            "has_news":          bool(top_news),
            "top_news_impact":   top_news[0]["impact"] if top_news else 0,
            "earnings_warn":     earn_warns.get(sym, ""),
            "halted":            sym in risk.get("halts", {}),
            "research_brief":    research.get("research_brief", ""),
            "pre_market_bias":   research.get("pre_market_bias", "NEUTRAL"),
            "research_score":    research.get("research_score", 50),
            "research_flags":    research.get("red_flags", []),
        })

    context_parts = [broad_context, etf_context, econ_block, earn_block]
    context_str   = "\n\n".join(p for p in context_parts if p)

    prompt = (
        f"{context_str}\n\n"
        f"Score ALL of these intraday momentum candidates 0-100.\n"
        f"Flag below-VWAP entries, falling volume trends, sector misalignment, "
        f"and earnings risk. Account for VIX level and economic events in "
        f"market_trend_score.\n\n"
        f"Candidates:\n{json.dumps(summary, indent=2)}\n\n"
        f"Respond with JSON array:\n"
        f'[{{"symbol":"X","score":85,'
        f'"momentum_score":22,"volume_score":18,"news_score":16,'
        f'"market_trend_score":12,"volatility_score":8,"liquidity_score":9,'
        f'"reasoning":"brief rationale, max 100 chars"}}]'
    )

    resp = _client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )

    text = resp.content[0].text.strip()
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]

    picks = json.loads(text.strip())

    for p in picks:
        score          = p.get("score", 0)
        p["tradeable"] = score >= MIN_SCORE_TO_TRADE
        p["watchlist"] = WATCHLIST_SCORE <= score < MIN_SCORE_TO_TRADE
        # Embed news stats for feed_logger consumption
        p["_news_stats"]   = news_stats
        p["_top_news_impact"] = next(
            (n["impact_score"] for n in all_news if n["ticker"] == p.get("symbol")), 0
        )

    picks.sort(key=lambda x: x.get("score", 0), reverse=True)
    return picks
