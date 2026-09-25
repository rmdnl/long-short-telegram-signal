"""
Test strategy.py module
"""
import pytest
from decimal import Decimal
from datetime import datetime, timezone
from typing import List

from app.strategy import (
    determine_hf_bias,
    check_long_setup,
    check_short_setup,
    check_rsi_recovery,
    check_rsi_breakdown,
    check_long_trigger,
    check_short_trigger,
    check_overextension,
)
from app.models import Candle, IndicatorValues, MarketBias, SignalDirection


# ============================================================================
# FIXTURES
# ============================================================================

def make_ind(
    ema_fast: Decimal = None,
    ema_slow: Decimal = None,
    rsi: Decimal = None,
    atr: Decimal = None,
    adx: Decimal = None,
    plus_di: Decimal = None,
    minus_di: Decimal = None,
) -> IndicatorValues:
    """Helper to create IndicatorValues with selective fields"""
    return IndicatorValues(
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        rsi=rsi,
        atr=atr,
        adx=adx,
        plus_di=plus_di,
        minus_di=minus_di,
    )


def make_candle(
    open_: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    volume: Decimal = None,
    timestamp=None,
) -> Candle:
    """Helper to create Candle"""
    return Candle(
        timestamp=timestamp or datetime.now(timezone.utc),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume or Decimal('1000'),
    )


# ============================================================================
# A. HTF BIAS
# ============================================================================

def test_hf_bias_bullish():
    """EMA50 > EMA200 + close > EMA50 → BULLISH"""
    ind = make_ind(
        ema_fast=Decimal('105'),
        ema_slow=Decimal('100'),
    )
    close = Decimal('110')  # close > ema_fast

    result = determine_hf_bias(ind, close)
    assert result == MarketBias.BULLISH


def test_hf_bias_bearish():
    """EMA50 < EMA200 + close < EMA50 → BEARISH"""
    ind = make_ind(
        ema_fast=Decimal('95'),
        ema_slow=Decimal('100'),
    )
    close = Decimal('90')  # close < ema_fast

    result = determine_hf_bias(ind, close)
    assert result == MarketBias.BEARISH


def test_hf_bias_neutral_conflicting():
    """Conflicting conditions → NEUTRAL"""
    # Case 1: EMA bullish but price below
    ind = make_ind(
        ema_fast=Decimal('105'),
        ema_slow=Decimal('100'),
    )
    close = Decimal('100')  # close < ema_fast

    result = determine_hf_bias(ind, close)
    assert result == MarketBias.NEUTRAL


def test_hf_bias_neutral_missing_data():
    """Missing EMA values → NEUTRAL"""
    ind = make_ind(
        ema_fast=None,
        ema_slow=Decimal('100'),
    )
    close = Decimal('105')

    result = determine_hf_bias(ind, close)
    assert result == MarketBias.NEUTRAL


def test_hf_bias_neutral_equal_ema():
    """EMA50 = EMA200 → NEUTRAL"""
    ind = make_ind(
        ema_fast=Decimal('100'),
        ema_slow=Decimal('100'),
    )
    close = Decimal('105')

    result = determine_hf_bias(ind, close)
    assert result == MarketBias.NEUTRAL


# ============================================================================
# B. LONG SETUP
# ============================================================================

def test_long_setup_valid_all_conditions():
    """All conditions met → valid LONG setup"""
    hf_ind = make_ind(ema_fast=Decimal('110'), ema_slow=Decimal('100'))
    hf_close = Decimal('115')

    setup_ind = make_ind(
        ema_fast=Decimal('105'),
        ema_slow=Decimal('100'),
        adx=Decimal('25'),
        plus_di=Decimal('28'),
        minus_di=Decimal('18'),
        rsi=Decimal('55'),
    )

    # prev_rsi <= 50, curr_rsi > 50
    is_valid, reason = check_long_setup(
        hf_ind,
        hf_close,
        setup_ind,
        Decimal('48'),  # prev_rsi
        Decimal('22'),  # adx_min
        Decimal('50'),   # rsi_midline
    )

    assert is_valid is True
    assert reason == "LONG setup valid on 15M"


def test_long_setup_invalid_ema_bearish():
    """EMA fast < EMA slow → invalid"""
    hf_ind = make_ind(ema_fast=Decimal('110'), ema_slow=Decimal('100'))
    hf_close = Decimal('115')

    setup_ind = make_ind(
        ema_fast=Decimal('95'),  # ema_fast < ema_slow
        ema_slow=Decimal('100'),
        adx=Decimal('25'),
        plus_di=Decimal('28'),
        minus_di=Decimal('18'),
        rsi=Decimal('55'),
    )

    is_valid, reason = check_long_setup(
        hf_ind,
        hf_close,
        setup_ind,
        Decimal('48'),
        Decimal('22'),
        Decimal('50'),
    )

    assert is_valid is False


def test_long_setup_invalid_adx_weak():
    """ADX < threshold → invalid"""
    hf_ind = make_ind(ema_fast=Decimal('110'), ema_slow=Decimal('100'))
    hf_close = Decimal('115')

    setup_ind = make_ind(
        ema_fast=Decimal('105'),
        ema_slow=Decimal('100'),
        adx=Decimal('15'),  # adx < 22
        plus_di=Decimal('28'),
        minus_di=Decimal('18'),
        rsi=Decimal('55'),
    )

    is_valid, _ = check_long_setup(
        hf_ind,
        hf_close,
        setup_ind,
        Decimal('48'),
        Decimal('22'),
        Decimal('50'),
    )

    assert is_valid is False


def test_long_setup_invalid_di_direction():
    """+DI <= -DI → invalid"""
    hf_ind = make_ind(ema_fast=Decimal('110'), ema_slow=Decimal('100'))
    hf_close = Decimal('115')

    setup_ind = make_ind(
        ema_fast=Decimal('105'),
        ema_slow=Decimal('100'),
        adx=Decimal('25'),
        plus_di=Decimal('18'),  # +DI < -DI
        minus_di=Decimal('28'),
        rsi=Decimal('55'),
    )

    is_valid, _ = check_long_setup(
        hf_ind,
        hf_close,
        setup_ind,
        Decimal('48'),
        Decimal('22'),
        Decimal('50'),
    )

    assert is_valid is False


def test_long_setup_invalid_rsi_not_recovered():
    """RSI not recovered (prev > 50) → invalid"""
    hf_ind = make_ind(ema_fast=Decimal('110'), ema_slow=Decimal('100'))
    hf_close = Decimal('115')

    setup_ind = make_ind(
        ema_fast=Decimal('105'),
        ema_slow=Decimal('100'),
        adx=Decimal('25'),
        plus_di=Decimal('28'),
        minus_di=Decimal('18'),
        rsi=Decimal('55'),
    )

    # prev_rsi > 50 → no recovery
    is_valid, _ = check_long_setup(
        hf_ind,
        hf_close,
        setup_ind,
        Decimal('52'),  # prev_rsi > 50
        Decimal('22'),
        Decimal('50'),
    )

    assert is_valid is False


def test_long_setup_missing_data():
    """Missing indicator values → invalid"""
    hf_ind = make_ind(ema_fast=Decimal('110'), ema_slow=Decimal('100'))
    hf_close = Decimal('115')

    # Missing ema_fast
    setup_ind = make_ind(
        ema_fast=None,
        ema_slow=Decimal('100'),
        adx=Decimal('25'),
        plus_di=Decimal('28'),
        minus_di=Decimal('18'),
        rsi=Decimal('55'),
    )

    is_valid, _ = check_long_setup(
        hf_ind,
        hf_close,
        setup_ind,
        Decimal('48'),
        Decimal('22'),
        Decimal('50'),
    )

    assert is_valid is False


# ============================================================================
# C. SHORT SETUP
# ============================================================================

def test_short_setup_valid_all_conditions():
    """All conditions met → valid SHORT setup"""
    hf_ind = make_ind(ema_fast=Decimal('95'), ema_slow=Decimal('100'))
    hf_close = Decimal('90')

    setup_ind = make_ind(
        ema_fast=Decimal('95'),
        ema_slow=Decimal('100'),
        adx=Decimal('25'),
        plus_di=Decimal('18'),
        minus_di=Decimal('28'),
        rsi=Decimal('45'),
    )

    # prev_rsi >= 50, curr_rsi < 50
    is_valid, reason = check_short_setup(
        hf_ind,
        hf_close,
        setup_ind,
        Decimal('52'),  # prev_rsi
        Decimal('22'),  # adx_min
        Decimal('50'),   # rsi_midline
    )

    assert is_valid is True
    assert reason == "SHORT setup valid on 15M"


def test_short_setup_invalid_ema_bullish():
    """EMA fast > EMA slow → invalid for SHORT"""
    hf_ind = make_ind(ema_fast=Decimal('95'), ema_slow=Decimal('100'))
    hf_close = Decimal('90')

    setup_ind = make_ind(
        ema_fast=Decimal('105'),  # ema_fast > ema_slow
        ema_slow=Decimal('100'),
        adx=Decimal('25'),
        plus_di=Decimal('18'),
        minus_di=Decimal('28'),
        rsi=Decimal('45'),
    )

    is_valid, _ = check_short_setup(
        hf_ind,
        hf_close,
        setup_ind,
        Decimal('52'),
        Decimal('22'),
        Decimal('50'),
    )

    assert is_valid is False


def test_short_setup_invalid_rsi_not_broken_down():
    """RSI not broken down (prev < 50) → invalid"""
    hf_ind = make_ind(ema_fast=Decimal('95'), ema_slow=Decimal('100'))
    hf_close = Decimal('90')

    setup_ind = make_ind(
        ema_fast=Decimal('95'),
        ema_slow=Decimal('100'),
        adx=Decimal('25'),
        plus_di=Decimal('18'),
        minus_di=Decimal('28'),
        rsi=Decimal('45'),
    )

    # prev_rsi < 50 → no breakdown
    is_valid, _ = check_short_setup(
        hf_ind,
        hf_close,
        setup_ind,
        Decimal('48'),  # prev_rsi < 50
        Decimal('22'),
        Decimal('50'),
    )

    assert is_valid is False


# ============================================================================
# D. RSI MOMENTUM
# ============================================================================

def test_rsi_recovery_valid():
    """prev <= 50, curr > 50 → valid recovery"""
    assert check_rsi_recovery(Decimal('49'), Decimal('51'), Decimal('50')) is True
    assert check_rsi_recovery(Decimal('50'), Decimal('51'), Decimal('50')) is True


def test_rsi_recovery_invalid():
    """prev > 50 → no recovery"""
    assert check_rsi_recovery(Decimal('51'), Decimal('55'), Decimal('50')) is False


def test_rsi_recovery_boundary_4999_5001():
    """49.99 → 50.01 should be valid"""
    assert check_rsi_recovery(Decimal('49.99'), Decimal('50.01'), Decimal('50')) is True


def test_rsi_recovery_boundary_5000_5001():
    """50.00 → 50.01 should be valid"""
    assert check_rsi_recovery(Decimal('50.00'), Decimal('50.01'), Decimal('50')) is True


def test_rsi_recovery_boundary_5001_5000():
    """50.01 → 50.00 should be invalid (curr not > midline)"""
    assert check_rsi_recovery(Decimal('50.01'), Decimal('50.00'), Decimal('50')) is False


def test_rsi_breakdown_valid():
    """prev >= 50, curr < 50 → valid breakdown"""
    assert check_rsi_breakdown(Decimal('51'), Decimal('49'), Decimal('50')) is True
    assert check_rsi_breakdown(Decimal('50'), Decimal('49'), Decimal('50')) is True


def test_rsi_breakdown_invalid():
    """prev < 50 → no breakdown"""
    assert check_rsi_breakdown(Decimal('49'), Decimal('45'), Decimal('50')) is False


def test_rsi_breakdown_boundary_5000_4999():
    """50.00 → 49.99 should be valid"""
    assert check_rsi_breakdown(Decimal('50.00'), Decimal('49.99'), Decimal('50')) is True


def test_rsi_missing_data():
    """Missing RSI values → invalid"""
    assert check_rsi_recovery(None, Decimal('55'), Decimal('50')) is False
    assert check_rsi_recovery(Decimal('45'), None, Decimal('50')) is False
    assert check_rsi_breakdown(None, Decimal('45'), Decimal('50')) is False


# ============================================================================
# E. TRIGGER
# ============================================================================

def test_long_trigger_valid():
    """Closed bullish candle + close > prev high → valid"""
    from datetime import datetime, timezone

    ts1 = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
    ts2 = datetime(2024, 1, 1, 10, 5, tzinfo=timezone.utc)

    prev_candle = make_candle(
        open_=Decimal('100'),
        high=Decimal('105'),
        low=Decimal('98'),
        close=Decimal('102'),
        timestamp=ts1,
    )

    curr_candle = make_candle(
        open_=Decimal('103'),
        high=Decimal('108'),
        low=Decimal('102'),
        close=Decimal('106'),  # close > prev high (105)
        timestamp=ts2,
    )

    assert check_long_trigger([prev_candle, curr_candle]) is True


def test_long_trigger_invalid_not_bullish():
    """Bearish candle → invalid"""
    from datetime import datetime, timezone

    ts1 = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
    ts2 = datetime(2024, 1, 1, 10, 5, tzinfo=timezone.utc)

    prev_candle = make_candle(
        open_=Decimal('100'),
        high=Decimal('105'),
        low=Decimal('98'),
        close=Decimal('102'),
        timestamp=ts1,
    )

    curr_candle = make_candle(
        open_=Decimal('103'),
        high=Decimal('108'),
        low=Decimal('101'),
        close=Decimal('102'),  # close < open → bearish
        timestamp=ts2,
    )

    assert check_long_trigger([prev_candle, curr_candle]) is False


def test_long_trigger_invalid_no_breakout():
    """Close not above prev high → invalid"""
    from datetime import datetime, timezone

    ts1 = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
    ts2 = datetime(2024, 1, 1, 10, 5, tzinfo=timezone.utc)

    prev_candle = make_candle(
        open_=Decimal('100'),
        high=Decimal('108'),  # high = 108
        low=Decimal('98'),
        close=Decimal('102'),
        timestamp=ts1,
    )

    curr_candle = make_candle(
        open_=Decimal('103'),
        high=Decimal('108'),
        low=Decimal('102'),
        close=Decimal('107'),  # close < prev high
        timestamp=ts2,
    )

    assert check_long_trigger([prev_candle, curr_candle]) is False


def test_long_trigger_equal_high():
    """Close = prev high → invalid (must be >)"""
    from datetime import datetime, timezone

    ts1 = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
    ts2 = datetime(2024, 1, 1, 10, 5, tzinfo=timezone.utc)

    prev_candle = make_candle(
        open_=Decimal('100'),
        high=Decimal('105'),
        low=Decimal('98'),
        close=Decimal('102'),
        timestamp=ts1,
    )

    curr_candle = make_candle(
        open_=Decimal('103'),
        high=Decimal('108'),
        low=Decimal('102'),
        close=Decimal('105'),  # close = prev high
        timestamp=ts2,
    )

    assert check_long_trigger([prev_candle, curr_candle]) is False


def test_long_trigger_insufficient_candles():
    """Less than 2 candles → invalid"""
    from datetime import datetime, timezone

    ts1 = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)

    single_candle = make_candle(
        open_=Decimal('100'),
        high=Decimal('105'),
        low=Decimal('98'),
        close=Decimal('102'),
        timestamp=ts1,
    )

    assert check_long_trigger([single_candle]) is False
    assert check_long_trigger([]) is False


def test_short_trigger_valid():
    """Closed bearish candle + close < prev low → valid"""
    from datetime import datetime, timezone

    ts1 = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
    ts2 = datetime(2024, 1, 1, 10, 5, tzinfo=timezone.utc)

    prev_candle = make_candle(
        open_=Decimal('105'),
        high=Decimal('108'),
        low=Decimal('102'),
        close=Decimal('103'),
        timestamp=ts1,
    )

    curr_candle = make_candle(
        open_=Decimal('102'),
        high=Decimal('104'),
        low=Decimal('98'),
        close=Decimal('101'),  # close < prev low (102)
        timestamp=ts2,
    )

    assert check_short_trigger([prev_candle, curr_candle]) is True


def test_short_trigger_invalid_not_bearish():
    """Bullish candle → invalid for SHORT"""
    from datetime import datetime, timezone

    ts1 = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
    ts2 = datetime(2024, 1, 1, 10, 5, tzinfo=timezone.utc)

    prev_candle = make_candle(
        open_=Decimal('105'),
        high=Decimal('108'),
        low=Decimal('102'),
        close=Decimal('103'),
        timestamp=ts1,
    )

    curr_candle = make_candle(
        open_=Decimal('102'),
        high=Decimal('106'),
        low=Decimal('101'),
        close=Decimal('105'),  # close > open → bullish
        timestamp=ts2,
    )

    assert check_short_trigger([prev_candle, curr_candle]) is False


def test_short_trigger_equal_low():
    """Close = prev low → invalid (must be <)"""
    from datetime import datetime, timezone

    ts1 = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
    ts2 = datetime(2024, 1, 1, 10, 5, tzinfo=timezone.utc)

    prev_candle = make_candle(
        open_=Decimal('105'),
        high=Decimal('108'),
        low=Decimal('102'),
        close=Decimal('103'),
        timestamp=ts1,
    )

    curr_candle = make_candle(
        open_=Decimal('102'),
        high=Decimal('104'),
        low=Decimal('98'),
        close=Decimal('102'),  # close = prev low
        timestamp=ts2,
    )

    assert check_short_trigger([prev_candle, curr_candle]) is False


# ============================================================================
# F. OVEREXTENSION
# ============================================================================

def test_overextension_within_limit():
    """Price within ATR distance → valid"""
    price = Decimal('105')
    ema = Decimal('100')
    atr = Decimal('5')
    max_distance = Decimal('2.0')

    # distance = 5, limit = 5 * 2.0 = 10
    assert check_overextension(price, ema, atr, max_distance) is True


def test_overextension_beyond_limit():
    """Price beyond ATR distance → invalid"""
    price = Decimal('120')
    ema = Decimal('100')
    atr = Decimal('5')
    max_distance = Decimal('2.0')

    # distance = 20, limit = 5 * 2.0 = 10
    assert check_overextension(price, ema, atr, max_distance) is False


def test_overextension_boundary_exact():
    """Price exactly at limit → valid"""
    price = Decimal('110')
    ema = Decimal('100')
    atr = Decimal('5')
    max_distance = Decimal('2.0')

    # distance = 10, limit = 5 * 2.0 = 10
    assert check_overextension(price, ema, atr, max_distance) is True


def test_overextension_zero_atr():
    """Zero ATR → cannot calculate, return True (don't reject)"""
    price = Decimal('105')
    ema = Decimal('100')
    atr = Decimal('0')

    assert check_overextension(price, ema, atr, Decimal('2.0')) is True


def test_overextension_none_atr():
    """None ATR → cannot calculate, return True"""
    price = Decimal('105')
    ema = Decimal('100')
    atr = None

    assert check_overextension(price, ema, atr, Decimal('2.0')) is True
