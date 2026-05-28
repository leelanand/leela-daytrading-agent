"""
LIVE trading mode profile — capital preservation, proven setups only.
Loaded by config.py when TRADING_MODE=LIVE (default when PAPER_TRADING=false).
"""

# ── Score thresholds by regime ────────────────────────────────────────────────
SCORE_THRESHOLDS = {
    "base":          78,
    "TRENDING_UP":   78,
    "TRENDING_DOWN": 99,
    "CHOPPY":        73,
    "LOW_VOLUME":    82,
    "HIGH_VOL":      80,
    "NO_TRADE":      99,
}

# ── Score thresholds by setup type ────────────────────────────────────────────
SETUP_THRESHOLDS = {
    "gap_and_go":         78,
    "news_momentum":      78,
    "vol_spike":          80,
    "trend_continuation": 80,
    "orb_breakout":       75,
    "pullback":           78,
    "midday_reversal":    82,
    "low_float_squeeze":  88,
    "power_hour":         76,
    "vwap_reclaim":       76,
    "orb_continuation":   76,
    "hod_breakout":       78,
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
# MAX_POSITIONS=2 conserves the allowance: worst case 2 trades/day × 3 days = fine.
# 40% target per position × 2 max = 80% deployed at full capacity (~90% target met
# across real fills which are rarely simultaneous).
MAX_POSITIONS         = 2
POSITION_SIZE_PCT     = 0.40   # target per position
MIN_POSITION_SIZE_PCT = 0.25   # floor — never smaller than 25%
MAX_POSITION_SIZE_PCT = 0.45   # cap  — 2 × 0.45 = 90% max deployment

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
