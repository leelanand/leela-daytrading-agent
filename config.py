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

# ── Execution ──────────────────────────────────────────────────────────────────
MAX_POSITIONS     = 3
POSITION_SIZE_PCT = 0.20
STOP_LOSS_PCT     = 0.015
TAKE_PROFIT_PCT   = 0.030
FORCE_CLOSE_HOUR  = 15
FORCE_CLOSE_MIN   = 45

USE_LIMIT_ORDERS  = True       # limit orders preferred over market
LIMIT_OFFSET_PCT  = 0.001      # buy limit = price * (1 + LIMIT_OFFSET_PCT)
STAGED_ENTRY      = False      # True = 50% initial, 50% on confirmation

# ── Scoring thresholds ─────────────────────────────────────────────────────────
MIN_SCORE_TO_TRADE = 78        # 0-100: minimum score to place a real order
WATCHLIST_SCORE    = 60        # 0-100: monitor but don't trade yet

# ── Risk controls ──────────────────────────────────────────────────────────────
DAILY_LOSS_LIMIT    = 0.03     # stop trading if down 3% on the day
MAX_TRADES_PER_DAY  = 5        # max new entries per day
MAX_SECTOR_EXPOSURE = 0.40     # max 40% of portfolio in one sector
MAX_SPREAD_PCT      = 0.30     # max bid/ask spread as % of price
MIN_VOLUME_DAILY    = 300_000  # minimum daily share volume
GAP_TOLERANCE_PCT   = 2.5      # max price drift from prescan price (%)
KILL_SWITCH         = False    # set True to disable ALL trading immediately

# ── Scanner filters ────────────────────────────────────────────────────────────
MIN_GAP_PCT     = 1.5
MIN_REL_VOLUME  = 1.3

# ── Candidate storage ──────────────────────────────────────────────────────────
CANDIDATE_EXPIRY_MINS = 90     # prescan candidates expire after 90 min
CANDIDATES_FILE       = Path(__file__).parent / "candidates.json"
PAPER_TRADES_FILE     = Path(__file__).parent / "paper_trades.json"
AUDIT_LOG_FILE        = Path(__file__).parent / "audit.log"

# ── Database ───────────────────────────────────────────────────────────────────
DB_PATH = Path(__file__).parent / "daytrades.db"

# ── Watchlist ──────────────────────────────────────────────────────────────────
WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "TSLA", "AMD", "META", "GOOGL", "AMZN", "NFLX", "AVGO",
    "COIN", "MARA", "PLTR", "SOFI", "HOOD", "RIVN", "LCID",
    "SMCI", "MU", "QCOM", "INTC", "ARM", "MRVL",
    "DDOG", "ZS", "CRWD", "SNOW", "ROKU", "TTD", "UBER", "LYFT",
    "SPY", "QQQ", "SOXL", "TQQQ",
    "GNRC",
]
