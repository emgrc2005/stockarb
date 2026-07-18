#!/usr/bin/env python3
"""Entrypoint: fetch latest OHLCV data for the configured universe. Run daily."""
from __future__ import annotations

import sys

from app.config import get_config
from app.data_pipeline.fetch_data import run
from app.db import init_db
from app.logging_config import setup_logging

logger = setup_logging("run_data_pipeline")


def main() -> int:
    try:
        config = get_config()
    except Exception as exc:  # noqa: BLE001
        logger.critical("Configuration error: %s", exc)
        return 1

    init_db(config.db_path)

    try:
        summary = run(config)
    except Exception as exc:  # noqa: BLE001
        logger.critical("Unhandled error in data pipeline: %s", exc, exc_info=True)
        return 1

    # Partial failures (a few delisted/rate-limited tickers) are logged but not fatal.
    # A total failure (nothing succeeded) is treated as a hard error for cron alerting.
    if not summary["succeeded"]:
        logger.critical("Data pipeline fetched zero tickers successfully.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
