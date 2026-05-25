"""
Scans watchlist for intraday momentum — gap-ups, volume spikes, news catalysts.
"""
import finnhub
import yfinance as yf
from datetime import date, timedelta
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockSnapshotRequest
from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, FINNHUB_API_KEY, WATCHLIST, MIN_GAP_PCT, MIN_REL_VOLUME


def _snapshots(symbols: list) -> dict:
    client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    try:
        return client.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=symbols))
    except Exception:
        return {}


def _avg_volume(symbol: str, days: int = 10) -> float | None:
    try:
        hist = yf.Ticker(symbol).history(period=f"{days}d")
        return hist["Volume"].mean() if len(hist) >= 3 else None
    except Exception:
        return None


def _news(symbol: str) -> list[str]:
    try:
        fc = finnhub.Client(api_key=FINNHUB_API_KEY)
        today     = date.today().isoformat()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        items = fc.company_news(symbol, _from=yesterday, to=today)
        return [n["headline"] for n in items[:3]]
    except Exception:
        return []


def scan_for_candidates() -> list[dict]:
    """
    Return top momentum candidates sorted by gap * rel_volume.
    Each dict: symbol, price, prev_close, gap_pct, rel_volume, today_volume, news.
    """
    print(f"   Scanning {len(WATCHLIST)} symbols...")
    snaps = _snapshots(WATCHLIST)
    candidates = []

    for symbol in WATCHLIST:
        snap = snaps.get(symbol)
        if not snap:
            continue
        try:
            price      = float(snap.latest_trade.price)
            prev_close = float(snap.prev_daily_bar.close)
            today_vol  = int(snap.daily_bar.volume) if snap.daily_bar else 0

            gap_pct = (price - prev_close) / prev_close * 100

            # Only long / gap-up trades
            if gap_pct < MIN_GAP_PCT:
                continue

            avg_vol    = _avg_volume(symbol)
            rel_volume = today_vol / avg_vol if avg_vol and avg_vol > 0 else 0

            if rel_volume < MIN_REL_VOLUME:
                continue

            news = _news(symbol)

            candidates.append({
                "symbol":      symbol,
                "price":       round(price, 2),
                "prev_close":  round(prev_close, 2),
                "gap_pct":     round(gap_pct, 2),
                "rel_volume":  round(rel_volume, 2),
                "today_volume": today_vol,
                "news":        news,
            })
        except Exception:
            continue

    candidates.sort(key=lambda x: x["gap_pct"] * x["rel_volume"], reverse=True)
    print(f"   Found {len(candidates)} momentum candidates (gap >{MIN_GAP_PCT}%, vol >{MIN_REL_VOLUME}x)")
    return candidates[:10]
