import logging
import sys
from pathlib import Path

from app.config import get_config


def setup_logger(name: str = "crypto_signal_bot") -> logging.Logger:
    """Setup structured logging"""

    config = get_config()

    # Flush any stale/closed handlers left over from a previous run so we can
    # re-attach fresh ones (idempotent; no-op on a clean process).
    from app.config import _flush_loggers
    _flush_loggers()

    logger = logging.getLogger(name)
    logger.setLevel(config.log_level)

    # Prevent duplicate handlers
    if logger.handlers:
        return logger

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(config.log_level)
    
    # Format: timestamp - level - message
    formatter = logging.Formatter(
        fmt='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    console_handler.setFormatter(formatter)
    
    logger.addHandler(console_handler)
    
    # File handler
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    
    file_handler = logging.FileHandler(log_dir / "signal_bot.log")
    file_handler.setLevel(config.log_level)
    file_handler.setFormatter(formatter)
    
    logger.addHandler(file_handler)
    
    return logger


def get_logger(name: str = "crypto_signal_bot") -> logging.Logger:
    """Get logger instance"""
    return logging.getLogger(name)
