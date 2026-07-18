"""Historical OHLCV collection from Yahoo Finance -> SQLite.

Each ticker is fetched independently and wrapped in its own try/except so a
single bad/delisted symbol or a transient network blip cannot abort the
whole run. Network errors are retried with exponential backoff before being
logged and skipped.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

from app.config import Config
from app.db import db_cursor
from app.utils.retry import network_retry

logger = logging.getLogger(__name__)


def _fetch_history(ticker: str, lookback_days: int, timeout: int, max_retries: int) -> pd.DataFrame:
    @network_retry(logger, max_retries)
    def _do_fetch() -> pd.DataFrame:
        tk = yf.Ticker(ticker)
        df = tk.history(period=f"{lookback_days}d", auto_adjust=False, timeout=timeout)
        return df

    return _do_fetch()


def _upsert_ohlcv(db_path: str, ticker: str, df: pd.DataFrame) -> int:
    if df.empty:
        return 0

    rows = []
    for idx, row in df.iterrows():
        date_str = idx.strftime("%Y-%m-%d")
        try:
            rows.append((
                ticker,
                date_str,
                float(row["Open"]),
                float(row["High"]),
                float(row["Low"]),
                float(row["Close"]),
                float(row["Adj Close"]),
                int(row["Volume"]),
            ))
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("Skipping malformed row for %s on %s: %s", ticker, date_str, exc)

    if not rows:
        return 0

    with db_cursor(db_path) as cur:
        cur.executemany(
            """
            INSERT INTO ohlcv (ticker, date, open, high, low, close, adj_close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, date) DO UPDATE SET
                open=excluded.open, high=excluded.high, low=excluded.low,
                close=excluded.close, adj_close=excluded.adj_close, volume=excluded.volume
            """,
            rows,
        )
    return len(rows)


def run(config: Config) -> dict:
    """Fetch and persist OHLCV history for the configured universe.

    Returns a summary dict for logging/inspection by the caller.
    """
    started = datetime.now(timezone.utc)
    succeeded, failed = [], []
    total_rows = 0

    logger.info(
        "Starting data pipeline run: %d tickers, lookback=%dd, db=%s",
        len(config.universe_tickers), config.lookback_days, config.db_path,
    )

    for ticker in config.universe_tickers:
        try:
            df = _fetch_history(
                ticker,
                config.lookback_days,
                config.yfinance_timeout_seconds,
                config.yfinance_max_retries,
            )
            if df is None or df.empty:
                logger.warning("No data returned for %s — skipping.", ticker)
                failed.append(ticker)
                continue

            n = _upsert_ohlcv(config.db_path, ticker, df)
            total_rows += n
            succeeded.append(ticker)
            logger.info("Fetched %s: %d rows upserted (%s -> %s)",
                        ticker, n, df.index.min().date(), df.index.max().date())
        except Exception as exc:  # noqa: BLE001 - isolate per-ticker failures
            logger.error("Failed to fetch/store %s after retries: %s", ticker, exc, exc_info=True)
            failed.append(ticker)

    duration = (datetime.now(timezone.utc) - started).total_seconds()
    summary = {
        "succeeded": succeeded,
        "failed": failed,
        "total_rows_upserted": total_rows,
        "duration_seconds": round(duration, 2),
    }
    logger.info(
        "Data pipeline run complete in %.1fs: %d/%d tickers succeeded, %d rows upserted. Failed: %s",
        duration, len(succeeded), len(config.universe_tickers), total_rows, failed or "none",
    )
    return summary
