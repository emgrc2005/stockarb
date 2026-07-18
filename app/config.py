"""Centralized, validated configuration loaded from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _get_str(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _get_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _get_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _get_list(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    return [t.strip().upper() for t in raw.split(",") if t.strip()]


@dataclass(frozen=True)
class Config:
    # Alpaca
    alpaca_api_key: str = field(default_factory=lambda: _get_str("ALPACA_API_KEY", required=True))
    alpaca_secret_key: str = field(default_factory=lambda: _get_str("ALPACA_SECRET_KEY", required=True))
    alpaca_base_url: str = field(default_factory=lambda: _get_str("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"))
    alpaca_max_retries: int = field(default_factory=lambda: _get_int("ALPACA_MAX_RETRIES", 4))
    alpaca_timeout_seconds: int = field(default_factory=lambda: _get_int("ALPACA_TIMEOUT_SECONDS", 15))

    # Universe
    universe_tickers: list[str] = field(default_factory=lambda: _get_list(
        "UNIVERSE_TICKERS", "NEE,DUK,SO,D,AEP,EXC,XEL,ED,WEC,ES,PEG,EIX,SRE,PPL,FE"
    ))

    # Data pipeline
    db_path: str = field(default_factory=lambda: _get_str("DB_PATH", "/app/data/stat_arb.db"))
    lookback_days: int = field(default_factory=lambda: _get_int("LOOKBACK_DAYS", 756))
    yfinance_max_retries: int = field(default_factory=lambda: _get_int("YFINANCE_MAX_RETRIES", 4))
    yfinance_timeout_seconds: int = field(default_factory=lambda: _get_int("YFINANCE_TIMEOUT_SECONDS", 30))

    # Analytics
    cointegration_pvalue_threshold: float = field(default_factory=lambda: _get_float("COINTEGRATION_PVALUE_THRESHOLD", 0.05))
    adf_pvalue_threshold: float = field(default_factory=lambda: _get_float("ADF_PVALUE_THRESHOLD", 0.05))
    zscore_lookback_window: int = field(default_factory=lambda: _get_int("ZSCORE_LOOKBACK_WINDOW", 30))
    min_half_life_days: float = field(default_factory=lambda: _get_float("MIN_HALF_LIFE_DAYS", 1))
    max_half_life_days: float = field(default_factory=lambda: _get_float("MAX_HALF_LIFE_DAYS", 45))

    # Execution
    zscore_entry_threshold: float = field(default_factory=lambda: _get_float("ZSCORE_ENTRY_THRESHOLD", 2.0))
    zscore_exit_threshold: float = field(default_factory=lambda: _get_float("ZSCORE_EXIT_THRESHOLD", 0.5))
    zscore_stoploss_threshold: float = field(default_factory=lambda: _get_float("ZSCORE_STOPLOSS_THRESHOLD", 3.5))
    position_size_usd: float = field(default_factory=lambda: _get_float("POSITION_SIZE_USD", 2000))
    max_concurrent_pairs: int = field(default_factory=lambda: _get_int("MAX_CONCURRENT_PAIRS", 5))

    # Misc
    timezone: str = field(default_factory=lambda: _get_str("TIMEZONE", "America/New_York"))
    log_level: str = field(default_factory=lambda: _get_str("LOG_LEVEL", "INFO"))


def get_config() -> Config:
    return Config()
