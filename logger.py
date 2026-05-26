"""SQLite trade log and full audit trail."""
import sqlite3
import json
from datetime import date, datetime
from config import DB_PATH, AUDIT_LOG_FILE


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            date       TEXT,
            symbol     TEXT,
            shares     INTEGER,
            entry      REAL,
            exit_price REAL,
            pnl        REAL,
            pnl_pct    REAL,
            reason     TEXT,
            ts         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            date    TEXT,
            ts      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            action  TEXT,
            symbol  TEXT,
            details TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS paper_trades (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            date      TEXT,
            ts        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            symbol    TEXT,
            shares    INTEGER,
            price     REAL,
            side      TEXT,
            score     INTEGER,
            reasoning TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS execution_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            date            TEXT,
            ts              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            symbol          TEXT,
            intended_price  REAL,
            limit_price     REAL,
            fill_price      REAL,
            slippage        REAL,
            shares          INTEGER,
            order_type      TEXT,
            score           INTEGER,
            size_pct        REAL,
            sizing_note     TEXT
        )
    """)
    con.commit()
    con.close()


def log_trade(symbol: str, shares: int, entry: float, exit_price: float, reason: str = ""):
    pnl     = (exit_price - entry) * shares
    pnl_pct = (exit_price - entry) / entry * 100
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO trades (date,symbol,shares,entry,exit_price,pnl,pnl_pct,reason) VALUES (?,?,?,?,?,?,?,?)",
        (date.today().isoformat(), symbol, shares, entry, exit_price, pnl, pnl_pct, reason),
    )
    con.commit()
    con.close()


def log_audit(action: str, symbol: str = "", details: dict | None = None):
    """Record every decision — trades placed, rejected, blocked, or simulated."""
    details_str = json.dumps(details or {})
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO audit_log (date,action,symbol,details) VALUES (?,?,?,?)",
        (date.today().isoformat(), action, symbol, details_str),
    )
    con.commit()
    con.close()

    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {action:<22} {symbol:<6} {details_str}\n"
    AUDIT_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(AUDIT_LOG_FILE, "a") as f:
        f.write(line)


def log_execution(
    symbol: str, intended_price: float, limit_price: float,
    shares: int, order_type: str, score: int, size_pct: float, sizing_note: str,
):
    """Record execution intent. fill_price/slippage updated later from Alpaca fills."""
    con = sqlite3.connect(DB_PATH)
    con.execute(
        """INSERT INTO execution_log
           (date,symbol,intended_price,limit_price,shares,order_type,score,size_pct,sizing_note)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (date.today().isoformat(), symbol, intended_price, limit_price,
         shares, order_type, score, size_pct, sizing_note),
    )
    con.commit()
    con.close()


def log_paper_trade(symbol: str, shares: int, price: float, side: str, score: int, reasoning: str):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO paper_trades (date,symbol,shares,price,side,score,reasoning) VALUES (?,?,?,?,?,?,?)",
        (date.today().isoformat(), symbol, shares, price, side, score, reasoning),
    )
    con.commit()
    con.close()


def today_audit() -> list[dict]:
    con  = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT action,symbol,details FROM audit_log WHERE date=?",
        (date.today().isoformat(),),
    ).fetchall()
    con.close()
    result = []
    for action, symbol, details_str in rows:
        try:
            details = json.loads(details_str)
        except Exception:
            details = {}
        result.append({"action": action, "symbol": symbol, "details": details})
    return result


def today_summary() -> list[tuple]:
    con  = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT symbol,shares,entry,exit_price,pnl,pnl_pct FROM trades WHERE date=?",
        (date.today().isoformat(),),
    ).fetchall()
    con.close()
    return rows


def all_time_summary() -> dict:
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT COUNT(*), SUM(pnl), AVG(pnl_pct), "
        "SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) FROM trades"
    ).fetchone()
    con.close()
    total, pnl, avg_pct, wins = row
    win_rate = wins / total * 100 if total else 0
    return {"trades": total, "total_pnl": pnl or 0, "avg_pct": avg_pct or 0, "win_rate": win_rate}
