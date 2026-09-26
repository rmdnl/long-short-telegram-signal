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
from typing import Optional

from app.config import get_config, Config, ConfigValidationError
from app.logger import setup_logger, get_logger
from app.scanner import Scanner
from app.signal_store import SignalStore, DELIVERY_PENDING
from app.telegram_bot import TelegramBot
from app.trading_guard import assert_no_execution_code, ExecutionCodeFound
from app.outcome_monitor import (
    evaluate_signal_outcome,
    OutcomeMonitorRunner,
    OUTCOME_MAX_ATTEMPTS,
)
from app.market_data import BinanceMarketData

setup_logger()
logger = get_logger(__name__)

# Delivery retry bound: each signal is retried at most this many times.
# After DELIVERY_MAX_ATTEMPTS failed sends the signal is left as-is (non-
# DELIVERED, non-retried) so it does not spam Telegram indefinitely if
# the target chat or bot is permanently broken.
DELIVERY_MAX_ATTEMPTS = 3

# ------------------------------------------------------------------
# Startup-notification crash-loop protection
# ------------------------------------------------------------------
# A systemd / Docker restart loop would otherwise send "BOT ONLINE" every few
# seconds and flood the chat. We persist the last startup-notification time
# in SQLite and suppress the message when the previous successful start was
# less than STARTUP_NOTICE_MIN_INTERVAL_SECONDS ago.
STARTUP_NOTICE_MIN_INTERVAL_SECONDS = 300
_STARTUP_NOTICE_META_KEY = "last_startup_notice_at"

# Epoch seconds of the first-ever boot. Used to report uptime even when the
# process that started us is a fresh restart.
_BOT_BOOTED_AT_META_KEY = "bot_booted_at_epoch"


def _parse_epoch(raw: Optional[str]) -> Optional[float]:
    """Parse a persisted epoch-seconds string, returning None when unusable."""
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _send_startup_notification(config, store: SignalStore,
                               telegram_bot: Optional[TelegramBot],
                               send_telegram: bool) -> None:
    """Send exactly one 🚀 BOT ONLINE message per session, rate-limited.

    Rate limiting is persisted (not in-memory) so a crash-restart loop cannot
    bypass it: the timestamp of the last *successful* startup notice lives in
    SQLite, and a restart inside the cooldown window is logged but silent.
    """
    now = datetime.now(timezone.utc)

    # First boot ever: record the boot epoch so uptime is reportable.
    if store.get_meta(_BOT_BOOTED_AT_META_KEY) is None:
        store.set_meta(_BOT_BOOTED_AT_META_KEY, str(now.timestamp()))

    booted_at = _parse_epoch(store.get_meta(_BOT_BOOTED_AT_META_KEY)) or now.timestamp()
    uptime_seconds = max(0, int(now.timestamp() - booted_at))

    # Crash-loop guard.
    last_notice = _parse_epoch(store.get_meta(_STARTUP_NOTICE_META_KEY))
    if last_notice is not None:
        elapsed = (now.timestamp() - last_notice)
        if elapsed < STARTUP_NOTICE_MIN_INTERVAL_SECONDS:
            logger.warning(
                f"Startup notification SUPPRESSED: last sent {elapsed:.0f}s ago "
                f"(< {STARTUP_NOTICE_MIN_INTERVAL_SECONDS}s cooldown). "
                f"Possible crash-restart loop."
            )
            return

    # Component health snapshot.
    components = {
        "Scanner": True,
        "Signal store": True,
        "Telegram": bool(send_telegram),
        "Outcome monitor": True,
    }

    if not send_telegram or telegram_bot is None:
        logger.info(
            "Telegram disabled: startup notification not sent (dry-run/no delivery)"
        )
        return

    try:
        ok = telegram_bot.send_startup(
            symbols=config.symbols,
            interval_seconds=config.scan_interval_seconds,
            min_score=config.min_score,
            components=components,
        )
    except Exception as e:
        # A notification must never take the process down.
        logger.error(f"Startup notification raised, ignoring: {e}", exc_info=True)
        return

    if ok:
        store.set_meta(_STARTUP_NOTICE_META_KEY, str(now.timestamp()))
        logger.info("BOT ONLINE notification delivered")
    else:
        logger.warning("BOT ONLINE notification failed (not retried until next boot)")


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

    # ------------------------------------------------------------------
    # Startup notification: one 🚀 BOT ONLINE message after successful init.
    # ------------------------------------------------------------------
    _send_startup_notification(config, store, telegram_bot, send_telegram)

    # ------------------------------------------------------------------
    # Outcome monitor: evaluates active signals against closed market data to
    # detect TP1 / TP2 / SL hits. Created only when Telegram is live, because
    # an undelivered signal is never monitorable and we must not pay for
    # market data we cannot act on.
    # ------------------------------------------------------------------
    outcome_monitor = None
    if send_telegram:
        try:
            outcome_monitor = OutcomeMonitorRunner(store, telegram_bot)
            logger.info("Outcome monitor initialized")
        except Exception as e:
            logger.error(
                f"Failed to initialize outcome monitor: {e}", exc_info=True)
            outcome_monitor = None

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

            # Outcome monitor: one pass per cycle, isolated from the scanner.
            if outcome_monitor is not None:
                try:
                    outcome_monitor.run_once()
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    logger.error(
                        f"Outcome monitor failure (cycle {cycle_count}): {e}",
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
