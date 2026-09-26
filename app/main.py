"""
Main entry point for Crypto Long/Short Signal Bot.

PHASE 3: SIGNAL-ONLY, DRY-RUN.

- Fetches public Binance market data (no trading endpoints, no credentials).
- Validates + synchronizes closed candles across 1H/15M/5M.
- Evaluates the signal engine and logs per-symbol decisions.
- NEVER creates orders.

Usage:
    python -m app.main                # run loop per config.dry_run
    python -m app.main --dry-run      # force DRY_RUN=true for this run
    python -m app.main --cycles N     # stop after N scan cycles (live validation)
"""
import argparse
import os
import sys
import time
from datetime import datetime, timezone

from app.config import get_config, Config, ConfigValidationError
from app.logger import setup_logger, get_logger
from app.scanner import Scanner
from app.signal_store import SignalStore, DELIVERY_PENDING
from app.telegram_bot import TelegramBot
from app.trading_guard import assert_no_execution_code, ExecutionCodeFound

setup_logger()
logger = get_logger(__name__)

# Delivery retry bound: each signal is retried at most this many times.
# After DELIVERY_MAX_ATTEMPTS failed sends the signal is left as-is (non-
# DELIVERED, non-retried) so it does not spam Telegram indefinitely if
# the target chat or bot is permanently broken.
DELIVERY_MAX_ATTEMPTS = 3


def _apply_dry_run(force: bool) -> None:
    """Force DRY_RUN=true for this process when --dry-run is passed.

    Done via env so the config singleton picks it up on next load.
    """
    if force:
        os.environ["DRY_RUN"] = "true"


def _make_scanner() -> Scanner:
    """Build a scanner wired to config-driven rate-limit safety."""
    config = get_config()
    return Scanner()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Crypto Long/Short Signal Bot (SIGNAL-ONLY, dry-run capable)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Force DRY_RUN=true: log decisions, never send Telegram, never trade.",
    )
    parser.add_argument(
        "--cycles", type=int, default=None,
        help="Stop after N scan cycles (for live validation). Default: run until Ctrl+C.",
    )
    args = parser.parse_args()

    _apply_dry_run(args.dry_run)

    # Hard security boundary: refuse to start if any trading-execution
    # code has been added to app/ or backtest/ (PHASE 4 requirement #28).
    try:
        guard_result = assert_no_execution_code()
        logger.info(
            f"Trading-execution guard OK: scanned {guard_result.files_scanned} "
            f"file(s), no execution symbols found"
        )
    except ExecutionCodeFound as e:
        logger.error(f"Trading-execution code detected: {e}")
        sys.exit(1)

    # Force a fresh config so the forced DRY_RUN is honored
    import app.config as cfgmod
    cfgmod.config = None
    config = get_config()

    logger.info("Starting Crypto Long/Short Signal Bot V1 (SIGNAL-ONLY)")
    logger.info(f"Symbols: {config.symbols}")
    logger.info(f"Scan interval: {config.scan_interval_seconds}s")
    logger.info(f"Min score: {config.min_score}")
    logger.info(f"Dry run: {config.dry_run}")
    logger.info(f"Telegram enabled: {config.telegram_enabled}")
    if config.dry_run:
        logger.info("DRY-RUN active: no orders, no Telegram sending, public data only")

    scanner = _make_scanner()

    # PHASE 9: Telegram delivery controlled by TELEGRAM_ENABLED (default false).
    # send_telegram = dry_run is false AND telegram_enabled is true
    send_telegram = not config.dry_run and config.telegram_enabled
    telegram_bot = TelegramBot() if send_telegram else None
    if send_telegram:
        logger.info("Telegram delivery ENABLED")
    else:
        logger.info(f"Telegram delivery DISABLED (dry_run={config.dry_run}, "
                     f"telegram_enabled={config.telegram_enabled})")

    logger.info("All components initialized")

    # PHASE 9: Signal store is always initialized for persistence.
    # When telegram_enabled, signals are sent to Telegram AND saved.
    # When telegram is disabled, signals are only saved to prevent duplicates on restart.
    store = SignalStore()

    # ------------------------------------------------------------------
    # Restart safety: rehydrate the cooldown / opposite-direction gate from
    # SQLite so a process restart cannot be used to bypass the cooldown
    # window for any symbol.
    # ------------------------------------------------------------------
    try:
        scanner.restore_cooldown_state(store.get_last_delivered_by_symbol())
    except Exception as e:
        logger.error(f"Failed to restore cooldown state: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # Restart-safe delivery retry: on startup, retry any signals that were
    # generated but never delivered (delivery_status != DELIVERED) and that
    # have not yet exhausted their bounded retry budget.
    # ------------------------------------------------------------------
    if send_telegram:
        _retry_pending_deliveries(store, telegram_bot)

    cycle_count = 0
    try:
        while True:
            cycle_count += 1
            logger.info(f"--- Scan cycle {cycle_count} ---")
            cycle_start = datetime.now(timezone.utc)

            try:
                signals = scanner.scan_all_symbols()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                logger.error(
                    f"Scanner failure (cycle {cycle_count}): {e}", exc_info=True)
                signals = []

            logger.info(f"Scan completed. Found {len(signals)} candidate signal(s)")

            for signal in signals:
                try:
                    _handle_signal(
                        signal, scanner, store, telegram_bot,
                        send_telegram, config.dry_run,
                    )
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    logger.error(
                        f"Delivery error for {signal.signal_id}: {e}",
                        exc_info=True,
                    )

            # Pacing: sleep the remainder of the scan interval
            cycle_duration = (datetime.now(timezone.utc) - cycle_start).total_seconds()
            sleep_time = max(0, config.scan_interval_seconds - cycle_duration)
            if args.cycles is not None and cycle_count >= args.cycles:
                logger.info(f"Completed {args.cycles} scan cycle(s); stopping")
                break
            if sleep_time > 0:
                logger.info(f"Sleeping {sleep_time:.1f}s until next scan")
                time.sleep(sleep_time)

    except KeyboardInterrupt:
        logger.info("Shutting down gracefully (Ctrl+C)")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


def _handle_signal(
    signal,
    scanner,
    store,
    telegram_bot,
    send_telegram,
    dry_run,
) -> None:
    """Decide whether a candidate signal is new, delivered or undelivered.

    Duplicate suppression is keyed on DELIVERY status, not mere row
    existence. A row with delivery_status=PENDING or FAILED is a signal
    whose Telegram message never reached the user and must be retried.

    Scanner in-memory state is cleared for failed deliveries so the
    scanner can re-generate the identical deterministic signal on the next
    evaluation of the same closed candle.
    """
    # 1. If already delivered: skip.
    if store.is_delivered(signal.signal_id):
        logger.debug(f"Signal {signal.signal_id} already delivered, skipping")
        return

    # 2. If the row already exists (previous cycle / restart) and is still
    #    pending or has failed but not exhausted retries, attempt delivery
    #    of the stored version (it may carry a richer failure history).
    if store.signal_exists(signal.signal_id):
        attempts = store.get_delivery_attempts(signal.signal_id)
        if not send_telegram:
            # Dry-run: mark as delivered (nothing to send).
            store.mark_delivered(signal.signal_id)
            logger.info(
                f"Signal {signal.signal_id} saved locally (Telegram disabled)")
            return
        if attempts >= DELIVERY_MAX_ATTEMPTS:
            logger.warning(
                f"Signal {signal.signal_id} exhausted retry budget "
                f"({attempts}/{DELIVERY_MAX_ATTEMPTS}), giving up")
            return
        # Fall through to delivery attempt below, using the latest
        # generated Signal object (same deterministic id).
        logger.info(
            f"Retrying undelivered signal {signal.signal_id} "
            f"(attempt {attempts + 1}/{DELIVERY_MAX_ATTEMPTS})")
    else:
        # Brand-new signal: persist it before attempting delivery so the
        # id is known even if we crash between save and send.
        store.save_signal(signal)

    # 3. Attempt Telegram delivery.
    if not send_telegram:
        # Dry-run path (row was just saved above).
        store.mark_delivered(signal.signal_id)
        logger.info(
            f"Signal {signal.signal_id} saved locally (Telegram disabled)")
        return

    ok = telegram_bot.send_signal(signal)
    if ok:
        store.mark_delivered(signal.signal_id)
        logger.info(f"Signal sent and saved: {signal.signal_id}")
    else:
        attempts = store.record_delivery_failure(signal.signal_id)
        logger.warning(
            f"Telegram failed for {signal.signal_id} "
            f"(attempt {attempts}/{DELIVERY_MAX_ATTEMPTS})")
        # Reset scanner in-memory state so this same candle can regenerate
        # the identical deterministic signal on the next scan (bounded by
        # DELIVERY_MAX_ATTEMPTS so we never spam).
        if attempts < DELIVERY_MAX_ATTEMPTS:
            scanner.clear_signal_state(signal.symbol, signal.signal_id)


def _retry_pending_deliveries(store, telegram_bot) -> None:
    """On startup, retry signals that were saved but never delivered.

    Survives process restart because delivery_status lives in SQLite.
    """
    pending = store.get_undelivered_signals(limit=DELIVERY_MAX_ATTEMPTS)
    if not pending:
        return
    logger.info(
        f"Retrying {len(pending)} undelivered signal(s) from previous session")
    for signal in pending:
        attempts = store.get_delivery_attempts(signal.signal_id)
        if attempts >= DELIVERY_MAX_ATTEMPTS:
            logger.warning(
                f"Skipping retry for {signal.signal_id}: "
                f"exhausted {attempts}/{DELIVERY_MAX_ATTEMPTS} attempts")
            continue
        ok = telegram_bot.send_signal(signal)
        if ok:
            store.mark_delivered(signal.signal_id)
            logger.info(f"Retry delivered: {signal.signal_id}")
        else:
            store.record_delivery_failure(signal.signal_id, "startup_retry")
            logger.warning(f"Retry failed: {signal.signal_id}")


if __name__ == "__main__":
    main()
