"""
Massive.com / Polygon.io enhanced market data provider.

Massive.com = Polygon.io rebranded (same infrastructure, same API keys).
Stocks Starter plan ($29/month) — confirmed 2026-05-26:
  ✅ Daily historical bars  — /v2/aggs/ticker/{sym}/range/1/day/{from}/{to}
  ✅ Previous-day bar       — /v2/aggs/ticker/{sym}/prev
  ✅ Reference/meta data    — /v1/meta/exchanges
  ✅ Snapshot endpoint      — day/min OHLCV + VWAP (15-min delayed; no lastQuote)
  ✅ WebSocket AM.*         — wss://delayed.polygon.io/stocks; minute aggs + acc. volume
  ❌ WebSocket Q.*/T.*      — quotes and trades blocked on Starter (not authorized)
  ❌ Real-time bid/ask      — snapshot has no lastQuote; mid derived from min.c
  ❌ Real-time WS stream    — requires Stocks Advanced ($199/month)

All data on Starter plan is 15-minute delayed. Alpaca provides real-time prices for
execution; Massive/Polygon Starter contributes delayed RVOL + VWAP enrichment.
Upgrade to Stocks Advanced to unlock real-time Q.*/T.* and live bid/ask snapshots.

Provider hierarchy (configurable via PREFERRED_PROVIDER):
  1. Massive.com  — primary   (set MASSIVE_API_KEY)
  2. Polygon.io   — fallback  (set POLYGON_API_KEY)
  3. None         — graceful skip, trading continues with Alpaca alone

Capabilities:
  REST   — normalised snapshot: bid/ask/spread/VWAP/volume/timestamp
  WS     — burst-mode streaming → local JSON cache; auto-reconnect
  Analysis — RVOL, VWAP distance, spread stability, exhaustion, quality score

Modular interface contract (add future providers by implementing these):
  get_rest_quote(symbol)   → dict | None  (bid, ask, mid, spread_pct, volume, vwap, age_secs)
  stream_quotes_burst(symbols, duration_secs) → dict[symbol → quote_dict]

Future extensions documented but not yet wired:
  Databento, options-flow providers, dark-pool feeds, order-book feeds
"""

import json
import time
import threading
import requests
from datetime import datetime, timezone

try:
    import websocket as _ws_lib
    _WS_AVAILABLE = True
except ImportError:
    _WS_AVAILABLE = False

from config import (
    MASSIVE_API_KEY, POLYGON_API_KEY,
    PREFERRED_PROVIDER,
    MASSIVE_REST_BASE, POLYGON_REST_BASE,
    MASSIVE_WS_URL, POLYGON_WS_URL,
    ENABLE_WS_STREAMING, WS_STREAM_DURATION_SECS,
    WS_RECONNECT_DELAY_SECS, WS_MAX_RECONNECT,
    QUOTE_MAX_STALENESS_SECS,
    RVOL_STRONG_THRESHOLD, RVOL_MIN_THRESHOLD,
    VWAP_FAR_THRESHOLD_PCT, SPREAD_WIDEN_THRESHOLD_PCT,
    MIN_INTRADAY_QUALITY_SCORE,
    WS_CACHE_FILE,
)


# ── Provider selection ────────────────────────────────────────────────────────

def active_providers() -> list[str]:
    """Returns ordered list of available providers."""
    ordered = (
        ["massive", "polygon"] if PREFERRED_PROVIDER == "massive"
        else ["polygon", "massive"]
    )
    available = []
    if MASSIVE_API_KEY:
        available.append("massive")
    if POLYGON_API_KEY:
        available.append("polygon")
    # Return in preferred order, only available ones
    return [p for p in ordered if p in available]


def _api_key(provider: str) -> str:
    return MASSIVE_API_KEY if provider == "massive" else POLYGON_API_KEY


def _rest_base(provider: str) -> str:
    return MASSIVE_REST_BASE if provider == "massive" else POLYGON_REST_BASE


def _ws_url(provider: str) -> str:
    return MASSIVE_WS_URL if provider == "massive" else POLYGON_WS_URL


# ── REST snapshot ─────────────────────────────────────────────────────────────

def _parse_snapshot(ticker: dict) -> dict | None:
    """
    Parse a snapshot response into a normalised quote dict.

    Two supported formats:
      Polygon-format:  lastQuote.p (bid), lastQuote.P (ask) — real bid/ask
      Massive-format:  no lastQuote; min.c used as price proxy; no spread

    Keys returned: bid, ask, mid, spread_pct, volume, acc_volume, vwap,
                   age_secs, stale, has_realtime_quotes.
    """
    try:
        lq      = ticker.get("lastQuote", {})
        day     = ticker.get("day", {})
        min_bar = ticker.get("min", {})

        # Polygon convention: lowercase p = bid, uppercase P = ask
        bid = float(lq.get("p", 0) or 0)
        ask = float(lq.get("P", 0) or 0)
        has_rt = bid > 0 and ask > 0

        if has_rt:
            mid        = (bid + ask) / 2
            spread_pct = round((ask - bid) / mid * 100, 3) if mid > 0 else 99.0
            bid        = round(bid, 4)
            ask        = round(ask, 4)
            mid        = round(mid, 4)
            ts_raw   = int(lq.get("t", 0) or 0)
            ts_secs  = ts_raw / 1e9 if ts_raw > 1e12 else ts_raw / 1e3
            age_secs = time.time() - ts_secs if ts_secs > 0 else 9_999
        else:
            # Bar-data-only (Massive format): last minute close as price proxy
            price = float(min_bar.get("c", 0) or day.get("c", 0) or 0)
            if price <= 0:
                return None
            bid = ask = spread_pct = None
            mid = round(price, 4)
            # Use end of last minute bar as timestamp (min.t is bar-start in ms)
            min_t_ms   = int(min_bar.get("t", 0) or 0)
            updated_ns = int(ticker.get("updated", 0) or 0)
            if min_t_ms > 0:
                age_secs = max(0.0, time.time() - (min_t_ms / 1000 + 60))
            elif updated_ns > 0:
                age_secs = max(0.0, time.time() - updated_ns / 1e9)
            else:
                age_secs = 9_999

        volume     = int(day.get("v",  0) or 0)
        acc_volume = int(min_bar.get("av", 0) or 0)
        vwap       = float(day.get("vw", 0) or 0) or None

        return {
            "bid":                 bid,
            "ask":                 ask,
            "mid":                 mid,
            "spread_pct":          spread_pct,
            "volume":              volume,
            "acc_volume":          acc_volume or volume,
            "vwap":                round(vwap, 4) if vwap else None,
            "age_secs":            round(age_secs, 1),
            "stale":               has_rt and age_secs > QUOTE_MAX_STALENESS_SECS,
            "has_realtime_quotes": has_rt,
        }
    except Exception:
        return None


def _fetch_snapshot_rest(symbol: str, provider: str) -> dict | None:
    """Fetch normalised quote from a single provider via REST."""
    key  = _api_key(provider)
    base = _rest_base(provider)
    if not key:
        return None
    try:
        r = requests.get(
            f"{base}/v2/snapshot/locale/us/markets/stocks/tickers/{symbol}",
            params={"apiKey": key},
            timeout=5,
        )
        if r.status_code != 200:
            return None
        ticker = r.json().get("ticker")
        return _parse_snapshot(ticker) if ticker else None
    except Exception:
        return None


def get_rest_quote(symbol: str) -> dict | None:
    """
    Try preferred provider first, fall back to the other.
    Returns normalised quote dict or None if both unavailable.
    """
    for provider in active_providers():
        q = _fetch_snapshot_rest(symbol, provider)
        if q is not None:
            q["provider"] = provider
            return q
    return None


# ── In-memory + file quote cache ─────────────────────────────────────────────

_cache: dict[str, dict] = {}      # {symbol: {quote_fields..., cached_at}}
_cache_lock = threading.Lock()


def _load_ws_cache() -> dict:
    if not WS_CACHE_FILE.exists():
        return {}
    try:
        payload  = json.loads(WS_CACHE_FILE.read_text(encoding="utf-8"))
        saved_at = datetime.fromisoformat(payload.get("saved_at", "2000-01-01"))
        age_secs = (datetime.now(timezone.utc) - saved_at.replace(tzinfo=timezone.utc)).total_seconds()
        if age_secs > 300:   # 5-min TTL for cross-session cache
            return {}
        return payload.get("data", {})
    except Exception:
        return {}


def _save_ws_cache(data: dict):
    payload = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "data":     data,
    }
    WS_CACHE_FILE.write_text(json.dumps(payload, default=str), encoding="utf-8")


def get_cached_quote(symbol: str, max_age_secs: int = 30) -> dict | None:
    """Return in-memory cached quote if fresh, else try the persisted file cache."""
    with _cache_lock:
        entry = _cache.get(symbol)
        if entry:
            age = time.time() - entry.get("cached_at", 0)
            if age <= max_age_secs:
                return entry

    # Try file cache (populated by a previous --scan WS burst)
    file_cache = _load_ws_cache()
    entry = file_cache.get(symbol)
    if entry:
        age = time.time() - entry.get("cached_at", 0)
        if age <= max_age_secs:
            return entry
    return None


def _update_cache(symbol: str, data: dict):
    data["cached_at"] = time.time()
    with _cache_lock:
        _cache[symbol] = data


# ── WebSocket burst streaming ─────────────────────────────────────────────────

class _WSBurstSession:
    """
    Short-lived WebSocket session: connect → subscribe → collect → disconnect.
    Uses Polygon-compatible protocol (same format for Massive.com assumed).

    Message types handled:
      AM — per-minute aggregate (accumulated VWAP, volume, OHLC)
      Q  — NBBO quote (bid/ask in real-time)
      T  — trade (last price)
    """

    def __init__(self, symbols: list[str], provider: str, duration: int):
        self.symbols    = symbols
        self.provider   = provider
        self.api_key    = _api_key(provider)
        self.ws_url     = _ws_url(provider)
        self.duration   = duration
        self.data: dict[str, dict] = {s: {"bars": [], "quotes": [], "trades": []} for s in symbols}
        self._auth_done = threading.Event()
        self._done      = threading.Event()
        self._ws        = None

    def _on_open(self, ws):
        ws.send(json.dumps({"action": "auth", "params": self.api_key}))

    def _on_message(self, ws, message):
        try:
            msgs = json.loads(message)
        except Exception:
            return

        for msg in msgs:
            ev  = msg.get("ev")
            sym = msg.get("sym", "")

            if ev == "status":
                if msg.get("status") == "auth_success":
                    # AM = per-minute aggregates (works on Starter/delayed plan)
                    # Q + T = quotes/trades (requires Stocks Advanced; skipped here)
                    params = ",".join(f"AM.{s}" for s in self.symbols)
                    ws.send(json.dumps({"action": "subscribe", "params": params}))
                    self._auth_done.set()
                elif msg.get("status") == "auth_failed":
                    self._done.set()

            elif ev == "AM" and sym in self.data:   # per-minute aggregate
                bar = {
                    "o": msg.get("o"), "c": msg.get("c"),
                    "h": msg.get("h"), "l": msg.get("l"),
                    "v": msg.get("v"),  "av": msg.get("av"),
                    "vw": msg.get("vw"), "t": msg.get("s"),
                }
                self.data[sym]["bars"].append(bar)
                if msg.get("vw"):
                    self.data[sym]["vwap"] = float(msg["vw"])
                if msg.get("av"):
                    self.data[sym]["acc_volume"] = int(msg["av"])

            elif ev == "Q" and sym in self.data:    # NBBO quote
                q = {
                    "bid": msg.get("bp"), "ask": msg.get("ap"),
                    "bid_size": msg.get("bs"), "ask_size": msg.get("as"),
                    "t": msg.get("t"),
                }
                self.data[sym]["quotes"].append(q)
                if msg.get("bp") and msg.get("ap"):
                    self.data[sym]["bid"] = float(msg["bp"])
                    self.data[sym]["ask"] = float(msg["ap"])

            elif ev == "T" and sym in self.data:    # trade
                self.data[sym]["trades"].append({
                    "price": msg.get("p"), "size": msg.get("s"), "t": msg.get("t"),
                })
                if msg.get("p"):
                    self.data[sym]["last_trade"] = float(msg["p"])

    def _on_error(self, ws, error):
        pass  # reconnect logic handled by caller

    def _on_close(self, ws, code, msg):
        self._done.set()

    def run(self) -> dict:
        if not _WS_AVAILABLE or not self.api_key:
            return {}
        reconnects = 0
        while reconnects <= WS_MAX_RECONNECT:
            try:
                import ssl as _ssl
                self._ws = _ws_lib.WebSocketApp(
                    self.ws_url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                thread = threading.Thread(
                    target=self._ws.run_forever,
                    kwargs={
                        "ping_interval": 10,
                        # Polygon/Massive uses a self-signed cert on some hosts
                        "sslopt": {"cert_reqs": _ssl.CERT_NONE},
                    },
                    daemon=True,
                )
                thread.start()

                # Wait for auth then let data accumulate
                self._auth_done.wait(timeout=8)
                if not self._auth_done.is_set():
                    self._ws.close()
                    break  # auth failed — no point retrying

                self._done.wait(timeout=self.duration)
                self._ws.close()
                thread.join(timeout=3)
                break

            except Exception:
                reconnects += 1
                if reconnects <= WS_MAX_RECONNECT:
                    time.sleep(WS_RECONNECT_DELAY_SECS)

        return self.data


def stream_quotes_burst(
    symbols: list[str],
    duration_secs: int | None = None,
) -> dict:
    """
    Run a short WebSocket session and return streaming data by symbol.
    Falls back to REST if WS is unavailable or API key missing.
    Updates the local cache from results.

    Return dict per symbol: {bid, ask, vwap, acc_volume, bars, quotes, trades, cached_at}
    """
    dur       = duration_secs or WS_STREAM_DURATION_SECS
    providers = active_providers()

    if ENABLE_WS_STREAMING and _WS_AVAILABLE and providers:
        provider = providers[0]
        session  = _WSBurstSession(symbols, provider, dur)
        ws_data  = session.run()
        if ws_data:
            now = time.time()
            for sym, data in ws_data.items():
                data["cached_at"] = now
                _update_cache(sym, data)
            _save_ws_cache({sym: _cache.get(sym, {}) for sym in symbols})
            return ws_data

    # WS unavailable or no data — fall back to REST for each symbol
    result = {}
    for sym in symbols:
        q = get_rest_quote(sym)
        if q:
            q["cached_at"] = time.time()
            _update_cache(sym, q)
            result[sym] = q
    if result:
        _save_ws_cache(result)
    return result


# ── Historical daily bars (free-tier endpoint) ────────────────────────────────

def get_historical_stats(symbol: str, days: int = 10) -> dict:
    """
    Fetch recent daily bars to compute avg volume and prev-day close.
    Works with Massive/Polygon free tier. Returns {} on failure.

    Keys returned: avg_volume, prev_close, prev_vwap.
    Use avg_volume as the RVOL denominator for more reliable RVOL calculation.
    """
    from datetime import date, timedelta
    providers = active_providers()
    if not providers:
        return {}

    since = (date.today() - timedelta(days=days + 5)).isoformat()
    today = date.today().isoformat()

    for provider in providers:
        key  = _api_key(provider)
        base = _rest_base(provider)
        try:
            r = requests.get(
                f"{base}/v2/aggs/ticker/{symbol}/range/1/day/{since}/{today}",
                params={"apiKey": key, "adjusted": "true", "sort": "asc", "limit": days + 5},
                timeout=6,
            )
            if r.status_code != 200:
                continue
            results = r.json().get("results", [])
            if not results:
                continue

            # Use last `days` bars for avg volume (skip today's partial bar)
            bars     = results[:-1] if len(results) > 1 else results
            bars     = bars[-days:]
            avg_vol  = sum(b.get("v", 0) for b in bars) / len(bars) if bars else 0
            prev     = results[-2] if len(results) >= 2 else results[-1]

            return {
                "avg_volume": round(avg_vol),
                "prev_close": round(float(prev.get("c", 0)), 4),
                "prev_vwap":  round(float(prev.get("vw", 0)), 4),
                "provider":   provider,
                "bars_used":  len(bars),
            }
        except Exception:
            continue
    return {}


_hist_cache: dict[str, dict] = {}   # {symbol: {stats..., cached_at}}
_hist_lock = threading.Lock()


def get_historical_stats_cached(symbol: str, days: int = 10, ttl_secs: int = 3600) -> dict:
    """Cache-wrapped version of get_historical_stats (1-hour TTL per symbol)."""
    with _hist_lock:
        entry = _hist_cache.get(symbol)
        if entry and time.time() - entry.get("cached_at", 0) < ttl_secs:
            return entry
    stats = get_historical_stats(symbol, days)
    if stats:
        stats["cached_at"] = time.time()
        with _hist_lock:
            _hist_cache[symbol] = stats
    return stats


# ── Intraday analysis ─────────────────────────────────────────────────────────

def calculate_rvol(
    current_volume: int,
    avg_daily_volume: float,
    mins_elapsed: float,
) -> float:
    """
    Relative volume = current_vol / expected_vol_at_this_time.
    expected = avg_daily * (mins_elapsed / 390).
    Returns 0.0 if inputs invalid.
    """
    if avg_daily_volume <= 0 or mins_elapsed <= 0:
        return 0.0
    expected = avg_daily_volume * (mins_elapsed / 390)
    if expected <= 0:
        return 0.0
    return round(current_volume / expected, 2)


def get_vwap_distance_pct(price: float, vwap: float) -> float:
    """Signed % distance of price from VWAP. Positive = above VWAP."""
    if not vwap or vwap <= 0:
        return 0.0
    return round((price - vwap) / vwap * 100, 3)


def _check_spread_stability(
    current_spread: float,
    baseline_spread: float,
) -> tuple[bool, str]:
    """
    stable=True if spread hasn't widened beyond SPREAD_WIDEN_THRESHOLD_PCT.
    baseline is the prescan spread.
    """
    if baseline_spread <= 0:
        return True, "no_baseline"
    widening_pct = (current_spread - baseline_spread) / baseline_spread * 100
    if widening_pct > SPREAD_WIDEN_THRESHOLD_PCT:
        return False, (
            f"spread_widening: {current_spread:.3f}% vs baseline {baseline_spread:.3f}% "
            f"(+{widening_pct:.0f}%)"
        )
    return True, "stable"


def _detect_exhaustion(
    vwap_distance_pct: float,
    rvol: float,
    spread_stable: bool,
) -> tuple[bool, str]:
    """
    Flag exhaustion when: price far from VWAP + RVOL declining + spread widening.
    Requires at least two of three signals to trigger.
    """
    signals = []
    if abs(vwap_distance_pct) > VWAP_FAR_THRESHOLD_PCT:
        signals.append(f"far_from_vwap ({vwap_distance_pct:+.2f}%)")
    if rvol < RVOL_MIN_THRESHOLD:
        signals.append(f"low_rvol ({rvol:.2f}x)")
    if not spread_stable:
        signals.append("spread_widening")

    if len(signals) >= 2:
        return True, "exhaustion: " + " + ".join(signals)
    return False, "ok"


# ── Combined quality score ────────────────────────────────────────────────────

def get_intraday_quality(
    symbol: str,
    price: float,
    spread_pct: float,          # current spread from Alpaca
    volume: int,                # current day volume from Alpaca
    avg_daily_volume: float = 0,
    mins_elapsed: float = 60,
    baseline_spread: float = 0, # prescan spread for stability comparison
) -> dict:
    """
    Compute an intraday quality score 0-100.
    Sources: WS cache → REST snapshot → Alpaca-only (degraded).

    Returns:
    {
        ok: bool,               # False = reject trade due to poor quality
        score: int,             # 0-100
        rvol: float,
        vwap: float | None,
        vwap_distance_pct: float,
        spread_stable: bool,
        exhausted: bool,
        reason: str,
        data_source: str,       # 'ws_cache' | 'rest' | 'alpaca_only'
    }
    """
    # Try WS cache first (freshest), then REST, then Alpaca-only
    cached   = get_cached_quote(symbol, max_age_secs=30)
    rest_q   = None
    if not cached:
        rest_q = get_rest_quote(symbol)

    secondary = cached or rest_q
    data_src  = "ws_cache" if cached else ("rest" if rest_q else "alpaca_only")

    # Pull VWAP and volume from secondary provider if available
    vwap        = (secondary or {}).get("vwap")
    sec_volume  = (secondary or {}).get("acc_volume") or (secondary or {}).get("volume") or volume
    live_spread = (secondary or {}).get("spread_pct") or spread_pct

    # If avg_daily_volume not provided by caller, fetch from historical bars (free tier)
    if avg_daily_volume <= 0:
        hist = get_historical_stats_cached(symbol)
        avg_daily_volume = hist.get("avg_volume", 0)
        if avg_daily_volume and data_src == "alpaca_only" and hist.get("provider"):
            data_src = f"hist_bars ({hist['provider']})"

    # ── RVOL ─────────────────────────────────────────────────────────────────
    rvol = calculate_rvol(sec_volume, avg_daily_volume, mins_elapsed)

    # ── VWAP distance ─────────────────────────────────────────────────────────
    vwap_dist = get_vwap_distance_pct(price, vwap) if vwap else 0.0

    # ── Spread stability ──────────────────────────────────────────────────────
    spread_stable, spread_reason = _check_spread_stability(
        live_spread, baseline_spread or spread_pct
    )

    # ── Exhaustion ────────────────────────────────────────────────────────────
    exhausted, exhaust_reason = _detect_exhaustion(vwap_dist, rvol, spread_stable)

    # ── Score (0-100) ─────────────────────────────────────────────────────────
    score = 50  # neutral baseline

    # RVOL component (−30 to +30)
    if rvol >= RVOL_STRONG_THRESHOLD:
        score += 30
    elif rvol >= RVOL_MIN_THRESHOLD:
        score += 10
    elif rvol > 0:
        score -= 20
    # rvol == 0 means no data — no penalty

    # VWAP distance component (−20 to +15)
    if vwap:
        if abs(vwap_dist) < 1.0:
            score += 15    # close to VWAP — healthy
        elif abs(vwap_dist) < 2.0:
            score += 5
        else:
            score -= 20    # far from VWAP

    # Spread component (−15 to +15)
    if spread_stable:
        score += 15
    else:
        score -= 15

    # Exhaustion penalty (−25)
    if exhausted:
        score -= 25

    score = max(0, min(100, score))
    ok    = score >= MIN_INTRADAY_QUALITY_SCORE and not exhausted

    # Build human-readable reason
    parts = []
    if rvol > 0:
        parts.append(f"rvol={rvol:.1f}x")
    if vwap:
        parts.append(f"vwap_dist={vwap_dist:+.2f}%")
    if not spread_stable:
        parts.append(spread_reason)
    if exhausted:
        parts.append(exhaust_reason)
    reason = " | ".join(parts) if parts else "ok"

    return {
        "ok":                ok,
        "score":             score,
        "rvol":              rvol,
        "vwap":              vwap,
        "vwap_distance_pct": vwap_dist,
        "spread_stable":     spread_stable,
        "exhausted":         exhausted,
        "reason":            reason,
        "data_source":       data_src,
        "provider":          (secondary or {}).get("provider", "none"),
    }
