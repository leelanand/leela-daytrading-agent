import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
FINNHUB_API_KEY   = os.getenv("FINNHUB_API_KEY")
PAPER_TRADING     = os.getenv("PAPER_TRADING", "true").lower() == "true"

ALPACA_BASE_URL = (
    "https://paper-api.alpaca.markets" if PAPER_TRADING
    else "https://api.alpaca.markets"
)

# Risk parameters
MAX_POSITIONS      = 3      # Max simultaneous open positions
POSITION_SIZE_PCT  = 0.20   # 20% of portfolio per trade (~$40k on $200k)
STOP_LOSS_PCT      = 0.015  # 1.5% stop loss
TAKE_PROFIT_PCT    = 0.030  # 3.0% take profit (2:1 risk/reward)
DAILY_LOSS_LIMIT   = 0.03   # Stop trading if down 3% on the day
MIN_CONFIDENCE     = 8      # Min Claude score (1-10) to place a trade
MIN_GAP_PCT        = 1.5    # Minimum gap from prev close to consider
MIN_REL_VOLUME     = 1.3    # Minimum relative volume vs 10-day average
FORCE_CLOSE_HOUR   = 15     # Force-close all positions at...
FORCE_CLOSE_MIN    = 45     # ...3:45pm ET — no overnight risk

DB_PATH = Path(__file__).parent / "daytrades.db"

# High-liquidity watchlist — tight spreads, high volume, moves well intraday
WATCHLIST = [
    # Mega cap — most liquid
    "AAPL", "MSFT", "NVDA", "TSLA", "AMD", "META", "GOOGL", "AMZN", "NFLX", "AVGO",
    # High-beta momentum
    "COIN", "MARA", "PLTR", "SOFI", "HOOD", "RIVN", "LCID",
    # Semis / AI
    "SMCI", "MU", "QCOM", "INTC", "ARM", "MRVL",
    # High-vol growth
    "DDOG", "ZS", "CRWD", "SNOW", "ROKU", "TTD", "UBER", "LYFT",
    # ETFs — market direction / leverage
    "SPY", "QQQ", "SOXL", "TQQQ",
]
