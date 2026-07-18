"""Signal-driven pairs trading execution.

Reads the latest z-score signals produced by the analytics engine and:
  - Closes open positions whose spread has reverted to the mean (or hit the
    stop-loss threshold).
  - Opens new dual-legged positions (long one leg / short the other) for
    tradable pairs whose z-score has crossed the entry threshold, up to
    `MAX_CONCURRENT_PAIRS` concurrent pairs.

Every order pair is submitted leg-by-leg; if the second leg fails after the
first filled, the engine immediately attempts to unwind the filled leg so we
never carry an accidental unhedged single-leg position, and logs a CRITICAL
alert either way.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

from alpaca.trading.enums import OrderSide

from app.config import Config
from app.db import db_cursor
from app.execution.alpaca_client import AlpacaClient, AlpacaExecutionError

logger = logging.getLogger(__name__)


def _get_open_positions(db_path: str) -> list[dict]:
    with db_cursor(db_path) as cur:
        cur.execute("SELECT * FROM positions WHERE status = 'OPEN'")
        return [dict(row) for row in cur.fetchall()]


def _get_tradable_pairs(db_path: str) -> list[dict]:
    with db_cursor(db_path) as cur:
        cur.execute("SELECT * FROM pairs WHERE is_tradable = 1")
        return [dict(row) for row in cur.fetchall()]


def _get_latest_signal(db_path: str, pair_id: str) -> dict | None:
    with db_cursor(db_path) as cur:
        cur.execute(
            "SELECT * FROM signals WHERE pair_id = ? ORDER BY date DESC LIMIT 1",
            (pair_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def _log_order(db_path: str, position_id: int | None, ticker: str, side: str, qty: float, order_id: str | None, status: str | None) -> None:
    with db_cursor(db_path) as cur:
        cur.execute(
            """
            INSERT INTO orders_log (position_id, ticker, side, qty, alpaca_order_id, alpaca_status, submitted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (position_id, ticker, side, qty, order_id, status, datetime.now(timezone.utc).isoformat()),
        )


def _close_position_record(db_path: str, position: dict, exit_price_long: float, exit_price_short: float,
                            exit_zscore: float, exit_reason: str) -> None:
    # Long leg P&L: (exit - entry) * qty. Short leg P&L: (entry - exit) * qty.
    pnl_long = (exit_price_long - position["entry_price_long"]) * position["qty_long"]
    pnl_short = (position["entry_price_short"] - exit_price_short) * position["qty_short"]
    realized_pnl = pnl_long + pnl_short

    with db_cursor(db_path) as cur:
        cur.execute(
            """
            UPDATE positions SET
                status = 'CLOSED', exit_price_long = ?, exit_price_short = ?,
                exit_zscore = ?, exit_reason = ?, realized_pnl = ?, closed_at = ?
            WHERE id = ?
            """,
            (exit_price_long, exit_price_short, exit_zscore, exit_reason, realized_pnl,
             datetime.now(timezone.utc).isoformat(), position["id"]),
        )
    logger.info(
        "CLOSED position %s (%s/%s) reason=%s realized_pnl=%.2f",
        position["id"], position["ticker_long"], position["ticker_short"], exit_reason, realized_pnl,
    )


def _process_exits(db_path: str, client: AlpacaClient, config: Config, open_positions: list[dict]) -> int:
    closed_count = 0
    for position in open_positions:
        signal = _get_latest_signal(db_path, position["pair_id"])
        if signal is None:
            logger.warning("No signal available for open position %s (pair %s) — cannot evaluate exit.",
                            position["id"], position["pair_id"])
            continue

        z = signal["zscore"]
        reason = None
        if abs(z) <= config.zscore_exit_threshold:
            reason = "mean_reversion"
        elif abs(z) >= config.zscore_stoploss_threshold:
            reason = "stop_loss"

        if reason is None:
            logger.debug("Position %s (%s) holding: zscore=%.3f", position["id"], position["pair_id"], z)
            continue

        try:
            fill_long = client.close_position(position["ticker_long"])
            fill_short = client.close_position(position["ticker_short"])
            _log_order(db_path, position["id"], position["ticker_long"], "sell", position["qty_long"],
                       fill_long["order_id"], fill_long["status"])
            _log_order(db_path, position["id"], position["ticker_short"], "buy", position["qty_short"],
                       fill_short["order_id"], fill_short["status"])
            _close_position_record(
                db_path, position,
                exit_price_long=fill_long["filled_avg_price"] or signal["price_a"],
                exit_price_short=fill_short["filled_avg_price"] or signal["price_b"],
                exit_zscore=z, exit_reason=reason,
            )
            closed_count += 1
        except AlpacaExecutionError as exc:
            logger.critical(
                "Failed to fully close position %s (%s/%s): %s — MANUAL INTERVENTION MAY BE REQUIRED.",
                position["id"], position["ticker_long"], position["ticker_short"], exc,
            )
    return closed_count


def _compute_quantities(config: Config, price_a: float, price_b: float, hedge_ratio: float) -> tuple[int, int]:
    """Beta-weighted, whole-share quantities so leg B approximates hedge_ratio * leg A in value."""
    qty_a = math.floor(config.position_size_usd / price_a)
    qty_b = math.floor((config.position_size_usd * abs(hedge_ratio)) / price_b)
    return max(qty_a, 0), max(qty_b, 0)


def _open_position(db_path: str, client: AlpacaClient, pair: dict, signal: dict, side_a: OrderSide, side_b: OrderSide,
                    qty_a: int, qty_b: int) -> None:
    ticker_a, ticker_b = pair["ticker_a"], pair["ticker_b"]
    long_ticker, short_ticker = (ticker_a, ticker_b) if side_a == OrderSide.BUY else (ticker_b, ticker_a)
    long_qty, short_qty = (qty_a, qty_b) if side_a == OrderSide.BUY else (qty_b, qty_a)

    fill_a = None
    try:
        fill_a = client.submit_market_order(ticker_a, qty_a, side_a)
        fill_b = client.submit_market_order(ticker_b, qty_b, side_b)
    except AlpacaExecutionError as exc:
        logger.critical(
            "Leg failure opening pair %s: %s. Attempting to unwind any filled leg.", pair["pair_id"], exc,
        )
        if fill_a is not None:
            try:
                client.close_position(ticker_a)
                logger.warning("Unwound leg %s after sibling leg %s failed to fill.", ticker_a, ticker_b)
            except AlpacaExecutionError as unwind_exc:
                logger.critical(
                    "FAILED TO UNWIND %s after partial pair entry on %s — MANUAL INTERVENTION REQUIRED: %s",
                    ticker_a, pair["pair_id"], unwind_exc,
                )
        return

    entry_price_long = fill_a["filled_avg_price"] if side_a == OrderSide.BUY else fill_b["filled_avg_price"]
    entry_price_short = fill_b["filled_avg_price"] if side_a == OrderSide.BUY else fill_a["filled_avg_price"]
    order_id_long = fill_a["order_id"] if side_a == OrderSide.BUY else fill_b["order_id"]
    order_id_short = fill_b["order_id"] if side_a == OrderSide.BUY else fill_a["order_id"]

    now = datetime.now(timezone.utc).isoformat()
    with db_cursor(db_path) as cur:
        cur.execute(
            """
            INSERT INTO positions (
                pair_id, ticker_long, ticker_short, qty_long, qty_short,
                entry_price_long, entry_price_short, entry_zscore, status,
                alpaca_order_id_long, alpaca_order_id_short, opened_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?)
            """,
            (pair["pair_id"], long_ticker, short_ticker, long_qty, short_qty,
             entry_price_long, entry_price_short, signal["zscore"], order_id_long, order_id_short, now),
        )
        position_id = cur.lastrowid

    _log_order(db_path, position_id, ticker_a, side_a.value, qty_a, fill_a["order_id"], fill_a["status"])
    _log_order(db_path, position_id, ticker_b, side_b.value, qty_b, fill_b["order_id"], fill_b["status"])

    logger.info(
        "OPENED position %s: LONG %s x%s @ %.2f / SHORT %s x%s @ %.2f (entry_zscore=%.3f)",
        position_id, long_ticker, long_qty, entry_price_long, short_ticker, short_qty, entry_price_short, signal["zscore"],
    )


def _process_entries(db_path: str, client: AlpacaClient, config: Config, open_positions: list[dict]) -> int:
    open_pair_ids = {p["pair_id"] for p in open_positions}
    slots_available = config.max_concurrent_pairs - len(open_positions)
    if slots_available <= 0:
        logger.info("Max concurrent pairs (%d) already open — skipping entry scan.", config.max_concurrent_pairs)
        return 0

    opened_count = 0
    for pair in _get_tradable_pairs(db_path):
        if opened_count >= slots_available:
            break
        if pair["pair_id"] in open_pair_ids:
            continue

        signal = _get_latest_signal(db_path, pair["pair_id"])
        if signal is None:
            continue

        z = signal["zscore"]
        if abs(z) < config.zscore_entry_threshold:
            continue

        # z >= entry: spread (A - beta*B) is abnormally high -> A overpriced -> short A / long B.
        # z <= -entry: spread abnormally low -> A underpriced -> long A / short B.
        if z >= config.zscore_entry_threshold:
            side_a, side_b = OrderSide.SELL, OrderSide.BUY
        else:
            side_a, side_b = OrderSide.BUY, OrderSide.SELL

        tradable_a, shortable_a = client.is_tradable_and_shortable(pair["ticker_a"])
        tradable_b, shortable_b = client.is_tradable_and_shortable(pair["ticker_b"])
        needs_short_a = side_a == OrderSide.SELL
        needs_short_b = side_b == OrderSide.SELL

        if not tradable_a or not tradable_b or (needs_short_a and not shortable_a) or (needs_short_b and not shortable_b):
            logger.warning(
                "Skipping pair %s: tradable_a=%s tradable_b=%s shortable_a=%s shortable_b=%s",
                pair["pair_id"], tradable_a, tradable_b, shortable_a, shortable_b,
            )
            continue

        qty_a, qty_b = _compute_quantities(config, signal["price_a"], signal["price_b"], pair["hedge_ratio"])
        if qty_a < 1 or qty_b < 1:
            logger.warning(
                "Skipping pair %s: position size $%.2f too small for share prices (%.2f / %.2f)",
                pair["pair_id"], config.position_size_usd, signal["price_a"], signal["price_b"],
            )
            continue

        logger.info("ENTRY signal on %s: zscore=%.3f (threshold=%.2f) -> %s %s / %s %s",
                    pair["pair_id"], z, config.zscore_entry_threshold,
                    side_a.value, pair["ticker_a"], side_b.value, pair["ticker_b"])
        _open_position(db_path, client, pair, signal, side_a, side_b, qty_a, qty_b)
        opened_count += 1

    return opened_count


def _check_broker_drift(db_path: str, client: AlpacaClient) -> None:
    """Cross-checks local OPEN positions against Alpaca's live position list.

    Doesn't attempt to auto-reconcile — a mismatch usually means a manual
    intervention (or a bug) happened out of band, and silently "fixing" it
    could mask a real problem. We just log loudly so it gets noticed.
    """
    try:
        live = client.get_open_positions_map()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not fetch live Alpaca positions for drift check: %s", exc)
        return

    expected: dict[str, float] = {}
    for position in _get_open_positions(db_path):
        expected[position["ticker_long"]] = expected.get(position["ticker_long"], 0) + position["qty_long"]
        expected[position["ticker_short"]] = expected.get(position["ticker_short"], 0) - position["qty_short"]

    mismatched = {
        symbol for symbol in set(expected) | set(live)
        if round(expected.get(symbol, 0), 4) != round(live.get(symbol, 0), 4)
    }
    if mismatched:
        logger.critical(
            "Broker/DB position drift detected for %s — expected=%s live=%s. "
            "Investigate before trusting automated entries/exits.",
            sorted(mismatched), expected, live,
        )
    else:
        logger.info("Broker/DB position reconciliation OK (%d symbols).", len(expected))


def run(config: Config) -> dict:
    started = datetime.now(timezone.utc)
    client = AlpacaClient(config)

    if not client.is_market_open():
        logger.info("Market is closed — skipping execution cycle.")
        return {"market_open": False, "closed": 0, "opened": 0}

    account = client.get_account()
    logger.info("Account status=%s, buying_power=%s, equity=%s", account.status, account.buying_power, account.equity)

    _check_broker_drift(config.db_path, client)

    open_positions = _get_open_positions(config.db_path)
    logger.info("Evaluating %d open position(s) for exit.", len(open_positions))
    closed = _process_exits(config.db_path, client, config, open_positions)

    # Re-fetch after exits so entry logic sees freed-up slots.
    remaining_open = _get_open_positions(config.db_path)
    opened = _process_entries(config.db_path, client, config, remaining_open)

    duration = (datetime.now(timezone.utc) - started).total_seconds()
    logger.info("Execution cycle complete in %.1fs: %d closed, %d opened", duration, closed, opened)
    return {"market_open": True, "closed": closed, "opened": opened}
