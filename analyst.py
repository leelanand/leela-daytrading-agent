"""Claude scores intraday momentum candidates."""
import anthropic
import json
from config import ANTHROPIC_API_KEY, MIN_CONFIDENCE

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM = """You are an expert intraday day trader. Evaluate stocks for same-day momentum trades.
Score each 1-10. Only flag as actionable if confidence >= 8.
Consider: gap strength, volume confirmation, news catalyst quality, risk/reward.
Respond with a JSON array only — no markdown, no explanation outside the array."""


def analyse_candidates(candidates: list[dict]) -> list[dict]:
    if not candidates:
        return []

    summary = [
        {
            "symbol":     c["symbol"],
            "price":      c["price"],
            "gap_pct":    c["gap_pct"],
            "rel_volume": c["rel_volume"],
            "news":       c["news"],
        }
        for c in candidates
    ]

    prompt = (
        f"Evaluate these intraday momentum candidates for day trading.\n"
        f"Only include symbols with confidence >= {MIN_CONFIDENCE}.\n\n"
        f"Candidates:\n{json.dumps(summary, indent=2)}\n\n"
        f'Respond with JSON array: [{{"symbol":"X","confidence":8,"reasoning":"..."}}]'
    )

    resp = _client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )

    text = resp.content[0].text.strip()
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]

    picks = json.loads(text.strip())
    return [p for p in picks if p.get("confidence", 0) >= MIN_CONFIDENCE]
