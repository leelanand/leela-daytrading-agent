"""Save, load, and expire prescan candidates."""
import json
from datetime import datetime, timezone
from pathlib import Path
from config import CANDIDATES_FILE, CANDIDATE_EXPIRY_MINS, DECAY_EXPIRE_MINS, DECAY_STRICT_EXPIRE

LATE_ADDITIONS_FILE        = Path(__file__).parent / "late_additions.json"
LATE_ADDITIONS_EXPIRY_MINS = 45


def save_candidates(candidates: list[dict]):
    payload = {
        "saved_at":  datetime.now(timezone.utc).isoformat(),
        "candidates": candidates,
    }
    CANDIDATES_FILE.write_text(json.dumps(payload, indent=2))
    print(f"   Saved {len(candidates)} candidates → {CANDIDATES_FILE.name}")


def _load_late_additions() -> list[str]:
    """Return unexpired late-addition symbols written by the ops agent."""
    if not LATE_ADDITIONS_FILE.exists():
        return []
    try:
        data     = json.loads(LATE_ADDITIONS_FILE.read_text())
        saved_at = datetime.fromisoformat(data["saved_at"])
        age_mins = (datetime.now(timezone.utc) - saved_at).total_seconds() / 60
        if age_mins > LATE_ADDITIONS_EXPIRY_MINS:
            return []
        return [s for s in data.get("symbols", []) if isinstance(s, str)]
    except Exception:
        return []


def load_valid_candidates() -> list[dict]:
    candidates = []

    if CANDIDATES_FILE.exists():
        try:
            payload  = json.loads(CANDIDATES_FILE.read_text())
            saved_at = datetime.fromisoformat(payload["saved_at"])
            age_mins = (datetime.now(timezone.utc) - saved_at).total_seconds() / 60
            if age_mins > CANDIDATE_EXPIRY_MINS:
                print(f"   [PRESCAN] Candidates expired ({age_mins:.0f} min old, limit {CANDIDATE_EXPIRY_MINS} min)")
            else:
                stale = age_mins > DECAY_EXPIRE_MINS
                if stale and DECAY_STRICT_EXPIRE:
                    print(f"   [PRESCAN] Candidates stale ({age_mins:.0f} min > {DECAY_EXPIRE_MINS} min LIVE limit) — expired")
                else:
                    for c in payload.get("candidates", []):
                        c["_age_mins"] = round(age_mins, 1)
                        c["_stale"]    = stale
                    candidates = payload.get("candidates", [])
                    stale_tag = " [STALE]" if stale else ""
                    print(f"   Loaded {len(candidates)} prescan candidates ({age_mins:.0f} min old){stale_tag}")
        except Exception as e:
            print(f"   [PRESCAN] Failed to load candidates: {e}")

    # Merge late additions injected by the ops agent feedback loop
    late_syms = _load_late_additions()
    if late_syms:
        existing = {c["symbol"] for c in candidates}
        added    = []
        for sym in late_syms:
            if sym not in existing:
                # _is_top_gapper=True forces the analyst to send to Claude even with thin scanner data
                candidates.append({
                    "symbol":        sym,
                    "watchlist":     True,
                    "tradeable":     False,
                    "score":         0,
                    "_late_addition": True,
                    "_is_top_gapper": True,
                    "_age_mins":     0,
                    "_stale":        False,
                })
                added.append(sym)
        if added:
            print(f"   [LATE ADD] Injected {len(added)} ops-agent late addition(s): {', '.join(added)}")

    return candidates


def clear_candidates():
    if CANDIDATES_FILE.exists():
        CANDIDATES_FILE.unlink()
