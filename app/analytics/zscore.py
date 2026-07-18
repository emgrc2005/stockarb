"""Rolling z-score computation for tradable pairs.

For every pair flagged `is_tradable` in the `pairs` table, recompute the
spread series (using the pair's fitted hedge ratio) and a rolling z-score
over `zscore_lookback_window` trading days. The full recomputed series is
upserted into `signals` so the execution engine always reads a consistent,
up-to-date view, and short gaps (e.g. a missed pipeline run) self-heal.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd

from app.config import Config
from app.db import db_cursor

logger = logging.getLogger(__name__)


def _load_tradable_pairs(db_path: str) -> list[dict]:
    with db_cursor(db_path) as cur:
        cur.execute("SELECT * FROM pairs WHERE is_tradable = 1")
        return [dict(row) for row in cur.fetchall()]


def _load_price_series(db_path: str, ticker_a: str, ticker_b: str) -> pd.DataFrame:
    with db_cursor(db_path) as cur:
        cur.execute(
            "SELECT ticker, date, adj_close FROM ohlcv WHERE ticker IN (?, ?) ORDER BY date",
            (ticker_a, ticker_b),
        )
        rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=["ticker", "date", "adj_close"])
    return df.pivot(index="date", columns="ticker", values="adj_close").dropna()


def _upsert_signals(db_path: str, pair_id: str, signal_rows: pd.DataFrame) -> int:
    if signal_rows.empty:
        return 0

    now = datetime.now(timezone.utc).isoformat()
    records = [
        (pair_id, date, float(r.price_a), float(r.price_b), float(r.spread), float(r.zscore), now)
        for date, r in signal_rows.iterrows()
    ]

    with db_cursor(db_path) as cur:
        cur.executemany(
            """
            INSERT INTO signals (pair_id, date, price_a, price_b, spread, zscore, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(pair_id, date) DO UPDATE SET
                price_a=excluded.price_a, price_b=excluded.price_b,
                spread=excluded.spread, zscore=excluded.zscore, created_at=excluded.created_at
            """,
            records,
        )
    return len(records)


def run(config: Config) -> dict:
    started = datetime.now(timezone.utc)
    pairs = _load_tradable_pairs(config.db_path)
    logger.info("Computing rolling z-scores for %d tradable pairs (window=%d)",
                len(pairs), config.zscore_lookback_window)

    updated_pairs, total_rows, errors = 0, 0, 0

    for pair in pairs:
        try:
            prices = _load_price_series(config.db_path, pair["ticker_a"], pair["ticker_b"])
            if len(prices) < config.zscore_lookback_window + 1:
                logger.debug("Not enough data yet for %s to compute rolling z-score", pair["pair_id"])
                continue

            spread = prices[pair["ticker_a"]] - pair["hedge_ratio"] * prices[pair["ticker_b"]]
            rolling_mean = spread.rolling(window=config.zscore_lookback_window).mean()
            rolling_std = spread.rolling(window=config.zscore_lookback_window).std()
            zscore = (spread - rolling_mean) / rolling_std

            out = pd.DataFrame({
                "price_a": prices[pair["ticker_a"]],
                "price_b": prices[pair["ticker_b"]],
                "spread": spread,
                "zscore": zscore,
            }).dropna()

            # Only persist the trailing window's worth of history — plenty for
            # signal continuity/backfill without unbounded table growth.
            out = out.tail(config.zscore_lookback_window * 3)

            n = _upsert_signals(config.db_path, pair["pair_id"], out)
            total_rows += n
            updated_pairs += 1

            latest = out.iloc[-1]
            logger.info("Signal updated for %s: latest zscore=%.3f (spread=%.4f)",
                        pair["pair_id"], latest["zscore"], latest["spread"])
        except Exception as exc:  # noqa: BLE001 - isolate per-pair failures
            errors += 1
            logger.warning("Z-score computation failed for %s: %s", pair["pair_id"], exc)

    duration = (datetime.now(timezone.utc) - started).total_seconds()
    logger.info("Z-score run complete in %.1fs: %d pairs updated, %d rows written, %d errors",
                duration, updated_pairs, total_rows, errors)
    return {"pairs_updated": updated_pairs, "rows_written": total_rows, "errors": errors}
