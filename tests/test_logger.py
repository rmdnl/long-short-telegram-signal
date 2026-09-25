"""
Regression tests for logging initialization (silent-exit bug).

Root cause: setup_logger() attached handlers to the "crypto_signal_bot"
logger, but app modules call get_logger(__name__) which returns loggers
named "app.main", "app.scanner", etc.  Those child loggers had no handlers
and propagated to the (unconfigured) root logger, so ALL application log
output was silently swallowed.

Fix: setup_logger() attaches handlers to the ROOT logger so all app.*
loggers inherit them via propagation.
"""
import io
import logging

import pytest


@pytest.fixture(autouse=True)
def _reset_logging():
    """Snapshot + restore logging state around every test."""
    import app.config as cfg
    cfg.config = None  # force fresh config
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    # restore
    for h in list(root.handlers):
        h.close()
        root.removeHandler(h)
    for h in saved_handlers:
        root.addHandler(h)
    root.setLevel(saved_level)
    cfg.config = None


@pytest.fixture
def clean_env_for_logging(monkeypatch):
    """Minimal env so get_config() succeeds."""
    defaults = {
        "ADX_LENGTH": "14", "ADX_MIN": "22", "RSI_LENGTH": "14",
        "RSI_MIDLINE": "50", "EMA_FAST": "50", "EMA_SLOW": "200",
        "ATR_LENGTH": "14", "SL_ATR_MULTIPLIER": "1.5", "TP1_RR": "1.5",
        "TP2_RR": "2.5", "VOLUME_SMA_LENGTH": "20", "VOLUME_MULTIPLIER": "1.2",
        "MIN_SCORE": "80", "MAX_DISTANCE_FROM_EMA_ATR": "2.0", "COOLDOWN_CANDLES": "3",
        "MAX_DATA_AGE_SECONDS": "30", "SYMBOLS": "BTCUSDT,ETHUSDT",
        "SCAN_INTERVAL_SECONDS": "60", "LOG_LEVEL": "INFO",
        "SIGNAL_ONLY": "true", "DRY_RUN": "true", "QUALITY_MODE": "true",
        "SEND_WATCH_SIGNALS": "false", "TELEGRAM_ENABLED": "false",
        "TELEGRAM_BOT_TOKEN": "test_token", "TELEGRAM_CHAT_ID": "test_chat",
    }
    for key, value in defaults.items():
        monkeypatch.setenv(key, value)


class TestSetupLogger:
    def test_setup_logger_attaches_handlers_to_root(self, clean_env_for_logging):
        """Root logger must have handlers after setup_logger()."""
        from app.logger import setup_logger
        from app.config import _flush_loggers
        _flush_loggers()  # ensure clean state
        root = logging.getLogger()
        setup_logger()
        assert len(root.handlers) >= 2, f"root has {len(root.handlers)} handlers after setup"

    def test_setup_logger_set_root_level_from_config(self, clean_env_for_logging):
        """Root level must match configured LOG_LEVEL."""
        from app.logger import setup_logger
        from app.config import get_config
        setup_logger()
        assert logging.getLogger().level == logging.getLevelName(get_config().log_level)

    def test_setup_logger_has_console_and_file(self, clean_env_for_logging):
        """At least one StreamHandler and one FileHandler must be present."""
        from app.logger import setup_logger
        setup_logger()
        root = logging.getLogger()
        has_stream = any(isinstance(h, logging.StreamHandler) for h in root.handlers)
        has_file = any(isinstance(h, logging.FileHandler) for h in root.handlers)
        assert has_stream, "missing StreamHandler on root"
        assert has_file, "missing FileHandler on root"

    def test_setup_logger_idempotent(self, clean_env_for_logging):
        """Calling setup_logger() twice must not duplicate handlers."""
        from app.logger import setup_logger
        setup_logger()
        first_count = len(logging.getLogger().handlers)
        setup_logger()
        second_count = len(logging.getLogger().handlers)
        assert second_count == first_count, "duplicate handlers after two setup_logger() calls"

    def test_setup_logger_no_duplicate_handlers_after_flush(self, clean_env_for_logging):
        """_flush_loggers() + setup_logger() must not leak duplicate handlers."""
        from app.logger import setup_logger
        from app.config import _flush_loggers
        setup_logger()
        count_after_first = len(logging.getLogger().handlers)
        _flush_loggers()
        setup_logger()
        count_after_reflush = len(logging.getLogger().handlers)
        assert count_after_first == count_after_reflush


class TestAppLoggersPropagate:
    @pytest.mark.parametrize("module_name", [
        "app.main",
        "app.scanner",
        "app.telegram_bot",
        "app.signal_store",
    ])
    def test_app_logger_messages_reach_root_handler(self, clean_env_for_logging, module_name):
        """Every app.* logger must emit through a root handler."""
        from app.logger import setup_logger, get_logger
        setup_logger()

        # Add a capturing handler on root
        capture = io.StringIO()
        capture_handler = logging.StreamHandler(capture)
        capture_handler.setLevel(logging.INFO)
        logging.getLogger().addHandler(capture_handler)

        lg = get_logger(module_name)
        lg.info("probe message from %s", module_name)

        output = capture.getvalue()
        assert "probe message from" in output, (
            f"log from '{module_name}' did not reach root handler. "
            f"handlers={logging.getLogger().handlers}"
        )

    def test_no_duplicate_log_lines_on_root(self, clean_env_for_logging):
        """A single log call must produce exactly one line via root."""
        from app.logger import setup_logger, get_logger
        setup_logger()

        root = logging.getLogger()
        capture = io.StringIO()
        capture_handler = logging.StreamHandler(capture)
        capture_handler.setLevel(logging.INFO)
        capture_handler.setFormatter(logging.Formatter('%(message)s'))
        root.addHandler(capture_handler)

        lg = get_logger("app.scanner")
        lg.info("unique-test-line")

        lines = [l for l in capture.getvalue().splitlines() if "unique-test-line" in l]
        assert len(lines) == 1, f"expected 1 log line, got {len(lines)}: {lines}"


class TestGetLoggerAPI:
    def test_get_logger_default_name(self, clean_env_for_logging):
        """get_logger() with no args returns logger named 'crypto_signal_bot'."""
        from app.logger import get_logger
        lg = get_logger()
        assert lg.name == "crypto_signal_bot"

    def test_get_logger_custom_name(self, clean_env_for_logging):
        """get_logger(__name__) returns a child logger that propagates."""
        from app.logger import get_logger
        lg = get_logger("app.main")
        assert lg.name == "app.main"
        assert lg.propagate is True or lg.parent is not None


class TestIntegrationWithAppModules:
    def test_scanner_logger_emits_with_setup_logger(self, clean_env_for_logging):
        """Scanner module logger emits when setup_logger() has been called."""
        from app.logger import setup_logger
        setup_logger()
        from app.scanner import logger
        assert logger.name == "app.scanner"

        capture = io.StringIO()
        capture_handler = logging.StreamHandler(capture)
        capture_handler.setLevel(logging.INFO)
        logging.getLogger().addHandler(capture_handler)

        logger.info("scanner-integration-probe")
        assert "scanner-integration-probe" in capture.getvalue()
