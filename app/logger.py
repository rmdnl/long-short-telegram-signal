import logging
import sys
from pathlib import Path

from app.config import get_config


def setup_logger(name: str = "crypto_signal_bot") -> logging.Logger:
    """Setup structured logging.

    Attaches console + file handlers to the *root* logger so that every
    module-level logger created via ``get_logger(__name__)`` (e.g.
    ``app.main``, ``app.scanner``) inherits them through standard
    propagation.  ``propagate`` defaults to ``True`` on child loggers,
    so messages flow to root once handlers are attached there.
    """

    config = get_config()

    # Flush any stale/closed handlers left over from a previous run so we can
    # re-attach fresh ones (idempotent; no-op on a clean process).
    from app.config import _flush_loggers
    _flush_loggers()

    root = logging.getLogger()
    root.setLevel(config.log_level)

    formatter = logging.Formatter(
        fmt='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(config.log_level)
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    # File handler
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    file_handler = logging.FileHandler(log_dir / "signal_bot.log")
    file_handler.setLevel(config.log_level)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    return root


def get_logger(name: str = "crypto_signal_bot") -> logging.Logger:
    """Get logger instance.

    Returns a child logger named *name* that propagates to the root logger
    configured by ``setup_logger()``.
    """
    return logging.getLogger(name)
