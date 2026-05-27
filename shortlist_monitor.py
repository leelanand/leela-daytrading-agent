"""
Continuous shortlist monitoring.

Tracks candidates scored >= WATCHLIST_SCORE (60) that have not yet triggered
an entry. Checks them every SHORTLIST_MONITOR_SECS seconds for readiness.

Used in --monitor mode for live readiness logging.
Used in --scan mode as a pre-readiness signal before placing orders.
Orders are NEVER placed from this module — it is read-only intelligence.
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from config import (
    WATCHLIST_SCORE, GAP_TOLERANCE_PCT,
    SHORTLIST_STATE_FILE,
)

# ── File logger ────────────────────────────────────────────────────────────────
_log_path = Path(__file__).parent / "shortlist_monitor.log"
_flog     = logging.getLogger("shortlist_monitor")
if not _flog.handlers:
    _flog.setLevel(logging.INFO)
    _h = logging.FileHandler(_log_path, encoding="utf-8")
    _h.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    _flog.addHandler(_h)
    _flog.propagate = False


# ── State persistence ──────────────────────────────────────────────────────────

def _load() -> list[dict]:
    try:
        if SHORTLIST_STATE_FILE.exists():
            data = json.loads(SHORTLIST_STATE_FILE.read_text())
            return data if isinstance(data, list) else []
    except Exception:
        pass
    return []


def _save(entries: list[dict]):
    try:
        SHORTLIST_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        SHORTLIST_STATE_FILE.write_text(json.dumps(entries, indent=2, default=str))
    except Exception:
        pass


# ── Public API ─────────────────────────────────────────────────────────────────

def add_to_shortlist(candidate: dict):
    """Store candidate in shortlist. Overwrites existing entry for same symbol."""
    entries = _load()
    symbol  = candidate.get("symbol", "")
    entries = [e for e in entries if e.get("symbol") != symbol]
    entry   = dict(candidate)
    entry["shortlisted_at"] = datetime.now(timezone.utc).isoformat()
    entries.append(entry)
    _save(entries)
    _flog.info(f"SHORTLISTED  {symbol:<6} score={candidate.get('score', '?')} "
               f"price={candidate.get('price', '?')}")


def remove_from_shortlist(symbol: str):
    entries = _load()
    before  = len(entries)
    entries = [e for e in entries if e.get("symbol") != symbol]
    if len(entries) < before:
        _save(entries)
        _flog.info(f"REMOVED      {symbol}")


def clear_shortlist():
    _save([])
    _flog.info("CLEARED shortlist")


def get_shortlist() -> list[dict]:
    return _load()


def check_shortlist_candidate(candidate: dict) -> dict:
    """
    Fetch live price/spread/volume for one candidate and return readiness dict.

    Returns:
      symbol, live_price, spread_pct, vwap_distance_pct, rel_volume,
      momentum_ok (bool), price_extended (bool), ready_for_entry (bool),
      skip_reason (str or "")
    """
    symbol = candidate.get("symbol", "")
    result = {
        "symbol":            symbol,
        "live_price":        None,
        "spread_pct":        None,
        "vwap_distance_pct": None,
        "rel_volume":        None,
        "momentum_ok":       False,
        "price_extended":    False,
        "ready_for_entry":   False,
        "skip_reason":       "",
    }

    try:
        import yfinance as yf
        ticker_obj  = yf.Ticker(symbol)
        fast        = ticker_obj.fast_info
        live_price  = float(fast.last_price or 0)
        if live_price <= 0:
            result["skip_reason"] = "live price unavailable"
            _flog.info(f"CHECK        {symbol:<6} SKIP live_price=0")
            return result
        result["live_price"] = round(live_price, 4)

        # VWAP distance
        vwap = candidate.get("vwap", 0.0)
        if vwap and vwap > 0:
            vwap_dist = (live_price - vwap) / vwap * 100
            result["vwap_distance_pct"] = round(vwap_dist, 3)

        # Spread: use candidate's stored spread if live quotes unavailable
        spread = candidate.get("spread_pct", None)
        result["spread_pct"] = spread

        # Relative volume: use candidate's rel_volume (already calculated at scan)
        result["rel_volume"] = candidate.get("rel_volume", None)

        # Price extended check: > GAP_TOLERANCE_PCT above prescan price
        prescan_price = candidate.get("price", live_price)
        if prescan_price and prescan_price > 0:
            move_pct = (live_price - prescan_price) / prescan_price * 100
            result["price_extended"] = move_pct > GAP_TOLERANCE_PCT

        # Momentum check (use stored momentum or call analyser)
        try:
            from momentum import analyse_momentum, STRENGTHENING, STABLE
            mom = analyse_momentum(symbol)
            result["momentum_ok"] = mom["strength"] in (STRENGTHENING, STABLE)
        except Exception:
            result["momentum_ok"] = True  # default safe — don't block

        # Ready if: live price available, price not extended, momentum ok
        skip = ""
        if result["price_extended"]:
            skip = f"price_extended: +{move_pct:.1f}% from prescan"
        elif not result["momentum_ok"]:
            skip = "momentum not ok"

        result["ready_for_entry"] = (not skip and live_price > 0)
        result["skip_reason"]     = skip

        status = "READY" if result["ready_for_entry"] else f"NOT_READY({skip})"
        _flog.info(f"CHECK        {symbol:<6} {status} "
                   f"live={live_price:.2f} vwap_dist={result.get('vwap_distance_pct','?')}% "
                   f"mom={result['momentum_ok']} ext={result['price_extended']}")

    except Exception as ex:
        result["skip_reason"] = f"check_error: {ex}"
        _flog.info(f"CHECK        {symbol:<6} ERROR {ex}")

    return result


def monitor_shortlist() -> list[dict]:
    """
    Run check_shortlist_candidate for all entries.
    Remove entries older than 90 minutes.
    Return list of ready-for-entry candidates.
    """
    entries = _load()
    if not entries:
        return []

    now      = datetime.now(timezone.utc)
    survived = []
    ready    = []

    for entry in entries:
        # Staleness check — remove candidates older than 90 min
        added_at = entry.get("shortlisted_at")
        if added_at:
            try:
                age_mins = (now - datetime.fromisoformat(added_at)).total_seconds() / 60
                if age_mins > 90:
                    _flog.info(f"STALE        {entry.get('symbol','?')} age={age_mins:.0f}min — removed")
                    continue
            except Exception:
                pass

        survived.append(entry)
        check = check_shortlist_candidate(entry)
        if check.get("ready_for_entry"):
            ready.append({**entry, **check})

    _save(survived)
    return ready
