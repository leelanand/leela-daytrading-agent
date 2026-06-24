"""
Enhanced trade journal for detailed backtest/analysis.

Captures:
- Score vs outcome with mechanism-based tracking
- Setup type, regime, AND mechanism (edge type)
- MAE (maximum adverse excursion) during holding
- Realistic spread/slippage costs on both sides
- Entry/exit prices with cost modeling
- Mechanism preconditions and validation
- Repair audit trail (ops_agent mutations logged)
"""
import sqlite3
from datetime import datetime
from pathlib import Path
from config import DB_PATH


def init_trade_journal():
    """Expand schema with mechanism tracking, repair audit, and re-check logging."""
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")

    # Main trade journal — one row per round-trip (entry through exit)
    con.execute("""
        CREATE TABLE IF NOT EXISTS trade_journal (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            date                TEXT,
            ts_entry            TIMESTAMP,
            ts_exit             TIMESTAMP,
            symbol              TEXT NOT NULL,

            -- Entry details
            entry_price         REAL NOT NULL,
            entry_qty           INTEGER NOT NULL,
            entry_bid           REAL,
            entry_ask           REAL,
            entry_spread_pct    REAL,
            entry_slippage_pct  REAL,
            effective_entry_px  REAL,

            -- Scoring and regime context
            claude_score        INTEGER,
            local_score         INTEGER,
            score_used          INTEGER,  -- which was actually used
            score_confidence    REAL,     -- 0-1: confidence in this score
            setup_type          TEXT,     -- ORB, gap_and_go, pullback, news_momentum, etc.
            regime              TEXT,     -- TRENDING_UP, CHOPPY, LOW_VOLUME, HIGH_VOL

            -- MECHANISM TRACKING (Phase 1 change)
            mechanism           TEXT,     -- momentum_arbitrage, pead, reconstitution, forced_selling
            counterparty        TEXT,     -- who is on the losing side structurally?
            mechanism_confidence REAL,    -- 0-1: how confident mechanism is valid right now
            mechanism_precondition TEXT, -- what must still be true? (orb_window_open, post_earnings_unannounced, etc.)
            mechanism_precondition_valid BOOLEAN, -- validated at execution time

            atr_at_entry        REAL,
            atr_stop_pct        REAL,    -- ATR * multiplier to set stop

            -- Risk/reward parameters
            account_risk_pct    REAL,    -- e.g., 0.01 = 1% portfolio risk
            stop_price          REAL,
            target_price        REAL,
            intended_r_r        REAL,    -- intended risk:reward ratio

            -- EXECUTION-TIME RE-CHECKS (Phase 5 change)
            spread_at_prescan_pct   REAL,  -- spread % when candidate was scored
            spread_at_execution_pct REAL,  -- spread % when order was placed
            volume_at_prescan       INTEGER,
            volume_at_execution     INTEGER,
            cost_survival_check     BOOLEAN, -- passed non-negotiable #1?
            liquidity_check         BOOLEAN, -- passed non-negotiable #2?
            precondition_check      BOOLEAN, -- passed non-negotiable #4?

            -- Exit details
            exit_price          REAL NOT NULL,
            exit_bid            REAL,
            exit_ask            REAL,
            exit_spread_pct     REAL,
            exit_slippage_pct   REAL,
            effective_exit_px   REAL,
            exit_reason         TEXT,     -- TP_HIT, SL_HIT, TIME_EXIT, EOD_CLOSE, MANUAL, REJECTED_AT_EXECUTION, etc.

            -- Trade metrics
            mae_pct             REAL,     -- max adverse excursion % from entry
            mae_price           REAL,
            realized_pnl        REAL,
            realized_pnl_pct    REAL,
            realized_pnl_pre_cost   REAL,  -- P&L before spread/slippage costs
            realized_cost_total REAL,     -- entry_spread + entry_slippage + exit_spread + exit_slippage
            holding_minutes     INTEGER,

            -- Outcome classification
            outcome             TEXT,     -- WIN, LOSS, BREAKEVEN, REJECTED_AT_EXECUTION

            -- REPAIR AUDIT TRAIL (Phase 4 change)
            repair_actions      TEXT,     -- JSON array of {action, reason, timestamp, agent, old_val, new_val}
            repairs_count       INTEGER,  -- how many times was this trade repaired?

            -- SWING-PATH OVERNIGHT GAP LOGGING (Option B)
            overnight_holds_count       INTEGER,  -- how many nights was this position held?
            gap_events                  TEXT,     -- JSON array: [{date, close_prev, open_price, gap_pct, stop_price, gap_through_stop, slippage_vs_stop}]
            realized_overnight_slippage REAL,     -- total $ or % slippage from all gap events

            UNIQUE(date, symbol, ts_entry)
        )
    """)

    # Repair audit log for every mutation
    con.execute("""
        CREATE TABLE IF NOT EXISTS repair_audit (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id        INTEGER NOT NULL,
            ts              TIMESTAMP,
            agent           TEXT,       -- which agent made the repair? (ops_agent, executor, etc.)
            action          TEXT,       -- bracket_adjust, fill_retry, fill_backfill, order_modify
            reason          TEXT,       -- why? (fill_timeout_retry, missed_fill, slippage_protection, etc.)
            old_value       TEXT,       -- JSON: what changed? {field: old_val}
            new_value       TEXT,       -- JSON: {field: new_val}
            success         BOOLEAN,    -- did this repair succeed?
            FOREIGN KEY(trade_id) REFERENCES trade_journal(id)
        )
    """)

    # Intrabar OHLC snapshots for MAE calculation
    con.execute("""
        CREATE TABLE IF NOT EXISTS intrabar_snapshot (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id        INTEGER,
            ts              TIMESTAMP,
            price           REAL,
            is_low          BOOLEAN,  -- 1 if this was the low during holding
            FOREIGN KEY(trade_id) REFERENCES trade_journal(id)
        )
    """)

    con.commit()
    con.close()


def log_entry(
    symbol: str,
    entry_price: float,
    entry_qty: int,
    entry_bid: float,
    entry_ask: float,
    claude_score: int | None,
    local_score: int | None,
    score_confidence: float = 0.0,
    setup_type: str = "UNKNOWN",
    regime: str = "UNKNOWN",
    mechanism: str = "unknown",
    counterparty: str = "unknown",
    mechanism_confidence: float = 0.5,
    mechanism_precondition: str = "none",
    atr_at_entry: float = 0.0,
    atr_stop_pct: float = 0.0,
    account_risk_pct: float = 0.01,
    stop_price: float = 0.0,
    target_price: float = 0.0,
    intended_r_r: float = 0.0,
    entry_spread_pct: float = 0.0,
    entry_slippage_pct: float = 0.0,
    spread_at_prescan_pct: float = 0.0,
    volume_at_prescan: int = 0,
) -> int:
    """Log a trade entry. Returns trade_id for later exit logging."""
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA busy_timeout=30000")

    effective_entry_px = entry_price * (1 + entry_slippage_pct) * (1 + entry_spread_pct / 2)
    score_used = claude_score if claude_score is not None else local_score

    now = datetime.utcnow().isoformat()
    cursor = con.execute("""
        INSERT INTO trade_journal (
            date, ts_entry, symbol,
            entry_price, entry_qty, entry_bid, entry_ask,
            entry_spread_pct, entry_slippage_pct, effective_entry_px,
            claude_score, local_score, score_used, score_confidence,
            setup_type, regime,
            mechanism, counterparty, mechanism_confidence, mechanism_precondition,
            atr_at_entry, atr_stop_pct,
            account_risk_pct, stop_price, target_price, intended_r_r,
            spread_at_prescan_pct, volume_at_prescan
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.utcnow().date().isoformat(),
        now,
        symbol,
        entry_price, entry_qty, entry_bid, entry_ask,
        entry_spread_pct, entry_slippage_pct, effective_entry_px,
        claude_score, local_score, score_used, score_confidence,
        setup_type, regime,
        mechanism, counterparty, mechanism_confidence, mechanism_precondition,
        atr_at_entry, atr_stop_pct,
        account_risk_pct, stop_price, target_price, intended_r_r,
        spread_at_prescan_pct, volume_at_prescan
    ))
    trade_id = cursor.lastrowid
    con.commit()
    con.close()
    return trade_id


def log_exit(
    trade_id: int,
    exit_price: float,
    exit_bid: float,
    exit_ask: float,
    exit_reason: str,
    mae_pct: float,
    mae_price: float,
    holding_minutes: int,
    exit_spread_pct: float = 0.0,
    exit_slippage_pct: float = 0.0,
    spread_at_execution_pct: float = 0.0,
    volume_at_execution: int = 0,
    cost_survival_check: bool = True,
    liquidity_check: bool = True,
    precondition_check: bool = True,
):
    """Log a trade exit and compute realized P&L with costs."""
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA busy_timeout=30000")

    # Fetch entry details
    row = con.execute("""
        SELECT entry_price, entry_qty, effective_entry_px,
               entry_spread_pct, entry_slippage_pct, stop_price, target_price,
               spread_at_prescan_pct, volume_at_prescan
        FROM trade_journal WHERE id = ?
    """, (trade_id,)).fetchone()

    if not row:
        con.close()
        return

    (entry_price, entry_qty, effective_entry_px, entry_spread_pct,
     entry_slippage_pct, stop_px, target_px, spread_at_prescan, vol_at_prescan) = row

    effective_exit_px = exit_price * (1 - exit_slippage_pct) * (1 - exit_spread_pct / 2)

    # Compute realized P&L
    realized_pnl_pre_cost = (exit_price - entry_price) * entry_qty
    realized_cost_total = (
        (entry_spread_pct / 2 * entry_price) * entry_qty +
        (entry_slippage_pct * entry_price) * entry_qty +
        (exit_spread_pct / 2 * exit_price) * entry_qty +
        (exit_slippage_pct * exit_price) * entry_qty
    )
    realized_pnl = realized_pnl_pre_cost - realized_cost_total
    realized_pnl_pct = realized_pnl / (effective_entry_px * entry_qty) if effective_entry_px * entry_qty > 0 else 0

    # Classify outcome
    if exit_reason == "REJECTED_AT_EXECUTION":
        outcome = "REJECTED_AT_EXECUTION"
    elif realized_pnl > 0.5:
        outcome = "WIN"
    elif realized_pnl < -0.5:
        outcome = "LOSS"
    else:
        outcome = "BREAKEVEN"

    now = datetime.utcnow().isoformat()
    con.execute("""
        UPDATE trade_journal SET
            ts_exit = ?,
            exit_price = ?,
            exit_bid = ?,
            exit_ask = ?,
            exit_spread_pct = ?,
            exit_slippage_pct = ?,
            effective_exit_px = ?,
            exit_reason = ?,
            mae_pct = ?,
            mae_price = ?,
            holding_minutes = ?,
            realized_pnl = ?,
            realized_pnl_pct = ?,
            realized_pnl_pre_cost = ?,
            realized_cost_total = ?,
            outcome = ?,
            spread_at_execution_pct = ?,
            volume_at_execution = ?,
            cost_survival_check = ?,
            liquidity_check = ?,
            precondition_check = ?
        WHERE id = ?
    """, (
        now,
        exit_price, exit_bid, exit_ask,
        exit_spread_pct, exit_slippage_pct, effective_exit_px,
        exit_reason,
        mae_pct, mae_price,
        holding_minutes,
        realized_pnl, realized_pnl_pct,
        realized_pnl_pre_cost, realized_cost_total,
        outcome,
        spread_at_execution_pct, volume_at_execution,
        cost_survival_check, liquidity_check, precondition_check,
        trade_id
    ))
    con.commit()
    con.close()


def log_repair(
    trade_id: int,
    agent: str,
    action: str,
    reason: str,
    old_value: dict,
    new_value: dict,
    success: bool = True,
):
    """Log a repair action (ops_agent mutation) to audit trail."""
    import json
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA busy_timeout=30000")

    now = datetime.utcnow().isoformat()
    con.execute("""
        INSERT INTO repair_audit (
            trade_id, ts, agent, action, reason, old_value, new_value, success
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        trade_id, now, agent, action, reason,
        json.dumps(old_value), json.dumps(new_value), success
    ))

    # Also increment repairs_count and append to repair_actions
    con.execute("""
        UPDATE trade_journal SET
            repairs_count = COALESCE(repairs_count, 0) + 1
        WHERE id = ?
    """, (trade_id,))

    con.commit()
    con.close()


def log_mae_snapshot(trade_id: int, current_price: float, is_low: bool = False):
    """Log intrabar price snapshot for MAE tracking."""
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("""
        INSERT INTO intrabar_snapshot (trade_id, ts, price, is_low)
        VALUES (?, ?, ?, ?)
    """, (trade_id, datetime.utcnow().isoformat(), current_price, 1 if is_low else 0))
    con.commit()
    con.close()


def get_trades_for_analysis(limit: int | None = None) -> list[dict]:
    """Fetch completed trades for expectancy analysis."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    query = "SELECT * FROM trade_journal WHERE outcome IS NOT NULL ORDER BY ts_exit DESC"
    if limit:
        query += f" LIMIT {limit}"
    trades = [dict(row) for row in con.execute(query).fetchall()]
    con.close()
    return trades
