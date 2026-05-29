"""
PAPER trading mode profile — full end-to-end pipeline test.
Loaded by config.py when TRADING_MODE=PAPER (default when PAPER_TRADING=true).
All live settings are intentionally left unchanged — only paper-specific
execution limits are relaxed here to maximise trade throughput for testing.
"""

# ── Score thresholds by regime ────────────────────────────────────────────────
SCORE_THRESHOLDS = {
    "base":          72,
    "TRENDING_UP":   70,
    "TRENDING_DOWN": 95,   # effectively blocked
    "CHOPPY":        68,
    "LOW_VOLUME":    75,
    "HIGH_VOL":      75,
    "NO_TRADE":      99,
}

# ── Score thresholds by setup type ────────────────────────────────────────────
SETUP_THRESHOLDS = {
    "gap_and_go":         70,
    "news_momentum":      72,
    "vol_spike":          72,
    "trend_continuation": 73,
    "orb_breakout":       70,
    "pullback":           72,
    "midday_reversal":    76,
    "low_float_squeeze":  80,
    "power_hour":         70,
    "vwap_reclaim":       70,
    "orb_continuation":   70,
    "hod_breakout":       72,
}

# ── Quality override — allow lower-score trades if objective quality is strong ─
QUALITY_OVERRIDE_MIN_RVOL       = 2.5    # RVOL >= this
QUALITY_OVERRIDE_MAX_SPREAD     = 0.15   # spread <= this %
QUALITY_OVERRIDE_NEWS_IMPACT    = 70     # news impact >= this
QUALITY_OVERRIDE_REQUIRE_ALL    = False  # PAPER: 5/7 conditions suffice
QUALITY_OVERRIDE_MIN_CONDITIONS = 5
QUALITY_OVERRIDE_MAX_GAP_PTS    = 8     # override up to 8 pts below threshold

# ── Candidate confidence decay ────────────────────────────────────────────────
DECAY_BAND_1_MINS   = 20   # 0-20 min: no decay
DECAY_BAND_1_POINTS = 0
DECAY_BAND_2_MINS   = 45   # 20-45 min: -3 pts
DECAY_BAND_2_POINTS = 3
DECAY_BAND_3_MINS   = 60   # 45-60 min: -6 pts
DECAY_BAND_3_POINTS = 6
DECAY_EXPIRE_MINS   = 90   # >90 min: tag stale but still allow
DECAY_STRICT_EXPIRE = False

# ── Candidate expiry and gapper refresh ──────────────────────────────────────
CANDIDATE_EXPIRY_MINS        = 90
GAPPER_REFRESH_INTERVAL_MINS = 15

# ── Paper execution limits — relaxed for full pipeline testing ────────────────
# Live values are preserved in config_live.py and must not be changed here.
MAX_POSITIONS         = 10       # up to 10 concurrent positions (vs 3 live)
POSITION_SIZE_PCT     = 0.08     # 8% per position × 10 = ~80% deployed
MIN_POSITION_SIZE_PCT = 0.05     # floor
MAX_POSITION_SIZE_PCT = 0.10     # cap — 10 × 10% = 100% max deployment
MAX_TRADES_PER_DAY    = 20       # no meaningful cap — test all setups

# ── Paper time gates — full day coverage ─────────────────────────────────────
LIVE_ORDER_EARLIEST_ET = (9,  35)   # 09:35 ET / 14:35 BST — earlier entry window
LIVE_ORDER_LATEST_ET   = (15, 45)   # 15:45 ET / 20:45 BST — close to EOD close time

# ── Paper trading behaviour flags ────────────────────────────────────────────
CROSS_AGENT_GATE_ENABLED  = False   # allow both agents to trade same symbol independently
ALLOW_REENTRY             = True    # allow re-buying a symbol already held (cycling)
MAX_PORTFOLIO_UTILISATION = 0.95    # stop new entries when deployed >= 95% of portfolio
                                    # auto-resumes when utilisation falls back below limit
