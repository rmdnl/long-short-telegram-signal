import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import List

from dotenv import load_dotenv


class ConfigValidationError(Exception):
    """Raised when configuration validation fails"""
    pass


class Config:
    """Application configuration with validation"""
    
    def __init__(self):
        load_dotenv()
        self._validate_all()
    
    # Telegram
    @property
    def telegram_bot_token(self) -> str:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        if not token or token == "your_bot_token_here":
            raise ConfigValidationError("TELEGRAM_BOT_TOKEN not set")
        return token
    
    @property
    def telegram_chat_id(self) -> str:
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        if not chat_id or chat_id == "your_chat_id_here":
            raise ConfigValidationError("TELEGRAM_CHAT_ID not set")
        return chat_id
    
    @property
    def telegram_enabled(self) -> bool:
        """PHASE 9: Default false. No network calls when disabled."""
        return os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"
    
    # Application mode
    @property
    def signal_only(self) -> bool:
        return os.getenv("SIGNAL_ONLY", "true").lower() == "true"
    
    @property
    def dry_run(self) -> bool:
        return os.getenv("DRY_RUN", "false").lower() == "true"
    
    @property
    def quality_mode(self) -> bool:
        return os.getenv("QUALITY_MODE", "true").lower() == "true"
    
    @property
    def send_watch_signals(self) -> bool:
        return os.getenv("SEND_WATCH_SIGNALS", "false").lower() == "true"
    
    # Strategy parameters
    @property
    def adx_length(self) -> int:
        return self._get_positive_int("ADX_LENGTH", 14)
    
    @property
    def adx_min(self) -> Decimal:
        val = Decimal(os.getenv("ADX_MIN", "22"))
        if val <= 0:
            raise ConfigValidationError("ADX_MIN must be > 0")
        return val
    
    @property
    def rsi_length(self) -> int:
        return self._get_positive_int("RSI_LENGTH", 14)
    
    @property
    def rsi_midline(self) -> Decimal:
        return Decimal(os.getenv("RSI_MIDLINE", "50"))
    
    @property
    def ema_fast(self) -> int:
        return self._get_positive_int("EMA_FAST", 50)
    
    @property
    def ema_slow(self) -> int:
        return self._get_positive_int("EMA_SLOW", 200)
    
    @property
    def atr_length(self) -> int:
        return self._get_positive_int("ATR_LENGTH", 14)
    
    @property
    def sl_atr_multiplier(self) -> Decimal:
        val = Decimal(os.getenv("SL_ATR_MULTIPLIER", "1.5"))
        if val <= 0:
            raise ConfigValidationError("SL_ATR_MULTIPLIER must be > 0")
        return val
    
    @property
    def tp1_rr(self) -> Decimal:
        val = Decimal(os.getenv("TP1_RR", "1.5"))
        if val <= 0:
            raise ConfigValidationError("TP1_RR must be > 0")
        return val
    
    @property
    def tp2_rr(self) -> Decimal:
        val = Decimal(os.getenv("TP2_RR", "2.5"))
        if val <= 0:
            raise ConfigValidationError("TP2_RR must be > 0")
        if val <= self.tp1_rr:
            raise ConfigValidationError("TP2_RR must be > TP1_RR")
        return val
    
    @property
    def volume_sma_length(self) -> int:
        return self._get_positive_int("VOLUME_SMA_LENGTH", 20)
    
    @property
    def volume_multiplier(self) -> Decimal:
        val = Decimal(os.getenv("VOLUME_MULTIPLIER", "1.2"))
        if val <= 0:
            raise ConfigValidationError("VOLUME_MULTIPLIER must be > 0")
        return val
    
    @property
    def min_score(self) -> int:
        val = int(os.getenv("MIN_SCORE", "80"))
        if not 0 <= val <= 100:
            raise ConfigValidationError("MIN_SCORE must be 0-100")
        return val
    
    @property
    def max_distance_from_ema_atr(self) -> Decimal:
        val = Decimal(os.getenv("MAX_DISTANCE_FROM_EMA_ATR", "2.0"))
        if val <= 0:
            raise ConfigValidationError("MAX_DISTANCE_FROM_EMA_ATR must be > 0")
        return val
    
    @property
    def cooldown_candles(self) -> int:
        val = int(os.getenv("COOLDOWN_CANDLES", "3"))
        if val < 0:
            raise ConfigValidationError("COOLDOWN_CANDLES must be >= 0")
        return val
    
    @property
    def max_data_age_seconds(self) -> int:
        val = int(os.getenv("MAX_DATA_AGE_SECONDS", "30"))
        if val <= 0:
            raise ConfigValidationError("MAX_DATA_AGE_SECONDS must be > 0")
        return val

    # Rate-limit safety (PHASE 3)
    @property
    def request_timeout_seconds(self) -> int:
        val = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "10"))
        if val <= 0:
            raise ConfigValidationError("REQUEST_TIMEOUT_SECONDS must be > 0")
        return val

    @property
    def max_retries(self) -> int:
        val = int(os.getenv("MAX_RETRIES", "3"))
        if val < 0:
            raise ConfigValidationError("MAX_RETRIES must be >= 0")
        return val

    @property
    def backoff_base_seconds(self) -> int:
        val = int(os.getenv("BACKOFF_BASE_SECONDS", "1"))
        if val <= 0:
            raise ConfigValidationError("BACKOFF_BASE_SECONDS must be > 0")
        return val

    # Scanning
    @property
    def symbols(self) -> List[str]:
        symbols_str = os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,AVAXUSDT,LINKUSDT")
        symbols = [s.strip() for s in symbols_str.split(",") if s.strip()]
        if not symbols:
            raise ConfigValidationError("SYMBOLS list cannot be empty")
        return symbols
    
    @property
    def scan_interval_seconds(self) -> int:
        val = int(os.getenv("SCAN_INTERVAL_SECONDS", "60"))
        if val <= 0:
            raise ConfigValidationError("SCAN_INTERVAL_SECONDS must be > 0")
        return val
    
    # Logging
    @property
    def log_level(self) -> str:
        level = os.getenv("LOG_LEVEL", "INFO").upper()
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        if level not in valid_levels:
            raise ConfigValidationError(f"LOG_LEVEL must be one of {valid_levels}")
        return level
    
    # Helpers
    def _get_positive_int(self, key: str, default: int) -> int:
        val = int(os.getenv(key, str(default)))
        if val <= 0:
            raise ConfigValidationError(f"{key} must be > 0")
        return val
    
    def _validate_all(self):
        """Validate all configuration on startup"""
        try:
            # Force property access to trigger validation
            _ = self.adx_length
            _ = self.adx_min
            _ = self.rsi_length
            _ = self.rsi_midline
            _ = self.ema_fast
            _ = self.ema_slow
            _ = self.atr_length
            _ = self.sl_atr_multiplier
            _ = self.tp1_rr
            _ = self.tp2_rr
            _ = self.volume_sma_length
            _ = self.volume_multiplier
            _ = self.min_score
            _ = self.max_distance_from_ema_atr
            _ = self.cooldown_candles
            _ = self.max_data_age_seconds
            _ = self.request_timeout_seconds
            _ = self.max_retries
            _ = self.backoff_base_seconds
            _ = self.symbols
            _ = self.scan_interval_seconds
            _ = self.log_level
            
            # Check signal_only
            if not self.signal_only:
                raise ConfigValidationError("SIGNAL_ONLY must be true. This is a signal-only application.")
            
        except (ValueError, KeyError) as e:
            raise ConfigValidationError(f"Configuration error: {e}")


# Global config instance
config: Config | None = None


def get_config() -> Config:
    """Get or create global config instance"""
    global config
    if config is None:
        config = Config()
    return config


def _flush_loggers() -> None:
    """Force-close all logging handlers and reset to a clean state.

    Needed when main() is called multiple times in one process (tests,
    `--cycles` re-runs): setup_logger() skips handler setup when the
    crypto_signal_bot logger already has handlers, so stale closed handlers
    from a previous run would silently swallow all log output.
    """
    import logging
    for logger in (logging.getLogger(), logging.getLogger("crypto_signal_bot")):
        for h in list(logger.handlers):
            h.close()
            logger.removeHandler(h)
