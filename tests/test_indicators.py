"""
Test indicators.py module
"""
import pytest
from decimal import Decimal
from datetime import datetime, timezone

from app.indicators import ema, rsi, atr, adx, volume_sma, IndicatorError
from app.models import Candle


def test_ema_basic():
    """Test EMA calculation"""
    data = [Decimal(str(x)) for x in [100, 102, 101, 105, 107, 110, 108, 112, 115, 113]]
    
    result = ema(data, period=5)
    
    # First 4 values should be None
    assert result[0] is None
    assert result[1] is None
    assert result[2] is None
    assert result[3] is None
    
    # 5th value onwards should have EMA
    assert result[4] is not None
    assert isinstance(result[4], Decimal)


def test_ema_insufficient_data():
    """EMA with insufficient data should return all None"""
    data = [Decimal('100'), Decimal('102'), Decimal('101')]
    
    result = ema(data, period=10)
    
    assert all(x is None for x in result)


def test_ema_invalid_period():
    """EMA with invalid period should raise error"""
    data = [Decimal('100'), Decimal('102')]
    
    with pytest.raises(IndicatorError, match="period must be > 0"):
        ema(data, period=0)


def test_rsi_basic():
    """Test RSI calculation"""
    # Trending up data
    data = [Decimal(str(x)) for x in [100, 102, 104, 103, 105, 107, 106, 108, 110, 112, 111, 113, 115, 114, 116]]
    
    result = rsi(data, period=14)
    
    # First value is None (no delta)
    assert result[0] is None
    
    # After period, should have RSI
    assert result[-1] is not None
    assert Decimal('0') <= result[-1] <= Decimal('100')


def test_rsi_insufficient_data():
    """RSI with insufficient data should return None"""
    data = [Decimal('100'), Decimal('102'), Decimal('101')]
    
    result = rsi(data, period=14)
    
    assert all(x is None for x in result)


def test_atr_basic():
    """Test ATR calculation"""
    candles = []
    base_time = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
    
    for i in range(20):
        ts = base_time.replace(hour=i)
        candle = Candle(
            timestamp=ts,
            open=Decimal('100'),
            high=Decimal('105'),
            low=Decimal('95'),
            close=Decimal('102'),
            volume=Decimal('1000')
        )
        candles.append(candle)
    
    result = atr(candles, period=14)
    
    # First 14 should be None
    for i in range(14):
        assert result[i] is None
    
    # After period, should have ATR
    assert result[-1] is not None
    assert result[-1] > 0


def test_atr_insufficient_data():
    """ATR with insufficient data should return None"""
    candles = []
    base_time = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
    
    for i in range(5):
        ts = base_time.replace(hour=i)
        candle = Candle(
            timestamp=ts,
            open=Decimal('100'),
            high=Decimal('105'),
            low=Decimal('95'),
            close=Decimal('102'),
            volume=Decimal('1000')
        )
        candles.append(candle)
    
    result = atr(candles, period=14)
    
    assert all(x is None for x in result)


def test_adx_basic():
    """Test ADX calculation"""
    candles = []
    base_time = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
    
    # Trending up data
    for i in range(50):
        ts = base_time.replace(hour=i % 24, day=1 + i // 24)
        candle = Candle(
            timestamp=ts,
            open=Decimal(str(100 + i)),
            high=Decimal(str(105 + i)),
            low=Decimal(str(95 + i)),
            close=Decimal(str(102 + i)),
            volume=Decimal('1000')
        )
        candles.append(candle)
    
    adx_vals, plus_di_vals, minus_di_vals = adx(candles, period=14)
    
    # Should have values after 2*period
    assert adx_vals[-1] is not None
    assert plus_di_vals[-1] is not None
    assert minus_di_vals[-1] is not None
    
    # ADX should be in valid range
    assert Decimal('0') <= adx_vals[-1] <= Decimal('100')


def test_volume_sma_basic():
    """Test Volume SMA calculation"""
    candles = []
    base_time = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
    
    for i in range(25):
        ts = base_time.replace(hour=i % 24)
        candle = Candle(
            timestamp=ts,
            open=Decimal('100'),
            high=Decimal('105'),
            low=Decimal('95'),
            close=Decimal('102'),
            volume=Decimal(str(1000 + i * 10))
        )
        candles.append(candle)
    
    result = volume_sma(candles, period=20)
    
    # First 19 should be None
    for i in range(19):
        assert result[i] is None
    
    # After period, should have SMA
    assert result[-1] is not None
    assert result[-1] > 0


def test_volume_sma_invalid_period():
    """Volume SMA with invalid period should raise error"""
    candles = []
    base_time = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
    
    candle = Candle(
        timestamp=base_time,
        open=Decimal('100'),
        high=Decimal('105'),
        low=Decimal('95'),
        close=Decimal('102'),
        volume=Decimal('1000')
    )
    candles.append(candle)
    
    with pytest.raises(IndicatorError, match="period must be > 0"):
        volume_sma(candles, period=0)
