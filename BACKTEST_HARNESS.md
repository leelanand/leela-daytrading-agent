# Backtest Harness — Expectancy Analysis & Cost Modeling

This document describes the comprehensive backtest harness added to the Alpaca trading agent for detailed trade-level analysis with realistic costs.

## Overview

The backtest harness captures detailed metrics for every trade (entry → exit) and generates reports answering:
- **Does the scoring gate have positive edge?** (Claude vs local, overall expectancy net of costs)
- **Which setups are profitable?** (ORB, gap_and_go, pullback, news_momentum)
- **Which regimes are favorable?** (TRENDING_UP, CHOPPY, LOW_VOLUME, HIGH_VOL)
- **What's the net-of-cost expectancy?** (Win% × AvgWin) + (LossRate% × AvgLoss) - AvgCost
- **Is the edge statistically significant?** (Only reports for n ≥ 10 per segment, confident for n ≥ 100)

## Key Modules

### 1. `trade_journal.py` — Enhanced Trade Schema

**Database table: `trade_journal`**

Captures per-trade (entry → exit):

| Field | Purpose |
|-------|---------|
| `symbol`, `date`, `ts_entry`, `ts_exit` | Trade identity |
| `entry_price`, `entry_qty`, `entry_bid`, `entry_ask` | Entry quote |
| `entry_spread_pct`, `entry_slippage_pct` | Entry cost modeling |
| `effective_entry_px` | What you actually paid after costs |
| `claude_score`, `local_score`, `score_used` | Scoring comparison |
| `setup_type` | ORB, gap_and_go, pullback, news_momentum |
| `regime` | TRENDING_UP, CHOPPY, LOW_VOLUME, HIGH_VOL |
| `atr_at_entry`, `atr_stop_pct` | Volatility context |
| `account_risk_pct`, `stop_price`, `target_price`, `intended_r_r` | Risk sizing |
| `exit_price`, `exit_spread_pct`, `exit_slippage_pct`, `effective_exit_px` | Exit cost modeling |
| `exit_reason` | TP_HIT, SL_HIT, TIME_EXIT, EOD_CLOSE, MANUAL |
| `mae_pct`, `mae_price` | Maximum Adverse Excursion (worst price) |
| `realized_pnl`, `realized_pnl_pct` | Net P&L after all costs |
| `realized_pnl_pre_cost`, `realized_cost_total` | Gross P&L and cost breakdown |
| `holding_minutes` | Duration in trade |
| `outcome` | WIN, LOSS, BREAKEVEN |

**Functions:**
```python
from trade_journal import log_entry, log_exit, init_trade_journal, get_trades_for_analysis

# Initialize schema on startup (called by agent.py)
init_trade_journal()

# Log entry
trade_id = log_entry(symbol, entry_price, entry_qty, claude_score, local_score, 
                     setup_type, regime, atr_at_entry, atr_stop_pct,
                     account_risk_pct, stop_price, target_price, intended_r_r,
                     entry_spread_pct, entry_slippage_pct)

# Log exit
log_exit(trade_id, exit_price, exit_reason, mae_pct, mae_price, holding_minutes,
         exit_spread_pct, exit_slippage_pct)

# Fetch for analysis
trades = get_trades_for_analysis(limit=None)  # returns list[dict]
```

### 2. `cost_modeling.py` — Realistic Fill Simulation

Estimates what actual fills would cost, given market conditions.

**Key functions:**

```python
# Estimate bid/ask spread as % of price
spread_pct = estimate_spread_pct(price=100, daily_volume=1_000_000, volatility_20d=0.02)
# Returns ~0.02–0.03% for liquid, 0.10–0.30% for illiquid

# Estimate execution slippage (market impact + timing risk)
slippage_pct = estimate_slippage_pct(side="BUY", volatility_1d=0.015, volume_ratio=1.0)
# Returns positive for BUY (unfavorable), negative for SELL (worse)

# Calculate ATR-based stop with fixed account risk
stop_price = atr_based_stop(atr=0.75, entry_price=100, atr_multiplier=1.5)
# Returns entry - (ATR × 1.5) = fixed $ stop distance

position_size = fixed_account_risk_position_size(
    portfolio_equity=100_000, entry_price=100, stop_price=98.5, risk_pct=0.01
)
# Returns shares for 1% portfolio risk: (100k × 0.01) / 1.5 = 667 shares

# Calculate total round-trip cost in dollars
cost = realized_cost_total(entry_price=100, entry_qty=100, exit_price=102,
                           entry_spread_pct=0.0003, entry_slippage_pct=0.0001,
                           exit_spread_pct=0.0003, exit_slippage_pct=-0.0001)
```

**Cost Model Assumptions:**

| Factor | Range | Notes |
|--------|-------|-------|
| Base spread | 0.02% | minimum for any stock |
| Vol factor | +0.01% per 1% daily vol | reactive to intraday volatility |
| Liquidity factor | 0.0001 × (5M / volume) | inverse to daily volume |
| Entry slippage | +0.01–0.5% | BUY side positive (you pay more) |
| Exit slippage | -0.01–0.5% | SELL side negative (you receive less) |

### 3. `trade_logging.py` — Simple Integration Layer

Wraps `trade_journal` to make it easy to use from `agent.py` without passing all cost modeling details.

```python
from trade_logging import register_trade_entry, register_trade_exit

# At entry
trade_id = register_trade_entry(
    symbol="AAPL", entry_price=150.00, entry_qty=100,
    score=82, setup_type="ORB", regime="TRENDING_UP",
    atr_at_entry=0.75, atr_stop_pct=1.5,
    stop_price=148.50, target_price=154.00
)

# At exit
success = register_trade_exit(
    symbol="AAPL", exit_price=154.50,
    exit_reason="TP_HIT",
    mae_pct=-0.8,  # dipped 0.8% but came back
    mae_price=149.20,
    holding_minutes=45
)
```

### 4. `expectancy_report.py` — Analysis & Reporting

Generates multi-dimensional expectancy analysis.

```python
from expectancy_report import generate_report

# Run analysis
generate_report()
```

**Report sections:**

1. **OVERALL** — All trades combined
2. **BY SETUP TYPE** — Each setup's edge (n ≥ 10)
3. **BY REGIME** — Each regime's edge (n ≥ 10)
4. **BY (SETUP, REGIME) PAIR** — Cross-tabulation (n ≥ 5)
5. **BY SCORING METHOD** — Claude vs local (n ≥ 10 each)
6. **SCORE BUCKETING** — Low (<70), Med (70–84), High (≥85)
7. **SUMMARY & CONCLUSIONS** — Which components have edge

**Output metrics per segment:**

```
SETUP: gap_and_go ✓ EDGE (confident)
  Trades: 45 (W:29 L:16 BE:0)
  Win%: 64.4%
  Avg Winner: $47.23  Avg Loser: -$22.15
  Expectancy (gross): $17.89
  Avg Cost / Trade: $8.42
  Expectancy (net): $9.47      ← THE EDGE (what you keep after costs)
  Profit Factor: 2.04           ← Win$/Abs(Loss$)
  Total P&L: $425.73 (gross: $804.95, costs: $379.22)
```

## How to Use

### 1. Initialize on Agent Startup (Already Done)

```python
from trade_journal import init_trade_journal
init_trade_journal()  # Creates schema on first run
```

### 2. Log Trade Entries

When `place_bracket_order()` executes:

```python
from trade_logging import register_trade_entry

trade_id = register_trade_entry(
    symbol=symbol,
    entry_price=price,
    entry_qty=shares,
    score=score,
    setup_type=setup_type,
    regime=regime,
    atr_at_entry=atr,
    atr_stop_pct=atr_stop,
    stop_price=calculated_stop,
    target_price=calculated_target,
    daily_volume=daily_volume,
    volatility_20d=vol_20d,
    volatility_1d=vol_1d,
    volume_ratio=current_volume / expected_volume
)
```

### 3. Log Trade Exits

When a position closes (stop hit, target hit, EOD close, etc.):

```python
from trade_logging import register_trade_exit
from datetime import datetime

entry_time = ...  # from exits.py tracking
exit_time = datetime.now()
holding_minutes = int((exit_time - entry_time).total_seconds() / 60)

register_trade_exit(
    symbol=symbol,
    exit_price=close_price,
    exit_reason="TP_HIT",  # or SL_HIT, TIME_EXIT, EOD_CLOSE
    mae_pct=max_adverse_excursion_pct,
    mae_price=max_adverse_excursion_price,
    holding_minutes=holding_minutes
)
```

### 4. Generate Report

```bash
python agent.py --expectancy
```

Sample output:
```
================================================================================
EXPECTANCY ANALYSIS REPORT
================================================================================

OVERALL ✓ EDGE (confident)
  Trades: 127 (W:81 L:46 BE:0)
  Win%: 63.8%
  Avg Winner: $44.21  Avg Loser: -$28.73
  Expectancy (gross): $13.99
  Avg Cost / Trade: $7.81
  Expectancy (net): $6.18
  Profit Factor: 1.59
  Total P&L: $785.22 (gross: $1,788.51, costs: $1,003.29)

--- BY SETUP TYPE ---

SETUP: orb_breakout ✓ EDGE (confident)
  Trades: 52 (W:35 L:17 BE:0)
  Win%: 67.3%
  Avg Winner: $51.42  Avg Loser: -$32.15
  Expectancy (gross): $19.72
  Avg Cost / Trade: $8.10
  Expectancy (net): $11.62
  Profit Factor: 2.10
  Total P&L: $604.24 ...

SETUP: gap_and_go ✓ EDGE
  Trades: 45 (W:29 L:16 BE:0)
  Win%: 64.4%
  ... 

SETUP: pullback ✗ NO EDGE
  Trades: 18 (W:12 L:6 BE:0)
  Win%: 66.7%
  Avg Winner: $22.40  Avg Loser: -$18.50
  Expectancy (gross): $8.72
  Avg Cost / Trade: $6.50
  Expectancy (net): $2.22   ← TOO THIN, barely breaks even
  Profit Factor: 1.28
  Total P&L: $39.96 ...

--- BY REGIME ---

REGIME: TRENDING_UP ✓ EDGE (confident)
  Trades: 71 (W:49 L:22 BE:0)
  Win%: 69.0%
  Avg Winner: $49.18  Avg Loser: -$29.50
  Expectancy (gross): $21.56
  Avg Cost / Trade: $8.02
  Expectancy (net): $13.54
  Profit Factor: 2.34
  Total P&L: $961.34 ...

REGIME: LOW_VOLUME ✗ NO EDGE
  Trades: 32 (W:18 L:14 BE:0)
  Win%: 56.2%
  Avg Winner: $31.75  Avg Loser: -$28.40
  Expectancy (gross): $2.57
  Avg Cost / Trade: $7.50
  Expectancy (net): -$4.93   ← NEGATIVE EDGE: avoid trading low-vol
  Profit Factor: 0.92
  Total P&L: -$157.76 ...

--- COMPONENT EDGES ---

Setups with positive expectancy:
  ✓ orb_breakout: $11.62/trade (n=52)
  ✓ gap_and_go: $9.47/trade (n=45)
  ✗ pullback: $2.22/trade (n=18)
  ✗ news_momentum: -$3.15/trade (n=12)

Regimes with positive expectancy:
  ✓ TRENDING_UP: $13.54/trade (n=71)
  ✓ CHOPPY: $5.82/trade (n=24)
  ✗ LOW_VOLUME: -$4.93/trade (n=32)
  ✗ HIGH_VOL: $1.20/trade (n=0, n<10, skipped)
```

## Interpretation Guide

### What is Expectancy?

**Gross Expectancy** = (Win% × AvgWin) + ((1 - Win%) × AvgLoss)

Example: 65% win rate, $50 avg winner, $30 avg loser
- Gross = (0.65 × $50) + (0.35 × -$30) = $32.50 - $10.50 = **$22.00/trade**

**Net Expectancy** = Gross Expectancy - AvgCost

Example (continuing above): $7.50 avg cost per trade
- Net = $22.00 - $7.50 = **$14.50/trade** ← This is what you keep

### Minimum Sample Size

- **n < 10:** Reported but flagged as "n<10, skipped" — too noisy
- **n ≥ 10:** Reported with basic stats
- **n ≥ 100:** Marked "(confident)" — statistically significant

### Red Flags

| Flag | Meaning |
|------|---------|
| `✗ NO EDGE` | Expectancy net ≤ $0 — don't use this setup/regime in live trading |
| `Profit Factor < 1.5` | Win% too low to overcome losses — need higher win rate or tighter stops |
| `n < 100 and confident=false` | Possible luck, not skill — keep testing |
| `Setup works ONLY in one regime` | Overfitted — test robustness across all regimes before going live |

### What to Do With Results

**Before going live:**

1. **Identify positive-edge components:**
   - Which setups have net expectancy > $5–$10/trade?
   - Which regimes are favorable?
   - Does the scoring gate (Claude vs local) add value?

2. **Calculate target sample size for go-live:**
   - Current win rate from report
   - Target confidence interval (e.g., 95%)
   - Min trades = roughly (1.96² × WR × (1-WR)) / (acceptable_error²)
   - For 65% WR, ±5% error = ~360 trades

3. **Flag underperformers to disable:**
   - Pullback setup with -$2.15/trade? Disable or redesign.
   - LOW_VOLUME regime with -$4.93/trade? Add hard blocker.
   - News_momentum <50% win rate? Only use on extreme news days.

4. **Test parameter sensitivity:**
   - Increase stop loss to 2% instead of 1.5%? Re-run to see expectancy change.
   - Higher score gate (80 instead of 75)? Re-run with filtered trades.
   - **Do NOT change parameters mid-test** — invalidates results.

## Integration Checklist

- [x] `trade_journal.py` — Schema + logging functions
- [x] `cost_modeling.py` — Realistic spread/slippage estimation
- [x] `trade_logging.py` — Simple integration layer
- [x] `expectancy_report.py` — Multi-dimensional analysis
- [x] `agent.py` — Initialize journal + `--expectancy` command
- [ ] **TODO: Update `place_bracket_order()` call site** to capture and pass trade context
- [ ] **TODO: Update `exits.py` and position monitor** to log exits when trades close
- [ ] **TODO: Run 100+ trades to populate journal**
- [ ] **TODO: Generate first report and interpret results**

## Next Steps (for user)

1. Run normal trading (paper or live, your choice).
2. Trades will be logged automatically as they enter/exit.
3. After 50–100 completed trades, run:
   ```bash
   python agent.py --expectancy
   ```
4. Review the report:
   - Which setups have positive edge?
   - Which regimes are profitable?
   - Does Claude scoring add value over local scoring?
5. **Before going live:** Only trade setups with confident edge (n ≥ 100, net expectancy > $5/trade).

---

**Questions?** Check `trade_journal.py`, `cost_modeling.py`, and `expectancy_report.py` for function signatures and defaults.
