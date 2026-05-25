"""SQLite trade log."""
import sqlite3
from datetime import date
from config import DB_PATH


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
