"""
Strategy module: evaluate HTF bias, setup conditions, and trigger confirmation
for LONG/SHORT setups.

Multi-timeframe logic:
- 1H: market bias (BULLISH / BEARISH / NEUTRAL)
- 15M: setup (EMA alignment, ADX, DI direction, RSI momentum)
- 5M: trigger confirmation

All functions are pure / deterministic — no I/O, no randomness.
"""
from decimal import Decimal
from typing import List, Optional, Tuple

from app.models import Candle, MarketBias, SignalDirection, SignalType, IndicatorValues


class StrategyError(Exception):
    """Raised when strategy logic cannot produce a valid result"""
    pass


def determine_hf_bias(
    ind: IndicatorValues,
    close: Decimal,
) -> MarketBias:
    """
    Determine 1H market bias.

    BULLISH: ema_fast > ema_slow AND close > ema_fast
    BEARISH: ema_fast < ema_slow AND close < ema_fast
    NEUTRAL: anything else (including None components)

    Args:
        ind: 1H indicator values
        close: 1H candle close

    Returns:
        MarketBias
    """
    if (ind.ema_fast is None or ind.ema_slow is None or close is None):
        return MarketBias.NEUTRAL

    if ind.ema_fast > ind.ema_slow and close > ind.ema_fast:
        return MarketBias.BULLISH

    if ind.ema_fast < ind.ema_slow and close < ind.ema_fast:
        return MarketBias.BEARISH

    return MarketBias.NEUTRAL


def check_long_setup(
    hf_ind: IndicatorValues,
    hf_close: Decimal,
    setup_ind: IndicatorValues,
    prev_rsi: Optional[Decimal],
    adx_min: Decimal,
    rsi_midline: Decimal,
) -> Tuple[bool, str]:
    """
    Check LONG setup on 15M given 1H bias is already BULLISH.

    Conditions:
    - 15M ema_fast > ema_slow
    - 15M ADX >= adx_min
    - 15M +DI > -DI
    - 15M RSI recovery through rsi_midline (prev <= mid, current > mid)

    Args:
        hf_ind: 1H indicator values
        hf_close: 1H close
        setup_ind: 15M indicator values (current candle)
        prev_rsi: Previous 15M RSI value
        adx_min: ADX threshold
        rsi_midline: RSI midline (default 50)

    Returns:
        (is_valid, reason) tuple. Reason is non-empty when valid; empty when invalid.
    """
    if setup_ind.ema_fast is None or setup_ind.ema_slow is None:
        return False, ""

    if setup_ind.ema_fast <= setup_ind.ema_slow:
        return False, ""

    if setup_ind.adx is None or setup_ind.adx < adx_min:
        return False, ""

    if setup_ind.plus_di is None or setup_ind.minus_di is None:
        return False, ""

    if setup_ind.plus_di <= setup_ind.minus_di:
        return False, ""

    if setup_ind.rsi is None:
        return False, ""

    # RSI momentum confirmation
    if not check_rsi_recovery(prev_rsi, setup_ind.rsi, rsi_midline):
        return False, ""

    return True, "LONG setup valid on 15M"


def check_short_setup(
    hf_ind: IndicatorValues,
    hf_close: Decimal,
    setup_ind: IndicatorValues,
    prev_rsi: Optional[Decimal],
    adx_min: Decimal,
    rsi_midline: Decimal,
) -> Tuple[bool, str]:
    """
    Check SHORT setup on 15M given 1H bias is already BEARISH.

    Conditions:
    - 15M ema_fast < ema_slow
    - 15M ADX >= adx_min
    - 15M -DI > +DI
    - 15M RSI breakdown through rsi_midline (prev >= mid, current < mid)

    Args:
        hf_ind: 1H indicator values
        hf_close: 1H close
        setup_ind: 15M indicator values (current candle)
        prev_rsi: Previous 15M RSI value
        adx_min: ADX threshold
        rsi_midline: RSI midline (default 50)

    Returns:
        (is_valid, reason) tuple
    """
    if setup_ind.ema_fast is None or setup_ind.ema_slow is None:
        return False, ""

    if setup_ind.ema_fast >= setup_ind.ema_slow:
        return False, ""

    if setup_ind.adx is None or setup_ind.adx < adx_min:
        return False, ""

    if setup_ind.plus_di is None or setup_ind.minus_di is None:
        return False, ""

    if setup_ind.minus_di <= setup_ind.plus_di:
        return False, ""

    if setup_ind.rsi is None:
        return False, ""

    # RSI momentum confirmation
    if not check_rsi_breakdown(prev_rsi, setup_ind.rsi, rsi_midline):
        return False, ""

    return True, "SHORT setup valid on 15M"


def check_rsi_recovery(
    prev_rsi: Optional[Decimal],
    curr_rsi: Optional[Decimal],
    rsi_midline: Decimal,
) -> bool:
    """
    RSI recovery: previous <= midline, current > midline.

    Used for LONG momentum confirmation.
    """
    if prev_rsi is None or curr_rsi is None:
        return False

    return prev_rsi <= rsi_midline and curr_rsi > rsi_midline


def check_rsi_breakdown(
    prev_rsi: Optional[Decimal],
    curr_rsi: Optional[Decimal],
    rsi_midline: Decimal,
) -> bool:
    """
    RSI breakdown: previous >= midline, current < midline.

    Used for SHORT momentum confirmation.
    """
    if prev_rsi is None or curr_rsi is None:
        return False

    return prev_rsi >= rsi_midline and curr_rsi < rsi_midline


def check_long_trigger(
    trigger_candles: List[Candle],
) -> bool:
    """
    5M LONG trigger confirmation.

    Last closed candle must be bullish AND close > previous high.

    Args:
        trigger_candles: List of 5M candles, last one is the trigger candle.
                        At least 2 candles required.
    """
    if len(trigger_candles) < 2:
        return False

    prev = trigger_candles[-2]
    curr = trigger_candles[-1]

    is_bullish = curr.close > curr.open
    breakout = curr.close > prev.high

    return is_bullish and breakout


def check_short_trigger(
    trigger_candles: List[Candle],
) -> bool:
    """
    5M SHORT trigger confirmation.

    Last closed candle must be bearish AND close < previous low.
    """
    if len(trigger_candles) < 2:
        return False

    prev = trigger_candles[-2]
    curr = trigger_candles[-1]

    is_bearish = curr.close < curr.open
    breakdown = curr.close < prev.low

    return is_bearish and breakdown


def classify_signal_type(
    direction: SignalDirection,
    is_breakout: bool,
) -> SignalType:
    """
    Classify the signal type. V1 prioritizes TREND_PULLBACK.

    - If trigger is breakout/breakdown (close through prev high/low),
      classify as TREND_BREAKOUT.
    - Otherwise, classify as TREND_PULLBACK (default for V1).
    """
    if is_breakout:
        return SignalType.TREND_BREAKOUT
    return SignalType.TREND_PULLBACK


def check_overextension(
    price: Decimal,
    ema_fast: Decimal,
    atr_val: Decimal,
    max_distance_atr: Decimal,
) -> bool:
    """
    Overextension filter.

    Returns True if price is WITHIN allowed distance (i.e., no overextension).
    Returns False if price is too far from EMA (signal should be rejected).

    Args:
        price: current close
        ema_fast: EMA 50
        atr_val: ATR 14
        max_distance_atr: MAX_DISTANCE_FROM_EMA_ATR from config (default 2.0)
    """
    if atr_val is None or atr_val <= 0:
        return True  # No ATR data, don't reject

    distance = abs(price - ema_fast)
    return distance <= atr_val * max_distance_atr
