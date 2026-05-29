"""Overnight candidate queue — persists scored candidates when daily budget is fully deployed.
Consumed at next-morning scan to pre-seed watchlist with yesterday's high-scorers.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

QUEUE_FILE        = Path(__file__).parent / "morning_queue.jsonl"
_MAX_AGE_HOURS    = 20   # entries older than this are ignored at load time


def queue_candidates(candidates: list[dict], reason: str) -> None:
    """Append scored candidates to the overnight queue."""
    if not candidates:
        return
    now = datetime.now(timezone.utc).isoformat()
    with open(QUEUE_FILE, "a", encoding="utf-8") as f:
        for c in candidates:
            entry = {
                "queued_at":    now,
                "queue_reason": reason,
                "symbol":       c.get("symbol"),
                "score":        c.get("score", 0),
                "setup_type":   c.get("setup_type", ""),
                "sector":       c.get("sector", ""),
                "gap_pct":      c.get("gap_pct", 0),
                "rvol":         c.get("rvol", 0),
                "reasoning":    c.get("reasoning", ""),
                "tradeable":    c.get("tradeable", False),
                "watchlist":    c.get("watchlist", False),
                "regime":       c.get("regime", ""),
            }
            f.write(json.dumps(entry) + "\n")


def load_morning_queue() -> list[dict]:
    """Return queue entries written within the last _MAX_AGE_HOURS hours."""
    if not QUEUE_FILE.exists():
        return []
    cutoff = datetime.now(timezone.utc).timestamp() - _MAX_AGE_HOURS * 3600
    results = []
    try:
        for line in QUEUE_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            ts = datetime.fromisoformat(entry["queued_at"]).timestamp()
            if ts >= cutoff:
                results.append(entry)
    except Exception:
        pass
    return results


def clear_old_queue() -> None:
    """Prune entries older than _MAX_AGE_HOURS to keep the file small."""
    entries = load_morning_queue()
    try:
        QUEUE_FILE.write_text(
            "".join(json.dumps(e) + "\n" for e in entries),
            encoding="utf-8",
        )
    except Exception:
        pass
