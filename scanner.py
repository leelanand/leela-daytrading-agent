"""
Scans watchlist for intraday momentum — gap-ups, volume spikes, news catalysts.
Returns enriched candidates with spread, volatility, sector, and bid/ask data.
"""
import finnhub
import yfinance as yf
from datetime import date, timedelta, datetime
from ib_insync import Stock
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockSnapshotRequest
from zoneinfo import ZoneInfo
from ibkr_client import get_ib
from config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, FINNHUB_API_KEY, WATCHLIST,
    MIN_GAP_PCT, MIN_REL_VOLUME, MIN_VOLUME_DAILY, MAX_SPREAD_PCT,
    MAX_MOVE_BEFORE_ENTRY_PCT, MIN_VOLUME_TREND_RATIO, VWAP_PREFERENCE,
)
from gapper import get_daily_gappers

ET = ZoneInfo("America/New_York")


def _is_afternoon() -> bool:
    return datetime.now(ET).hour >= 12

def _afternoon_min_rvol() -> float:
    return 1.0 if _is_afternoon() else MIN_REL_VOLUME

def _is_afternoon_continuation(price: float, vwap: float, day_high: float,
                                open_price: float, rel_volume: float) -> bool:
    """True if current data fits a VWAP reclaim, ORB continuation, or HOD breakout pattern."""
    if not _is_afternoon() or price <= 0:
        return False
    vwap_is_real = vwap > 0 and abs(vwap - price) / price > 0.0001
    hod_gap_pct  = (day_high - price) / price * 100 if day_high > 0 else 99
    move_pct     = (price - open_price) / open_price * 100 if open_price > 0 else 0
    if vwap_is_real:
        vwap_gap_pct = (price - vwap) / vwap * 100
        if 0 <= vwap_gap_pct <= 0.75 and rel_volume >= 0.7:
            return True
    if move_pct >= 2.0 and hod_gap_pct <= 0.5 and rel_volume >= 0.7:
        return True
    if hod_gap_pct <= 0.3 and rel_volume >= 0.8:
        return True
    return False


def _ibkr_snapshots(symbols: list[str]) -> dict:
    """Fetch real-time Level 1 snapshots from IB Gateway — real NBBO bid/ask."""
    ib = get_ib()
    contracts = [Stock(s, "SMART", "USD") for s in symbols]
    ib.qualifyContracts(*contracts)
    tickers = ib.reqTickers(*contracts)
    ib.sleep(1)
    return {t.contract.symbol: t for t in tickers}


def _snapshots(symbols: list) -> dict:
    """Alpaca snapshot fallback — used when IB Gateway is unavailable."""
    client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    try:
        return client.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=symbols))
    except Exception:
        return {}


def _normalize_ibkr(ticker, symbol: str) -> dict | None:
    """Convert IBKR ticker to normalised market data dict."""
    try:
        price = float(ticker.last or ticker.close or 0)
        if price <= 0:
            return None
        hist_yf = yf.Ticker(symbol).history(period="2d")
        if len(hist_yf) < 2:
            return None
        prev_close = float(hist_yf["Close"].iloc[-2])
        raw_vwap   = getattr(ticker, "vwap", None)
        return {
            "price":      price,
            "prev_close": prev_close,
            "today_vol":  int(ticker.volume or 0),
            "bid":        float(ticker.bid or price * 0.999),
            "ask":        float(ticker.ask or price * 1.001),
            "day_high":   float(ticker.high or price),
            "day_low":    float(ticker.low  or price),
            "open_price": float(ticker.open or prev_close),
            "vwap":       float(raw_vwap) if raw_vwap and float(raw_vwap) > 0 else 0.0,
            "data_src":   "ibkr",
        }
    except Exception:
        return None


def _normalize_alpaca(snap, symbol: str) -> dict | None:
    """Convert Alpaca snapshot to normalised market data dict."""
    try:
        price      = float(snap.latest_trade.price)
        prev_close = float(snap.previous_daily_bar.close)
        raw_vwap   = snap.daily_bar.vwap if (snap.daily_bar and hasattr(snap.daily_bar, "vwap")) else None
        return {
            "price":      price,
            "prev_close": prev_close,
            "today_vol":  int(snap.daily_bar.volume) if snap.daily_bar else 0,
            "bid":        float(snap.latest_quote.bid_price) if snap.latest_quote else price * 0.999,
            "ask":        float(snap.latest_quote.ask_price) if snap.latest_quote else price * 1.001,
            "day_high":   float(snap.daily_bar.high)  if snap.daily_bar else price,
            "day_low":    float(snap.daily_bar.low)   if snap.daily_bar else price,
            "open_price": float(snap.daily_bar.open)  if snap.daily_bar else prev_close,
            "vwap":       float(raw_vwap) if raw_vwap and float(raw_vwap) > 0 else 0.0,
            "data_src":   "alpaca",
        }
    except Exception:
        return None


def _avg_volume(symbol: str, days: int = 10) -> tuple[float | None, int | None]:
    """Returns (avg_daily_volume, today_volume) using consolidated market data from yfinance."""
    try:
        hist = yf.Ticker(symbol).history(period=f"{days + 1}d")
        if len(hist) < 3:
            return None, None
        today = date.today()
        today_vol_yf = None
        past_vols: list[float] = []
        for ts, row in hist.iterrows():
            d = ts.date() if hasattr(ts, "date") else ts
            if d == today:
                today_vol_yf = int(row["Volume"])
            else:
                past_vols.append(float(row["Volume"]))
        base = past_vols if len(past_vols) >= 3 else list(hist["Volume"].astype(float))
        return sum(base) / len(base), today_vol_yf
    except Exception:
        return None, None


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


def _classify_setup(gap_pct: float, rel_volume: float, has_news: bool, vol_trend_ratio: float,
                    price: float = 0.0, vwap: float = 0.0,
                    day_high: float = 0.0, open_price: float = 0.0) -> str:
    """Classify the type of momentum setup for analytics and expectancy tracking."""
    if _is_afternoon() and price > 0:
        vwap_is_real = vwap > 0 and abs(vwap - price) / price > 0.0001
        hod_gap_pct  = (day_high - price) / price * 100 if day_high > 0 else 99
        move_pct     = (price - open_price) / open_price * 100 if open_price > 0 else 0
        if vwap_is_real:
            vwap_gap_pct = (price - vwap) / vwap * 100
            if 0 <= vwap_gap_pct <= 0.75 and rel_volume >= 0.7:
                return "vwap_reclaim"
        if move_pct >= 2.0 and hod_gap_pct <= 0.5 and rel_volume >= 0.7:
            return "orb_continuation"
        if hod_gap_pct <= 0.3 and rel_volume >= 0.8:
            return "hod_breakout"
    if gap_pct >= 2.5 and has_news and rel_volume >= 2.0:
        return "gap_and_go"
    if rel_volume >= 3.0 and not has_news:
        return "vol_spike"
    if has_news and gap_pct >= 1.5:
        return "news_momentum"
    return "trend_continuation"


def scan_for_candidates() -> list[dict]:
    """
    Return top momentum candidates sorted by gap * rel_volume.
    Primary data source: IBKR real-time NBBO via IB Gateway.
    Fallback: Alpaca snapshots (used if IB Gateway unreachable).
    """
    gappers  = get_daily_gappers()
    universe = list(dict.fromkeys(WATCHLIST + gappers))
    extra    = [s for s in gappers if s not in WATCHLIST]

    # Primary: IBKR real-time; fallback: Alpaca snapshots
    try:
        raw_snaps = _ibkr_snapshots(universe)
        normalize = lambda sym, s: _normalize_ibkr(s, sym)
        src_label = "IBKR"
    except Exception as _e:
        print(f"   [SCAN] IBKR unavailable ({_e}) — falling back to Alpaca snapshots")
        raw_snaps = _snapshots(universe)
        normalize = lambda sym, s: _normalize_alpaca(s, sym)
        src_label = "Alpaca"

    print(f"   Scanning {len(universe)} symbols ({src_label})"
          + (f" + {len(extra)} gappers: {', '.join(extra)}" if extra else "") + "...")
    candidates = []

    for symbol in universe:
        raw = raw_snaps.get(symbol)
        if not raw:
            continue
        try:
            md = normalize(symbol, raw)
            if not md:
                continue

            price      = md["price"]
            prev_close = md["prev_close"]
            today_vol  = md["today_vol"]
            bid        = md["bid"]
            ask        = md["ask"]
            day_high   = md["day_high"]
            day_low    = md["day_low"]
            open_price = md["open_price"]
            vwap       = md["vwap"]

            gap_pct = (price - prev_close) / prev_close * 100
            if gap_pct < MIN_GAP_PCT:
                continue

            avg_vol, yf_today_vol = _avg_volume(symbol)
            effective_vol = max(today_vol, yf_today_vol or 0)

            if effective_vol < MIN_VOLUME_DAILY:
                continue

            now_et          = datetime.now(ET)
            market_open     = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
            mins_elapsed    = max(1, (now_et - market_open).total_seconds() / 60)
            projected_vol   = effective_vol * (390 / mins_elapsed)
            vol_trend_ratio = projected_vol / avg_vol if avg_vol and avg_vol > 0 else 0
            rel_volume      = effective_vol / avg_vol if avg_vol and avg_vol > 0 else 0
            if vol_trend_ratio < _afternoon_min_rvol():
                continue

            spread_pct = (ask - bid) / price * 100 if price > 0 else 99.0
            if spread_pct > MAX_SPREAD_PCT:
                continue

            volatility_pct = (day_high - day_low) / price * 100 if price > 0 else 0.0
            move_from_open = (price - open_price) / open_price * 100 if open_price > 0 else 0.0
            afternoon_cont = _is_afternoon_continuation(price, vwap, day_high, open_price, rel_volume)
            if not afternoon_cont and move_from_open > MAX_MOVE_BEFORE_ENTRY_PCT:
                continue

            below_vwap = price < vwap if (VWAP_PREFERENCE and vwap > 0) else False
            sector     = _sector(symbol)
            news       = _news(symbol)
            setup_type = _classify_setup(gap_pct, rel_volume, len(news) > 0, vol_trend_ratio,
                                         price, vwap, day_high, open_price)

            candidates.append({
                "symbol":             symbol,
                "price":              round(price, 2),
                "prev_close":         round(prev_close, 2),
                "open_price":         round(open_price, 2),
                "gap_pct":            round(gap_pct, 2),
                "move_from_open":     round(move_from_open, 2),
                "rel_volume":         round(rel_volume, 2),
                "today_volume":       effective_vol,
                "vol_trend_ratio":    round(vol_trend_ratio, 2),
                "bid":                round(bid, 2),
                "ask":                round(ask, 2),
                "spread_pct":         round(spread_pct, 3),
                "volatility_pct":     round(volatility_pct, 2),
                "vwap":               round(vwap, 2),
                "below_vwap":         below_vwap,
                "sector":             sector,
                "news":               news,
                "has_news":           len(news) > 0,
                "setup_type":         setup_type,
                "is_afternoon_setup": afternoon_cont,
                "_is_top_gapper":     symbol in gappers,
                "_data_src":          md["data_src"],
            })
        except Exception:
            continue

    candidates.sort(key=lambda x: x["gap_pct"] * x["rel_volume"], reverse=True)
    min_rvol_used = _afternoon_min_rvol()
    print(f"   Found {len(candidates)} momentum candidates "
          f"(gap >{MIN_GAP_PCT}%, vol >{min_rvol_used:.1f}x, spread <{MAX_SPREAD_PCT}%)")
    return candidates[:10]
