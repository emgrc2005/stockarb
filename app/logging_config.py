"""Stdout-only structured logging, safe for `docker logs` / supercronic capture."""
from __future__ import annotations

import logging
import os
import sys


class _UTCFormatter(logging.Formatter):
    converter = __import__("time").gmtime


_FORMAT = "%(asctime)s.%(msecs)03dZ %(levelname)-8s [%(name)s] %(message)s"
_DATEFMT = "%Y-%m-%dT%H:%M:%S"


def setup_logging(name: str) -> logging.Logger:
    level = os.getenv("LOG_LEVEL", "INFO").upper()

    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(stream=sys.stdout)
        handler.setFormatter(_UTCFormatter(_FORMAT, datefmt=_DATEFMT))
        root.addHandler(handler)
        root.setLevel(level)

        # Keep noisy third-party libraries at WARNING unless we're debugging.
        if level != "DEBUG":
            logging.getLogger("urllib3").setLevel(logging.WARNING)
            logging.getLogger("yfinance").setLevel(logging.WARNING)
            logging.getLogger("peewee").setLevel(logging.WARNING)

    return logging.getLogger(name)
