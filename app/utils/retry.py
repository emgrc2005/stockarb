"""Shared retry/backoff policies for network calls (yfinance, Alpaca)."""
from __future__ import annotations

import logging

import requests
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

_RETRYABLE_EXCEPTIONS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
    TimeoutError,
    ConnectionError,
    OSError,
)


def network_retry(logger: logging.Logger, max_attempts: int):
    """Retry decorator factory for flaky network I/O with exponential backoff + jitter.

    Deliberately does NOT retry on generic exceptions (e.g. bad data,
    programming errors) — only on connection/timeout style failures — so
    real bugs surface immediately instead of being retried into a timeout.
    """
    return retry(
        reraise=True,
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential_jitter(initial=1, max=30),
        retry=retry_if_exception_type(_RETRYABLE_EXCEPTIONS),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )
