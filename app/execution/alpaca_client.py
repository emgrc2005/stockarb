"""Thin, retrying wrapper around the Alpaca `alpaca-py` TradingClient.

Hardcodes `paper=True` — this framework only ever talks to the Alpaca paper
(sandbox) endpoint. If ALPACA_BASE_URL doesn't look like a paper endpoint we
refuse to start rather than silently risk routing orders at a live account.
"""
from __future__ import annotations

import logging
import time

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from app.config import Config
from app.utils.retry import network_retry

logger = logging.getLogger(__name__)

_FILL_POLL_INTERVAL_SECONDS = 2
_FILL_POLL_MAX_ATTEMPTS = 15  # ~30s max wait for a market order to fill


class AlpacaExecutionError(RuntimeError):
    """Raised when an order cannot be confirmed filled after retries/polling."""


class AlpacaClient:
    def __init__(self, config: Config):
        if "paper" not in config.alpaca_base_url:
            raise RuntimeError(
                f"ALPACA_BASE_URL ({config.alpaca_base_url!r}) does not look like a paper-trading "
                "endpoint. This framework is hardcoded to paper trading only — refusing to start."
            )

        self._config = config
        self._client = TradingClient(
            api_key=config.alpaca_api_key,
            secret_key=config.alpaca_secret_key,
            paper=True,
            url_override=config.alpaca_base_url,
        )

    def _retry(self):
        return network_retry(logger, self._config.alpaca_max_retries)

    def get_account(self):
        return self._retry()(self._client.get_account)()

    def is_market_open(self) -> bool:
        clock = self._retry()(self._client.get_clock)()
        return bool(clock.is_open)

    def is_tradable_and_shortable(self, symbol: str) -> tuple[bool, bool]:
        try:
            asset = self._retry()(self._client.get_asset)(symbol)
            return bool(asset.tradable), bool(asset.shortable)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not fetch asset info for %s: %s", symbol, exc)
            return False, False

    def get_open_positions_map(self) -> dict[str, float]:
        """Returns {symbol: qty (signed, +long/-short)} for all live Alpaca positions."""
        positions = self._retry()(self._client.get_all_positions)()
        return {p.symbol: float(p.qty) for p in positions}

    def submit_market_order(self, symbol: str, qty: float, side: OrderSide) -> dict:
        """Submits a market/day order and polls until it reaches a terminal state.

        Returns a dict with order_id, status, and filled_avg_price (None if unfilled).
        Raises AlpacaExecutionError if the order is rejected/canceled/expired.
        """
        request = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            time_in_force=TimeInForce.DAY,
        )
        order = self._retry()(self._client.submit_order)(request)
        logger.info("Submitted %s order: %s x%s (id=%s)", side.value, symbol, qty, order.id)

        for attempt in range(_FILL_POLL_MAX_ATTEMPTS):
            refreshed = self._retry()(self._client.get_order_by_id)(order.id)
            status = refreshed.status.value if hasattr(refreshed.status, "value") else str(refreshed.status)

            if status == "filled":
                logger.info("Order %s filled: %s x%s @ %s", order.id, symbol, refreshed.filled_qty, refreshed.filled_avg_price)
                return {
                    "order_id": str(order.id),
                    "status": status,
                    "filled_avg_price": float(refreshed.filled_avg_price) if refreshed.filled_avg_price else None,
                    "filled_qty": float(refreshed.filled_qty) if refreshed.filled_qty else None,
                }

            if status in ("rejected", "canceled", "expired"):
                raise AlpacaExecutionError(f"Order {order.id} for {symbol} ended in status={status}")

            time.sleep(_FILL_POLL_INTERVAL_SECONDS)

        raise AlpacaExecutionError(
            f"Order {order.id} for {symbol} did not reach a terminal state within "
            f"{_FILL_POLL_MAX_ATTEMPTS * _FILL_POLL_INTERVAL_SECONDS}s (last status={status})"
        )

    def close_position(self, symbol: str) -> dict:
        """Fully liquidates a single-symbol position via Alpaca's close endpoint."""
        order = self._retry()(self._client.close_position)(symbol)
        logger.info("Submitted close order for %s (id=%s)", symbol, order.id)

        for attempt in range(_FILL_POLL_MAX_ATTEMPTS):
            refreshed = self._retry()(self._client.get_order_by_id)(order.id)
            status = refreshed.status.value if hasattr(refreshed.status, "value") else str(refreshed.status)

            if status == "filled":
                return {
                    "order_id": str(order.id),
                    "status": status,
                    "filled_avg_price": float(refreshed.filled_avg_price) if refreshed.filled_avg_price else None,
                    "filled_qty": float(refreshed.filled_qty) if refreshed.filled_qty else None,
                }
            if status in ("rejected", "canceled", "expired"):
                raise AlpacaExecutionError(f"Close order {order.id} for {symbol} ended in status={status}")

            time.sleep(_FILL_POLL_INTERVAL_SECONDS)

        raise AlpacaExecutionError(f"Close order {order.id} for {symbol} did not fill in time")
