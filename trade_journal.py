"""
Enhanced trade journal for detailed backtest/analysis.

Captures:
- Score vs outcome
- Setup type and regime at entry
- MAE (maximum adverse excursion) during holding
- Realistic spread/slippage costs
- Entry/exit prices with cost modeling
"""
import sqlite3
from datetime import datetime
from pathlib import Path
from config import DB_PATH


def init_trade_journal():
    """Extend schema with detailed trade metrics for backtest analysis."""
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
            setup_type          TEXT,     -- ORB, gap_and_go, pullback, news_momentum
            regime              TEXT,     -- TRENDING_UP, CHOPPY, LOW_VOLUME, HIGH_VOL
            atr_at_entry        REAL,
            atr_stop_pct        REAL,     -- ATR * multiplier to set stop

            -- Risk/reward parameters
            account_risk_pct    REAL,     -- e.g., 0.01 = 1% portfolio risk
            stop_price          REAL,
            target_price        REAL,
            intended_r_r        REAL,     -- intended risk:reward ratio

            -- Exit details
            exit_price          REAL NOT NULL,
            exit_bid            REAL,
            exit_ask            REAL,
            exit_spread_pct     REAL,
            exit_slippage_pct   REAL,
            effective_exit_px   REAL,
            exit_reason         TEXT,     -- TP_HIT, SL_HIT, TIME_EXIT, EOD_CLOSE, MANUAL, etc.

            -- Trade metrics
            mae_pct             REAL,     -- max adverse excursion % from entry
            mae_price           REAL,
            realized_pnl        REAL,
            realized_pnl_pct    REAL,
            realized_pnl_pre_cost  REAL,  -- P&L before spread/slippage costs
            realized_cost_total REAL,     -- entry_spread + entry_slippage + exit_spread + exit_slippage
            holding_minutes     INTEGER,

            -- Outcome classification
            outcome             TEXT,     -- WIN, LOSS, BREAKEVEN

            UNIQUE(date, symbol, ts_entry)
        )
    """)

    # Intrabar OHLC snapshots for MAE calculation (optional, heavy logging)
    # Can be purged after session-end analysis
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
    setup_type: str,
    regime: str,
    atr_at_entry: float,
    atr_stop_pct: float,
    account_risk_pct: float,
    stop_price: float,
    target_price: float,
    intended_r_r: float,
    entry_spread_pct: float = 0.0,
    entry_slippage_pct: float = 0.0,
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
            claude_score, local_score, score_used,
            setup_type, regime, atr_at_entry, atr_stop_pct,
            account_risk_pct, stop_price, target_price, intended_r_r
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.utcnow().date().isoformat(),
        now,
        symbol,
        entry_price, entry_qty, entry_bid, entry_ask,
        entry_spread_pct, entry_slippage_pct, effective_entry_px,
        claude_score, local_score, score_used,
        setup_type, regime, atr_at_entry, atr_stop_pct,
        account_risk_pct, stop_price, target_price, intended_r_r
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
):
    """Log a trade exit and compute realized P&L with costs."""
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA busy_timeout=30000")

    # Fetch entry details
    row = con.execute("""
        SELECT entry_price, entry_qty, effective_entry_px,
               entry_spread_pct, entry_slippage_pct, stop_price, target_price
        FROM trade_journal WHERE id = ?
    """, (trade_id,)).fetchone()

    if not row:
        con.close()
        return

    entry_price, entry_qty, effective_entry_px, entry_spread_pct, entry_slippage_pct, stop_px, target_px = row

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
    if realized_pnl > 0.5:
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
            outcome = ?
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
        trade_id
    ))
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
