"""Pairwise cointegration screening (Engle-Granger + ADF) over the universe.

For every candidate pair (A, B) in the configured universe:
  1. Engle-Granger cointegration test (statsmodels.tsa.stattools.coint).
  2. OLS hedge ratio: A = alpha + beta * B  ->  spread = A - beta * B.
  3. Augmented Dickey-Fuller test on the spread itself (extra confirmation
     beyond the Engle-Granger test).
  4. Half-life of mean reversion via AR(1) fit on the spread, used to filter
     out pairs that revert too fast (noise) or too slow (not tradable).

Results are upserted into the `pairs` table, keyed by pair_id, and flagged
`is_tradable` when all thresholds are satisfied.
"""
from __future__ import annotations

import itertools
import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import statsmodels.api as sm
from statsmodels.tsa.stattools import adfuller, coint

from app.config import Config
from app.db import db_cursor

logger = logging.getLogger(__name__)

MIN_OBSERVATIONS = 100  # minimum aligned trading days required to test a pair


def _load_price_matrix(db_path: str, tickers: list[str]) -> pd.DataFrame:
    placeholders = ",".join("?" for _ in tickers)
    with db_cursor(db_path) as cur:
        cur.execute(
            f"SELECT ticker, date, adj_close FROM ohlcv WHERE ticker IN ({placeholders}) ORDER BY date",
            tickers,
        )
        rows = cur.fetchall()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=["ticker", "date", "adj_close"])
    matrix = df.pivot(index="date", columns="ticker", values="adj_close")
    return matrix


def _hedge_ratio(series_a: pd.Series, series_b: pd.Series) -> tuple[float, float]:
    """OLS: A = alpha + beta * B. Returns (beta, alpha)."""
    x = sm.add_constant(series_b.values)
    model = sm.OLS(series_a.values, x).fit()
    alpha, beta = model.params[0], model.params[1]
    return float(beta), float(alpha)


def _half_life(spread: pd.Series) -> float | None:
    """Half-life of mean reversion from an AR(1) fit: d(spread) = lambda * spread_lag + c."""
    spread_lag = spread.shift(1).dropna()
    spread_diff = spread.diff().dropna()
    spread_lag = spread_lag.loc[spread_diff.index]

    if len(spread_lag) < 20:
        return None

    x = sm.add_constant(spread_lag.values)
    model = sm.OLS(spread_diff.values, x).fit()
    lam = model.params[1]

    if lam >= 0:
        return None  # not mean-reverting (positive coefficient => explosive)

    return float(-np.log(2) / lam)


def _pair_id(ticker_a: str, ticker_b: str) -> str:
    return f"{ticker_a}_{ticker_b}"


def _upsert_pair(db_path: str, record: dict) -> None:
    with db_cursor(db_path) as cur:
        cur.execute(
            """
            INSERT INTO pairs (
                pair_id, ticker_a, ticker_b, last_test_date, coint_pvalue, adf_pvalue,
                hedge_ratio, spread_mean, spread_std, half_life_days, lookback_days,
                is_tradable, updated_at
            ) VALUES (:pair_id, :ticker_a, :ticker_b, :last_test_date, :coint_pvalue, :adf_pvalue,
                      :hedge_ratio, :spread_mean, :spread_std, :half_life_days, :lookback_days,
                      :is_tradable, :updated_at)
            ON CONFLICT(pair_id) DO UPDATE SET
                last_test_date=excluded.last_test_date,
                coint_pvalue=excluded.coint_pvalue,
                adf_pvalue=excluded.adf_pvalue,
                hedge_ratio=excluded.hedge_ratio,
                spread_mean=excluded.spread_mean,
                spread_std=excluded.spread_std,
                half_life_days=excluded.half_life_days,
                lookback_days=excluded.lookback_days,
                is_tradable=excluded.is_tradable,
                updated_at=excluded.updated_at
            """,
            record,
        )


def run(config: Config) -> dict:
    started = datetime.now(timezone.utc)
    logger.info("Starting cointegration screen over %d tickers (%d candidate pairs)",
                len(config.universe_tickers), len(config.universe_tickers) * (len(config.universe_tickers) - 1) // 2)

    prices = _load_price_matrix(config.db_path, config.universe_tickers)
    if prices.empty:
        logger.error("No price data available in DB — run the data pipeline first.")
        return {"tested": 0, "tradable": 0, "errors": 0}

    tested, tradable, errors = 0, 0, 0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for ticker_a, ticker_b in itertools.combinations(config.universe_tickers, 2):
        if ticker_a not in prices.columns or ticker_b not in prices.columns:
            continue

        pair_df = prices[[ticker_a, ticker_b]].dropna()
        if len(pair_df) < MIN_OBSERVATIONS:
            logger.debug("Skipping %s/%s: only %d aligned observations (< %d)",
                         ticker_a, ticker_b, len(pair_df), MIN_OBSERVATIONS)
            continue

        try:
            series_a, series_b = pair_df[ticker_a], pair_df[ticker_b]

            _, coint_pvalue, _ = coint(series_a, series_b)
            beta, alpha = _hedge_ratio(series_a, series_b)
            spread = series_a - beta * series_b

            adf_stat = adfuller(spread, autolag="AIC")
            adf_pvalue = float(adf_stat[1])

            half_life = _half_life(spread)

            is_tradable = (
                coint_pvalue < config.cointegration_pvalue_threshold
                and adf_pvalue < config.adf_pvalue_threshold
                and half_life is not None
                and config.min_half_life_days <= half_life <= config.max_half_life_days
            )

            record = {
                "pair_id": _pair_id(ticker_a, ticker_b),
                "ticker_a": ticker_a,
                "ticker_b": ticker_b,
                "last_test_date": today,
                "coint_pvalue": float(coint_pvalue),
                "adf_pvalue": adf_pvalue,
                "hedge_ratio": beta,
                "spread_mean": float(spread.mean()),
                "spread_std": float(spread.std()),
                "half_life_days": half_life,
                "lookback_days": len(pair_df),
                "is_tradable": int(is_tradable),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            _upsert_pair(config.db_path, record)
            tested += 1
            if is_tradable:
                tradable += 1
                logger.info(
                    "TRADABLE pair %s/%s: coint_p=%.4f adf_p=%.4f hedge_ratio=%.3f half_life=%.1fd",
                    ticker_a, ticker_b, coint_pvalue, adf_pvalue, beta, half_life,
                )
        except Exception as exc:  # noqa: BLE001 - isolate per-pair failures
            errors += 1
            logger.warning("Cointegration test failed for %s/%s: %s", ticker_a, ticker_b, exc)

    duration = (datetime.now(timezone.utc) - started).total_seconds()
    logger.info(
        "Cointegration screen complete in %.1fs: %d pairs tested, %d tradable, %d errors",
        duration, tested, tradable, errors,
    )
    return {"tested": tested, "tradable": tradable, "errors": errors}
