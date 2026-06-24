# Option 2: One-Week Execution Plan

**Decision Date**: End of Week (Friday EOD)
**Final Verdict**: Which edges proceed to live → which are killed by data

---

## Day 1 (Wednesday 2026-06-24)

### Morning: Load Historical Data
```bash
# Obtain 12+ months of historical data

# PEAD: Earnings data
# Source options (in priority order):
# 1. Finnhub API (has historical earnings; free tier limits apply)
# 2. Yahoo Finance CSV (historical earnings + prices)
# 3. Local CSV file (if you have pre-cached earnings history)

# Save as: data/earnings_history_12m.csv
# Format: symbol,date,actual_eps,estimate_eps,price_day_before,price_day_after,atr_5d

# Reconstitution: Index rebalance history
# Source options:
# 1. Russell index website (historical additions/deletions, May/August)
# 2. S&P index announcements (real-time and historical)
# 3. Local CSV (pre-cached rebalance events)

# Save as: data/rebalances_history_12m.csv
# Format: index,event_type,symbol,announcement_date,effective_date,price_at_announcement,price_at_effective
```

### Afternoon: Run Backtests
```bash
# Wire up CSV paths in backtest_filter.py
# Update earnings_csv and rebalances_csv parameters

python backtest_filter.py
# Output: PEAD edge signal + reconstitution edge signal
# Format: PROCEED | KILL | INSUFFICIENT_DATA
```

### End of Day 1: Filter Decision
**Expected Output**:
```
FILTER SIGNAL:
  PEAD:              PROCEED ($X/trade, n=50+)  OR  KILL (negative edge)  OR  INSUFFICIENT
  Reconstitution:    PROCEED ($Y/trade, n=20+)  OR  KILL (negative edge)  OR  INSUFFICIENT

RECOMMENDATION:
  Paper trade: [mechanisms with PROCEED]
  Kill immediately: [mechanisms with KILL]
  Skip: [mechanisms with INSUFFICIENT]
```

**Action**:
- ✓ If PEAD shows PROCEED: start paper trading PEAD tomorrow
- ✗ If PEAD shows KILL: delete pead_scanner.py, skip PEAD entirely
- ✓ If Reconstitution shows PROCEED: start paper trading reconstitution tomorrow
- ✗ If Reconstitution shows KILL: delete recon_scanner.py, skip reconstitution entirely

---

## Days 2–6 (Thursday–Monday)

### Paper Trading (Execution Validation)

**Setup** (Wednesday evening or Thursday morning):
```bash
# Disable live trading
export PAPER_TRADING=true

# Start agent in paper mode
python agent.py --continuous

# Start dashboard (monitoring)
python dashboard.py

# Start ops_agent (autonomous health checks)
python ops_agent.py
```

**What's happening**:
- Agent scans PEAD and/or reconstitution (depending on PROCEED signals)
- Places paper orders (no real capital)
- Logs every trade: entry, exit, costs, outcome
- Compares paper execution to backtest assumptions

**What you're validating**:
1. **Spread assumptions**: Are real spreads close to model estimates?
2. **Slippage reality**: Are fills better/worse than expected?
3. **Entry timing**: Can we actually enter on backtest signal dates?
4. **Exit execution**: Are stop/target fills realistic?
5. **Holding period**: Do positions hold as expected?

**Daily checks** (Thursday–Monday):
```bash
# Each morning:
python agent.py --status

# Shows:
# - Open paper positions (from PROCEED mechanisms)
# - Daily P&L (should roughly match backtest)
# - Any divergence from backtest expectations

# Each evening:
# Review audit.log for:
# - Order fills vs expectations
# - Slippage vs cost_modeling
# - Entry/exit prices vs backtest
```

### Mid-Week Analysis (Friday EOD)

```bash
# After 2–3 trading days of paper trading (10–15 trades):

python agent.py --paper  # One more full simulation
python agent.py --status # Check positions

# Questions to answer:
# 1. Did paper execution match backtest?
#    → Slippage within 10% of estimate?
#    → Spreads match model?
#    → Fills on timing assumptions?
#
# 2. Any unexpected mechanics?
#    → Gap risk different than expected?
#    → Liquidity issues?
#    → Regime changes?
#
# 3. What would the paper P&L be if we go live?
#    → Annualize 2-day paper return
#    → Compare to backtest expectancy
```

---

## Day 5 (Friday) – Go/No-Go Decision

### Morning: Collect Final Data
```bash
# After 4 trading days of paper trading:
python agent.py --paper

# Generate final paper trading report
python agent.py --status

# Audit trail
cat audit.log | tail -50  # Recent paper trades
```

### Afternoon: Compare Paper vs Backtest

**Comparison checklist**:
| Metric | Backtest Assumption | Paper Actual | Match? |
|--------|----------------------|--------------|--------|
| Avg entry spread | model (e.g., 0.025%) | actual | ±10% OK? |
| Avg exit spread | model (e.g., 0.030%) | actual | ±10% OK? |
| Avg entry slippage | model (e.g., 0.01%) | actual | ±50% OK? |
| Win rate (first 10 trades) | model (e.g., 58%) | actual | ±10% OK? |
| Avg winner | model (e.g., $8) | actual | ±15% OK? |
| Avg loser | model (e.g., $3) | actual | ±15% OK? |

**Decision logic**:
```
For each PROCEED mechanism:

IF paper metrics match backtest (within 10–15% tolerance):
  → Execution is valid
  → Backtest is honest
  → CONFIDENCE HIGH for live

IF paper metrics diverge significantly (>20% error):
  → Execution different than model
  → Backtest may overestimate (or underestimate)
  → CONFIDENCE MEDIUM
  → Investigate cause before live

IF paper shows immediate losses (first 5 trades negative):
  → Backtest may be wrong
  → Mechanism might not work
  → CONFIDENCE LOW
  → Do NOT go live; iterate
```

### End of Day: Final Decision

**Three possible outcomes**:

#### Outcome A: "Go Live" (High Confidence)
Conditions:
- Backtest shows positive edge (expectancy_net > $0/trade)
- Sample size ≥ 30 trades (PEAD) or ≥ 20 trades (recon)
- Paper trading matches backtest (within 10–15%)
- No unexpected mechanics discovered

**Action**:
```
Next week (Week of July 1):
  1. Switch to LIVE mode
  2. Start with small capital ($5k–10k test pool)
  3. Run agent normally (both paths)
  4. Monitor daily P&L vs expectancy
  5. If live matches paper: scale to full portfolio
```

#### Outcome B: "Iterate" (Medium Confidence)
Conditions:
- Backtest shows edge, but paper diverges (15–25% error)
- OR paper initial results mixed (50/50 win rate, but n<10)
- OR mechanism valid but execution needs tuning

**Action**:
```
Week of July 1:
  1. Stay in paper mode
  2. Collect 20 more trades with refined execution
  3. Adjust cost model or entry timing based on paper results
  4. Re-run backtest comparison
  5. Decision point: live or kill, mid-week July 8
```

#### Outcome C: "Kill" (Low Confidence)
Conditions:
- Backtest shows negative edge (expectancy_net < $0/trade)
- OR paper trading immediately unprofitable (first 5 trades: -3%+)
- OR mechanism preconditions don't hold in reality

**Action**:
```
Immediate:
  1. Delete mechanism scanner from codebase
  2. Log decision: "killed PEAD/recon by data"
  3. Do NOT pursue live
  4. Do NOT tune parameters (would overfit)
  5. Move to next candidate (forced-selling, new ideas)
```

---

## Execution Checklist

### Data Preparation (Day 1)
- [ ] Download 12+ months of earnings history (PEAD)
- [ ] Download 12+ months of rebalance history (reconstitution)
- [ ] Save as CSV files in `data/` directory
- [ ] Verify CSV format matches backtest expectations

### Backtest Run (Day 1)
- [ ] Wire CSV paths into `backtest_filter.py`
- [ ] Run `python backtest_filter.py`
- [ ] Capture output: PEAD signal + reconstitution signal
- [ ] Determine which mechanisms to paper-trade

### Paper Trading (Days 2–6)
- [ ] Set `PAPER_TRADING=true` in config
- [ ] Start agent: `python agent.py --continuous`
- [ ] Start dashboard: `python dashboard.py`
- [ ] Start ops_agent: `python ops_agent.py`
- [ ] Monitor daily: `python agent.py --status`
- [ ] Audit daily: review entry/exit prices vs expectations

### Analysis (Friday EOD)
- [ ] Collect final paper trading results
- [ ] Compare paper metrics to backtest
- [ ] Fill comparison table (spread, slippage, win%, expectancy)
- [ ] Assess divergence tolerance (<10%? >20%?)
- [ ] Make go/no-go decision

### Final Decision (Friday EOD)
- [ ] Document decision: Go Live | Iterate | Kill
- [ ] If Kill: remove mechanism from codebase
- [ ] If Go Live or Iterate: document next steps
- [ ] Commit to git with decision timestamp

---

## Success Criteria

### For "Go Live" Decision
✓ Backtest edge: +$X/trade (statistically significant)
✓ Paper validation: ±10–15% of backtest metrics
✓ Sample size: ≥30 (PEAD) or ≥20 (recon)
✓ No contradictory mechanics discovered
✓ Confidence rating: MEDIUM or HIGH

### For "Kill" Decision
✗ Backtest: negative edge or insufficient data
✗ Paper: immediate losses or divergence >25%
✗ Mechanism: preconditions fail in reality
✗ No reason to pursue further

### For "Iterate" Decision
~ Backtest: positive but uncertain
~ Paper: divergence 15–25% or mixed results
~ Need: more data or execution refinement
~ Next decision: mid-week week of July 8

---

## Key Principle

**One week filters; it does not confirm.**

- Backtest can say "no edge here" (decisive rejection)
- Paper trading validates "yes, we can execute at estimated costs"
- But 1 week cannot confirm "this edge is real long-term"

That confirmation comes from:
- 50+ live trades (minimum)
- Multiple market regimes (not just current week)
- Season-adjusted results (earnings calendar, rebalance cycles repeat)

---

## Actual Commands

```bash
# Day 1 (Wednesday): Load data, run backtest
python backtest_filter.py

# Days 2-6 (Thu-Mon): Paper trading, validation
python agent.py --continuous  # Background, continuous loop
python agent.py --status       # Check daily status
python agent.py --paper        # Single simulation (optional)

# Friday EOD: Final decision
python agent.py --status  # Collect final results
# Review audit.log for detailed trades
# Fill comparison table
# Make go/no-go decision

# Next: commit decision to git
git add -A
git commit -m "Decision: [Go Live | Iterate | Kill] after 1-week filter"
```

---

## One Week: Maximum Timeline

| Day | Task | Output |
|-----|------|--------|
| **Wed** | Load data, run backtest | PROCEED / KILL signals |
| **Thu** | Paper trading starts | 5–10 trades |
| **Fri** | Paper trading continues | 10–15 trades, mid-week analysis |
| **Mon** | Paper trading continues | 15–20 trades, prep for decision |
| **Tue** | Final validation | Complete comparison table |
| **Wed** | Go/no-go decision | Commit decision to git |
| **Thu** | Execute decision | Live trading (if Go) OR iteration/kill |

**No extensions.** Data decides.

