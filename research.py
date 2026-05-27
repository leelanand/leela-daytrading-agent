"""
Pre-market research agent — runs at 9:00 ET (14:00 BST) on the full watchlist.

Gathers per-symbol fundamentals (short interest, float, analyst ratings,
52-week range position) and macro context, then uses Claude to synthesise
a research brief and directional bias per symbol.

The analyst reads this cache at prescan/scan time and includes the brief
in Claude's scoring context, raising or lowering scores accordingly.

Saved to research_cache.json. Expires after RESEARCH_CACHE_HOURS.
"""
import json
import finnhub
import yfinance as yf
import anthropic
from datetime import datetime, timezone, date
from config import (
    ANTHROPIC_API_KEY, FINNHUB_API_KEY, WATCHLIST,
    RESEARCH_CACHE_FILE, RESEARCH_CACHE_HOURS,
)

_claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

RESEARCH_SYSTEM = """You are a pre-market research analyst preparing context for an intraday day trader.

For each stock, review the fundamentals, short interest, analyst ratings, and price context.
Generate a concise brief that helps evaluate whether this stock is a good intraday trade candidate today.

Key factors to assess:
- Float: <50M shares = highly volatile, can move fast on catalyst
- Short ratio >5 = short squeeze potential if catalyst present
- near_52w_high = breakout zone (bullish if volume confirms)
- near_52w_low = avoid longs — downtrend
- Analyst rating: strong_buy/buy = tailwind; sell/strong_sell = headwind
- target_price vs current price: >15% upside = analyst backing

Respond with a JSON array only — no markdown, no text outside the array.
Each element: {
  "symbol": "X",
  "research_brief": "2-3 sentences: key facts relevant to today's trade",
  "pre_market_bias": "BULLISH" | "NEUTRAL" | "BEARISH" | "AVOID",
  "research_score": 0-100,
  "red_flags": ["list of concerns, empty if none"]
}

research_score: 100 = ideal intraday candidate fundamentally, 0 = hard avoid.
pre_market_bias AVOID = cap analyst score at 50 regardless of momentum."""


# ── Cache ─────────────────────────────────────────────────────────────────────

def _load_cache() -> dict | None:
    try:
        if RESEARCH_CACHE_FILE.exists():
            data     = json.loads(RESEARCH_CACHE_FILE.read_text())
            saved    = datetime.fromisoformat(data["generated_at"])
            age_h    = (datetime.now(timezone.utc) - saved).total_seconds() / 3600
            if age_h < RESEARCH_CACHE_HOURS:
                return data
    except Exception:
        pass
    return None


def _save_cache(data: dict):
    RESEARCH_CACHE_FILE.write_text(json.dumps(data, indent=2))


# ── Data fetchers ─────────────────────────────────────────────────────────────

def _fetch_symbol_data(symbol: str) -> dict:
    """Fetch fundamentals for one symbol via yfinance. Silent on failure."""
    out = {"symbol": symbol}
    try:
        fi = yf.Ticker(symbol).fast_info
        out["price"]      = round(float(fi.last_price or 0), 2)
        out["prev_close"] = round(float(fi.previous_close or 0), 2)
        out["52w_high"]   = round(float(fi.fifty_two_week_high or 0), 2)
        out["52w_low"]    = round(float(fi.fifty_two_week_low or 0), 2)
    except Exception:
        pass
    try:
        info = yf.Ticker(symbol).info
        out["short_ratio"]    = round(float(info.get("shortRatio")    or 0), 2)
        out["float_shares_m"] = round(float(info.get("floatShares")   or 0) / 1e6, 1)
        out["shares_short_m"] = round(float(info.get("sharesShort")   or 0) / 1e6, 1)
        out["analyst_rating"] = info.get("recommendationKey", "none") or "none"
        out["target_price"]   = round(float(info.get("targetMeanPrice") or 0), 2)
        out["market_cap_b"]   = round(float(info.get("marketCap")     or 0) / 1e9, 2)
    except Exception:
        pass
    try:
        price = out.get("price", 0)
        hi    = out.get("52w_high", 0)
        lo    = out.get("52w_low", 0)
        if hi > lo > 0 and price > 0:
            pct = (price - lo) / (hi - lo) * 100
            out["pct_of_52w_range"] = round(pct, 1)
            out["near_52w_high"]    = pct >= 85
            out["near_52w_low"]     = pct <= 15
    except Exception:
        pass
    return out


def _macro_context() -> dict:
    """VIX, SPY/QQQ current levels, and today's high-impact economic events."""
    macro = {}
    try:
        macro["vix"] = round(float(yf.Ticker("^VIX").fast_info.last_price), 2)
    except Exception:
        pass
    for sym in ("SPY", "QQQ", "IWM"):
        try:
            fi   = yf.Ticker(sym).fast_info
            prev = float(fi.previous_close or 0)
            last = float(fi.last_price or 0)
            if prev > 0:
                macro[f"{sym}_pct"] = round((last - prev) / prev * 100, 2)
        except Exception:
            pass
    try:
        fc     = finnhub.Client(api_key=FINNHUB_API_KEY)
        today  = date.today().isoformat()
        cal    = fc.economic_calendar()
        events = [
            e["event"] for e in cal.get("economicCalendar", [])
            if e.get("time", "")[:10] == today
            and e.get("impact", "") in ("high", "medium")
        ]
        macro["economic_events"] = events[:5]
    except Exception:
        macro["economic_events"] = []
    return macro


# ── Claude synthesis ──────────────────────────────────────────────────────────

def _synthesise(symbol_data: list[dict], macro: dict) -> list[dict]:
    vix     = macro.get("vix", "n/a")
    spy_pct = macro.get("SPY_pct", 0)
    qqq_pct = macro.get("QQQ_pct", 0)
    iwm_pct = macro.get("IWM_pct", 0)
    events  = macro.get("economic_events", [])

    macro_str = (
        f"VIX {vix}  SPY {spy_pct:+.2f}%  QQQ {qqq_pct:+.2f}%  IWM {iwm_pct:+.2f}%"
    )
    events_str = (", ".join(events)) if events else "none"

    prompt = (
        f"Today's macro: {macro_str}\n"
        f"High-impact economic events today: {events_str}\n\n"
        f"Generate pre-market research briefs for these stocks:\n"
        f"{json.dumps(symbol_data, indent=2)}\n\n"
        f"Return JSON array with research_brief, pre_market_bias, "
        f"research_score, red_flags for each symbol."
    )

    resp = _claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=6000,
        system=RESEARCH_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip()
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


# ── Public API ────────────────────────────────────────────────────────────────

def run_premarket_research() -> dict:
    """
    Main entry point — called by agent.py --research.
    Fetches data, synthesises with Claude, saves cache. Returns full result dict.
    Skips data fetch if cache is still fresh.
    """
    cached = _load_cache()
    if cached:
        n = len(cached.get("symbols", {}))
        print(f"   [RESEARCH] Cache fresh — {n} symbols already researched.")
        return cached

    print(f"   [RESEARCH] Fetching fundamentals for {len(WATCHLIST)} symbols...")
    macro = _macro_context()

    vix = macro.get("vix", "?")
    print(f"   [RESEARCH] Macro: VIX={vix}  "
          f"SPY={macro.get('SPY_pct', 0):+.2f}%  "
          f"QQQ={macro.get('QQQ_pct', 0):+.2f}%  "
          f"IWM={macro.get('IWM_pct', 0):+.2f}%")
    if macro.get("economic_events"):
        print(f"   [RESEARCH] Key events today: {', '.join(macro['economic_events'])}")

    symbol_data = []
    for sym in WATCHLIST:
        d = _fetch_symbol_data(sym)
        symbol_data.append(d)
        print(f"   [RESEARCH]   {sym:6s}  float={d.get('float_shares_m', '?')}M  "
              f"short_ratio={d.get('short_ratio', '?')}  "
              f"rating={d.get('analyst_rating', '?')}")

    print(f"   [RESEARCH] Synthesising {len(symbol_data)} symbols with Claude...")
    briefs    = _synthesise(symbol_data, macro)
    brief_map = {b["symbol"]: b for b in briefs}

    symbols_out = {}
    for d in symbol_data:
        sym = d["symbol"]
        b   = brief_map.get(sym, {})
        symbols_out[sym] = {
            **d,
            "research_brief":  b.get("research_brief", ""),
            "pre_market_bias": b.get("pre_market_bias", "NEUTRAL"),
            "research_score":  b.get("research_score", 50),
            "red_flags":       b.get("red_flags", []),
        }

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "macro":        macro,
        "symbols":      symbols_out,
    }
    _save_cache(result)

    bullish = sum(1 for s in symbols_out.values() if s.get("pre_market_bias") == "BULLISH")
    avoid   = sum(1 for s in symbols_out.values() if s.get("pre_market_bias") in ("BEARISH", "AVOID"))
    print(f"   [RESEARCH] Done — BULLISH:{bullish}  NEUTRAL:{len(symbols_out)-bullish-avoid}  "
          f"BEARISH/AVOID:{avoid}")

    bullish_syms = [s for s, d in symbols_out.items() if d.get("pre_market_bias") == "BULLISH"]
    avoid_syms   = [s for s, d in symbols_out.items() if d.get("pre_market_bias") == "AVOID"]
    if bullish_syms:
        print(f"   [RESEARCH] Watch closely: {', '.join(bullish_syms)}")
    if avoid_syms:
        print(f"   [RESEARCH] Avoid today:   {', '.join(avoid_syms)}")

    return result


def get_research(symbol: str) -> dict:
    """Return cached research brief for a symbol. Empty dict if cache missing."""
    cached = _load_cache()
    if not cached:
        return {}
    return cached.get("symbols", {}).get(symbol, {})
