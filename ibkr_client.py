"""
IBKR market data client for the Alpaca agent.
Connects to IB Gateway for real-time quotes and 1-min bars.
Execution remains via Alpaca API — this module is data-only.
"""
from __future__ import annotations
from ib_insync import IB, Stock, util
from config import IBKR_HOST, IBKR_PORT, IBKR_CLIENT_ID

util.startLoop()

_ib: IB | None = None


def connect(timeout: int = 10) -> IB:
    global _ib
    if _ib and _ib.isConnected():
        return _ib
    ib = IB()
    ib.connect(IBKR_HOST, IBKR_PORT, clientId=IBKR_CLIENT_ID, timeout=timeout)
    _ib = ib
    print(f"   [IBKR-DATA] Connected (port={IBKR_PORT}, clientId={IBKR_CLIENT_ID})")
    return _ib


def get_ib() -> IB:
    if _ib and _ib.isConnected():
        return _ib
    return connect()


def disconnect():
    global _ib
    if _ib:
        _ib.disconnect()
        _ib = None


def get_1min_bars_ibkr(symbol: str, n: int = 25) -> list[dict]:
    """
    Fetch last n 1-min OHLCV bars via IBKR historical data API.
    Returns list of dicts with open/high/low/close/volume keys.
    Empty list on any failure — callers fall back to yfinance.
    """
    try:
        ib       = get_ib()
        contract = Stock(symbol, "SMART", "USD")
        ib.qualifyContracts(contract)
        bars = ib.reqHistoricalData(
            contract,
            endDateTime    = "",
            durationStr    = "1 D",
            barSizeSetting = "1 min",
            whatToShow     = "TRADES",
            useRTH         = True,
            formatDate     = 1,
            keepUpToDate   = False,
            timeout        = 10,
        )
        if not bars or len(bars) < 5:
            return []
        result = [
            {"open": b.open, "high": b.high, "low": b.low,
             "close": b.close, "volume": b.volume}
            for b in bars
        ]
        return result[-n:] if len(result) >= n else result
    except Exception:
        return []
