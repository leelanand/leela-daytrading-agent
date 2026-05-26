"""
Scans watchlist for intraday momentum — gap-ups, volume spikes, news catalysts.
Returns enriched candidates with spread, volatility, sector, and bid/ask data.
"""
import finnhub
import yfinance as yf
from datetime import date, timedelta
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockSnapshotRequest
from datetime import datetime
from zoneinfo import ZoneInfo
from config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, FINNHUB_API_KEY, WATCHLIST,
    MIN_GAP_PCT, MIN_REL_VOLUME, MIN_VOLUME_DAILY, MAX_SPREAD_PCT,
    MAX_MOVE_BEFORE_ENTRY_PCT, MIN_VOLUME_TREND_RATIO, VWAP_PREFERENCE,
)

ET = ZoneInfo("America/New_York")


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


def _sector(symbol: str) -> str:
    try:
        info = yf.Ticker(symbol).info
        return info.get("sector", "Unknown") or "Unknown"
    except Exception:
        return "Unknown"


def _news(symbol: str) -> list[str]:
    try:
        fc        = finnhub.Client(api_key=FINNHUB_API_KEY)
        today     = date.today().isoformat()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        items     = fc.company_news(symbol, _from=yesterday, to=today)
        return [n["headline"] for n in items[:3]]
    except Exception:
        return []


def scan_for_candidates() -> list[dict]:
    """
    Return top momentum candidates sorted by gap * rel_volume.
    Each dict: symbol, price, prev_close, gap_pct, rel_volume, today_volume,
               bid, ask, spread_pct, volatility_pct, sector, news, has_news.
    """
    print(f"   Scanning {len(WATCHLIST)} symbols...")
    snaps      = _snapshots(WATCHLIST)
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
            if gap_pct < MIN_GAP_PCT:
                continue

            if today_vol < MIN_VOLUME_DAILY:
                continue

            avg_vol    = _avg_volume(symbol)
            rel_volume = today_vol / avg_vol if avg_vol and avg_vol > 0 else 0
            if rel_volume < MIN_REL_VOLUME:
                continue

            # Bid/ask spread
            bid        = float(snap.latest_quote.bid_price) if snap.latest_quote else price * 0.999
            ask        = float(snap.latest_quote.ask_price) if snap.latest_quote else price * 1.001
            spread_pct = (ask - bid) / price * 100 if price > 0 else 99.0

            if spread_pct > MAX_SPREAD_PCT:
                continue

            # Intraday volatility (daily range) and open price
            day_high    = float(snap.daily_bar.high)  if snap.daily_bar else price
            day_low     = float(snap.daily_bar.low)   if snap.daily_bar else price
            open_price  = float(snap.daily_bar.open)  if snap.daily_bar else prev_close
            vwap        = float(snap.daily_bar.vwap)  if (snap.daily_bar and hasattr(snap.daily_bar, "vwap") and snap.daily_bar.vwap) else price
            volatility_pct = (day_high - day_low) / price * 100 if price > 0 else 0.0

            # Quality filter 1: chased-move check — skip if price already ran too far from open
            move_from_open = (price - open_price) / open_price * 100 if open_price > 0 else 0.0
            if move_from_open > MAX_MOVE_BEFORE_ENTRY_PCT:
                continue

            # Quality filter 2: volume trend — project full-day volume and check vs average
            now_et      = datetime.now(ET)
            market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
            mins_elapsed = max(1, (now_et - market_open).total_seconds() / 60)
            projected_vol = today_vol * (390 / mins_elapsed)
            vol_trend_ratio = projected_vol / avg_vol if avg_vol and avg_vol > 0 else 1.0
            if vol_trend_ratio < MIN_VOLUME_TREND_RATIO:
                continue

            # VWAP context flag (not a hard filter — passed to analyst)
            below_vwap = price < vwap if VWAP_PREFERENCE else False

            sector = _sector(symbol)
            news   = _news(symbol)

            candidates.append({
                "symbol":           symbol,
                "price":            round(price, 2),
                "prev_close":       round(prev_close, 2),
                "open_price":       round(open_price, 2),
                "gap_pct":          round(gap_pct, 2),
                "move_from_open":   round(move_from_open, 2),
                "rel_volume":       round(rel_volume, 2),
                "today_volume":     today_vol,
                "vol_trend_ratio":  round(vol_trend_ratio, 2),
                "bid":              round(bid, 2),
                "ask":              round(ask, 2),
                "spread_pct":       round(spread_pct, 3),
                "volatility_pct":   round(volatility_pct, 2),
                "vwap":             round(vwap, 2),
                "below_vwap":       below_vwap,
                "sector":           sector,
                "news":             news,
                "has_news":         len(news) > 0,
            })
        except Exception:
            continue

    candidates.sort(key=lambda x: x["gap_pct"] * x["rel_volume"], reverse=True)
    print(f"   Found {len(candidates)} momentum candidates "
          f"(gap >{MIN_GAP_PCT}%, vol >{MIN_REL_VOLUME}x, spread <{MAX_SPREAD_PCT}%)")
    return candidates[:10]
