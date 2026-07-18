#!/usr/bin/env python3
"""Entrypoint: cointegration screen + rolling z-score update. Run daily after the data pipeline."""
from __future__ import annotations

import sys

from app.analytics import cointegration, zscore
from app.config import get_config
from app.db import init_db
from app.logging_config import setup_logging

logger = setup_logging("run_analytics")


def main() -> int:
    try:
        config = get_config()
    except Exception as exc:  # noqa: BLE001
        logger.critical("Configuration error: %s", exc)
        return 1

    init_db(config.db_path)

    try:
        coint_summary = cointegration.run(config)
        zscore_summary = zscore.run(config)
    except Exception as exc:  # noqa: BLE001
        logger.critical("Unhandled error in analytics engine: %s", exc, exc_info=True)
        return 1

    if coint_summary["tested"] == 0:
        logger.error("Cointegration screen tested zero pairs — check that the data pipeline has run.")
        return 1

    logger.info("Analytics run complete: %s | %s", coint_summary, zscore_summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
