#!/usr/bin/env python3
"""Entrypoint: evaluate signals and execute/close pairs trades. Run every N minutes during market hours."""
from __future__ import annotations

import sys

from app.config import get_config
from app.db import init_db
from app.execution.trade_pairs import run
from app.logging_config import setup_logging

logger = setup_logging("run_execution")


def main() -> int:
    try:
        config = get_config()
    except Exception as exc:  # noqa: BLE001
        logger.critical("Configuration error: %s", exc)
        return 1

    init_db(config.db_path)

    try:
        run(config)
    except Exception as exc:  # noqa: BLE001
        logger.critical("Unhandled error in execution engine: %s", exc, exc_info=True)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
