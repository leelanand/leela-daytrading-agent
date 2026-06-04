"""
LIVE trading mode profile — capital preservation, proven setups only.
Loaded by config.py when TRADING_MODE=LIVE (default when PAPER_TRADING=false).
"""

# ── Score thresholds by regime ────────────────────────────────────────────────
SCORE_THRESHOLDS = {
    "base":          75,
    "TRENDING_UP":   75,
    "TRENDING_DOWN": 99,
    "CHOPPY":        75,
    "LOW_VOLUME":    82,
    "HIGH_VOL":      80,
    "NO_TRADE":      99,
}

# ── Score thresholds by setup type ────────────────────────────────────────────
SETUP_THRESHOLDS = {
    "gap_and_go":         75,
    "news_momentum":      75,
    "vol_spike":          80,
    "trend_continuation": 80,
    "orb_breakout":       75,
    "pullback":           75,
    "midday_reversal":    82,
    "low_float_squeeze":  88,
    "power_hour":         75,
    "vwap_reclaim":       75,
    "orb_continuation":   75,
    "hod_breakout":       75,
}

# ── Quality override — require ALL conditions in LIVE mode ────────────────────
QUALITY_OVERRIDE_MIN_RVOL       = 2.5
QUALITY_OVERRIDE_MAX_SPREAD     = 0.15
QUALITY_OVERRIDE_NEWS_IMPACT    = 70
QUALITY_OVERRIDE_REQUIRE_ALL    = True  # LIVE: all 7 conditions must pass
QUALITY_OVERRIDE_MIN_CONDITIONS = 7
QUALITY_OVERRIDE_MAX_GAP_PTS    = 5    # override up to 5 pts below threshold

# ── Candidate confidence decay — stricter in LIVE ─────────────────────────────
DECAY_BAND_1_MINS   = 20
DECAY_BAND_1_POINTS = 0
DECAY_BAND_2_MINS   = 45
DECAY_BAND_2_POINTS = 4
DECAY_BAND_3_MINS   = 60
DECAY_BAND_3_POINTS = 8
DECAY_EXPIRE_MINS   = 60   # >60 min: expire strictly
DECAY_STRICT_EXPIRE = True

# ── Candidate expiry and gapper refresh ──────────────────────────────────────
CANDIDATE_EXPIRY_MINS        = 60
GAPPER_REFRESH_INTERVAL_MINS = 15

# ── Position sizing — tuned for sub-$25k account under PDT rule ───────────────
# PDT allows 3 day trades per rolling 5-day window.
# 3 × 28% = 84% deployed at target; 3 × 30% = 90% max — same band as before.
MAX_POSITIONS         = 3
POSITION_SIZE_PCT     = 0.28   # target per position (~$370 on $1,320 account)
MIN_POSITION_SIZE_PCT = 0.18   # floor
MAX_POSITION_SIZE_PCT = 0.30   # cap  — 3 × 0.30 = 90% max deployment

# Daily loss limit: 2% of equity — tighter than paper given real capital at risk
# On $1,328 this is ~$26.56; stops all trading for the day if hit.
DAILY_LOSS_LIMIT = 0.02

# ── Live order time gate (ET) ─────────────────────────────────────────────────
# Orders are blocked outside this window.  Earliest = after opening chaos clears
# (chaos lockout already blocks < 09:45); this gate is the hard live-account
# safety layer that survives any future chaos-lockout change.
# Latest new entry = 15:30 ET so positions have time to fill + trail before
# force-close at 15:44.
LIVE_ORDER_EARLIEST_ET = (9,  45)   # 09:45 ET / 14:45 BST — no live orders before this
LIVE_ORDER_LATEST_ET   = (15, 30)   # 15:30 ET / 20:30 BST — no new entries after this

# ── Live entry execution: slippage-capped marketable limit ───────────────────
# Buy limit set 0.3% above quote: fills immediately on liquid names (crosses the
# spread) but NEVER pays more than 0.3% above — protects against filling a spike.
# Realized cost is ~half-spread (far below 0.3%); the cap only binds on a violent
# move. Brackets are anchored to the entry price (executor.py), so R:R stays exact.
# Requires real-time data to be meaningful — set live alongside the SIP upgrade. (2026-06-04)
USE_LIMIT_ORDERS  = True
LIMIT_OFFSET_PCT  = 0.003
