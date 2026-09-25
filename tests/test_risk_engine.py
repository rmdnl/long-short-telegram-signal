"""
Test risk_engine.py module
"""
import pytest
from decimal import Decimal
from datetime import datetime, timezone

from app.risk_engine import (
    calculate_entry_zone,
    calculate_stop_loss,
    calculate_take_profit,
    find_swing_low,
    find_swing_high,
    RiskEngineError,
)
from app.models import Candle, SignalDirection


def make_candle(
    open_: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    volume: Decimal = None,
    timestamp=None,
) -> Candle:
    return Candle(
        timestamp=timestamp or datetime.now(timezone.utc),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume or Decimal('1000'),
    )


# ============================================================================
# ENTRY ZONE
# ============================================================================

def test_entry_zone_long():
    """LONG: entry_low = close - 0.3*ATR, entry_high = close + 0.1*ATR"""
    trigger = make_candle(
        open_=Decimal('100'),
        high=Decimal('105'),
        low=Decimal('98'),
        close=Decimal('103'),
    )
    atr = Decimal('10')

    entry_low, entry_high = calculate_entry_zone(trigger, atr, SignalDirection.LONG)

    expected_low = Decimal('103') - (Decimal('10') * Decimal('0.3'))
    expected_high = Decimal('103') + (Decimal('10') * Decimal('0.1'))

    assert entry_low == expected_low
    assert entry_high == expected_high
    assert entry_low < entry_high


def test_entry_zone_short():
    """SHORT: entry_low = close - 0.1*ATR, entry_high = close + 0.3*ATR"""
    trigger = make_candle(
        open_=Decimal('100'),
        high=Decimal('105'),
        low=Decimal('98'),
        close=Decimal('103'),
    )
    atr = Decimal('10')

    entry_low, entry_high = calculate_entry_zone(trigger, atr, SignalDirection.SHORT)

    expected_low = Decimal('103') - (Decimal('10') * Decimal('0.1'))
    expected_high = Decimal('103') + (Decimal('10') * Decimal('0.3'))

    assert entry_low == expected_low
    assert entry_high == expected_high


def test_entry_zone_zero_atr_raises():
    """Zero ATR raises RiskEngineError"""
    trigger = make_candle(Decimal('100'), Decimal('105'), Decimal('98'), Decimal('103'))

    with pytest.raises(RiskEngineError, match="ATR required"):
        calculate_entry_zone(trigger, Decimal('0'), SignalDirection.LONG)


def test_entry_zone_none_atr_raises():
    """None ATR raises RiskEngineError"""
    trigger = make_candle(Decimal('100'), Decimal('105'), Decimal('98'), Decimal('103'))

    with pytest.raises(RiskEngineError):
        calculate_entry_zone(trigger, None, SignalDirection.LONG)


# ============================================================================
# STOP LOSS
# ============================================================================

def test_stop_loss_long_atr_fallback():
    """LONG: SL = entry_mid - ATR * multiplier"""
    entry_mid = Decimal('103')
    atr = Decimal('10')
    sl_mult = Decimal('1.5')

    sl = calculate_stop_loss(SignalDirection.LONG, entry_mid, atr, sl_mult)
    assert sl == Decimal('103') - (Decimal('10') * Decimal('1.5'))
    assert sl == Decimal('88')


def test_stop_loss_short_atr_fallback():
    """SHORT: SL = entry_mid + ATR * multiplier"""
    entry_mid = Decimal('103')
    atr = Decimal('10')
    sl_mult = Decimal('1.5')

    sl = calculate_stop_loss(SignalDirection.SHORT, entry_mid, atr, sl_mult)
    assert sl == Decimal('103') + (Decimal('10') * Decimal('1.5'))
    assert sl == Decimal('118')


def test_stop_loss_uses_swing_low_for_long():
    """LONG with swing level uses swing level"""
    entry_mid = Decimal('103')
    atr = Decimal('10')
    sl_mult = Decimal('1.5')
    swing_low = Decimal('97')

    sl = calculate_stop_loss(SignalDirection.LONG, entry_mid, atr, sl_mult, swing_level=swing_low)
    assert sl == Decimal('97')


def test_stop_loss_uses_swing_high_for_short():
    """SHORT with swing level uses swing level"""
    entry_mid = Decimal('103')
    atr = Decimal('10')
    sl_mult = Decimal('1.5')
    swing_high = Decimal('110')

    sl = calculate_stop_loss(SignalDirection.SHORT, entry_mid, atr, sl_mult, swing_level=swing_high)
    assert sl == Decimal('110')


def test_stop_loss_zero_atr_raises():
    """Zero ATR raises RiskEngineError"""
    with pytest.raises(RiskEngineError):
        calculate_stop_loss(SignalDirection.LONG, Decimal('103'), Decimal('0'), Decimal('1.5'))


# ============================================================================
# TAKE PROFIT
# ============================================================================

def test_take_profit_long():
    """LONG: TP1/TP2 above entry based on R:R"""
    entry_mid = Decimal('100')
    stop_loss = Decimal('90')  # risk = 10
    tp1_rr = Decimal('1.5')
    tp2_rr = Decimal('2.5')

    tp1, tp2 = calculate_take_profit(SignalDirection.LONG, entry_mid, stop_loss, tp1_rr, tp2_rr)

    assert tp1 == Decimal('100') + (Decimal('10') * Decimal('1.5'))
    assert tp1 == Decimal('115')
    assert tp2 == Decimal('100') + (Decimal('10') * Decimal('2.5'))
    assert tp2 == Decimal('125')
    assert tp1 < tp2


def test_take_profit_short():
    """SHORT: TP1/TP2 below entry based on R:R"""
    entry_mid = Decimal('100')
    stop_loss = Decimal('110')  # risk = 10
    tp1_rr = Decimal('1.5')
    tp2_rr = Decimal('2.5')

    tp1, tp2 = calculate_take_profit(SignalDirection.SHORT, entry_mid, stop_loss, tp1_rr, tp2_rr)

    assert tp1 == Decimal('100') - (Decimal('10') * Decimal('1.5'))
    assert tp1 == Decimal('85')
    assert tp2 == Decimal('100') - (Decimal('10') * Decimal('2.5'))
    assert tp2 == Decimal('75')
    assert tp2 < tp1


def test_take_profit_zero_risk_raises():
    """Zero risk (entry == SL) raises RiskEngineError"""
    with pytest.raises(RiskEngineError, match="Risk is zero"):
        calculate_take_profit(SignalDirection.LONG, Decimal('100'), Decimal('100'), Decimal('1.5'), Decimal('2.5'))


# ============================================================================
# SWING LEVELS
# ============================================================================

def test_find_swing_low():
    """Returns lowest low in lookback"""
    candles = [
        make_candle(Decimal('102'), Decimal('105'), Decimal('100'), Decimal('103')),
        make_candle(Decimal('103'), Decimal('104'), Decimal('98'), Decimal('100')),
        make_candle(Decimal('100'), Decimal('103'), Decimal('99'), Decimal('102')),
    ]

    swing_low = find_swing_low(candles, lookback=10)
    assert swing_low == Decimal('98')


def test_find_swing_low_lookback_limit():
    """Uses only lookback candles"""
    candles = [
        make_candle(Decimal('102'), Decimal('105'), Decimal('100'), Decimal('103')),
        make_candle(Decimal('103'), Decimal('104'), Decimal('90'), Decimal('100')),  # 90 = far low
        make_candle(Decimal('100'), Decimal('103'), Decimal('99'), Decimal('102')),
    ]

    # lookback=1 → only last candle
    swing_low = find_swing_low(candles, lookback=1)
    assert swing_low == Decimal('99')


def test_find_swing_low_empty_raises():
    """Empty candles raises RiskEngineError"""
    with pytest.raises(RiskEngineError, match="No candles"):
        find_swing_low([])


def test_find_swing_high():
    """Returns highest high in lookback"""
    candles = [
        make_candle(Decimal('102'), Decimal('105'), Decimal('100'), Decimal('103')),
        make_candle(Decimal('103'), Decimal('108'), Decimal('98'), Decimal('100')),
        make_candle(Decimal('100'), Decimal('103'), Decimal('99'), Decimal('102')),
    ]

    swing_high = find_swing_high(candles, lookback=10)
    assert swing_high == Decimal('108')


def test_find_swing_high_empty_raises():
    """Empty candles raises RiskEngineError"""
    with pytest.raises(RiskEngineError):
        find_swing_high([])
