"""
Test signal_filter.py module
"""
import pytest
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from app.signal_filter import (
    check_duplicate,
    check_cooldown,
    check_stale_data,
    check_candle_closed,
    check_opposite_signal_protection,
    generate_signal_id,
)
from app.models import Candle, SignalDirection


def make_candle(open_: Decimal, high: Decimal, low: Decimal, close: Decimal,
                timestamp, volume=None) -> Candle:
    return Candle(
        timestamp=timestamp,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume or Decimal('1000'),
    )


BASE = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)

# ============================================================================
# DUPLICATE PROTECTION
# ============================================================================

def test_duplicate_detected():
    seen = {"BTCUSDT|5M|2024-01-01T10:00:00+00:00|LONG"}
    assert check_duplicate("BTCUSDT|5M|2024-01-01T10:00:00+00:00|LONG", seen) is True


def test_duplicate_new_signal():
    seen = {"BTCUSDT|5M|2024-01-01T09:00:00+00:00|LONG"}
    assert check_duplicate("BTCUSDT|5M|2024-01-01T10:00:00+00:00|LONG", seen) is False


def test_duplicate_empty_seen():
    assert check_duplicate("ANY|ID", set()) is False


# ============================================================================
# COOLDOWN
# ============================================================================

def test_cooldown_no_previous_signal():
    """No last signal → cooldown elapsed"""
    assert check_cooldown(None, BASE, 300, 3) is True


def test_cooldown_zero_candles():
    """Zero cooldown → always allowed"""
    assert check_cooldown(BASE, BASE, 300, 0) is True


def test_cooldown_not_elapsed():
    """Elapsed < cooldown → rejected"""
    last = BASE
    now = BASE + timedelta(minutes=10)  # 10 min < 15 min (3 x 300s)
    assert check_cooldown(last, now, 300, 3) is False


def test_cooldown_elapsed():
    """Elapsed >= cooldown → allowed"""
    last = BASE
    now = BASE + timedelta(minutes=15)
    assert check_cooldown(last, now, 300, 3) is True


def test_cooldown_exact_boundary():
    """Exact boundary elapsed → allowed"""
    last = BASE
    now = BASE + timedelta(seconds=3 * 300)
    assert check_cooldown(last, now, 300, 3) is True


# ============================================================================
# STALE DATA
# ============================================================================

def test_stale_data_fresh():
    """Data within max age → fresh"""
    latest = BASE
    now = BASE + timedelta(seconds=20)
    assert check_stale_data(latest, now, 30) is True


def test_stale_data_expired():
    """Data older than max age → stale"""
    latest = BASE
    now = BASE + timedelta(seconds=40)
    assert check_stale_data(latest, now, 30) is False


def test_stale_data_exact_boundary():
    """Age == max → still fresh (<=)"""
    latest = BASE
    now = BASE + timedelta(seconds=30)
    assert check_stale_data(latest, now, 30) is True


def test_stale_data_none_rejected():
    """No candle time → rejected"""
    assert check_stale_data(None, BASE, 30) is False


# ============================================================================
# CANDLE CLOSED
# ============================================================================

def test_candle_closed_after_interval():
    """Candle timestamp + 5min <= now → closed"""
    candle = make_candle(Decimal('1'), Decimal('2'), Decimal('0.5'), Decimal('1.5'),
                          timestamp=BASE)
    now = BASE + timedelta(minutes=5)
    assert check_candle_closed(candle, now) is True


def test_candle_not_closed_during_forming():
    """Candle still forming → not closed"""
    candle = make_candle(Decimal('1'), Decimal('2'), Decimal('0.5'), Decimal('1.5'),
                          timestamp=BASE)
    now = BASE + timedelta(minutes=4, seconds=59)
    assert check_candle_closed(candle, now) is False


def test_candle_closed_before_timestamp():
    """now before candle open → not closed"""
    candle = make_candle(Decimal('1'), Decimal('2'), Decimal('0.5'), Decimal('1.5'),
                          timestamp=BASE)
    now = BASE - timedelta(minutes=1)
    assert check_candle_closed(candle, now) is False


# ============================================================================
# OPPOSITE SIGNAL PROTECTION
# ============================================================================

def test_opposite_detected():
    """Different direction → opposite"""
    assert check_opposite_signal_protection(SignalDirection.LONG, SignalDirection.SHORT) is True


def test_same_direction_not_opposite():
    assert check_opposite_signal_protection(SignalDirection.LONG, SignalDirection.LONG) is False


def test_no_previous_not_opposite():
    assert check_opposite_signal_protection(None, SignalDirection.LONG) is False


# ============================================================================
# SIGNAL ID
# ============================================================================

def test_signal_id_deterministic():
    id1 = generate_signal_id("BTCUSDT", "5M", BASE, SignalDirection.LONG)
    id2 = generate_signal_id("BTCUSDT", "5M", BASE, SignalDirection.LONG)
    assert id1 == id2
    assert id1 == "BTCUSDT|5M|2024-01-01T10:00:00+00:00|LONG"


def test_signal_id_direction_sensitive():
    long_id = generate_signal_id("BTCUSDT", "5M", BASE, SignalDirection.LONG)
    short_id = generate_signal_id("BTCUSDT", "5M", BASE, SignalDirection.SHORT)
    assert long_id != short_id


def test_signal_id_time_sensitive():
    other_time = BASE + timedelta(minutes=5)
    id1 = generate_signal_id("BTCUSDT", "5M", BASE, SignalDirection.LONG)
    id2 = generate_signal_id("BTCUSDT", "5M", other_time, SignalDirection.LONG)
    assert id1 != id2


def test_signal_id_symbol_sensitive():
    id1 = generate_signal_id("BTCUSDT", "5M", BASE, SignalDirection.LONG)
    id2 = generate_signal_id("ETHUSDT", "5M", BASE, SignalDirection.LONG)
    assert id1 != id2
