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

# ── Market Regime ──────────────────────────────────────────────────────────────
REGIME_CACHE_MINS  = 30          # re-detect every 30 min
SPY_TREND_DAYS     = 3           # rolling days for SPY trend calculation
TRADEABLE_REGIMES  = ["TRENDING_UP", "CHOPPY"]   # trading allowed in these
# HIGH_VOL is allowed but dynamic sizing cuts size; TRENDING_DOWN + LOW_VOLUME blocked

# ── Dynamic Sizing ─────────────────────────────────────────────────────────────
MIN_POSITION_SIZE_PCT      = 0.10   # floor — never size below 10% of portfolio
MAX_POSITION_SIZE_PCT      = 0.25   # cap  — never size above 25% (hard ceiling)
SCORE_SIZE_BOOST_THRESHOLD = 85     # score >= this → +25% to base size
HIGH_VOL_THRESHOLD         = 3.0    # intraday vol% above which size is reduced

# ── Advanced Exits ─────────────────────────────────────────────────────────────
TRAILING_STOP_TRIGGER_PCT  = 0.015  # activate trailing stop after +1.5% gain
TRAILING_STOP_DISTANCE_PCT = 0.010  # trail 1.0% below running high-watermark
TIME_EXIT_MINS             = 90     # exit if <0.5% move after this many minutes

# ── Trade Quality Filters ──────────────────────────────────────────────────────
MAX_MOVE_BEFORE_ENTRY_PCT  = 3.0    # skip if price already moved >3% from open
MIN_VOLUME_TREND_RATIO     = 0.70   # projected daily vol must be ≥70% of 10-day avg
VWAP_PREFERENCE            = True   # label below-VWAP entries for analyst context

# ── Time-of-Day Gates (ET) ─────────────────────────────────────────────────────
BLOCK_MIDDAY       = True           # skip 12:00–13:00 ET (lunch lull)
BLOCK_MIDDAY_START = (12, 0)        # (hour, minute) ET
BLOCK_MIDDAY_END   = (13, 0)        # (hour, minute) ET

# ── Performance / Adaptive ─────────────────────────────────────────────────────
PERF_LOOKBACK_DAYS    = 10     # rolling window for adaptive sizing / pause logic
MIN_WIN_RATE_TO_TRADE = 0.35   # pause trading if rolling win rate drops below this
PERFORMANCE_FILE      = Path(__file__).parent / "performance.json"
PERF_HISTORY_FILE     = Path(__file__).parent / "performance_history.jsonl"
EXIT_STATE_FILE       = Path(__file__).parent / "exit_state.json"
REGIME_CACHE_FILE     = Path(__file__).parent / "regime_cache.json"

# ── Intraday Momentum (1-min bars) ────────────────────────────────────────────
INTRADAY_BARS             = 15     # 1-min bars per symbol for momentum classification
INTRADAY_ALIGN_CACHE_MINS = 5      # minutes to cache SPY intraday direction check
INTRADAY_CACHE_FILE       = Path(__file__).parent / "intraday_cache.json"
MIN_MOMENTUM_TO_TRADE     = ["STRENGTHENING", "STABLE"]  # reject WEAKENING + EXHAUSTED
BLOCK_ON_SELLING_OFF      = True   # block all longs when SPY is actively selling off

# ── Fast Loss Control ──────────────────────────────────────────────────────────
RAPID_INVALIDATION_MINS  = 8       # exit immediately if in loss after this many minutes
TIGHT_STOP_PCT           = 0.008   # 0.8% stop used when momentum is WEAKENING
TIGHT_STOP_REGIMES       = ["HIGH_VOL"]  # regimes that also trigger tighter stop

# ── Time-Window Adaptive Blocking ─────────────────────────────────────────────
WINDOW_BLOCK_MIN_TRADES  = 5       # min samples in a window before blocking it
WINDOW_BLOCK_AVG_PNL     = -40.0   # block window if avg P&L per trade < this ($)

# ── Sector ETFs (for sector/theme strength confirmation) ───────────────────────
SECTOR_ETFS = {
    "Technology":             "XLK",
    "Financial Services":     "XLF",
    "Healthcare":             "XLV",
    "Consumer Cyclical":      "XLY",
    "Communication Services": "XLC",
    "Industrials":            "XLI",
    "Energy":                 "XLE",
    "Materials":              "XLB",
    "Consumer Defensive":     "XLP",
    "Utilities":              "XLU",
    "Real Estate":            "XLRE",
}

# ── Theme / Correlation Groups ─────────────────────────────────────────────────
THEME_MAP = {
    "ai_chips":   ["NVDA", "AMD", "INTC", "QCOM", "ARM", "MRVL", "AVGO", "MU", "SMCI"],
    "crypto":     ["COIN", "MARA"],
    "ev":         ["TSLA", "RIVN", "LCID"],
    "big_tech":   ["AAPL", "MSFT", "META", "GOOGL", "AMZN"],
    "cloud_saas": ["DDOG", "ZS", "CRWD", "SNOW", "PLTR"],
    "streaming":  ["NFLX", "ROKU", "TTD"],
    "fintech":    ["SOFI", "HOOD"],
    "leveraged":  ["SOXL", "TQQQ"],
    "mobility":   ["UBER", "LYFT"],
}
MAX_THEME_POSITIONS = 1   # max 1 open position per theme group

# ── Watchlist ──────────────────────────────────────────────────────────────────
WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "TSLA", "AMD", "META", "GOOGL", "AMZN", "NFLX", "AVGO",
    "COIN", "MARA", "PLTR", "SOFI", "HOOD", "RIVN", "LCID",
    "SMCI", "MU", "QCOM", "INTC", "ARM", "MRVL",
    "DDOG", "ZS", "CRWD", "SNOW", "ROKU", "TTD", "UBER", "LYFT",
    "SPY", "QQQ", "SOXL", "TQQQ",
    "GNRC",
]
