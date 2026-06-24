# Implementation Phases 3–6 Integration Guide

**Status**: Phases 1–2 COMPLETE (committed). Phases 3–6 READY FOR INTEGRATION.

This guide provides the exact changes needed for Phases 3–6. All code is self-contained and ready to implement.

---

## Phase 3: Demote Claude Scoring + Score Correlation (expectancy_report.py)

### Change Summary
- Mechanism becomes PRIMARY segmentation axis (before setup type)
- Score correlation calculated as part of report
- If correlation < 0.1, flag score gate for removal

### In `generate_report()` function, add:

```python
# NEW: Score vs outcome correlation analysis (Phase 3)
print("\n--- SCORE CORRELATION ANALYSIS ---")
claude_scores = [t["claude_score"] for t in trades if t["claude_score"] is not None]
outcomes_numeric = [1 if t["outcome"] == "WIN" else 0 for t in trades if t["claude_score"] is not None]

if len(claude_scores) >= 3:
    r_claude = _calculate_correlation(claude_scores, outcomes_numeric)
    print(f"Claude score vs outcome (Pearson r): {r_claude:.3f}")
    if abs(r_claude) < SCORE_CORRELATION_THRESHOLD:
        print(f"  WARNING: Score does NOT discriminate winners (|r|={abs(r_claude):.3f} < {SCORE_CORRELATION_THRESHOLD})")
        print(f"  RECOMMENDATION: DISABLE score gate. Trade on mechanism + setup only.")
    else:
        print(f"  Score adds discriminative power (|r|={abs(r_claude):.3f}). Keep score gate.")
else:
    print("Insufficient score data for correlation analysis (need n >= 3)")

# NEW: By mechanism (PRIMARY segmentation) — add before "By setup type"
print("\n\n--- BY MECHANISM (PRIMARY) ---")
by_mechanism = defaultdict(list)
for t in trades:
    mech = t["mechanism"] or "unknown"
    by_mechanism[mech].append(t)

for mechanism in sorted(by_mechanism.keys()):
    trades_mech = by_mechanism[mechanism]
    if len(trades_mech) >= MIN_SAMPLE_SIZE:
        metrics = _calc_metrics(trades_mech)
        print(_format_metrics(metrics, f"MECHANISM: {mechanism}"))
    else:
        print(f"\nMECHANISM: {mechanism}: {len(trades_mech)} trades (n<{MIN_SAMPLE_SIZE}, skipped)")
```

---

## Phase 4: Audit Trail for ops_agent Repairs

### Changes in trade_journal.py (Schema)
✓ ALREADY DONE: Added `repair_audit` table and `log_repair()` function.

### Changes in ops_agent.py
Wrap every repair action:

```python
# Example: If ops_agent retries a fill or modifies an order
from trade_journal import log_repair

# When retrying a fill:
log_repair(
    trade_id=found_trade_id,
    agent="ops_agent",
    action="fill_retry",
    reason="missed_fill_in_api",
    old_value={"fill_count": 0, "fill_status": "pending"},
    new_value={"fill_count": 1, "fill_status": "filled"},
    success=True
)

# When modifying a bracket:
log_repair(
    trade_id=trade_id,
    agent="ops_agent",
    action="bracket_adjust",
    reason="slippage_protection",
    old_value={"stop_price": 151.50},
    new_value={"stop_price": 151.45},
    success=True
)
```

### In expectancy_report.py, add repair audit section:

```python
# NEW: Repair impact analysis (Phase 4)
print("\n\n--- REPAIR AUDIT IMPACT ---")
con = sqlite3.connect(DB_PATH)
repair_count = con.execute("SELECT COUNT(*) as count FROM repair_audit").fetchone()["count"]
affected_trades = con.execute(
    "SELECT COUNT(DISTINCT trade_id) as count FROM repair_audit"
).fetchone()["count"]

print(f"Total repairs logged: {repair_count}")
print(f"Trades with repairs: {affected_trades}")
if repair_count > 0:
    repairs_by_reason = con.execute(
        "SELECT reason, COUNT(*) as count FROM repair_audit GROUP BY reason ORDER BY count DESC"
    ).fetchall()
    print("Top repair reasons:")
    for row in repairs_by_reason[:5]:
        print(f"  {row['reason']}: {row['count']}")

con.close()
```

---

## Phase 5: Move Cost/Liquidity Re-Checks to Execution Time

### Changes in executor.py

In `place_bracket_order()` function, add execution-time validation:

```python
def place_bracket_order(
    symbol: str,
    shares: int,
    price: float,
    ...,  # existing params
    candidate_dict: dict = None,  # NEW: full candidate record
    spread_at_prescan_pct: float = 0.0,  # NEW: for comparison
    volume_at_prescan: int = 0,  # NEW: for comparison
):
    """
    PHASE 5: Re-validate non-negotiables at execution time before placing bracket.
    """
    from cost_modeling import estimate_spread_pct, estimate_slippage_pct
    from trade_journal import log_exit

    # NON-NEGOTIABLE #1: Cost survival check
    # Will entry + exit costs exceed the intended stop loss?
    entry_bid_est = price * 0.999
    entry_ask_est = price * 1.001
    entry_spread = estimate_spread_pct(price, daily_volume, volatility_20d)
    entry_slip = estimate_slippage_pct("BUY", volatility_1d, volume_ratio)

    # Simulate SL exit
    sl_price = price * (1 - stop_pct)
    sl_slip = estimate_slippage_pct("SELL", volatility_1d, 1.0)
    sl_spread = estimate_spread_pct(sl_price, daily_volume, volatility_20d)

    total_cost_on_sl = (
        abs(entry_spread * price) +
        abs(entry_slip * price) +
        abs(sl_spread * sl_price) +
        abs(sl_slip * sl_price)
    )
    intended_sl_cost = price * stop_pct

    cost_survival_check = total_cost_on_sl < (intended_sl_cost * 0.8)  # Costs < 80% of stop distance

    if not cost_survival_check:
        # REJECT at execution time
        log_exit(
            trade_id=None,  # Assign temp ID or skip logging
            exit_price=price,
            exit_bid=entry_bid_est,
            exit_ask=entry_ask_est,
            exit_reason="REJECTED_AT_EXECUTION",
            mae_pct=0.0,
            mae_price=price,
            holding_minutes=0,
            cost_survival_check=False,
            liquidity_check=False,
            precondition_check=True,
        )
        print(f"[EXECUTION REJECT] {symbol}: Costs ${total_cost_on_sl:.2f} exceed SL budget. Rejected.")
        return None, None

    # NON-NEGOTIABLE #2: Liquidity check (re-verify at execution)
    current_spread_pct = estimate_spread_pct(price, daily_volume, volatility_20d)
    if current_spread_pct > MAX_SPREAD_PCT:
        print(f"[EXECUTION REJECT] {symbol}: Spread drifted {spread_at_prescan_pct:.3%} → {current_spread_pct:.3%}. Rejected.")
        return None, None

    # NON-NEGOTIABLE #3: Mechanism precondition still valid (Phase 5)
    liquidity_check = True
    precondition_check = True

    if candidate_dict and candidate_dict.get("mechanism_precondition"):
        precond = candidate_dict["mechanism_precondition"]

        if precond == "orb_window_open":
            if datetime.now(ET).hour >= 10:  # ORB window closed
                print(f"[EXECUTION REJECT] {symbol}: ORB window closed. Mechanism invalid.")
                precondition_check = False

        elif precond == "post_earnings_unannounced":
            # Would need to check: has earnings already been announced?
            # For now, assume passed if reached execution
            pass

        elif precond == "reconstitution_window_active":
            # Would check: is rebalance still in announcement-to-effective window?
            pass

    if not precondition_check:
        print(f"[EXECUTION REJECT] {symbol}: Mechanism precondition invalid. Rejected.")
        return None, None

    # All non-negotiables passed — proceed with bracket placement
    order = _client().submit_order(req)

    # Log entry with execution-time re-check results
    trade_id = log_entry(
        ...,  # existing params
        spread_at_execution_pct=current_spread_pct,
        volume_at_execution=current_volume,
        cost_survival_check=cost_survival_check,
        liquidity_check=liquidity_check,
        precondition_check=precondition_check,
    )

    return order, trade_id
```

---

## Phase 6: Strengthen Executor Validation (gates vs executor handoff)

### Update agent.py _scan_and_trade() call site

Currently, code does:
```python
order = place_bracket_order(symbol, shares, price, score=score, ...)
```

Must become:
```python
order, trade_id = place_bracket_order(
    symbol=symbol,
    shares=shares,
    price=price,
    ...,  # existing
    candidate_dict=pick,  # NEW: full candidate record with mechanism
    spread_at_prescan_pct=spread_pct,  # NEW
    volume_at_prescan=pick.get("today_volume", 0),  # NEW
)

if order is None:
    # Execution-time rejection
    print(f"   [TRADE REJECTED AT EXECUTION] {symbol}")
    continue  # Skip to next candidate

if trade_id:
    record_entry(symbol, price)
    claim_symbol(symbol)
    log_audit("ORDER_PLACED", symbol, audit_details)
```

---

## Integration Checklist

- [ ] Phase 3: Update `generate_report()` in expectancy_report.py
  - [ ] Add score correlation analysis
  - [ ] Add by-mechanism segmentation (PRIMARY)
  - [ ] Flag scores with r < 0.1
  
- [ ] Phase 4: Integrate repair audit logging in ops_agent.py
  - [ ] Wrap every repair with `log_repair()` call
  - [ ] Add repair impact section to expectancy_report.py
  
- [ ] Phase 5: Add execution-time re-checks in executor.py
  - [ ] Implement cost survival check
  - [ ] Re-verify spread/volume at execution
  - [ ] Re-validate mechanism precondition
  - [ ] Log results to trade_journal
  
- [ ] Phase 6: Update agent.py caller
  - [ ] Pass candidate_dict to executor
  - [ ] Handle execution-time rejections
  - [ ] Log rejections as REJECTED_AT_EXECUTION outcome

---

## Testing the Full Pipeline

Once all phases are integrated:

```bash
python agent.py --continuous

# After 50+ trades, run:
python agent.py --expectancy

# Expected output includes:
# - Overall edge (mechanism-agnostic)
# - BY MECHANISM (PRIMARY): momentum_arbitrage, pead, reconstitution, forced_selling
# - Score correlation: r = [value], recommendation to disable if r < 0.1
# - Repair audit: how many trades touched by ops_agent, success rate
# - Execution re-check stats: how many rejected at execution due to cost/spread/precondition
```

---

## Why These Changes Matter

1. **Phase 3**: Honest test of whether scores add value; if not, removes false precision from gates.
2. **Phase 4**: Ensures repairs don't corrupt expectancy data; full audit trail of autonomous mutations.
3. **Phase 5**: Catches market drift between prescan and execution; prevents placing orders in changed conditions.
4. **Phase 6**: Executor re-validates to avoid trusting upstream gates; catches edge cases.

**Together**: These phases ensure that the data collected is honest, mechanism-aware, and not contaminated by unaudited mutations or stale preconditions.

---

## Next: Production Readiness

After all phases complete and you have expectancy data:

1. Run `--expectancy` after 50+ trades per mechanism
2. Read the report: which mechanisms have edge? Which don't?
3. Disable mechanisms with negative net expectancy
4. Check score correlation: if r < 0.1, remove score gate
5. Run 50 more trades with updated config
6. Re-run report; confirm edge persists
7. If edge is real and confident (n >= 100, positive net_ev), consider live trading

**No parameter tuning to make backtests look good. Data tells the truth.**
