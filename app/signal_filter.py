"""
Signal filter module: anti false-signal guards.

All filters are deterministic and stateless (or accept state explicitly).
"""
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional, Set

from app.models import Candle, Signal, SignalDirection


class SignalFilterError(Exception):
    """Raised when filter logic fails"""
    pass


def check_duplicate(
    signal_id: str,
    seen_ids: Set[str],
) -> bool:
    """
    Check if a signal with this ID has already been sent.

    Args:
        signal_id: Deterministic signal ID
        seen_ids: Set of previously sent signal IDs

    Returns:
        True if duplicate (should be skipped), False if new.
    """
    return signal_id in seen_ids


def check_cooldown(
    last_signal_time: Optional[datetime],
    current_time: datetime,
    trigger_candle_interval_seconds: int,
    cooldown_candles: int,
) -> bool:
    """
    Cooldown check: no new signals within cooldown_candles of the last signal.

    Args:
        last_signal_time: Timestamp of last sent signal (None if never)
        current_time: Current time (timezone-aware)
        trigger_candle_interval_seconds: Seconds per trigger timeframe candle
        cooldown_candles: Number of candles to cooldown (default 3)

    Returns:
        True if cooldown is elapsed (signal allowed), False if still cooling down.
    """
    if last_signal_time is None:
        return True

    if cooldown_candles <= 0:
        return True

    cooldown_seconds = cooldown_candles * trigger_candle_interval_seconds
    elapsed_seconds = (current_time - last_signal_time).total_seconds()

    return elapsed_seconds >= cooldown_seconds


def check_stale_data(
    latest_candle_time: datetime,
    current_time: datetime,
    max_data_age_seconds: int,
) -> bool:
    """
    Stale data protection.

    Args:
        latest_candle_time: Timestamp of the most recent closed candle
        current_time: Current time (timezone-aware)
        max_data_age_seconds: Maximum allowed data age

    Returns:
        True if data is fresh (proceed), False if stale (reject).
    """
    if latest_candle_time is None:
        return False

    age = (current_time - latest_candle_time).total_seconds()
    return age <= max_data_age_seconds


def check_candle_closed(
    candle: Candle,
    current_time: datetime,
) -> bool:
    """
    Ensure the candle has actually closed before using it.

    A candle is considered closed when its timestamp + interval < current_time.
    For 5M candles: candle timestamp is the OPEN time; close time = open + 5 min.

    Args:
        candle: The candidate trigger candle
        current_time: Current time (timezone-aware)

    Returns:
        True if candle is closed, False if still forming.
    """
    # Heuristic: if candle timestamp + 1 candle interval <= current time, it's closed.
    # For simplicity, check if the next candle's open time would be <= current time.
    # In live context, if a newer candle exists, this candle is closed.
    # For tests, we approximate: a 5M candle at time T is closed at T + 5 min.
    from datetime import timedelta
    # Assume 5-minute interval for trigger timeframe
    close_time = candle.timestamp + timedelta(minutes=5)
    return close_time <= current_time


def check_opposite_signal_protection(
    last_direction: Optional[SignalDirection],
    new_direction: SignalDirection,
) -> bool:
    """
    Opposite signal protection: flag if new signal direction is opposite to
    the most recent signal for the same symbol. Caller decides whether to
    suppress or allow (e.g., allow if cooldown + valid setup).

    Args:
        last_direction: Previous signal direction (None if none)
        new_direction: Proposed new signal direction

    Returns:
        True if opposite direction detected.
    """
    if last_direction is None:
        return False

    return last_direction != new_direction


def generate_signal_id(
    symbol: str,
    timeframe: str,
    candle_close_time: datetime,
    direction: SignalDirection,
) -> str:
    """
    Generate a deterministic signal ID.

    Format: SYMBOL|TIMEFRAME|CANDLE_CLOSE_TS|DIRECTION

    The same combination always produces the same ID.
    """
    return f"{symbol}|{timeframe}|{candle_close_time.isoformat()}|{direction.value}"
