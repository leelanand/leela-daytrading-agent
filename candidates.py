"""Save, load, and expire prescan candidates."""
import json
from datetime import datetime, timezone
from config import CANDIDATES_FILE, CANDIDATE_EXPIRY_MINS


def save_candidates(candidates: list[dict]):
    payload = {
        "saved_at":  datetime.now(timezone.utc).isoformat(),
        "candidates": candidates,
    }
    CANDIDATES_FILE.write_text(json.dumps(payload, indent=2))
    print(f"   Saved {len(candidates)} candidates → {CANDIDATES_FILE.name}")


def load_valid_candidates() -> list[dict]:
    if not CANDIDATES_FILE.exists():
        return []
    try:
        payload  = json.loads(CANDIDATES_FILE.read_text())
        saved_at = datetime.fromisoformat(payload["saved_at"])
        age_mins = (datetime.now(timezone.utc) - saved_at).total_seconds() / 60
        if age_mins > CANDIDATE_EXPIRY_MINS:
            print(f"   [PRESCAN] Candidates expired ({age_mins:.0f} min old, limit {CANDIDATE_EXPIRY_MINS} min)")
            return []
        candidates = payload.get("candidates", [])
        print(f"   Loaded {len(candidates)} prescan candidates ({age_mins:.0f} min old)")
        return candidates
    except Exception as e:
        print(f"   [PRESCAN] Failed to load candidates: {e}")
        return []


def clear_candidates():
    if CANDIDATES_FILE.exists():
        CANDIDATES_FILE.unlink()
