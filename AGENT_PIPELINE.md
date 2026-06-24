# Agent Pipeline & Handoff Architecture

## 1. TRADING AGENT PIPELINE (High-Level Flow)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         MARKET DATA INGESTION                               │
│  Alpaca API → Polygon → Massive Data → Yahoo Finance (fallback)            │
│                                                                             │
│  Inputs: Price, Volume, Spreads, News, Fundamentals                       │
└────────────────────────────┬────────────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                      PRESCAN & GAP DETECTION                               │
│  (--prescan @ 9:45 ET / gapper.py)                                        │
│                                                                             │
│  Finds: Stocks gapped up ≥1.5% with rel_volume ≥1.3x                     │
│  Saves: candidates.json (9 candidates max, 90-min expiry)                 │
└────────────────────────────┬────────────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    RESEARCH & SCORING                                       │
│  (analyst.py → Claude + local scoring)                                     │
│                                                                             │
│  Per Candidate:                                                             │
│  ├─ Claude Score (0-100): momentum, news, technicals, conviction           │
│  ├─ Local Score: ORB, pullback, gap-strength, RVOL, spread                │
│  ├─ Setup Type: ORB, gap_and_go, pullback, news_momentum                  │
│  └─ Research Notes: risks, catalysts, sector context                       │
│                                                                             │
│  Handoff: candidates.json + research_cache.json                            │
└────────────────────────────┬────────────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    GATES & VALIDATION (Multiple Passes)                     │
│  (agent.py _scan_and_trade)                                                │
│                                                                             │
│  Gate 1 - Score Gate:          score >= MIN_SCORE (75 base, 85 low-vol)    │
│  Gate 2 - Event Risk Gate:     No earnings, halts, circuit breakers        │
│  Gate 3 - Risk Gate:           Max positions (3), daily loss limit (3%)     │
│  Gate 4 - Spread Gate:         spread_pct < 0.30%                         │
│  Gate 5 - Regime Gate:         Only trade TRENDING_UP/CHOPPY/LOW_VOL       │
│  Gate 6 - Setup Gate:          ORB/pullback/news confirmed                 │
│  Gate 7 - Quality Override:    Low score but high RVOL/news/spread OK?     │
│  Gate 8 - PDT Guard (live):    Protect Pattern Day Trader limits           │
│                                                                             │
│  Rejected trades → audit trail (rejection reason logged)                    │
│  Approved trades → ENTRY EXECUTION                                         │
└────────────────────────────┬────────────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    ENTRY EXECUTION                                          │
│  (executor.py → place_bracket_order)                                       │
│                                                                             │
│  1. Calculate Position Size (dynamic by score, ATR, volatility)            │
│  2. Place Bracket Order:                                                    │
│     ├─ BUY @ market/limit (entry_price)                                    │
│     ├─ TP @ +2.5% (take_profit_price)                                      │
│     └─ SL @ -1.5% (stop_loss_price)                                        │
│  3. Log Entry → trade_journal:                                              │
│     ├─ entry_price, entry_qty, entry_spread_pct, entry_slippage_pct       │
│     ├─ claude_score, setup_type, regime, atr_at_entry                      │
│     └─ stop_price, target_price, intended_r_r                              │
│                                                                             │
│  Handoff: trade_id (for later exit logging)                                │
└────────────────────────────┬────────────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    POSITION MONITORING (--monitor every 2 min)              │
│  (exits.py → check_exits + advanced exit conditions)                       │
│                                                                             │
│  Exits Checked:                                                             │
│  1. Bracket Hit:    TP hit (+2.5%) or SL hit (-1.5%) [automatic]          │
│  2. Trailing Stop:  Triggers after +1.5% gain, trails at -1.0%            │
│  3. Momentum Flip:  Peaked but fell back below entry                       │
│  4. Time-Based:     No movement after 90 min → exit                        │
│  5. EOD Close:      All positions force-closed @ 15:45 ET                  │
│                                                                             │
│  MAE Tracking: Min price during holding period recorded                    │
└────────────────────────────┬────────────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    EXIT EXECUTION & LOGGING                                 │
│  (exits.py → close_position + log_exit)                                    │
│                                                                             │
│  1. Close Position @ market price                                          │
│  2. Log Exit → trade_journal:                                               │
│     ├─ exit_price, exit_spread_pct, exit_slippage_pct                      │
│     ├─ exit_reason (TP_HIT, SL_HIT, TIME_EXIT, EOD_CLOSE, MANUAL)         │
│     ├─ mae_pct, mae_price (max adverse excursion)                          │
│     ├─ realized_pnl (gross), realized_cost_total (spread+slippage)         │
│     └─ outcome (WIN, LOSS, BREAKEVEN)                                      │
│                                                                             │
│  Handoff: Completed trade record → expectancy analysis                     │
└────────────────────────────┬────────────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    ANALYSIS & FEEDBACK                                      │
│  (expectancy_report.py → --expectancy command)                             │
│                                                                             │
│  Post-Trade Analysis (requires n ≥ 50 trades):                             │
│  ├─ Win Rate & Expectancy (gross & net of costs)                          │
│  ├─ By Setup Type: ORB edge vs gap_and_go edge vs pullback edge           │
│  ├─ By Regime: TRENDING_UP edge vs LOW_VOLUME edge                        │
│  ├─ By Score Method: Claude scoring vs local scoring effectiveness        │
│  └─ Profit Factor & MAE Distribution                                      │
│                                                                             │
│  Output: Which components have positive edge? Which to disable?           │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. AGENT ORCHESTRATION & HANDOFFS

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                     ORCHESTRATOR: Scheduler + agent.py                       │
│                                                                              │
│  Windows Task Scheduler fires at fixed times:                               │
│  ├─ 08:30 / 13:30  --research       Pre-market fundamentals               │
│  ├─ 09:40 / 14:40  --precheck       Data readiness gate                    │
│  ├─ 09:45 / 14:45  --prescan         Find candidates (no orders)           │
│  ├─ 09:45 / 14:45  --continuous     Adaptive scan loop until 15:30 ET     │
│  ├─ 10:00+ / 15:00+ --scan (3x)     Fixed scans + --monitor-loop          │
│  ├─ 15:30 / 20:30  --cutoff         Cancel unfilled limits               │
│  ├─ 15:44 / 20:44  --close          Force-close all positions EOD        │
│  └─ 16:15 / 21:15  --report         P&L summary                           │
└────────┬───────────────────────────────────────────────────────┬───────────┘
         │                                                       │
         ▼                                                       ▼
    ┌─────────────────┐                              ┌──────────────────┐
    │  agent.py       │                              │  dashboard.py    │
    │  (main logic)   │◄────────────────────────────►│  (web monitoring) │
    └──┬──────────────┘                              └────────┬─────────┘
       │                                                      │
       ├─ Prescan → candidates.json                         │
       ├─ Score → research_cache.json                       │
       ├─ Gates → audit.log                                 │
       ├─ Entry → place_bracket_order()                     │
       ├─ Monitor → check_exits()                           │
       └─ Exit → trade_journal                              │
                                                            │
                                                    Reads & displays:
                                                    • candidates
                                                    • positions
                                                    • P&L
                                                    • regime
                                                    • audit log
                                                    
         ▼
    ┌──────────────────────────┐
    │  executor.py             │
    │  (order placement)        │
    ├──────────────────────────┤
    │ place_bracket_order()    │
    │  ├─ Calculate qty        │
    │  ├─ Alpaca submit order  │
    │  └─ Log to trade_journal │
    └──┬───────────────────────┘
       │
       ▼
    ┌──────────────────────────┐
    │  exits.py                │
    │  (position management)    │
    ├──────────────────────────┤
    │ check_exits()            │
    │  ├─ Monitor TP/SL        │
    │  ├─ Trailing stops       │
    │  ├─ Time exits           │
    │  └─ Log to trade_journal │
    └──────────────────────────┘

         ▼
    ┌──────────────────────────────────────┐
    │  ops_agent.py (Autonomous OPS)       │
    │  (runs every 8 min during trading)    │
    ├──────────────────────────────────────┤
    │ 1. Fetch full BPM dashboard          │
    │ 2. Reason over 14 pipeline sections  │
    │ 3. Auto-fix common issues:           │
    │    ├─ Restart dashboard if down      │
    │    ├─ Kill zombie processes          │
    │    ├─ Check data freshness           │
    │    └─ Alert on critical failures     │
    │ 4. Return: {fixed, notify, obs}      │
    └──────────────────────────────────────┘

         ▼
    ┌──────────────────────────────────────┐
    │  trade_journal.db                    │
    │  (persistent trade record)            │
    ├──────────────────────────────────────┤
    │ Entries logged:                      │
    │  ├─ Entry price, qty, spread, slip  │
    │  ├─ Claude/local score, setup       │
    │  ├─ ATR, regime, R:R                │
    │  └─ trade_id for later exit logging │
    │                                      │
    │ Exits logged:                        │
    │  ├─ Exit price, spread, slip        │
    │  ├─ Exit reason, MAE                │
    │  ├─ Realized P&L (gross & net)      │
    │  └─ Outcome (WIN/LOSS/BE)           │
    └──────────────────────────────────────┘

         ▼
    ┌──────────────────────────────────────┐
    │  expectancy_report.py                │
    │  (post-trade analysis)                │
    ├──────────────────────────────────────┤
    │ Queries: trade_journal (n ≥ 50)      │
    │                                      │
    │ Reports:                             │
    │  ├─ Overall edge & confidence        │
    │  ├─ Edge by setup type               │
    │  ├─ Edge by regime                   │
    │  ├─ Edge by score method             │
    │  └─ Profit factor & survival %       │
    │                                      │
    │ Output: Which components work?       │
    │         Which to disable?            │
    └──────────────────────────────────────┘
```

---

## 3. DATA FLOW & HANDOFF POINTS

### Handoff #1: Market Data → Prescan
```
Alpaca API          Polygon              Massive Data
    │                 │                      │
    └─────────────────┴──────────────────────┘
                     │
              (cross-validation)
                     │
                     ▼
           GAPPER DETECTION (gapper.py)
           ├─ Price gaps ≥1.5%
           ├─ Relative volume ≥1.3x
           └─ Filter delisted / low-float
                     │
                     ▼
           candidates.json (9 candidates)
           ├─ symbol, gap_pct, rel_volume
           ├─ prescan_price, prescan_time
           └─ tradeable = True/False
```

### Handoff #2: Candidates → Research & Scoring
```
candidates.json (from prescan)
           │
           ▼
ANALYST.PY (analyst.py)
├─ Claude Scoring:
│  ├─ Input: news, technicals, momentum, conviction
│  ├─ Output: claude_score (0-100)
│  └─ Confidence level
│
├─ Local Scoring:
│  ├─ ORB breakout strength
│  ├─ Pullback quality
│  ├─ RVOL quantile
│  └─ Spread penalty
│
└─ Output: score, setup_type, reasoning
           │
           ▼
research_cache.json
├─ symbol, claude_score, local_score
├─ setup_type, regime_fit
├─ news_impact, institutional_activity
└─ risks, catalysts
```

### Handoff #3: Scored Candidates → Entry Gates
```
research_cache.json + real-time data
           │
           ▼
GATES (agent.py _scan_and_trade)
├─ Score gate:       score >= threshold?
├─ Event risk:       earnings/halt/circuit?
├─ Risk limit:       max positions / daily loss?
├─ Spread gate:      spread < 0.30%?
├─ Regime gate:      trade in this market?
├─ Setup confirmed:  ORB/pullback/news live?
└─ Quality override: low score but high-quality setup?
           │
           ├─ REJECT → audit.log (reason)
           │
           └─ APPROVE → Entry execution
                     │
                     ▼
           executor.py (place_bracket_order)
           ├─ Entry price (quote-adjusted)
           ├─ Position size (ATR-scaled)
           ├─ Bracket: TP +2.5% / SL -1.5%
           └─ Log entry → trade_journal (trade_id)
```

### Handoff #4: Open Positions → Exit Monitoring
```
trade_journal (entry logged)
           │
           ▼
MONITOR LOOP (exits.py every 2 min)
├─ Check bracket hits (TP/SL automatic)
├─ Evaluate advanced exits:
│  ├─ Trailing stop
│  ├─ Momentum flip
│  ├─ Rapid invalidation
│  └─ Time exit (90 min)
│
├─ Track MAE (max adverse price)
│
└─ Exit triggers → close_position()
                     │
                     ▼
           executor.py (close order)
           ├─ SELL @ market price
           ├─ Calculate realized P&L
           └─ Log exit → trade_journal (complete record)
```

### Handoff #5: Completed Trades → Expectancy Analysis
```
trade_journal.db (50+ completed trades)
           │
           ▼
expectancy_report.py (--expectancy command)
├─ Calculate per-trade costs (spread + slippage)
├─ Compute net P&L (gross − costs)
├─ Segment analysis:
│  ├─ By setup (ORB, gap_and_go, pullback, news)
│  ├─ By regime (TRENDING_UP, CHOPPY, LOW_VOL, HIGH_VOL)
│  ├─ By score method (Claude vs local)
│  └─ By (setup, regime) pair
│
├─ Calculate metrics:
│  ├─ Win%, Avg Winner, Avg Loser
│  ├─ Expectancy (gross & net of costs)
│  ├─ Profit Factor
│  └─ Sample size & confidence
│
└─ Output:
   ├─ Which setups have edge?  (edge = net_ev > $5/trade, n≥100)
   ├─ Which regimes are profitable?
   ├─ Does Claude scoring add value?
   └─ What should be disabled before live?
```

---

## 4. AUTONOMOUS OPS FEEDBACK LOOP

```
┌───────────────────────────────────────────────────────────────────┐
│                    OPS AGENT CYCLE (every 8 min)                  │
├───────────────────────────────────────────────────────────────────┤
│                                                                   │
│  1. OBSERVE                                                       │
│     ├─ Fetch BPM dashboard (14 sections)                         │
│     ├─ Check process health (Python instances, uptime)           │
│     ├─ Verify data freshness (last trade, log timestamp)         │
│     └─ Monitor system state (RAM, disk, network)                 │
│                                                                   │
│  2. REASON (Claude)                                              │
│     ├─ Input: Full BPM + historical state + error patterns       │
│     ├─ Assess: Is system healthy? Any degradation?              │
│     └─ Decide: What needs fixing?                               │
│                                                                   │
│  3. ACT (Autonomous repairs)                                     │
│     ├─ Dashboard down → Restart it                              │
│     ├─ Zombie processes → Kill old instances                    │
│     ├─ Stale logs → Indicate staleness in report                │
│     ├─ Data gap → Flag for manual investigation                 │
│     └─ IB Gateway unreachable → Note, don't auto-restart        │
│                                                                   │
│  4. REPORT                                                        │
│     └─ Output: {fixed: [...], notify_human: [...], obs: [...]}  │
│        └─ If critical: Alert (e.g., "Dashboard restarted")      │
│                                                                   │
│  Output → ops_fixes.log                                          │
│                                                                   │
└───────────────────────────────────────────────────────────────────┘
```

---

## 5. YOUR FULL AGENT ECOSYSTEM

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          LEELA'S AI AGENTS (4 Total)                        │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  1. YOUTUBE AGENT (ai-daily-news)                                          │
│     Scheduler: Daily @ 7pm / 19:00 BST                                     │
│     Flow: News topic → Claude script → ElevenLabs VO → Pexels imagery    │
│            → FFmpeg composite → YouTube upload                             │
│     Output: 1 video/day to @LeelaKanti-o8w                                │
│     Status: LIVE (daily uploads)                                           │
│                                                                             │
│  2. RUNWAY SHORTS AGENT (leela-runway-agent)                               │
│     Scheduler: Daily @ 12:00 PM / noon BST                                │
│     Flow: Reddit trending → Claude filter → Runway Gen-3 clip             │
│            → Slice into 5 Shorts → Elli voice + NCS beat → YouTube upload │
│     Output: 1 Short/day to Daily Lofi Beats                               │
│     Status: LIVE (since 2026-05-30)                                        │
│                                                                             │
│  3. MUSIC AGENT (leela-music-agent)                                        │
│     Scheduler: Daily @ 9am / 09:00 BST                                    │
│     Flow: Boom-bap prompt → MusicGen → AIFF render → YouTube upload       │
│     Output: 1 lofi beat/day to Daily Lofi Beats                           │
│     Status: LIVE (since 2026-05-25)                                        │
│                                                                             │
│  4. TRADING AGENTS (2 simultaneously)                                      │
│     ├─ ALPACA DAY TRADER (leela-daytrading-agent)                          │
│     │  Scheduler: Mon–Fri @ 14:30 BST / 9:30 ET                          │
│     │  Flow: Gappers → Claude score → Entry gates → Bracket order        │
│     │         → Exit monitor → Trade journal logging → Expectancy analysis │
│     │  Output: Trade P&L + backtest harness metrics                       │
│     │  Status: LIVE (data collection phase, paper trading)               │
│     │                                                                      │
│     └─ IBKR SWING TRADER (leela-ibkr-agent)  [DISABLED temporarily]      │
│        (Conflicts with day trader on same account; will enable on        │
│         separate account for live 2-position intraday vs swing split)    │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 6. CRITICAL HANDOFF SEQUENCES

### Sequence A: Morning Startup (Pre-Market)
```
08:30 ET (13:30 BST)
  ↓
--research (fundamentals + watchlist brief)
  ↓
Dashboard & Ops Agent (auto-start via scheduler)
  ↓
09:40 ET (14:40 BST)
  ↓
--precheck (data readiness gates)
  ↓
LIVE_ENABLED flag written (if all tests pass)
  ↓
09:45 ET (14:45 BST)
  ↓
--prescan (find gappers, score, save candidates)
  ↓
--continuous (loop: scan → score → execute → monitor → exit)
```

### Sequence B: During Market (Intraday)
```
Continuous Loop (every 5–15 min adaptive):
  ├─ Load candidates from JSON
  ├─ Re-score with latest data
  ├─ Check all gates
  ├─ Execute approved trades → trade_journal
  ├─ Monitor exits → trade_journal (exit logged)
  └─ Repeat
  
Parallel: Ops Agent (every 8 min)
  ├─ Observe system health
  ├─ Reason with Claude
  ├─ Auto-repair (restart dashboard, kill zombies)
  └─ Report status
```

### Sequence C: End of Day
```
15:30 ET (20:30 BST)
  ↓
--cutoff (cancel unfilled limit orders)
  ↓
15:44 ET (20:44 BST)
  ↓
--close (force-close all positions)
  ↓
--verify (emergency flatness check)
  ↓
16:15 ET (21:15 BST)
  ↓
--report (P&L summary)
  ↓
--performance (full dashboard + feed quality + expectancy if n>50)
```

### Sequence D: Weekly/Monthly Analysis
```
After 50–100 trades:
  ↓
--expectancy (multi-dimensional edge analysis)
  ↓
Review:
  ├─ Which setups work? (n≥10, edge>$5/trade)
  ├─ Which regimes profit? (n≥10, positive EV)
  ├─ Claude vs local: which scores better?
  └─ What to disable before going live?
  ↓
Update config.py:
  ├─ Disable underperforming setups
  ├─ Add hard blockers on negative-edge regimes
  ├─ Tune score gates based on empirical data
  └─ Re-run collection
```

---

## 7. FAILURE MODES & HANDOFFS

| Failure Point | Handoff | Recovery |
|--------------|---------|----------|
| Data stale | Prescan → gates | Skip scan, flag ops agent |
| Quote mismatch | Cross-validator | Reject entry, log reason |
| Spread too wide | Spread gate | Skip this candidate |
| Position stuck | Monitor → ops_agent | Auto-close or alert |
| Dashboard down | ops_agent detects | Auto-restart |
| Trade not logged | exit → trade_journal | Reconcile, backfill |
| Rate limit (Claude) | score_validator.py | Graceful skip, continue |
| Market closed | _market_open() gate | Skip scan entirely |

---

## Summary

Your **trading pipeline** is:
1. **Data** → **Prescan** → **Research/Score** → **Gates** → **Entry** → **Monitor** → **Exit** → **Log** → **Analysis**

**Autonomous handoffs** are:
- agent.py orchestrates via scheduler
- executor.py places orders + logs entries
- exits.py monitors + logs exits
- ops_agent.py repairs + observes (every 8 min)
- expectancy_report.py surfaces what works

**No manual intervention needed** — trades auto-log with realistic costs, and after 50–100 trades, you have empirical edge data to decide what to trade live.

Everything is **deterministic** (gates, logic, logging) and **observable** (logs, dashboard, DB queries, reports).
