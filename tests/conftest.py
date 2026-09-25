"""Shared test fixtures"""
import pytest
import os

from decimal import Decimal
from datetime import datetime, timezone, timedelta

from app.models import Candle, IndicatorValues, MarketBias


@pytest.fixture
def clean_env(monkeypatch):
    """Set default env vars for config tests, clean afterwards"""
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
    # Reset config singleton so fresh env is honored
    import app.config as cfg
    cfg.config = None
    yield
    cfg.config = None


@pytest.fixture
def sample_candles():
    """Generate 250 sample 1H candles, trending up"""
    candles = []
    base = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
    for i in range(250):
        open_p = Decimal(str(100 + i * 0.5))
        candles.append(Candle(
            timestamp=base,
            open=open_p,
            high=open_p + Decimal('2'),
            low=open_p - Decimal('2'),
            close=open_p + Decimal('0.5'),
            volume=Decimal('1000'),
        ))
        base = base + timedelta(hours=1)
    return candles


@pytest.fixture
def synth_data():
    """Multi-timeframe synthetic dataset helpers (from conftest_backtest)."""
    from tests import conftest_backtest as cb
    return cb
