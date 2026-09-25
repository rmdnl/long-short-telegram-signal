"""
Risk engine: calculate entry, stop loss, take profit levels.

All calculations based on ATR and market structure.
Deterministic and reproducible.
"""
from decimal import Decimal
from typing import Tuple, List

from app.models import Candle, SignalDirection


class RiskEngineError(Exception):
    """Raised when risk calculation fails"""
    pass


def calculate_entry_zone(
    trigger_candle: Candle,
    atr_val: Decimal,
    direction: SignalDirection,
) -> Tuple[Decimal, Decimal]:
    """
    Calculate entry zone (entry_low, entry_high) around trigger candle.

    For LONG:
        entry_low = trigger close - 0.3 * ATR
        entry_high = trigger close + 0.1 * ATR

    For SHORT:
        entry_low = trigger close - 0.1 * ATR
        entry_high = trigger close + 0.3 * ATR

    Args:
        trigger_candle: 5M trigger candle
        atr_val: ATR 14 value
        direction: LONG or SHORT

    Returns:
        (entry_low, entry_high)
    """
    if atr_val is None or atr_val <= 0:
        raise RiskEngineError("ATR required for entry zone calculation")

    close = trigger_candle.close

    if direction == SignalDirection.LONG:
        entry_low = close - (atr_val * Decimal('0.3'))
        entry_high = close + (atr_val * Decimal('0.1'))
    else:
        entry_low = close - (atr_val * Decimal('0.1'))
        entry_high = close + (atr_val * Decimal('0.3'))

    return entry_low, entry_high


def calculate_stop_loss(
    direction: SignalDirection,
    entry_mid: Decimal,
    atr_val: Decimal,
    sl_atr_multiplier: Decimal,
    swing_level: Decimal = None,
) -> Decimal:
    """
    Calculate stop loss.

    Priority:
    1. Use swing level (if provided and valid)
    2. Fallback: ATR-based stop

    For LONG:
        SL = entry_mid - (ATR × sl_atr_multiplier)

    For SHORT:
        SL = entry_mid + (ATR × sl_atr_multiplier)

    Args:
        direction: LONG or SHORT
        entry_mid: Mid-point of entry zone
        atr_val: ATR 14
        sl_atr_multiplier: SL_ATR_MULTIPLIER from config (default 1.5)
        swing_level: Optional swing low (LONG) or swing high (SHORT)

    Returns:
        Stop loss price
    """
    if atr_val is None or atr_val <= 0:
        raise RiskEngineError("ATR required for stop loss calculation")

    if swing_level is not None:
        # Use swing structure if provided
        if direction == SignalDirection.LONG:
            return swing_level
        else:
            return swing_level

    # Fallback: ATR-based SL
    if direction == SignalDirection.LONG:
        return entry_mid - (atr_val * sl_atr_multiplier)
    else:
        return entry_mid + (atr_val * sl_atr_multiplier)


def calculate_take_profit(
    direction: SignalDirection,
    entry_mid: Decimal,
    stop_loss: Decimal,
    tp1_rr: Decimal,
    tp2_rr: Decimal,
) -> Tuple[Decimal, Decimal]:
    """
    Calculate TP1 and TP2 based on risk/reward ratios.

    Risk = abs(entry_mid - stop_loss)
    Reward_TP1 = Risk × tp1_rr
    Reward_TP2 = Risk × tp2_rr

    For LONG:
        TP1 = entry_mid + Reward_TP1
        TP2 = entry_mid + Reward_TP2

    For SHORT:
        TP1 = entry_mid - Reward_TP1
        TP2 = entry_mid - Reward_TP2

    Args:
        direction: LONG or SHORT
        entry_mid: Mid-point of entry zone
        stop_loss: Stop loss price
        tp1_rr: TP1 R:R ratio (default 1.5)
        tp2_rr: TP2 R:R ratio (default 2.5)

    Returns:
        (tp1, tp2)
    """
    risk = abs(entry_mid - stop_loss)

    if risk == 0:
        raise RiskEngineError("Risk is zero; cannot calculate TP")

    reward_tp1 = risk * tp1_rr
    reward_tp2 = risk * tp2_rr

    if direction == SignalDirection.LONG:
        tp1 = entry_mid + reward_tp1
        tp2 = entry_mid + reward_tp2
    else:
        tp1 = entry_mid - reward_tp1
        tp2 = entry_mid - reward_tp2

    return tp1, tp2


def find_swing_low(candles: List[Candle], lookback: int = 10) -> Decimal:
    """
    Find recent swing low for LONG stop loss.

    Args:
        candles: Recent 15M candles
        lookback: Number of candles to look back

    Returns:
        Lowest low in lookback period
    """
    if not candles:
        raise RiskEngineError("No candles to find swing low")

    recent = candles[-lookback:] if len(candles) >= lookback else candles
    return min(c.low for c in recent)


def find_swing_high(candles: List[Candle], lookback: int = 10) -> Decimal:
    """
    Find recent swing high for SHORT stop loss.

    Args:
        candles: Recent 15M candles
        lookback: Number of candles to look back

    Returns:
        Highest high in lookback period
    """
    if not candles:
        raise RiskEngineError("No candles to find swing high")

    recent = candles[-lookback:] if len(candles) >= lookback else candles
    return max(c.high for c in recent)
