import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
FINNHUB_API_KEY   = os.getenv("FINNHUB_API_KEY")
POLYGON_API_KEY   = os.getenv("POLYGON_API_KEY", "")     # optional secondary feed
BENZINGA_API_KEY  = os.getenv("BENZINGA_API_KEY", "")    # optional fast news feed
PAPER_TRADING     = os.getenv("PAPER_TRADING", "true").lower() == "true"

# ── IBKR Market Data Connection ───────────────────────────────────────────────
IBKR_HOST      = os.getenv("IBKR_HOST",      "127.0.0.1")
IBKR_PORT      = int(os.getenv("IBKR_PORT",      "4001"))
IBKR_CLIENT_ID = int(os.getenv("IBKR_CLIENT_ID", "2"))

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
FORCE_CLOSE_MIN   = 44   # 15:44 ET / 20:44 BST — task fires at 20:44 BST

USE_LIMIT_ORDERS  = True       # limit orders preferred over market
LIMIT_OFFSET_PCT  = 0.001      # buy limit = price * (1 + LIMIT_OFFSET_PCT)
STAGED_ENTRY      = False      # True = 50% initial, 50% on confirmation

# ── Scoring thresholds ─────────────────────────────────────────────────────────
MIN_SCORE_TO_TRADE  = 78       # 0-100: minimum score to place a real order
CHOPPY_MIN_SCORE    = 73       # lower bar in CHOPPY regime — flat market, fewer high-scorers
WATCHLIST_SCORE     = 60       # 0-100: monitor but don't trade yet

# ── Risk controls ──────────────────────────────────────────────────────────────
DAILY_LOSS_LIMIT    = 0.03     # stop trading if down 3% on the day
MAX_TRADES_PER_DAY  = 3        # matches PDT raw limit; PDT guard further reduces to 2 usable
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
RESEARCH_CACHE_FILE   = Path(__file__).parent / "research_cache.json"
RESEARCH_CACHE_HOURS  = 8

# ── Database ───────────────────────────────────────────────────────────────────
DB_PATH = Path(__file__).parent / "daytrades.db"

# ── Market Regime ──────────────────────────────────────────────────────────────
REGIME_CACHE_MINS            = 15   # re-detect every 15 min
EARLY_SESSION_GRACE_MINS     = 10   # TOD-normalized baseline handles early session; 10 min covers opening auction only
SPY_TREND_DAYS     = 3           # rolling days for SPY trend calculation
TRADEABLE_REGIMES  = ["TRENDING_UP", "CHOPPY", "LOW_VOLUME", "HIGH_VOL"]
# TRENDING_DOWN is blocked; LOW_VOLUME and HIGH_VOL both trigger REDUCED_RISK mode
# HIGH_VOL: only aborts if extreme (ATR > HIGH_VOL_ABORT_ATR_PCT AND VIX > HIGH_VOL_ABORT_VIX)

# ── LOW_VOLUME adaptive thresholds ─────────────────────────────────────────────
# vol_ratio below ABORT → genuine liquidity collapse → NO_TRADE (full abort)
# vol_ratio below 0.60 but above ABORT → LOW_VOLUME → REDUCED_RISK mode
LOW_VOLUME_ABORT_RATIO  = 0.35   # below this = genuine collapse, abort trading
LOW_VOLUME_MIN_SCORE    = 85     # higher conviction required vs normal MIN_SCORE_TO_TRADE
LOW_VOLUME_MAX_TRADES   = 1      # cap new entries at 1 per scan cycle
LOW_VOLUME_STOCK_RVOL   = 1.5    # stock must show ≥ this RVOL to trade
LOW_VOLUME_EXCEPTIONAL_SCORE = 90     # score at/above this exempts from RVOL requirement
LOW_VOLUME_EXCEPTIONAL_NEWS  = 65     # news impact at/above this exempts from RVOL requirement

# ── HIGH_VOL REDUCED_RISK thresholds ──────────────────────────────────────────
# atr_pct > 2.0: HIGH_VOL → REDUCED_RISK (smaller size, higher bar, ORB/gap setups only)
# atr_pct > ABORT_ATR AND VIX > ABORT_VIX: extreme — abort all trading
HIGH_VOL_ABORT_ATR_PCT       = 3.5   # extreme volatility abort threshold (ATR % of price)
HIGH_VOL_ABORT_VIX           = 30    # extreme volatility abort VIX threshold (both required)
HIGH_VOL_MAX_TRADES          = 1     # cap new entries at 1 per scan cycle in HIGH_VOL mode
HIGH_VOL_STOCK_RVOL          = 2.0   # stock RVOL >= this required in HIGH_VOL mode
HIGH_VOL_SIZE_CUT            = 0.30  # reduce position size by 30% in mild HIGH_VOL mode
HIGH_VOL_MIN_SCORE_EXTRA     = 5     # require effective_min + this extra score in HIGH_VOL mode
HIGH_VOL_ALLOWED_SETUPS      = ["orb_breakout", "gap_and_go", "news_momentum"]
# HIGH_VOL severity bands: moderate = ATR >= 2.5%, extreme = ATR > 3.5 AND VIX > 30
HIGH_VOL_MODERATE_ATR_PCT    = 2.5   # ATR >= this → moderate HIGH_VOL (tighter rules)
HIGH_VOL_MODERATE_EXTRA_PTS  = 3     # additional score pts on top of HIGH_VOL_MIN_SCORE_EXTRA
HIGH_VOL_MODERATE_SIZE_CUT   = 0.40  # 40% size cut for moderate (vs 30% for mild)

# ── PAPER vs LIVE LOW_VOLUME score thresholds ──────────────────────────────────
# Paper uses two tiers: exploratory (learning) and live-realistic (capital-parity).
# LIVE threshold is unchanged — capital-protection mode, no relaxations.
PAPER_EXPLORATORY_LOW_VOLUME_MIN_SCORE    = 70   # score 70-74 → PAPER_EXPLORATORY_ONLY
PAPER_LIVE_REALISTIC_LOW_VOLUME_MIN_SCORE = 75   # score 75+  → PAPER_LIVE_REALISTIC
LIVE_LOW_VOLUME_MIN_SCORE                 = 82   # live: higher conviction required (unchanged)

# ── Extreme HIGH_VOL hard stop ────────────────────────────────────────────────
EXTREME_HIGH_VOL_VIX         = 35.0   # VIX >= this → extreme HIGH_VOL abort
EXTREME_HIGH_VOL_ATR_PCT     = 4.0    # intraday ATR >= this % → extreme
EXTREME_HIGH_VOL_SPREAD_MULT = 2.5    # avg spread > this × baseline → extreme

# ── Preferred spread band ──────────────────────────────────────────────────────
PREFERRED_SPREAD_PCT    = 0.18   # preferred max — above this is suboptimal but not blocked
SPREAD_PENALTY_ABOVE    = 0.20   # size penalty applies when spread exceeds this
SPREAD_SIZE_PENALTY_PCT = 0.25   # reduce size by 25% when spread > SPREAD_PENALTY_ABOVE

# ── Adaptive limit order offset ────────────────────────────────────────────────
LIMIT_OFFSET_TIGHT_PCT  = 0.0005  # spread < 0.10%: 0.05% limit offset
LIMIT_OFFSET_NORMAL_PCT = 0.0010  # spread 0.10–0.20%: 0.10% limit offset
LIMIT_OFFSET_WIDE_PCT   = 0.0015  # spread 0.20–0.30%: 0.15% limit offset
MAX_LIMIT_SLIPPAGE_PCT  = 0.0020  # hard ceiling on limit offset (0.20%)

# ── LIVE setup promotion framework ────────────────────────────────────────────
LIVE_PROMOTED_SETUPS = [           # setups with sufficient LIVE performance data
    "gap_and_go", "orb_breakout", "news_momentum", "pullback",
]
LIVE_REQUIRE_PROMOTED_SETUPS = False  # set True once enough LIVE P&L data exists

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

# ── Gapper Discovery ───────────────────────────────────────────────────────────
GAPPER_MIN_GAP_PCT  = 3.0    # minimum % gap to qualify
GAPPER_TOP_N        = 8      # max dynamic gappers added to daily scan
GAPPER_CACHE_FILE   = Path(__file__).parent / "gappers_today.json"
GAPPER_UNIVERSE = [
    # AI / next-gen tech
    "APP", "IONQ", "SOUN", "NBIS", "OKLO", "RKLB", "ASTS", "ACHR", "JOBY", "LUNR",
    # Chinese ADRs
    "BIDU", "BILI", "JD", "NIO", "XPEV", "LI",
    # Fintech
    "AFRM", "UPST", "LC", "OPEN", "OPFI",
    # Health / biotech
    "HIMS", "CELH", "RXRX", "ARWR", "CRSP", "NVAX", "MRNA",
    # Clean energy
    "FSLR", "ENPH", "RUN", "PLUG", "BE", "BLNK", "CHPT",
    # Crypto miners (beyond MARA/COIN)
    "RIOT", "CLSK", "HUT", "BTBT", "CORZ", "WULF",
    # Consumer / e-commerce
    "CHWY", "W", "ETSY", "SNAP", "PINS",
    # Semis adjacent
    "AMBA", "AEHR", "FORM", "ONTO",
    # Gene editing / biotech
    "FATE", "BEAM", "EDIT", "NTLA",
    # High-vol ETFs
    "ARKK", "ARKG", "LABU", "LABD", "TNA", "SPXL", "SPXS",
]

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

# ── Secondary Market Data — Massive.com / Polygon.io ──────────────────────────
# Data layer: Massive.com (primary) → Polygon.io (fallback) → None (skip, non-blocking)
# Execution layer: Alpaca only.  Market intelligence: Massive/Polygon only.
# Add MASSIVE_API_KEY and/or POLYGON_API_KEY to .env to enable.
#
# Massive.com REST: https://api.massive.com   (verify exact path against their docs)
# Polygon.io REST:  https://api.polygon.io    (documented)
# Preferred provider is used for both REST and WebSocket.
MASSIVE_API_KEY   = os.getenv("MASSIVE_API_KEY", "")
PREFERRED_PROVIDER = os.getenv("PREFERRED_PROVIDER", "polygon")  # "massive" | "polygon"

# Provider REST base URLs
MASSIVE_REST_BASE  = "https://api.massive.com"      # verify with Massive docs
POLYGON_REST_BASE  = "https://api.polygon.io"

# WebSocket streaming endpoints
# Massive = Polygon rebranded. Massive keys authenticate on Polygon's WS infrastructure.
# Stocks Starter plan: delayed endpoint only — AM (minute aggregates) confirmed working.
# Real-time endpoint (socket.polygon.io) requires Stocks Advanced ($199/month).
MASSIVE_WS_URL = "wss://delayed.polygon.io/stocks"
POLYGON_WS_URL = "wss://socket.polygon.io/stocks"  # requires Stocks Advanced plan

# WebSocket burst-streaming settings
ENABLE_WS_STREAMING       = True
WS_STREAM_DURATION_SECS   = 75    # AM events fire once/min — 75s ensures at least one is caught
WS_RECONNECT_DELAY_SECS   = 3     # seconds between reconnect attempts
WS_MAX_RECONNECT          = 5     # max reconnect attempts per burst session
WS_CACHE_FILE             = Path(__file__).parent / "ws_cache.json"

# Cross-provider quote validation thresholds
QUOTE_PRICE_MAX_DIFF_PCT   = 0.25   # reject if bid/ask midpoints differ >0.25%
QUOTE_SPREAD_MAX_DIFF_PCT  = 0.15   # reject if spread widths differ >0.15%
QUOTE_VOLUME_MAX_DIFF_PCT  = 25.0   # log (not reject) if day volumes differ >25%
QUOTE_MAX_STALENESS_SECS   = 30     # reject secondary quote older than 30 seconds

# Intraday quality thresholds (used by massive_feed.get_intraday_quality)
RVOL_STRONG_THRESHOLD      = 2.0   # RVOL >= this = strong participation
RVOL_MIN_THRESHOLD         = 1.0   # RVOL < this = low participation (warn)
VWAP_FAR_THRESHOLD_PCT     = 3.0   # price > this % from VWAP = potential exhaustion
SPREAD_WIDEN_THRESHOLD_PCT = 50.0  # spread widened > this % vs baseline = liquidity concern
MIN_INTRADAY_QUALITY_SCORE = 40    # trades below this quality score are rejected

# ── News Feed ──────────────────────────────────────────────────────────────────
NEWS_LOOKBACK_MINS              = 120   # fetch news from the past 2 hours
NEWS_DEDUP_SIMILARITY_THRESHOLD = 0.55  # Jaccard similarity above which headlines are dupes
NEWS_MIN_IMPACT_SCORE           = 15    # suppress news below this in analyst prompt
NEWS_MAX_AGE_MINS               = 120   # news older than this is considered stale

# ── Event Risk ─────────────────────────────────────────────────────────────────
BLOCK_ON_EARNINGS_WITHIN_DAYS = 1   # hard-block if earnings within 1 calendar day
EARNINGS_RISK_WITHIN_DAYS     = 3   # warn-only window for upcoming earnings

# ── Feed Health ────────────────────────────────────────────────────────────────
FEED_HEALTH_FILE             = Path(__file__).parent / "feed_health.json"
FEED_LOG_FILE                = Path(__file__).parent / "feed_log.jsonl"
FEED_QUALITY_FILE            = Path(__file__).parent / "feed_quality.json"
FEED_HEALTH_STALE_QUOTE_SECS = 60    # Alpaca quote age threshold for staleness alert
FEED_HEALTH_SPIKE_PCT        = 5.0   # intraday move that triggers abnormal-spike flag

# ── Shortlist Monitor ─────────────────────────────────────────────────────────
SHORTLIST_MONITOR_SECS = 45       # seconds between shortlist re-checks
SHORTLIST_STATE_FILE   = Path(__file__).parent / "shortlist_state.json"

# ── Pullback Entry ─────────────────────────────────────────────────────────────
PULLBACK_ENABLED       = True
PULLBACK_EMA_PERIOD    = 9
PULLBACK_VWAP_MAX_PCT  = 1.0      # within 1% of VWAP = good pullback
PULLBACK_MAX_WAIT_BARS = 5        # wait up to 5 bars for pullback

# ── Opening Range Breakout ────────────────────────────────────────────────────
ORB_ENABLED              = True
ORB_5MIN_ENABLED         = True
ORB_15MIN_ENABLED        = True
ORB_VOLUME_CONFIRM_RATIO = 1.5    # breakout volume must be >= 1.5x avg

# ── Opening Chaos Lockout ─────────────────────────────────────────────────────
CHAOS_LOCKOUT_END_ET = (9, 45)    # no new entries before 9:45 ET

# ── A+ Setup Tiering ──────────────────────────────────────────────────────────
TIER_NORMAL_MIN  = 78
TIER_HIGH_MIN    = 85
TIER_ELITE_MIN   = 90
ELITE_SIZE_BOOST = 0.25           # elite gets +25% to base size (before max cap)

# ── Volatility-Adjusted Extension Check ───────────────────────────────────────
ATR_PERIOD                    = 14       # bars for ATR calculation
VOLATILITY_EXTENSION_ATR_MULT = 2.0     # reject if price > 2x ATR above VWAP
VOLATILITY_EXTENSION_ENABLED  = True

# ── ATR-Aware Stops ───────────────────────────────────────────────────────────
ATR_STOP_ENABLED    = True
ATR_STOP_MULTIPLIER = 1.5         # stop = entry - (ATR * multiplier)
ATR_MIN_STOP_PCT    = 0.008       # never tighter than 0.8%
ATR_MAX_STOP_PCT    = 0.025       # never wider than 2.5%

# ── Execution Telemetry ───────────────────────────────────────────────────────
TELEMETRY_LOG_FILE = Path(__file__).parent / "execution_telemetry.jsonl"

# ── Claude Token Optimisation ─────────────────────────────────────────────────
CLAUDE_MIN_LOCAL_SCORE             = 65   # skip Claude if local pre-score below this (default)
CLAUDE_MIN_LOCAL_SCORE_EXPLORATORY = 60   # conditional gate: only for top gapper / strong catalyst /
                                          # RVOL>2.0 / within 10pts of exploratory threshold /
                                          # unusual float or short-interest. Paper-only.
ENABLE_CLAUDE_RESCORING       = True  # set False to run on local scores only (zero Claude cost)
MAX_SYMBOLS_PER_CLAUDE_BATCH  = 12    # max candidates per Claude API call
ANALYST_SCORE_CACHE_FILE      = Path(__file__).parent / "analyst_score_cache.json"

# ── Claude Cache Invalidation Thresholds ──────────────────────────────────────
CLAUDE_CACHE_PRICE_MOVE_INVALIDATE_PCT  = 1.25   # re-score if move_from_open shifts > this %
CLAUDE_CACHE_RVOL_CHANGE_INVALIDATE_PCT = 25     # re-score if RVOL changes > this % (relative)
CLAUDE_CACHE_REGIME_CHANGE_INVALIDATE   = True   # re-score all if market regime changes
CLAUDE_CACHE_NEW_CATALYST_INVALIDATE    = True   # re-score if a new top headline appears

# ── Research Mode ──────────────────────────────────────────────────────────────
CLAUDE_RESEARCH_TOP_N = 10   # send only top N symbols by research interest to Claude

# ── Claude Effectiveness Tracking ─────────────────────────────────────────────
TRACK_CLAUDE_DECISION_DELTA   = True
CLAUDE_EFFECTIVENESS_LOG_FILE = Path(__file__).parent / "claude_effectiveness.jsonl"

# ── Trading Mode (PAPER or LIVE) ──────────────────────────────────────────────
TRADING_MODE = os.getenv("TRADING_MODE", "PAPER" if os.getenv("PAPER_TRADING", "true").lower() == "true" else "LIVE").upper()

if TRADING_MODE == "PAPER":
    import config_paper as _mode
else:
    import config_live as _mode

# Override flat threshold constants with mode-appropriate values
MIN_SCORE_TO_TRADE    = _mode.SCORE_THRESHOLDS["base"]
CHOPPY_MIN_SCORE      = _mode.SCORE_THRESHOLDS["CHOPPY"]
LOW_VOLUME_MIN_SCORE  = _mode.SCORE_THRESHOLDS["LOW_VOLUME"]
HIGH_VOL_MIN_SCORE    = _mode.SCORE_THRESHOLDS.get("HIGH_VOL", 80)
CANDIDATE_EXPIRY_MINS = _mode.CANDIDATE_EXPIRY_MINS

# Position sizing and daily loss — mode can override defaults
if hasattr(_mode, "MAX_POSITIONS"):
    MAX_POSITIONS         = _mode.MAX_POSITIONS
if hasattr(_mode, "POSITION_SIZE_PCT"):
    POSITION_SIZE_PCT     = _mode.POSITION_SIZE_PCT
if hasattr(_mode, "MIN_POSITION_SIZE_PCT"):
    MIN_POSITION_SIZE_PCT = _mode.MIN_POSITION_SIZE_PCT
if hasattr(_mode, "MAX_POSITION_SIZE_PCT"):
    MAX_POSITION_SIZE_PCT = _mode.MAX_POSITION_SIZE_PCT
if hasattr(_mode, "DAILY_LOSS_LIMIT"):
    DAILY_LOSS_LIMIT      = _mode.DAILY_LOSS_LIMIT

# Quality override parameters
QUALITY_OVERRIDE_MIN_RVOL       = _mode.QUALITY_OVERRIDE_MIN_RVOL
QUALITY_OVERRIDE_MAX_SPREAD     = _mode.QUALITY_OVERRIDE_MAX_SPREAD
QUALITY_OVERRIDE_NEWS_IMPACT    = _mode.QUALITY_OVERRIDE_NEWS_IMPACT
QUALITY_OVERRIDE_REQUIRE_ALL    = _mode.QUALITY_OVERRIDE_REQUIRE_ALL
QUALITY_OVERRIDE_MIN_CONDITIONS = _mode.QUALITY_OVERRIDE_MIN_CONDITIONS
QUALITY_OVERRIDE_MAX_GAP_PTS    = _mode.QUALITY_OVERRIDE_MAX_GAP_PTS

# Candidate confidence decay parameters
DECAY_BAND_1_MINS   = _mode.DECAY_BAND_1_MINS
DECAY_BAND_1_POINTS = _mode.DECAY_BAND_1_POINTS
DECAY_BAND_2_MINS   = _mode.DECAY_BAND_2_MINS
DECAY_BAND_2_POINTS = _mode.DECAY_BAND_2_POINTS
DECAY_BAND_3_MINS   = _mode.DECAY_BAND_3_MINS
DECAY_BAND_3_POINTS = _mode.DECAY_BAND_3_POINTS
DECAY_EXPIRE_MINS   = _mode.DECAY_EXPIRE_MINS
DECAY_STRICT_EXPIRE = _mode.DECAY_STRICT_EXPIRE

GAPPER_REFRESH_INTERVAL_MINS = _mode.GAPPER_REFRESH_INTERVAL_MINS

# Live order time gate — earliest and latest ET times for new live entries
LIVE_ORDER_EARLIEST_ET = getattr(_mode, "LIVE_ORDER_EARLIEST_ET", (9,  45))
LIVE_ORDER_LATEST_ET   = getattr(_mode, "LIVE_ORDER_LATEST_ET",   (15, 30))

# Live base score — always 78, used to tag experimental trades in PAPER mode
LIVE_BASE_SCORE = 78


def get_min_score(regime: str = "base", setup_type: str | None = None) -> int:
    """
    Return mode + regime + setup-aware minimum trade score.
    Setup threshold raises the bar for risky setups (midday reversal, low-float)
    or confirms structure for easier setups (ORB, pullback) — effective = max of both.
    """
    regime_score = _mode.SCORE_THRESHOLDS.get(regime, _mode.SCORE_THRESHOLDS["base"])
    if regime_score >= 99:
        return 99
    if setup_type and setup_type in _mode.SETUP_THRESHOLDS:
        setup_score = _mode.SETUP_THRESHOLDS[setup_type]
        return max(regime_score, setup_score)
    return regime_score


# ── Watchlist ──────────────────────────────────────────────────────────────────
WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "TSLA", "AMD", "META", "GOOGL", "AMZN", "NFLX", "AVGO",
    "COIN", "MARA", "PLTR", "SOFI", "HOOD", "RIVN",
    "SMCI", "MU", "QCOM", "INTC", "ARM", "MRVL",
    "DDOG", "ZS", "CRWD", "SNOW", "ROKU", "TTD", "UBER", "LYFT",
    "SPY", "QQQ", "SOXL", "TQQQ",
    "APP", "HIMS",
]
