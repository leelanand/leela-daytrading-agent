"""
Per-trade feed input logging and daily feed quality report.

Two types of records in feed_log.jsonl:
  TRADE_DECISION — all feed inputs used for one trade decision
  HEALTH_CHECK / STALE_QUOTE / PRICE_MISMATCH / TRADE_REJECTED_DATA / etc.

Daily quality report is generated from the same JSONL file.
"""
import json
from datetime import date, datetime, timezone
from config import FEED_LOG_FILE, FEED_QUALITY_FILE


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Append helpers ────────────────────────────────────────────────────────────

def log_trade_feed_inputs(symbol: str, data: dict):
    """Record all feed sources and validation results used for one trade decision."""
    entry = {
        "ts":     _now_iso(),
        "event":  "TRADE_DECISION",
        "symbol": symbol,
        **data,
    }
    with open(FEED_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def log_feed_event(event_type: str, details: dict | None = None):
    """Append a generic feed event (mismatch, rejection, outage, news stats, etc.)."""
    entry = {"ts": _now_iso(), "event": event_type, **(details or {})}
    with open(FEED_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


# ── Daily quality report ──────────────────────────────────────────────────────

def generate_feed_quality_report() -> dict:
    """
    Aggregate today's feed_log.jsonl entries into a quality summary.
    Only processes entries with today's date prefix.
    """
    today = date.today().isoformat()
    report = {
        "date":                    today,
        "health_checks":           0,
        "health_failures":         0,
        "trade_decisions_logged":  0,
        "quote_validations":       0,
        "quote_mismatches":        0,
        "trades_rejected_data":    0,
        "stale_quote_events":      0,
        "price_spike_events":      0,
        "news_items_fetched":      0,
        "news_deduplicated":       0,
        "provider_outages":        [],
        "mismatch_details":        [],
    }

    if not FEED_LOG_FILE.exists():
        return report

    try:
        for line in FEED_LOG_FILE.read_text(encoding="utf-8").strip().split("\n"):
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue

            if not e.get("ts", "").startswith(today):
                continue

            ev = e.get("event", "")

            if ev == "HEALTH_CHECK":
                report["health_checks"] += 1
                if e.get("status") not in ("ok", None):
                    report["health_failures"] += 1

            elif ev == "TRADE_DECISION":
                report["trade_decisions_logged"] += 1
                if e.get("quote_validation_done"):
                    report["quote_validations"] += 1
                if not e.get("quote_ok", True):
                    report["quote_mismatches"] += 1

            elif ev == "TRADE_REJECTED_DATA":
                report["trades_rejected_data"] += 1
                report["mismatch_details"].append({
                    "symbol": e.get("symbol"),
                    "reason": e.get("reason"),
                })

            elif ev == "STALE_QUOTE":
                report["stale_quote_events"] += 1

            elif ev == "PRICE_SPIKE":
                report["price_spike_events"] += 1

            elif ev in ("ALPACA_OUTAGE", "POLYGON_OUTAGE",
                        "FINNHUB_OUTAGE", "BENZINGA_OUTAGE"):
                if ev not in report["provider_outages"]:
                    report["provider_outages"].append(ev)

            elif ev == "NEWS_FETCH":
                report["news_items_fetched"] += e.get("raw_count", 0)
                report["news_deduplicated"]  += e.get("dedup_removed", 0)

    except Exception:
        pass

    FEED_QUALITY_FILE.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def print_feed_quality_report(report: dict):
    print(f"\n{'='*64}")
    print(f"  FEED QUALITY REPORT — {report['date']}")
    print(f"{'='*64}")
    hc  = report["health_checks"]
    hf  = report["health_failures"]
    qv  = report["quote_validations"]
    qm  = report["quote_mismatches"]
    print(f"  Health checks    : {hc} run   {hf} failed")
    print(f"  Quote validations: {qv} run   {qm} mismatches")
    print(f"  Rejected on data : {report['trades_rejected_data']}")
    print(f"  Stale quotes     : {report['stale_quote_events']}")
    print(f"  Price spikes     : {report['price_spike_events']}")
    nf  = report["news_items_fetched"]
    nd  = report["news_deduplicated"]
    print(f"  News fetched     : {nf} items   {nd} deduplicated")

    if report["provider_outages"]:
        print(f"\n  Provider Outages:")
        for o in report["provider_outages"]:
            print(f"    {o}")

    if report["mismatch_details"]:
        print(f"\n  Data-Quality Rejections:")
        for m in report["mismatch_details"]:
            sym    = m.get("symbol", "?")
            reason = m.get("reason", "?")
            print(f"    {sym:8s}  {reason}")

    print(f"{'='*64}\n")
