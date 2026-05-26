"""
Unified news feed: Finnhub + optional Benzinga.

Pipeline:
  1. Fetch from both providers in parallel (Benzinga skipped if no key)
  2. Normalise to a common schema
  3. Deduplicate by ticker + timestamp proximity + headline Jaccard similarity
  4. Score each item 0-100 by freshness, catalyst type, source, sentiment
  5. Return sorted by impact score desc
"""
import time
import finnhub
import requests
from datetime import date, datetime, timedelta, timezone
from config import (
    FINNHUB_API_KEY, BENZINGA_API_KEY,
    NEWS_LOOKBACK_MINS, NEWS_DEDUP_SIMILARITY_THRESHOLD,
)

_BENZINGA_BASE = "https://api.benzinga.com/api/v2/news"

# ── Catalyst keyword detection ─────────────────────────────────────────────────

_CATALYST_MAP = {
    # Guidance must be checked before earnings ("full year guidance" vs "full year results")
    "guidance":  ["guidance", "outlook", "raises guidance", "lowers guidance",
                  "raises forecast", "lowers forecast", "narrows guidance",
                  "reaffirms guidance", "full year guidance", "full-year guidance"],
    "earnings":  ["earnings", " eps ", "quarterly results", "revenue beat", "revenue miss",
                  "profit", "q1 ", "q2 ", "q3 ", "q4 ", "annual results"],
    "fda":       ["fda", " nda ", " bla ", "approval", "clinical trial", "phase 1", "phase 2",
                  "phase 3", "drug", "therapy"],
    "ma":        ["acqui", "merger", "takeover", "buyout", " bid for", "strategic deal",
                  "definitive agreement", "all-cash"],
    "analyst":   ["analyst", "upgrade", "downgrade", "price target", " pt ", "outperform",
                  "underperform", "buy rating", "sell rating", "overweight", "underweight",
                  "initiates coverage"],
    "insider":   ["insider buy", "insider sell", "director buy", "director sell",
                  "executive purchase", "10b5-1"],
    "halt":      ["trading halt", "halted", "sec investigation", "sec probe",
                  "delisting", "subpoena", "fraud allegation"],
    "ipo":       ["ipo", "initial public offering", "direct listing", "debut"],
    "split":     ["stock split", "reverse split", "dividend", "special dividend", "spinoff"],
}

_POSITIVE_WORDS = {
    "beat", "beats", "exceeds", "record", "surge", "surges", "jump", "jumps",
    "soar", "soars", "strong", "upgrade", "upgraded", "raises", "raise",
    "breakthrough", "approval", "approved", "wins", "gains", "above", "positive",
    "outperform", "overweight", "bullish",
}
_NEGATIVE_WORDS = {
    "miss", "misses", "below", "fall", "falls", "decline", "declines",
    "downgrade", "downgraded", "cut", "cuts", "loss", "investigation",
    "halt", "halted", "weak", "disappoints", "slump", "slumps", "drops",
    "warns", "warning", "underperform", "underweight", "bearish", "fraud",
}

_SOURCE_TIERS = {
    "high":   {"reuters", "bloomberg", "wall street journal", "wsj", "financial times",
               "cnbc", "associated press", "ap "},
    "medium": {"seekingalpha", "barron", "marketwatch", "thestreet", "motley fool",
               "benzinga", "investor's business daily", "ibd"},
}


def _detect_catalyst(headline: str) -> str:
    hl = headline.lower()
    for catalyst, keywords in _CATALYST_MAP.items():
        if any(k in hl for k in keywords):
            return catalyst
    return "general"


def _detect_sentiment(headline: str) -> str:
    words = set(headline.lower().split())
    pos   = len(words & _POSITIVE_WORDS)
    neg   = len(words & _NEGATIVE_WORDS)
    if pos > neg:   return "positive"
    if neg > pos:   return "negative"
    return "neutral"


def _source_tier(source: str, provider: str) -> str:
    sl = source.lower()
    if provider == "benzinga":
        return "medium"
    for name in _SOURCE_TIERS["high"]:
        if name in sl:
            return "high"
    for name in _SOURCE_TIERS["medium"]:
        if name in sl:
            return "medium"
    return "low"


# ── Impact scoring ─────────────────────────────────────────────────────────────

def _score_impact(item: dict) -> int:
    """
    0-100 composite impact score.
    Freshness 30 + Catalyst 30 + Source 20 + Sentiment 15 + Ticker match 5.
    """
    score = 0

    age = item.get("age_mins", 999)
    if   age < 15:   score += 30
    elif age < 30:   score += 22
    elif age < 60:   score += 14
    elif age < 120:  score += 6

    cat_pts = {
        "earnings": 30, "fda": 30, "halt": 28, "ma": 25,
        "analyst": 20, "guidance": 20, "ipo": 18,
        "split": 14, "insider": 12, "general": 4,
    }
    score += cat_pts.get(item.get("catalyst_type", "general"), 4)

    tier = _source_tier(item.get("source", ""), item.get("provider", ""))
    score += {"high": 20, "medium": 14, "low": 8}.get(tier, 8)

    score += 15 if item.get("sentiment") in ("positive", "negative") else 3

    score += 5   # direct ticker relevance (all items are per-ticker)

    return min(score, 100)


# ── Deduplication ──────────────────────────────────────────────────────────────

def _similarity(a: str, b: str) -> float:
    """Jaccard word-overlap between two headlines."""
    wa = set(a.lower().split())
    wb = set(b.lower().split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def _deduplicate(items: list[dict]) -> tuple[list[dict], int]:
    """
    Remove near-duplicates (same ticker + timestamp within 10 min + high similarity).
    Returns (kept_items, n_removed).
    """
    kept    = []
    removed = 0
    for item in items:
        is_dup = False
        for k in kept:
            if k["ticker"] != item["ticker"]:
                continue
            if abs(k.get("timestamp", 0) - item.get("timestamp", 0)) > 600:
                continue
            if _similarity(k["headline"], item["headline"]) >= NEWS_DEDUP_SIMILARITY_THRESHOLD:
                if item.get("impact_score", 0) > k.get("impact_score", 0):
                    kept.remove(k)
                    removed += 1
                    break
                is_dup = True
                removed += 1
                break
        if not is_dup:
            kept.append(item)
    return kept, removed


# ── Provider fetch functions ───────────────────────────────────────────────────

def _fetch_finnhub(symbol: str, since_ts: int) -> list[dict]:
    try:
        fc    = finnhub.Client(api_key=FINNHUB_API_KEY)
        today = date.today().isoformat()
        yest  = (date.today() - timedelta(days=1)).isoformat()
        raw   = fc.company_news(symbol, _from=yest, to=today)
        now   = time.time()
        out   = []
        for it in raw:
            ts = int(it.get("datetime", 0))
            if ts < since_ts:
                continue
            headline = it.get("headline", "")
            out.append({
                "provider":      "finnhub",
                "ticker":        symbol,
                "headline":      headline,
                "timestamp":     ts,
                "age_mins":      round((now - ts) / 60, 1) if ts > 0 else 999,
                "source":        it.get("source", ""),
                "url":           it.get("url", ""),
                "catalyst_type": _detect_catalyst(headline),
                "sentiment":     _detect_sentiment(headline),
            })
        return out
    except Exception:
        return []


def _fetch_benzinga(symbols: list[str], since_ts: int) -> list[dict]:
    if not BENZINGA_API_KEY:
        return []
    try:
        r = requests.get(
            _BENZINGA_BASE,
            params={
                "apiKey":        BENZINGA_API_KEY,
                "tickers":       ",".join(symbols),
                "displayOutput": "headline",
                "pageSize":      50,
            },
            timeout=8,
        )
        if r.status_code != 200:
            return []
        now = time.time()
        out = []
        for it in r.json():
            created = it.get("created", "")
            try:
                ts = int(
                    datetime.strptime(created, "%Y-%m-%d %H:%M:%S")
                    .replace(tzinfo=timezone.utc)
                    .timestamp()
                )
            except Exception:
                ts = 0
            if ts < since_ts:
                continue
            headline = it.get("title", "")
            tickers  = [t.get("name", "") for t in it.get("tickers", [])]
            for ticker in (tickers if tickers else symbols):
                out.append({
                    "provider":      "benzinga",
                    "ticker":        ticker,
                    "headline":      headline,
                    "timestamp":     ts,
                    "age_mins":      round((now - ts) / 60, 1) if ts > 0 else 999,
                    "source":        "Benzinga",
                    "url":           it.get("url", ""),
                    "catalyst_type": _detect_catalyst(headline),
                    "sentiment":     _detect_sentiment(headline),
                })
        return out
    except Exception:
        return []


# ── Public API ────────────────────────────────────────────────────────────────

def get_all_news(
    symbols: list[str],
    lookback_mins: int | None = None,
) -> tuple[list[dict], dict]:
    """
    Fetch, deduplicate, and score news from Finnhub + Benzinga.
    Returns (items_sorted_by_impact_desc, stats_dict).
    stats_dict keys: raw_count, dedup_removed, providers_used.
    """
    lb       = lookback_mins or NEWS_LOOKBACK_MINS
    since_ts = int(time.time() - lb * 60)

    raw: list[dict] = []
    for sym in symbols:
        raw.extend(_fetch_finnhub(sym, since_ts))
    raw.extend(_fetch_benzinga(symbols, since_ts))

    raw_count = len(raw)

    # Score before dedup (keeps higher-scored duplicate)
    for item in raw:
        item["impact_score"] = _score_impact(item)

    deduped, n_removed = _deduplicate(raw)
    deduped.sort(key=lambda x: x.get("impact_score", 0), reverse=True)

    providers = ["finnhub"]
    if BENZINGA_API_KEY:
        providers.append("benzinga")

    stats = {
        "raw_count":     raw_count,
        "dedup_removed": n_removed,
        "final_count":   len(deduped),
        "providers_used": providers,
    }
    return deduped, stats


def news_for_symbol(symbol: str, items: list[dict]) -> list[dict]:
    """Filter a news list to items for a specific ticker."""
    return [i for i in items if i.get("ticker") == symbol]
