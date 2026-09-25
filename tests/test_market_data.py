"""
Test market_data.py module
"""
import pytest
from datetime import datetime, timezone
from decimal import Decimal

from app.market_data import BinanceMarketData, MarketDataError, normalize_candles
from app.models import Candle


def test_validate_candles_empty():
    """Empty candle list should raise error"""
    market_data = BinanceMarketData()
    
    with pytest.raises(MarketDataError, match="Empty candles list"):
        market_data.validate_candles([])


def test_validate_candles_duplicate_timestamps():
    """Duplicate timestamps should raise error"""
    market_data = BinanceMarketData()
    
    ts = datetime.now(timezone.utc)
    candle1 = Candle(ts, Decimal('100'), Decimal('105'), Decimal('95'), Decimal('102'), Decimal('1000'))
    candle2 = Candle(ts, Decimal('102'), Decimal('110'), Decimal('100'), Decimal('108'), Decimal('1200'))
    
    with pytest.raises(MarketDataError, match="Duplicate"):
        market_data.validate_candles([candle1, candle2])


def test_validate_candles_not_sorted():
    """Candles not sorted by timestamp should raise error"""
    market_data = BinanceMarketData()
    
    ts1 = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    ts2 = datetime(2024, 1, 1, 11, 0, tzinfo=timezone.utc)
    
    candle1 = Candle(ts1, Decimal('100'), Decimal('105'), Decimal('95'), Decimal('102'), Decimal('1000'))
    candle2 = Candle(ts2, Decimal('102'), Decimal('110'), Decimal('100'), Decimal('108'), Decimal('1200'))
    
    with pytest.raises(MarketDataError, match="not sorted"):
        market_data.validate_candles([candle1, candle2])


def test_validate_candles_invalid_ohlc():
    """Invalid OHLC relationships should raise error"""
    market_data = BinanceMarketData()
    
    ts = datetime.now(timezone.utc)
    
    # high < low
    candle_bad = Candle(ts, Decimal('100'), Decimal('95'), Decimal('105'), Decimal('102'), Decimal('1000'))
    
    with pytest.raises(MarketDataError, match="high < low"):
        market_data.validate_candles([candle_bad])


def test_validate_candles_valid():
    """Valid candles should pass"""
    market_data = BinanceMarketData()
    
    ts1 = datetime(2024, 1, 1, 11, 0, tzinfo=timezone.utc)
    ts2 = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    
    candle1 = Candle(ts1, Decimal('100'), Decimal('105'), Decimal('95'), Decimal('102'), Decimal('1000'))
    candle2 = Candle(ts2, Decimal('102'), Decimal('110'), Decimal('100'), Decimal('108'), Decimal('1200'))
    
    # Should not raise
    market_data.validate_candles([candle1, candle2])


def test_check_data_freshness_stale():
    """Stale data should raise error"""
    market_data = BinanceMarketData()
    
    ts_old = datetime(2024, 1, 1, 11, 0, tzinfo=timezone.utc)
    candle = Candle(ts_old, Decimal('100'), Decimal('105'), Decimal('95'), Decimal('102'), Decimal('1000'))
    
    with pytest.raises(MarketDataError, match="stale"):
        market_data.check_data_freshness([candle], max_age_seconds=30)


def test_check_data_freshness_fresh():
    """Fresh data should pass"""
    market_data = BinanceMarketData()
    
    ts_now = datetime.now(timezone.utc)
    candle = Candle(ts_now, Decimal('100'), Decimal('105'), Decimal('95'), Decimal('102'), Decimal('1000'))
    
    # Should not raise
    market_data.check_data_freshness([candle], max_age_seconds=30)


def test_normalize_candles():
    """Normalize should deduplicate and sort"""
    ts1 = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    ts2 = datetime(2024, 1, 1, 11, 0, tzinfo=timezone.utc)
    ts3 = datetime(2024, 1, 1, 13, 0, tzinfo=timezone.utc)
    
    candle1 = Candle(ts1, Decimal('100'), Decimal('105'), Decimal('95'), Decimal('102'), Decimal('1000'))
    candle2 = Candle(ts2, Decimal('90'), Decimal('95'), Decimal('85'), Decimal('92'), Decimal('900'))
    candle3 = Candle(ts3, Decimal('102'), Decimal('110'), Decimal('100'), Decimal('108'), Decimal('1200'))
    candle_dup = Candle(ts1, Decimal('100'), Decimal('105'), Decimal('95'), Decimal('102'), Decimal('1000'))
    
    normalized = normalize_candles([candle1, candle2, candle3, candle_dup])
    
    assert len(normalized) == 3
    assert normalized[0].timestamp == ts2
    assert normalized[1].timestamp == ts1
    assert normalized[2].timestamp == ts3
