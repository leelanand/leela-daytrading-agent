"""
Advanced exit monitoring — runs in --monitor mode every 15 min.

Exit conditions checked (beyond the bracket's fixed stop/TP):
  1. Trailing stop  — activates after TRAILING_STOP_TRIGGER_PCT gain
  2. Momentum flip  — price peaked then fell back below entry
  3. Time-based     — still flat after TIME_EXIT_MINS
"""
import json
from datetime import datetime, timezone
from alpaca.trading.client import TradingClient
from config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, PAPER_TRADING,
    TRAILING_STOP_TRIGGER_PCT, TRAILING_STOP_DISTANCE_PCT,
    TIME_EXIT_MINS, EXIT_STATE_FILE,
)
from logger import log_audit


def _client() -> TradingClient:
    return TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=PAPER_TRADING)


def _load_state() -> dict:
    if EXIT_STATE_FILE.exists():
        try:
            return json.loads(EXIT_STATE_FILE.read_text())
        except Exception:
            pass
    return {"watermarks": {}, "entry_times": {}}


def _save_state(state: dict):
    EXIT_STATE_FILE.write_text(json.dumps(state, default=str, indent=2))


def record_entry(symbol: str, entry_price: float):
    """Seed exit state immediately after an order is placed."""
    state = _load_state()
    state["watermarks"][symbol]  = entry_price
    state["entry_times"][symbol] = datetime.now(timezone.utc).isoformat()
    _save_state(state)


def check_exits() -> list[str]:
    """
    Evaluate all open positions against advanced exit conditions.
    Returns list of symbols to close. Updates high-watermarks in state file.
    """
    client    = _client()
    positions = client.get_all_positions()
    if not positions:
        return []

    state    = _load_state()
    to_close = []

    for p in positions:
        symbol     = p.symbol
        entry      = float(p.avg_entry_price)
        current    = float(p.current_price)
        unreal_pct = float(p.unrealized_plpc)   # fraction e.g. 0.015 = 1.5%
        reason     = None

        # Update running high-watermark
        high = state["watermarks"].get(symbol, entry)
        if current > high:
            state["watermarks"][symbol] = current
            high = current

        # 1. Trailing stop (activates only after enough gain)
        if unreal_pct >= TRAILING_STOP_TRIGGER_PCT:
            trail = high * (1 - TRAILING_STOP_DISTANCE_PCT)
            if current <= trail:
                reason = (f"trailing_stop: ${current:.2f} ≤ trail ${trail:.2f} "
                          f"(peak ${high:.2f}, was +{unreal_pct:.1%})")

        # 2. Momentum flip — peaked then fell back below entry
        if not reason and high > entry * 1.01:   # was up at least 1%
            if current < entry:
                reason = (f"momentum_flip: peaked ${high:.2f} "
                          f"(+{(high/entry-1):.1%}), now ${current:.2f} < entry")

        # 3. Time-based exit — no movement after TIME_EXIT_MINS
        if not reason:
            entry_iso = state.get("entry_times", {}).get(symbol)
            if entry_iso:
                entered  = datetime.fromisoformat(entry_iso)
                age_mins = (datetime.now(timezone.utc) - entered).total_seconds() / 60
                if age_mins > TIME_EXIT_MINS:
                    move_pct = abs(current - entry) / entry * 100
                    if move_pct < 0.50:
                        reason = (f"time_exit: {age_mins:.0f} min old, "
                                  f"only {move_pct:.2f}% move — no follow-through")

        if reason:
            to_close.append(symbol)
            log_audit("EXIT_SIGNAL", symbol, {
                "reason":     reason,
                "current":    round(current, 2),
                "entry":      round(entry, 2),
                "unrealized": round(unreal_pct, 4),
            })
            print(f"   [EXIT SIGNAL] {symbol}: {reason}")

    _save_state(state)
    return to_close


def execute_exits(symbols: list[str]):
    if not symbols:
        return
    client = _client()
    for symbol in symbols:
        try:
            client.cancel_orders()
            client.close_position(symbol)
            log_audit("EXIT_EXECUTED", symbol)
            print(f"   [CLOSED] {symbol}")
            state = _load_state()
            state["watermarks"].pop(symbol, None)
            state["entry_times"].pop(symbol, None)
            _save_state(state)
        except Exception as e:
            log_audit("EXIT_ERROR", symbol, {"error": str(e)})
            print(f"   [EXIT ERROR] {symbol}: {e}")


def monitor_positions():
    """Entry point for --monitor mode."""
    to_close = check_exits()
    if to_close:
        print(f"   Closing {len(to_close)} position(s): {', '.join(to_close)}")
        execute_exits(to_close)
    else:
        positions = _client().get_all_positions()
        count     = len(positions)
        if count:
            print(f"   {count} position(s) open — no advanced exits triggered")
        else:
            print("   No open positions.")
