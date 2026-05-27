"""
Pre-market gapper scanner — supplements the fixed watchlist with dynamic movers.

Runs at research time (~9:00 ET). Batch-downloads GAPPER_UNIVERSE via yfinance,
finds stocks gapping > GAPPER_MIN_GAP_PCT with volume activity, saves top N to
gappers_today.json.

scanner.py injects these symbols into every scan so the agent catches
"stock of the day" moves that are outside the permanent watchlist.
"""
import json
import yfinance as yf
import pandas as pd
from datetime import date, datetime, timezone
from config import GAPPER_UNIVERSE, GAPPER_MIN_GAP_PCT, GAPPER_TOP_N, GAPPER_CACHE_FILE, GAPPER_REFRESH_INTERVAL_MINS


def _load_cache() -> list[str] | None:
    try:
        if GAPPER_CACHE_FILE.exists():
            data = json.loads(GAPPER_CACHE_FILE.read_text())
            if data.get("date") == date.today().isoformat():
                return data["symbols"]
    except Exception:
        pass
    return None


def _save_cache(symbols: list[str], details: list[dict]):
    GAPPER_CACHE_FILE.write_text(json.dumps({
        "date":             date.today().isoformat(),
        "last_refreshed_at": datetime.now(timezone.utc).isoformat(),
        "symbols":          symbols,
        "details":          details,
    }, indent=2))


def run_gapper_scan(force: bool = False) -> list[str]:
    """
    Scan GAPPER_UNIVERSE for today's top gappers. Returns symbol list.
    Cached per calendar day; pass force=True to re-scan.
    """
    if not force:
        cached = _load_cache()
        if cached is not None:
            print(f"   [GAPPER] Cache fresh — {len(cached)} gappers"
                  + (f": {', '.join(cached)}" if cached else " (none above threshold)"))
            return cached

    print(f"   [GAPPER] Scanning {len(GAPPER_UNIVERSE)} symbols for gap >= {GAPPER_MIN_GAP_PCT}%...")
    try:
        raw = yf.download(
            GAPPER_UNIVERSE,
            period="3d",
            interval="1d",
            progress=False,
            auto_adjust=True,
        )
    except Exception as e:
        print(f"   [GAPPER] Download failed: {e}")
        _save_cache([], [])
        return []

    results = []
    is_multi = isinstance(raw.columns, pd.MultiIndex)

    for sym in GAPPER_UNIVERSE:
        try:
            if is_multi:
                close = raw["Close"][sym].dropna()
                vol   = raw["Volume"][sym].dropna()
                opn   = raw["Open"][sym].dropna()
            else:
                close = raw["Close"].dropna()
                vol   = raw["Volume"].dropna()
                opn   = raw["Open"].dropna()

            if len(close) < 2:
                continue

            prev_close  = float(close.iloc[-2])
            today_price = float(opn.iloc[-1]) if len(opn) > 0 else float(close.iloc[-1])
            today_vol   = int(vol.iloc[-1]) if len(vol) > 0 else 0
            avg_vol     = float(vol.iloc[:-1].mean()) if len(vol) > 1 else 0

            if prev_close <= 0 or today_price <= 0:
                continue

            gap_pct   = (today_price - prev_close) / prev_close * 100
            vol_ratio = today_vol / avg_vol if avg_vol > 0 else 0

            if gap_pct < GAPPER_MIN_GAP_PCT:
                continue

            score = gap_pct * max(vol_ratio, 0.3)
            results.append({
                "symbol":    sym,
                "gap_pct":   round(gap_pct, 2),
                "vol_ratio": round(vol_ratio, 2),
                "price":     round(today_price, 2),
                "score":     round(score, 2),
            })
        except Exception:
            continue

    results.sort(key=lambda x: -x["score"])
    top     = results[:GAPPER_TOP_N]
    symbols = [r["symbol"] for r in top]

    if symbols:
        parts = ", ".join(
            f"{r['symbol']} {r['gap_pct']:+.1f}% vol={r['vol_ratio']:.1f}x"
            for r in top
        )
        print(f"   [GAPPER] Top gappers: {parts}")
    else:
        print(f"   [GAPPER] No gappers above {GAPPER_MIN_GAP_PCT}% threshold today.")

    _save_cache(symbols, top)
    return symbols


def get_daily_gappers() -> list[str]:
    """Return today's gapper symbols from cache. Empty list if scan not yet run."""
    cached = _load_cache()
    return cached if cached is not None else []


def refresh_gappers_intraday(interval_mins: int | None = None) -> list[str]:
    """
    Lightweight intraday refresh — only re-scans if cache is older than interval_mins.
    Called at the start of each --scan cycle to keep gapper list current.
    Returns updated symbol list (may be same as before if cache still fresh).
    """
    ivl = interval_mins or GAPPER_REFRESH_INTERVAL_MINS
    try:
        if GAPPER_CACHE_FILE.exists():
            data = json.loads(GAPPER_CACHE_FILE.read_text())
            if data.get("date") == date.today().isoformat():
                refreshed_at = data.get("last_refreshed_at")
                if refreshed_at:
                    age_mins = (
                        datetime.now(timezone.utc)
                        - datetime.fromisoformat(refreshed_at)
                    ).total_seconds() / 60
                    if age_mins < ivl:
                        return data["symbols"]
    except Exception:
        pass
    print(f"   [GAPPER] Intraday refresh (>{ivl}min since last scan)...")
    return run_gapper_scan(force=True)
