"""
Live score validator — runs per-symbol every Claude scoring cycle.

Implements the full Validator spec:
  1. compute_sanity_score()   — deterministic SANITY_SCORE (0-100) from objective market data
  2. classify()               — compare Claude vs sanity → Case A/B/C/D + severity
  3. hard_checks()            — verify Claude didn't ignore/contradict mandatory evidence
  4. challenge_claude()       — second Claude call (haiku) for severe Case D, gated strictly
  5. apply_override()         — bump score when validator confirms Claude too conservative
  6. run_validator()          — main entry point, returns full ValidatorResult dict

Decision hierarchy:
  Hard safety gates → Objective evidence (SANITY) → Claude → Validator comparison
  → Optional challenge → Final execution decision

Files written:
  validator_flags.jsonl      — per-event anomaly / conflict flags
  score_overrides.jsonl      — every override applied with full audit trail
  validator_challenge.jsonl  — challenge call inputs + Claude's second-pass response
"""
import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_THIS_DIR  = Path(__file__).parent
_OTHER_DIR = Path(r"C:\Users\leela\leela-ibkr-agent")

VALIDATOR_FLAGS_FILE     = _THIS_DIR / "validator_flags.jsonl"
SCORE_OVERRIDES_FILE     = _THIS_DIR / "score_overrides.jsonl"
VALIDATOR_CHALLENGE_FILE = _THIS_DIR / "validator_challenge.jsonl"
FEED_HEALTH_FILE         = _THIS_DIR / "feed_health.json"

# ── Tuning constants ──────────────────────────────────────────────────────────

SANITY_BASE   = 30   # neutral starting point (no evidence either way)
SANITY_HIGH   = 65   # >= this = strong objective case exists
DIFF_MILD     = 10   # mild disagreement — log only
DIFF_ANOMALY  = 15   # anomaly — validator review required
DIFF_SEVERE   = 20   # severe — challenge/override path eligible

OVERRIDE_MIN_SANITY         = 65
OVERRIDE_MIN_DATA_CONF      = 90
OVERRIDE_MIN_VALIDATOR_CONF = 65
OVERRIDE_MAX_SPREAD         = 0.20
OVERRIDE_SCORE_CAP          = 85
MAX_OVERRIDE_PTS            = 8
CHALLENGE_HIGH_CONF_CAP     = 15   # high-confidence confirmed challenge can bridge more of the gap

CHALLENGE_MIN_SANITY    = 65
CHALLENGE_MIN_DIFF      = 20
CHALLENGE_MIN_DATA_CONF = 90

STALE_QUOTE_HARD_FAIL_SECS = 120


# ── I/O helpers ───────────────────────────────────────────────────────────────

def _append(path: Path, entry: dict):
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _now_str() -> str:
    return datetime.now().strftime("%H:%M")


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Missed-opportunity history ────────────────────────────────────────────────

def _load_missed_feedback(lookback_days: int = 5, min_high_pct: float = 5.0) -> dict[str, int]:
    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
    counts: dict[str, int] = defaultdict(int)
    for d in (_THIS_DIR, _OTHER_DIR):
        fb = d / "missed_feedback.jsonl"
        if not fb.exists():
            continue
        try:
            for line in fb.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get("date", "") >= cutoff and rec.get("high_pct", 0) >= min_high_pct:
                        counts[rec["symbol"]] += 1
                except Exception:
                    pass
        except Exception:
            pass
    return dict(counts)


# ── Data confidence (from feed_health.json) ───────────────────────────────────

def _data_confidence() -> tuple[int, bool]:
    """Return (confidence_0_100, hard_fail). hard_fail = stale quotes."""
    try:
        fh         = json.loads(FEED_HEALTH_FILE.read_text())
        age        = fh.get("quote_age_secs", 999)
        status     = fh.get("status", "unknown")
        blocked    = fh.get("block_live_trading", False)
        issues     = fh.get("issues", [])

        if blocked or status not in ("ok", "degraded"):
            return 50, False
        if age > STALE_QUOTE_HARD_FAIL_SECS:
            return 0, True     # hard fail — stale data
        if age < 5:
            conf = 100
        elif age < 30:
            conf = 95
        elif age < 60:
            conf = 85
        else:
            conf = 70
        if issues:
            conf = max(conf - 10 * len(issues), 50)
        return conf, False
    except Exception:
        return 85, False   # default: assume reasonably confident


# ── Signal derivation helpers ─────────────────────────────────────────────────

def _momentum_state(vtrend: float, move: float) -> str:
    """STRENGTHENING | STABLE | WEAKENING derived from vol_trend_ratio and move_from_open."""
    if vtrend >= 1.2 and move > 0:
        return "STRENGTHENING"
    if vtrend >= 0.8:
        return "STABLE"
    return "WEAKENING"


def _vwap_state(bvwap: bool, setup: str) -> str:
    """reclaim_confirmed | above_holding | below"""
    if setup == "vwap_reclaim":
        return "reclaim_confirmed"
    if not bvwap:
        return "above_holding"
    return "below"


def _orb_confirmed(setup: str) -> bool:
    return setup in ("orb_breakout", "orb_continuation")


def _failed_breakout(gap: float, move: float) -> bool:
    return gap > 1.5 and move < -1.0


def _severe_extension(gap: float, move: float) -> bool:
    """Proxy: total move > 15% from reference suggests > 2 ATR extension."""
    return (gap + move) > 15.0


def _market_alignment(spy_gap: float, qqq_gap: float) -> str:
    """aligned | neutral | selling_off"""
    if spy_gap > 0 and qqq_gap > 0:
        return "aligned"
    if spy_gap < -0.5 and qqq_gap < -0.5:
        return "selling_off"
    return "neutral"


# ── Section 2: SANITY_SCORE ────────────────────────────────────────────────────

def compute_sanity_score(inp: dict, ctx: dict) -> tuple[int, dict]:
    """
    Deterministic SANITY_SCORE (0-100) from objective market inputs.
    Returns (score, factors) where factors explains each contribution.
    """
    score   = SANITY_BASE
    factors = {}

    # RVOL
    rvol = inp.get("rvol", 0)
    if rvol >= 4:
        delta = 20; label = f"rvol={rvol:.1f}x (>=4)"
    elif rvol >= 3:
        delta = 15; label = f"rvol={rvol:.1f}x (>=3)"
    elif rvol >= 2:
        delta = 10; label = f"rvol={rvol:.1f}x (>=2)"
    else:
        delta = 0;  label = f"rvol={rvol:.1f}x (<2)"
    score += delta; factors["rvol"] = (delta, label)

    # Spread
    spread = inp.get("spread", 0)
    if spread <= 0.10:
        delta = 10; label = f"spread={spread:.3f}% (tight)"
    elif spread <= 0.15:
        delta = 7;  label = f"spread={spread:.3f}% (ok)"
    elif spread > 0.30:
        delta = -10; label = f"spread={spread:.3f}% (wide)"
    else:
        delta = 0;  label = f"spread={spread:.3f}%"
    score += delta; factors["spread"] = (delta, label)

    # Momentum
    vtrend = inp.get("vtrend", 1.0)
    move   = inp.get("move", 0)
    mom    = _momentum_state(vtrend, move)
    if mom == "STRENGTHENING":
        delta = 15
    elif mom == "STABLE":
        delta = 8
    else:
        delta = -15
    score += delta; factors["momentum"] = (delta, mom)

    # VWAP
    vwap_s = _vwap_state(inp.get("bvwap", False), inp.get("setup", ""))
    if vwap_s == "reclaim_confirmed":
        delta = 12; label = "VWAP reclaim confirmed"
    elif vwap_s == "above_holding":
        delta = 8;  label = "above VWAP holding"
    else:
        delta = -8; label = "below VWAP"
    score += delta; factors["vwap"] = (delta, label)

    # ORB breakout
    if _orb_confirmed(inp.get("setup", "")):
        delta = 12
        score += delta
        factors["orb"] = (delta, f"ORB confirmed ({inp.get('setup','')})")
    else:
        factors["orb"] = (0, "no ORB")

    # News impact
    news_impact = inp.get("news_impact", 0)
    if news_impact >= 80:
        delta = 15; label = f"news_impact={news_impact} (strong)"
    elif news_impact >= 70:
        delta = 10; label = f"news_impact={news_impact} (good)"
    else:
        delta = 0;  label = f"news_impact={news_impact}"
    score += delta; factors["news"] = (delta, label)

    # Market alignment
    spy_gap = ctx.get("spy_gap", 0)
    qqq_gap = ctx.get("qqq_gap", 0)
    align   = _market_alignment(spy_gap, qqq_gap)
    if align == "aligned":
        delta = 10; label = f"SPY {spy_gap:+.2f}% QQQ {qqq_gap:+.2f}% (aligned)"
    elif align == "selling_off":
        delta = -10; label = f"SPY {spy_gap:+.2f}% QQQ {qqq_gap:+.2f}% (selling off)"
    else:
        delta = 0; label = f"SPY {spy_gap:+.2f}% QQQ {qqq_gap:+.2f}% (neutral)"
    score += delta; factors["market"] = (delta, label)

    # Risk penalties
    gap = inp.get("gap", 0)
    if _failed_breakout(gap, move):
        score -= 15; factors["failed_breakout"] = (-15, f"gap={gap:.1f}% move={move:.1f}% (failed)")
    if _severe_extension(gap, move):
        score -= 12; factors["extension"] = (-12, f"total={gap+move:.1f}% (>15%)")
    if spread > 0.20:
        score -= 10; factors["spread_risk"] = (-10, f"spread={spread:.3f}% (widening risk)")

    return max(0, min(100, score)), factors


# ── Section 3: Compare ────────────────────────────────────────────────────────

def _classify(claude_score: int, sanity_score: int, effective_min: int) -> tuple[str, str]:
    """Return (case, severity)."""
    claude_high  = claude_score >= effective_min
    sanity_high  = sanity_score >= SANITY_HIGH

    if not claude_high and not sanity_high:
        case = "A"   # both low — reject normally
    elif claude_high and sanity_high:
        case = "B"   # both high — valid
    elif claude_high and not sanity_high:
        case = "C"   # Claude high, sanity low — possible over-optimism
    else:
        case = "D"   # Claude low, sanity high — possible false negative ← most important

    diff = abs(claude_score - sanity_score)
    if diff <= DIFF_MILD:
        severity = "normal"
    elif diff <= DIFF_ANOMALY:
        severity = "mild"
    elif diff <= DIFF_SEVERE:
        severity = "anomaly"
    else:
        severity = "severe"

    return case, severity


# ── Section 5: Hard checks ────────────────────────────────────────────────────

def _hard_checks(inp: dict, claude_result: dict, sanity_factors: dict) -> list[str]:
    """
    Verify Claude didn't ignore mandatory positive or negative evidence.
    Returns list of CLAUDE_REASONING_CONFLICT strings.
    """
    conflicts = []
    comp       = claude_result.get("components", {})
    red_flags  = claude_result.get("red_flags", [])

    # Mandatory positive evidence Claude should not have under-weighted
    rvol = inp.get("rvol", 0)
    if rvol >= 2.5 and comp.get("volume", 0) < 12:
        conflicts.append(f"RVOL_IGNORED: rvol={rvol:.1f}x but volume_score={comp.get('volume',0)}")

    if inp.get("setup") == "vwap_reclaim" and comp.get("momentum", 0) < 14:
        conflicts.append(f"VWAP_RECLAIM_IGNORED: confirmed reclaim but momentum_score={comp.get('momentum',0)}")

    if _orb_confirmed(inp.get("setup", "")) and comp.get("momentum", 0) < 14:
        conflicts.append(f"ORB_IGNORED: confirmed ORB but momentum_score={comp.get('momentum',0)}")

    news_impact = inp.get("news_impact", 0)
    if news_impact >= 80 and comp.get("news", 0) < 12:
        conflicts.append(f"CATALYST_IGNORED: impact={news_impact} but news_score={comp.get('news',0)}")

    if inp.get("bias") == "BULLISH" and inp.get("rscore", 0) >= 70 and comp.get("market_trend", 0) < 8:
        conflicts.append(f"BULLISH_BIAS_IGNORED: rscore={inp.get('rscore')} but market_trend={comp.get('market_trend',0)}")

    # Mandatory negative evidence Claude should have penalised
    if _failed_breakout(inp.get("gap", 0), inp.get("move", 0)) and not red_flags:
        conflicts.append("FAILED_BREAKOUT_MISSED: gap+move signals failed breakout but no red_flags")

    if _severe_extension(inp.get("gap", 0), inp.get("move", 0)) and comp.get("momentum", 0) > 18:
        conflicts.append(f"EXTENSION_IGNORED: >15% extension but momentum_score={comp.get('momentum',0)} (should be penalised)")

    return conflicts


# ── Section 8: Validator confidence ──────────────────────────────────────────

def _validator_confidence(
    sanity_score: int,
    sanity_factors: dict,
    data_conf: int,
    inp: dict,
    ctx: dict,
    conflicts: list,
) -> int:
    """VALIDATOR_CONFIDENCE (0-100): quality of evidence supporting the validator's position."""
    conf = 0

    # Data freshness
    if data_conf >= 95:   conf += 25
    elif data_conf >= 90: conf += 20
    elif data_conf >= 80: conf += 10
    # else: 0 — poor data quality undermines confidence

    # Sanity score strength
    if sanity_score >= 75:   conf += 20
    elif sanity_score >= 65: conf += 15
    elif sanity_score >= 55: conf += 8

    # Signal consistency — count positive signals pointing same direction
    positive_signals = sum([
        inp.get("rvol", 0) >= 2.0,
        inp.get("spread", 1) < 0.15,
        inp.get("vtrend", 0) >= 1.0 and inp.get("move", 0) > 0,
        not inp.get("bvwap", True),
        _orb_confirmed(inp.get("setup", "")),
        inp.get("news_impact", 0) >= 70,
        _market_alignment(ctx.get("spy_gap", 0), ctx.get("qqq_gap", 0)) == "aligned",
    ])
    if positive_signals >= 5: conf += 25
    elif positive_signals >= 4: conf += 18
    elif positive_signals >= 3: conf += 12
    elif positive_signals >= 2: conf += 6

    # Absence of hard conflicts and risk flags
    if not conflicts and not _failed_breakout(inp.get("gap", 0), inp.get("move", 0)):
        conf += 15
    elif conflicts:
        conf -= 10   # conflicts reduce confidence

    # Regime alignment
    if ctx.get("regime") in ("TRENDING_UP",) and inp.get("bias") in ("BULLISH", "NEUTRAL"):
        conf += 5

    return max(0, min(100, conf))


# ── Section 6: Challenge prompt ────────────────────────────────────────────────

_CHALLENGE_SYSTEM = (
    "You are a trading score reviewer. A first-pass analysis scored a stock candidate too low "
    "given the objective evidence. Re-evaluate objectively and return JSON only."
)

def challenge_claude(
    symbol: str,
    trace_entry: dict,
    sanity_score: int,
    sanity_factors: dict,
    conflicts: list,
) -> dict | None:
    """
    Send a second-pass challenge prompt to Claude Haiku.
    Only called for severe Case D with high data confidence.
    Returns challenge result dict or None on failure.
    """
    try:
        import anthropic
        from config import ANTHROPIC_API_KEY
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    except Exception:
        return None

    inp  = trace_entry.get("input", {})
    ctx  = trace_entry.get("context", {})
    orig = trace_entry.get("claude_score", 0)

    factor_lines = "; ".join(
        f"{k}={v[1]}" for k, v in sanity_factors.items() if v[0] != 0
    )
    conflict_lines = ("; ".join(conflicts)) if conflicts else "none"

    prompt = (
        f"Symbol: {symbol}\n"
        f"Original score: {orig} (below entry threshold)\n\n"
        f"Objective evidence:\n"
        f"  RVOL={inp.get('rvol',0):.1f}x  spread={inp.get('spread',0):.3f}%  "
        f"move={inp.get('move',0):+.1f}%  gap={inp.get('gap',0):.1f}%\n"
        f"  VWAP state={_vwap_state(inp.get('bvwap',True), inp.get('setup',''))}  "
        f"ORB={_orb_confirmed(inp.get('setup',''))}  "
        f"momentum={_momentum_state(inp.get('vtrend',1.0), inp.get('move',0))}\n"
        f"  news_impact={inp.get('news_impact',0)}  "
        f"research_bias={inp.get('bias','NEUTRAL')}  rscore={inp.get('rscore',50)}\n"
        f"  SPY={ctx.get('spy_gap',0):+.2f}%  QQQ={ctx.get('qqq_gap',0):+.2f}%  "
        f"VIX={ctx.get('vix',0):.1f}  regime={ctx.get('regime','?')}\n"
        f"Sanity score: {sanity_score}/100  Factors: {factor_lines}\n"
        f"Conflicts with original scoring: {conflict_lines}\n\n"
        f"Re-evaluate this candidate objectively. "
        f'Return JSON: {{"revised_score":0-100,"was_original_score_wrong":true/false,'
        f'"confidence":"low"|"medium"|"high","should_override":true/false,"reason":"..."}}'
    )

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=_CHALLENGE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        # Note: anthropic SDK raises RateLimitError (subclass of APIStatusError) on 429
        text = resp.content[0].text.strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        result = json.loads(text.strip())
        result["_symbol"]       = symbol
        result["_orig_score"]   = orig
        result["_sanity_score"] = sanity_score
        _append(VALIDATOR_CHALLENGE_FILE, {
            "ts": _ts(), "date": date.today().isoformat(), "time": _now_str(),
            "symbol": symbol, "orig_score": orig, "sanity_score": sanity_score,
            "result": result,
        })
        return result
    except Exception as exc:
        # Rate-limit: log as skipped, not a hard error — OPS agent must not crash on 429
        exc_str = str(exc)
        if "rate_limit" in exc_str or "429" in exc_str:
            _append(VALIDATOR_CHALLENGE_FILE, {
                "ts": _ts(), "date": date.today().isoformat(), "time": _now_str(),
                "symbol": symbol, "orig_score": orig, "error": "rate_limited_skipped",
            })
            return None
        _append(VALIDATOR_CHALLENGE_FILE, {
            "ts": _ts(), "date": date.today().isoformat(), "time": _now_str(),
            "symbol": symbol, "orig_score": orig, "error": str(exc),
        })
        return None


# ── Section 7: Override eligibility ──────────────────────────────────────────

def _override_eligible(
    score: int,
    effective_min: int,
    sanity_score: int,
    data_conf: int,
    validator_conf: int,
    inp: dict,
    red_flags: list,
    hard_fail: bool,
    challenge_result: dict | None = None,
) -> tuple[bool, str]:
    """
    Check all mandatory conditions from the spec.
    Returns (eligible, reason_if_not).
    """
    if hard_fail:
        return False, "hard_fail: stale data"
    if score >= effective_min:
        return False, "already tradeable"
    if sanity_score < OVERRIDE_MIN_SANITY:
        return False, f"sanity_score={sanity_score} < {OVERRIDE_MIN_SANITY}"
    if data_conf < OVERRIDE_MIN_DATA_CONF:
        return False, f"data_confidence={data_conf} < {OVERRIDE_MIN_DATA_CONF}"
    if validator_conf < OVERRIDE_MIN_VALIDATOR_CONF:
        return False, f"validator_confidence={validator_conf} < {OVERRIDE_MIN_VALIDATOR_CONF}"

    mom = _momentum_state(inp.get("vtrend", 1.0), inp.get("move", 0))
    if mom == "WEAKENING":
        return False, "momentum WEAKENING"

    spread = inp.get("spread", 1.0)
    if spread > OVERRIDE_MAX_SPREAD:
        return False, f"spread={spread:.3f}% > {OVERRIDE_MAX_SPREAD}"

    if _failed_breakout(inp.get("gap", 0), inp.get("move", 0)):
        return False, "failed_breakout detected"

    # Must have at least one confirmed setup signal (ORB, VWAP, pullback)
    setup = inp.get("setup", "")
    has_setup = _orb_confirmed(setup) or setup in ("vwap_reclaim", "pullback", "gap_and_go", "news_momentum")
    if not has_setup:
        return False, f"no confirmed setup (setup={setup})"

    # Check red flags from Claude — earnings, halt risk should block
    for flag in red_flags:
        flag_l = flag.lower()
        if any(kw in flag_l for kw in ("earnings", "halt", "pdt", "loss limit", "bracket")):
            return False, f"hard block in red_flags: {flag}"

    is_challenge_high = (
        challenge_result is not None
        and challenge_result.get("should_override")
        and challenge_result.get("confidence") == "high"
    )
    effective_cap = CHALLENGE_HIGH_CONF_CAP if is_challenge_high else MAX_OVERRIDE_PTS
    if (effective_min - score) > effective_cap:
        return False, f"gap {effective_min - score}pts > max override {effective_cap}"

    return True, ""


# ── Section 5+7: Override rules ───────────────────────────────────────────────

def _compute_override(
    symbol: str,
    score: int,
    effective_min: int,
    sanity_score: int,
    data_conf: int,
    validator_conf: int,
    inp: dict,
    missed_count: int,
    challenge_result: dict | None,
) -> tuple[int, list[str]]:
    """Build override bump and rules list. Returns (bump, rules)."""
    rules = []
    bump  = 0

    # Rule 1 — consistent missed-opportunity history
    if missed_count >= 3:
        rules.append(f"missed_feedback x{missed_count} (5d) → +5")
        bump += 5
    elif missed_count >= 2:
        rules.append(f"missed_feedback x{missed_count} (5d) → +3")
        bump += 3

    # Rule 2 — BULLISH research + regime
    bias   = inp.get("bias", "NEUTRAL")
    rscore = inp.get("rscore", 50)
    regime = ""   # passed via sanity context — use inp proxy
    if bias == "BULLISH" and rscore >= 60:
        rules.append(f"BULLISH bias rscore={rscore} → +3")
        bump += 3

    # Rule 3 — strong volume, clean setup
    rvol   = inp.get("rvol", 0)
    spread = inp.get("spread", 1.0)
    rflags = inp.get("rflags", [])
    if rvol >= 2.0 and not rflags and spread < 0.10:
        rules.append(f"rvol={rvol:.1f}x clean (spread={spread:.3f}%) → +2")
        bump += 2

    # Rule 4 — high sanity score (strong objective evidence)
    if sanity_score >= 75:
        rules.append(f"sanity_score={sanity_score} (strong) → +2")
        bump += 2
    elif sanity_score >= 65:
        rules.append(f"sanity_score={sanity_score} → +1")
        bump += 1

    # Rule 5 — near-miss
    gap_pts = effective_min - score
    if gap_pts <= 2 and (bias == "BULLISH" or rvol >= 1.8):
        rules.append(f"near-miss {gap_pts}pt from threshold → +1")
        bump += 1

    # Rule 6 — challenge confirmed override (tiered by confidence)
    challenge_high_conf = False
    if challenge_result and challenge_result.get("should_override"):
        confidence = challenge_result.get("confidence", "low")
        revised    = challenge_result.get("revised_score", score)
        if revised > score:
            if confidence == "high":
                extra = min(revised - score, CHALLENGE_HIGH_CONF_CAP)
                challenge_high_conf = True
                rules.append(f"challenge_high_conf: revised={revised} -> +{extra}")
            elif confidence == "medium":
                extra = min(revised - score, 6)
                rules.append(f"challenge_medium_conf: revised={revised} -> +{extra}")
            else:
                extra = min(revised - score, 2)
                rules.append(f"challenge_low_conf: revised={revised} -> +{extra}")
            bump += extra

    # High-confidence confirmed challenge gets a raised total cap
    total_cap = CHALLENGE_HIGH_CONF_CAP if challenge_high_conf else MAX_OVERRIDE_PTS
    bump = min(bump, total_cap)
    return bump, rules


# ── Main entry point ──────────────────────────────────────────────────────────

def run_validator(trace_entry: dict) -> dict:
    """
    Main entry point. Call once per fresh Claude score.
    Returns ValidatorResult dict:
      sanity_score, sanity_factors, case, diff, severity, anomalies,
      hard_conflicts, validator_confidence, data_confidence, hard_fail,
      override_eligible, override_rules, final_score, challenge_run, challenge_result
    """
    source = trace_entry.get("source", "")
    if source != "claude":
        # Only validate fresh Claude scores; cache/local need no re-evaluation
        return {"source": source, "skipped": True, "override_rules": [], "final_score": trace_entry.get("claude_score") or trace_entry.get("local_score", 0)}

    sym          = trace_entry.get("symbol", "?")
    inp          = trace_entry.get("input", {})
    ctx          = trace_entry.get("context", {})
    claude_score = trace_entry.get("claude_score", 0) or 0
    effective_min = trace_entry.get("effective_min", 75)
    red_flags    = trace_entry.get("red_flags", [])

    # ── 1. Data confidence ────────────────────────────────────────────────────
    data_conf, hard_fail = _data_confidence()

    # ── 2. Sanity score ───────────────────────────────────────────────────────
    sanity_score, sanity_factors = compute_sanity_score(inp, ctx)

    # ── 3. Classify ───────────────────────────────────────────────────────────
    case, severity = _classify(claude_score, sanity_score, effective_min)
    diff = abs(claude_score - sanity_score)

    # ── 4. Hard checks ────────────────────────────────────────────────────────
    conflicts = _hard_checks(inp, trace_entry, sanity_factors)

    # Collect all anomalies (component-level + conflicts)
    anomalies = list(conflicts)

    # ── 5. Validator confidence ───────────────────────────────────────────────
    validator_conf = _validator_confidence(sanity_score, sanity_factors, data_conf, inp, ctx, conflicts)

    # ── 6. Challenge (Case D, severe, gated) ──────────────────────────────────
    challenge_result = None
    challenge_run    = False

    if (case == "D" and severity == "severe"
            and sanity_score >= CHALLENGE_MIN_SANITY
            and data_conf >= CHALLENGE_MIN_DATA_CONF):
        challenge_result = challenge_claude(sym, trace_entry, sanity_score, sanity_factors, conflicts)
        challenge_run    = True

    # ── 7. Override eligibility ───────────────────────────────────────────────
    eligible, ineligible_reason = _override_eligible(
        claude_score, effective_min, sanity_score, data_conf,
        validator_conf, inp, red_flags, hard_fail,
        challenge_result=challenge_result,
    )

    override_rules = []
    final_score    = claude_score

    if eligible:
        missed       = _load_missed_feedback()
        missed_count = missed.get(sym, 0)
        bump, rules  = _compute_override(
            sym, claude_score, effective_min, sanity_score, data_conf,
            validator_conf, inp, missed_count, challenge_result,
        )
        if bump > 0:
            final_score  = min(claude_score + bump, OVERRIDE_SCORE_CAP)
            override_rules = rules
            now_tradeable  = final_score >= effective_min
            _append(SCORE_OVERRIDES_FILE, {
                "ts":              _ts(),
                "date":            date.today().isoformat(),
                "time":            _now_str(),
                "symbol":          sym,
                "original_score":  claude_score,
                "override_pts":    bump,
                "new_score":       final_score,
                "effective_min":   effective_min,
                "now_tradeable":   now_tradeable,
                "rules":           rules,
                "case":            case,
                "sanity_score":    sanity_score,
                "validator_conf":  validator_conf,
                "data_conf":       data_conf,
                "challenge_run":   challenge_run,
            })
    else:
        ineligible_reason = ineligible_reason or "no override rules fired"

    # ── 8. Write validator flag if anomaly/severe or conflicts ─────────────────
    if anomalies or severity in ("anomaly", "severe"):
        _append(VALIDATOR_FLAGS_FILE, {
            "ts":               _ts(),
            "date":             date.today().isoformat(),
            "time":             _now_str(),
            "symbol":           sym,
            "case":             case,
            "severity":         severity,
            "claude_score":     claude_score,
            "sanity_score":     sanity_score,
            "diff":             diff,
            "anomalies":        anomalies,
            "sanity_factors":   {k: v[1] for k, v in sanity_factors.items()},
            "validator_conf":   validator_conf,
            "data_conf":        data_conf,
            "override_applied": bool(override_rules),
        })

    return {
        "sanity_score":        sanity_score,
        "sanity_factors":      sanity_factors,
        "case":                case,
        "diff":                diff,
        "severity":            severity,
        "anomalies":           anomalies,
        "hard_conflicts":      conflicts,
        "validator_confidence": validator_conf,
        "data_confidence":     data_conf,
        "hard_fail":           hard_fail,
        "override_eligible":   eligible,
        "override_rules":      override_rules,
        "final_score":         final_score,
        "challenge_run":       challenge_run,
        "challenge_result":    challenge_result,
        "ineligible_reason":   ineligible_reason if not eligible else "",
    }
