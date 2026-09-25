"""
Data validation and candle synchronization for live market data.

All functions are pure / deterministic — no I/O.

Provides:
- Candle series validation (NaN, negatives, high<low, close range, duplicates,
  ordering, interval alignment, history sufficiency, staleness).
- Closed-candle cutoff (a candle's close time must be <= decision time T;
  the forming candle never enters calculations).
- Multi-timeframe synchronization: for a common decision point T, the latest
  CLOSED candle of each timeframe is deterministically derivable.
- Indicator warmup requirements (EMA200/ADX14/RSI14/ATR14/VolSMA20).
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional, Dict

from app.models import Candle

# ---------------------------------------------------------------------------
# Timeframe metadata
# ---------------------------------------------------------------------------

#: Seconds per candle for each supported timeframe.
TIMEFRAME_SECONDS: Dict[str, int] = {
    "5m": 300,
    "15m": 900,
    "1h": 3600,
}

#: Binance API interval tokens (klines `interval` param).
API_TIMEFRAMES: Dict[str, str] = {
    "5m": "5m",
    "15m": "15m",
    "1h": "1h",
}


def timeframe_seconds(tf: str) -> int:
    """Seconds per candle for a canonical timeframe key.

    Raises:
        ValueError: unknown timeframe.
    """
    if tf not in TIMEFRAME_SECONDS:
        raise ValueError(f"Unsupported timeframe: {tf}")
    return TIMEFRAME_SECONDS[tf]


# ---------------------------------------------------------------------------
# Closed-candle cutoff
# ---------------------------------------------------------------------------

def candle_close_time(candle: Candle, tf: str) -> datetime:
    """Close time of a candle = open timestamp + interval.

    A candle that OPENS at T is CLOSED at T + interval.
    """
    return candle.timestamp + timedelta(seconds=timeframe_seconds(tf))


def cutoff_candles(
    candles: List[Candle],
    tf: str,
    decision_time: datetime,
) -> List[Candle]:
    """
    Deterministic closed-candle cutoff.

    Returns only candles whose close time <= decision_time.
    The currently-forming candle is excluded.
    """
    out: List[Candle] = []
    for c in candles:
        if candle_close_time(c, tf) <= decision_time:
            out.append(c)
    return out


def ensure_closed(
    candles: List[Candle],
    tf: str,
    decision_time: datetime,
) -> List[Candle]:
    """
    Return the closed candle series ending at or before `decision_time`.

    Empty list if no candle has closed by decision_time.
    """
    return cutoff_candles(candles, tf, decision_time)


def latest_closed(candles: List[Candle], tf: str, decision_time: datetime) -> Optional[Candle]:
    """Most recent CLOSED candle at/before decision_time, or None."""
    closed = cutoff_candles(candles, tf, decision_time)
    return closed[-1] if closed else None


# ---------------------------------------------------------------------------
# Multi-timeframe synchronization
# ---------------------------------------------------------------------------

def sync_timeframe(candles: List[Candle], tf: str, decision_time: datetime) -> List[Candle]:
    """
    Align a timeframe's candles to a common decision point T.

    1. Deduplicate + order (defensive).
    2. Cutoff to closed candles only (close_time <= T).
    """
    deduped = normalize_candles(candles)
    return cutoff_candles(deduped, tf, decision_time)


def synchronization_point(candle: Candle, tf: str) -> datetime:
    """
    Deterministic decision timestamp T used to align all timeframes.

    T = close time of the trigger-timeframe candle under evaluation.
    Every higher timeframe keeps only candles with close_time <= T, so 1H /
    15M / 5M all refer to the same observation instant.
    """
    return candle_close_time(candle, tf)


def common_decision_point(
    trigger_candles: List[Candle],
    trigger_tf: str = "5m",
) -> Optional[datetime]:
    """
    Decision point T for a full scan cycle.

    T = close time of the most recent CLOSED trigger candle. All timeframes
    are cutoff to T. If the trigger series has no closed candle, returns None.
    """
    latest = latest_closed(trigger_candles, trigger_tf, datetime.max.replace(tzinfo=timezone.utc))
    if latest is None:
        return None
    return synchronization_point(latest, trigger_tf)


def sync_all(
    hf_candles: List[Candle],
    setup_candles: List[Candle],
    trigger_candles: List[Candle],
    decision_time: Optional[datetime] = None,
) -> Dict[str, List[Candle]]:
    """
    Synchronize 1H/15M/5M to one decision point.

    If decision_time is None it is derived as the close time of the most
    recent closed trigger candle (common_decision_point).
    """
    if decision_time is None:
        decision_time = common_decision_point(trigger_candles, "5m")
        if decision_time is None:
            return {"hf": [], "setup": [], "trigger": []}

    return {
        "hf": sync_timeframe(hf_candles, "1h", decision_time),
        "setup": sync_timeframe(setup_candles, "15m", decision_time),
        "trigger": sync_timeframe(trigger_candles, "5m", decision_time),
    }


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def normalize_candles(candles: List[Candle]) -> List[Candle]:
    """
    Remove duplicate timestamps (keep first) and sort ascending.

    Returns:
        Deduplicated, sorted candle list.
    """
    seen: set = set()
    unique: List[Candle] = []
    for c in candles:
        if c.timestamp in seen:
            continue
        seen.add(c.timestamp)
        unique.append(c)
    unique.sort(key=lambda c: c.timestamp)
    return unique


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class DataValidationError(Exception):
    """Raised when a candle series fails validation."""
    pass


def _is_invalid_num(value: Optional[Decimal]) -> bool:
    """NaN / infinite / None."""
    if value is None:
        return True
    return value.is_nan() or value.is_infinite()


def validate_series(
    candles: List[Candle],
    tf: str,
    min_history: int,
    now: Optional[datetime] = None,
    max_age_seconds: Optional[int] = None,
) -> None:
    """
    Validate a candle series before signal calculation.

    Checks (in order):
    1. non-empty
    2. every timestamp timezone-aware
    3. no duplicate timestamps
    4. strictly ascending (unordered rejected)
    5. interval alignment: consecutive closes spaced exactly `tf`
    6. open/high/low/close all valid numbers (no NaN/inf)
    7. no negative prices
    8. volume non-negative and valid
    9. high >= low
    10. close and open within [low, high]
    11. sufficient history (>= min_history)
    12. freshness: latest candle's close time within max_age_seconds of now

    Raises:
        DataValidationError with a short reason code.
    """
    if not candles:
        raise DataValidationError("EMPTY_SERIES")

    n = len(candles)

    # 2. timezone-aware timestamps
    for i, c in enumerate(candles):
        if c.timestamp.tzinfo is None:
            raise DataValidationError(f"NAIVE_TIMESTAMP@{i}")

    # 3. duplicates
    timestamps = [c.timestamp for c in candles]
    if len(timestamps) != len(set(timestamps)):
        raise DataValidationError("DUPLICATE_TIMESTAMP")

    # 4. ordering
    for i in range(1, n):
        if candles[i].timestamp <= candles[i - 1].timestamp:
            raise DataValidationError(f"UNORDERED@{i}")

    tf_sec = timeframe_seconds(tf)

    # 5. interval alignment
    for i in range(1, n):
        delta = (candles[i].timestamp - candles[i - 1].timestamp).total_seconds()
        if abs(delta - tf_sec) > 1e-6:
            raise DataValidationError(f"INTERVAL_GAP@{i}")

    # 6-10. per-candle numeric + relational
    for i, c in enumerate(candles):
        if _is_invalid_num(c.open) or _is_invalid_num(c.high) or _is_invalid_num(c.low) or _is_invalid_num(c.close):
            raise DataValidationError(f"INVALID_OHLC@{i}")
        if c.open < 0 or c.high < 0 or c.low < 0 or c.close < 0:
            raise DataValidationError(f"NEGATIVE_PRICE@{i}")
        if _is_invalid_num(c.volume) or c.volume < 0:
            raise DataValidationError(f"INVALID_VOLUME@{i}")
        if c.high < c.low:
            raise DataValidationError(f"HIGH_BELOW_LOW@{i}")
        if c.close > c.high or c.close < c.low:
            raise DataValidationError(f"CLOSE_OUT_OF_RANGE@{i}")
        if c.open > c.high or c.open < c.low:
            raise DataValidationError(f"OPEN_OUT_OF_RANGE@{i}")

    # 11. sufficient history
    if n < min_history:
        raise DataValidationError(f"INSUFFICIENT_HISTORY({n}<{min_history})")

    # 12. freshness (candle close time, not API response time)
    #
    # A CLOSED candle is "current" when the reference instant `now` has not
    # advanced more than 1.5 timeframes past its close time. Rationale:
    #   - The last CLOSED 1H candle may be up to a full hour old when the
    #     current hour is still forming. That is normal, not stale.
    #   - If `now` is >= 1.5 timeframes past the last close, a newer closed
    #     bar is expected but missing -> the series has lagged / is stale.
    #   - For 5M trigger: a 30-minute lag (6x the 5m window) is clearly stale.
    #
    # For a FORMING candle (close_time > now), age is negative and never stale
    # (the cutoff step already removed forming candles before this check).
    if now is not None and max_age_seconds is not None:
        latest_close = candle_close_time(candles[-1], tf)
        age = (now - latest_close).total_seconds()
        # Stale when the reference instant is >= 1.5x the timeframe past the
        # most recent close: the feed should have a newer closed bar by then.
        if age > 1.5 * tf_sec:
            raise DataValidationError(f"STALE_DATA(age={age:.0f}s)")


# ---------------------------------------------------------------------------
# Indicator warmup / minimum history
# ---------------------------------------------------------------------------

#: Candles required so the FINAL indicator value is reliable after warmup.
#:
#: - EMA200: seeded EMA needs a long warmup; use ~3x period for stable values.
#: - ADX14: double-smoothed (DM + DX), needs ~2x period.
#: - RSI14 / ATR14 / VolSMA20: ~period + small warmup.
#:
#: A single minimum history constant is used per timeframe to satisfy ALL
#: indicators at once.
def min_history_for_indicators(
    ema_slow_period: int = 200,
    adx_period: int = 14,
    rsi_period: int = 14,
    atr_period: int = 14,
    volume_sma_period: int = 20,
    warmup_factor: int = 3,
) -> int:
    """
    Minimum candle count so EMA_slow (the dominant warmup) is stable.

    EMA is seeded with the first value, so the earliest `period` outputs are
    unreliable. To ensure the LAST value is meaningful, require
    `period * warmup_factor` candles. Other indicators (ADX/RSI/ATR/VolSMA)
    need far less, so EMA_slow dominates.
    """
    ema_need = ema_slow_period * warmup_factor          # 200 * 3 = 600
    adx_need = adx_period * 2                           # 28
    rsi_need = rsi_period + 1                           # 15
    atr_need = atr_period + 1                           # 15
    vol_need = volume_sma_period                         # 20
    return max(ema_need, adx_need, rsi_need, atr_need, vol_need)
