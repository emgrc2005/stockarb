"""SQLite persistence layer.

Uses the stdlib `sqlite3` module (no extra ARM64 wheel risk) with WAL mode
enabled so the data pipeline, analytics engine, and execution engine
(each invoked as separate short-lived processes by supercronic) can safely
read/write the same file without locking each other out.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS ohlcv (
    ticker      TEXT NOT NULL,
    date        TEXT NOT NULL,          -- ISO 8601 YYYY-MM-DD
    open        REAL NOT NULL,
    high        REAL NOT NULL,
    low         REAL NOT NULL,
    close       REAL NOT NULL,
    adj_close   REAL NOT NULL,
    volume      INTEGER NOT NULL,
    PRIMARY KEY (ticker, date)
);

CREATE INDEX IF NOT EXISTS idx_ohlcv_ticker_date ON ohlcv (ticker, date);

CREATE TABLE IF NOT EXISTS pairs (
    pair_id             TEXT PRIMARY KEY,   -- "TICKA_TICKB"
    ticker_a            TEXT NOT NULL,
    ticker_b            TEXT NOT NULL,
    last_test_date      TEXT NOT NULL,
    coint_pvalue        REAL NOT NULL,
    adf_pvalue          REAL NOT NULL,
    hedge_ratio         REAL NOT NULL,
    spread_mean         REAL NOT NULL,
    spread_std          REAL NOT NULL,
    half_life_days      REAL,
    lookback_days       INTEGER NOT NULL,
    is_tradable         INTEGER NOT NULL DEFAULT 0,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_id         TEXT NOT NULL,
    date            TEXT NOT NULL,
    price_a         REAL NOT NULL,
    price_b         REAL NOT NULL,
    spread          REAL NOT NULL,
    zscore          REAL NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (pair_id, date)
);

CREATE INDEX IF NOT EXISTS idx_signals_pair_date ON signals (pair_id, date);

CREATE TABLE IF NOT EXISTS positions (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_id                 TEXT NOT NULL,
    ticker_long             TEXT NOT NULL,
    ticker_short            TEXT NOT NULL,
    qty_long                REAL NOT NULL,
    qty_short               REAL NOT NULL,
    entry_price_long        REAL NOT NULL,
    entry_price_short       REAL NOT NULL,
    entry_zscore            REAL NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'OPEN',   -- OPEN | CLOSED
    exit_price_long         REAL,
    exit_price_short        REAL,
    exit_zscore             REAL,
    exit_reason             TEXT,
    realized_pnl            REAL,
    alpaca_order_id_long    TEXT,
    alpaca_order_id_short   TEXT,
    opened_at               TEXT NOT NULL,
    closed_at               TEXT
);

CREATE INDEX IF NOT EXISTS idx_positions_status ON positions (status);
CREATE INDEX IF NOT EXISTS idx_positions_pair ON positions (pair_id);

CREATE TABLE IF NOT EXISTS orders_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id         INTEGER,
    ticker              TEXT NOT NULL,
    side                TEXT NOT NULL,     -- buy | sell
    qty                 REAL NOT NULL,
    alpaca_order_id     TEXT,
    alpaca_status       TEXT,
    submitted_at        TEXT NOT NULL,
    FOREIGN KEY (position_id) REFERENCES positions (id)
);
"""


def get_connection(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def init_db(db_path: str) -> None:
    conn = get_connection(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def db_cursor(db_path: str) -> Iterator[sqlite3.Cursor]:
    """Context manager yielding a cursor; commits on success, rolls back on error."""
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
