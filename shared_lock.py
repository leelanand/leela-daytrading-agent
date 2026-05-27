"""
Cross-agent symbol lock — prevents Alpaca and IBKR agents entering
the same symbol simultaneously, doubling exposure unintentionally.

Shared file: C:/Users/leela/shared_positions.json
Format: {"SYMBOL": "alpaca" | "ibkr", ...}

Each agent refreshes its own holdings at the start of every monitor
cycle, so stale entries clear automatically if an agent restarts.
"""
import json
from pathlib import Path

SHARED_FILE = Path(r"C:\Users\leela\shared_positions.json")
AGENT_NAME  = "alpaca"


def _read() -> dict:
    try:
        if SHARED_FILE.exists():
            return json.loads(SHARED_FILE.read_text())
    except Exception:
        pass
    return {}


def _write(data: dict):
    try:
        SHARED_FILE.write_text(json.dumps(data, indent=2))
    except Exception:
        pass


def is_symbol_taken(symbol: str) -> tuple[bool, str]:
    """Returns (taken, holder). taken=True means the other agent holds it."""
    holder = _read().get(symbol, "")
    if holder and holder != AGENT_NAME:
        return True, holder
    return False, ""


def claim_symbol(symbol: str):
    """Record that this agent has entered a position in symbol."""
    data = _read()
    data[symbol] = AGENT_NAME
    _write(data)


def release_symbol(symbol: str):
    """Remove this agent's claim when position is fully closed."""
    data = _read()
    if data.get(symbol) == AGENT_NAME:
        del data[symbol]
        _write(data)


def refresh_symbols(current_symbols: set[str]):
    """
    Replace all of this agent's entries with current open positions.
    Called at start of every monitor cycle so stale claims self-correct.
    """
    data = _read()
    data = {k: v for k, v in data.items() if v != AGENT_NAME}
    for sym in current_symbols:
        data[sym] = AGENT_NAME
    _write(data)
