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
from app.signal_store import SignalStore
from app.telegram_bot import TelegramBot
from app.trading_guard import assert_no_execution_code, ExecutionCodeFound

setup_logger()
logger = get_logger(__name__)


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
    # Signal generation always runs (dry_run only affects Telegram + trading).
    # send_telegram = dry_run is false AND telegram_enabled is true
    send_telegram = not config.dry_run and config.telegram_enabled
    telegram_bot = TelegramBot() if send_telegram else None
    signal_store = SignalStore() if send_telegram else None
    if send_telegram:
        logger.info("Telegram delivery ENABLED")
    else:
        logger.info(f"Telegram delivery DISABLED (dry_run={config.dry_run}, telegram_enabled={config.telegram_enabled})")

    logger.info("All components initialized")

    # PHASE 9: Signal store is always initialized for persistence.
    # When telegram_enabled, signals are sent to Telegram AND saved.
    # When telegram is disabled, signals are only saved to prevent duplicates on restart.
    always_store = SignalStore()

    cycle_count = 0
    try:
        while True:
            cycle_count += 1
            logger.info(f"--- Scan cycle {cycle_count} ---")
            cycle_start = datetime.now(timezone.utc)

            signals = scanner.scan_all_symbols()
            logger.info(f"Scan completed. Found {len(signals)} candidate signal(s)")

            for signal in signals:
                # Duplicate check: skip if already processed in a previous cycle/restart
                if always_store.signal_exists(signal.signal_id):
                    logger.debug(f"Signal {signal.signal_id} already processed, skipping")
                    continue

                if send_telegram:
                    # Telegram delivery + local persistence
                    if telegram_bot.send_signal(signal):
                        always_store.save_signal(signal)
                        logger.info(f"Signal sent and saved: {signal.signal_id}")
                    else:
                        logger.warning(f"Telegram failed for {signal.signal_id}, saving locally")
                        always_store.save_signal(signal)
                else:
                    # Dry-run / disabled: log message + save locally for dedup
                    bot = TelegramBot()
                    bot.send_signal(signal)  # logs "[TELEGRAM DISABLED] Would send..."
                    always_store.save_signal(signal)
                    logger.info(f"Signal saved locally (Telegram disabled): {signal.signal_id}")

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


if __name__ == "__main__":
    main()
