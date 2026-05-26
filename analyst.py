"""Claude scores intraday momentum candidates 0-100 with component breakdown.
Sector ETF strength is fetched and included in the prompt for theme confirmation.
"""
import anthropic
import json
import yfinance as yf
from config import ANTHROPIC_API_KEY, MIN_SCORE_TO_TRADE, WATCHLIST_SCORE, SECTOR_ETFS

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM = """You are an expert intraday day trader. Evaluate stocks for same-day momentum trades.

Score each candidate 0-100 using these weighted components:
- momentum_score    (max 25): gap strength, direction, sustainability, move from open
- volume_score      (max 20): relative volume, confirmation, absolute daily volume, volume trend
- news_score        (max 20): catalyst quality, relevance, recency
- market_trend_score (max 15): sector ETF alignment, broad market regime
- volatility_score  (max 10): intraday range suitability — not too quiet, not too wild
- liquidity_score   (max 10): bid/ask spread tightness, VWAP position

Key rules:
- BELOW-VWAP entries are lower quality — reduce market_trend_score and liquidity_score slightly
- FALLING volume trend (vol_trend_ratio < 1) is a red flag — reduce volume_score
- If sector ETF is down, reduce market_trend_score for stocks in that sector
- Spike detection: if move_from_open >> gap_pct, the initial move may be a temporary spike

Return ALL candidates with scores. Include low-scoring ones.
Respond with a JSON array only — no markdown, no explanation outside the array."""


def _sector_etf_strength(sectors: set[str]) -> dict[str, float]:
    """Return gap_pct for relevant sector ETFs. Quiet failure = empty dict."""
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
                gap   = (today - prev) / prev * 100
                result[ticker] = round(gap, 2)
        return result
    except Exception:
        return {}


def analyse_candidates(candidates: list[dict]) -> list[dict]:
    if not candidates:
        return []

    # Fetch sector ETF strength for sectors represented in candidates
    sectors  = {c.get("sector", "Unknown") for c in candidates if c.get("sector")}
    etf_data = _sector_etf_strength(sectors)

    # Build readable ETF context string
    etf_lines = [f"  {etf}: {gap:+.2f}%" for etf, gap in etf_data.items()]
    etf_context = "Sector ETF performance today:\n" + "\n".join(etf_lines) if etf_lines else ""

    summary = [
        {
            "symbol":          c["symbol"],
            "price":           c["price"],
            "gap_pct":         c["gap_pct"],
            "move_from_open":  c.get("move_from_open", 0),
            "rel_volume":      c["rel_volume"],
            "vol_trend_ratio": c.get("vol_trend_ratio", 1.0),
            "today_volume":    c["today_volume"],
            "spread_pct":      c.get("spread_pct", 0),
            "volatility_pct":  c.get("volatility_pct", 0),
            "below_vwap":      c.get("below_vwap", False),
            "sector":          c.get("sector", "Unknown"),
            "sector_etf":      SECTOR_ETFS.get(c.get("sector", ""), ""),
            "has_news":        c.get("has_news", False),
            "news":            c.get("news", []),
        }
        for c in candidates
    ]

    prompt = (
        f"{etf_context}\n\n"
        f"Score ALL of these intraday momentum candidates 0-100.\n"
        f"Flag below-VWAP entries, falling volume trends, and sector misalignment.\n\n"
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

    picks.sort(key=lambda x: x.get("score", 0), reverse=True)
    return picks
