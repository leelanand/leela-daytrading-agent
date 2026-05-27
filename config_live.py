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
