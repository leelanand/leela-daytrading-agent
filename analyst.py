"""Claude scores intraday momentum candidates 0-100 with component breakdown."""
import anthropic
import json
from config import ANTHROPIC_API_KEY, MIN_SCORE_TO_TRADE, WATCHLIST_SCORE

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM = """You are an expert intraday day trader. Evaluate stocks for same-day momentum trades.

Score each candidate 0-100 using these weighted components:
- momentum_score   (max 25): gap strength, direction, sustainability
- volume_score     (max 20): relative volume, confirmation, absolute daily volume
- news_score       (max 20): catalyst quality, relevance, recency
- market_trend_score (max 15): sector momentum, broad market alignment
- volatility_score (max 10): intraday range suitability for day trading
- liquidity_score  (max 10): bid/ask spread tightness, ease of entry/exit

Return ALL candidates with scores — do not filter any out.
Include low-scoring ones so the risk layer can decide.
Respond with a JSON array only — no markdown, no explanation outside the array."""


def analyse_candidates(candidates: list[dict]) -> list[dict]:
    if not candidates:
        return []

    summary = [
        {
            "symbol":         c["symbol"],
            "price":          c["price"],
            "gap_pct":        c["gap_pct"],
            "rel_volume":     c["rel_volume"],
            "today_volume":   c["today_volume"],
            "spread_pct":     c.get("spread_pct", 0),
            "volatility_pct": c.get("volatility_pct", 0),
            "sector":         c.get("sector", "Unknown"),
            "has_news":       c.get("has_news", False),
            "news":           c.get("news", []),
        }
        for c in candidates
    ]

    prompt = (
        f"Score ALL of these intraday momentum candidates 0-100.\n\n"
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
        score         = p.get("score", 0)
        p["tradeable"] = score >= MIN_SCORE_TO_TRADE
        p["watchlist"] = WATCHLIST_SCORE <= score < MIN_SCORE_TO_TRADE

    picks.sort(key=lambda x: x.get("score", 0), reverse=True)
    return picks
