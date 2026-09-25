"""
Test config.py module
"""
import pytest
import os
from decimal import Decimal

from app.config import Config, ConfigValidationError


@pytest.fixture(autouse=True)
def setup_env(monkeypatch):
    """Set all required env vars so Config() can be constructed"""
    defaults = {
        "ADX_LENGTH": "14",
        "ADX_MIN": "22",
        "RSI_LENGTH": "14",
        "RSI_MIDLINE": "50",
        "EMA_FAST": "50",
        "EMA_SLOW": "200",
        "ATR_LENGTH": "14",
        "SL_ATR_MULTIPLIER": "1.5",
        "TP1_RR": "1.5",
        "TP2_RR": "2.5",
        "VOLUME_SMA_LENGTH": "20",
        "VOLUME_MULTIPLIER": "1.2",
        "MIN_SCORE": "80",
        "MAX_DISTANCE_FROM_EMA_ATR": "2.0",
        "COOLDOWN_CANDLES": "3",
        "MAX_DATA_AGE_SECONDS": "30",
        "SYMBOLS": "BTCUSDT,ETHUSDT",
        "SCAN_INTERVAL_SECONDS": "60",
        "LOG_LEVEL": "INFO",
        "SIGNAL_ONLY": "true",
        "DRY_RUN": "true",
        "QUALITY_MODE": "true",
        "SEND_WATCH_SIGNALS": "false",
        "TELEGRAM_BOT_TOKEN": "test_token",
        "TELEGRAM_CHAT_ID": "12345",
    }
    for key, value in defaults.items():
        monkeypatch.setenv(key, value)


def test_config_adx_min_positive(setup_env):
    """ADX_MIN must be > 0"""
    config = Config()
    assert config.adx_min > 0
    assert config.adx_min == Decimal("22")


def test_config_adx_min_invalid(monkeypatch, setup_env):
    """ADX_MIN <= 0 should raise error"""
    monkeypatch.setenv("ADX_MIN", "0")
    with pytest.raises(ConfigValidationError, match="ADX_MIN must be > 0"):
        config = Config()
        _ = config.adx_min


def test_config_min_score_range(setup_env):
    """MIN_SCORE must be 0-100"""
    config = Config()
    assert 0 <= config.min_score <= 100


def test_config_min_score_invalid(monkeypatch, setup_env):
    """MIN_SCORE out of range should raise error"""
    monkeypatch.setenv("MIN_SCORE", "150")
    with pytest.raises(ConfigValidationError, match="MIN_SCORE must be 0-100"):
        config = Config()
        _ = config.min_score


def test_config_tp2_rr_greater_than_tp1(setup_env):
    """TP2_RR must be > TP1_RR"""
    config = Config()
    assert config.tp2_rr > config.tp1_rr


def test_config_tp2_rr_invalid(monkeypatch, setup_env):
    """TP2_RR <= TP1_RR should raise error"""
    monkeypatch.setenv("TP1_RR", "2.5")
    monkeypatch.setenv("TP2_RR", "1.5")
    with pytest.raises(ConfigValidationError, match="TP2_RR must be > TP1_RR"):
        config = Config()
        _ = config.tp2_rr


def test_config_cooldown_non_negative(setup_env):
    """COOLDOWN_CANDLES must be >= 0"""
    config = Config()
    assert config.cooldown_candles >= 0


def test_config_cooldown_invalid(monkeypatch, setup_env):
    """Negative COOLDOWN_CANDLES should raise error"""
    monkeypatch.setenv("COOLDOWN_CANDLES", "-1")
    with pytest.raises(ConfigValidationError, match="COOLDOWN_CANDLES must be >= 0"):
        config = Config()
        _ = config.cooldown_candles


def test_config_symbols_not_empty(setup_env):
    """SYMBOLS cannot be empty"""
    config = Config()
    assert len(config.symbols) > 0
    assert "BTCUSDT" in config.symbols


def test_config_log_level_valid(setup_env):
    """LOG_LEVEL must be valid"""
    config = Config()
    assert config.log_level in ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


def test_config_signal_only_must_be_true(monkeypatch, setup_env):
    """SIGNAL_ONLY must be true (no trading execution allowed)"""
    monkeypatch.setenv("SIGNAL_ONLY", "false")
    with pytest.raises(ConfigValidationError, match="SIGNAL_ONLY must be true"):
        config = Config()
