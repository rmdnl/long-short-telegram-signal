"""
PHASE 3: live-data validation tests.

Covers, WITHOUT any live Binance dependency (only the HTTP boundary is mocked
where needed):
- closed-candle cutoff
- unfinished-candle rejection
- stale candle
- malformed candle (NaN, infinite, negative, high<low, close out of range)
- duplicate / unordered candles
- insufficient history
- multi-timeframe synchronization
- freshness based on CANDLE CLOSE TIME (not API response time)

All tests are deterministic and run against synthetic data.
"""
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta, timezone

import pytest

from app.models import Candle
from app import data_validation as dv
from app.data_validation import DataValidationError

UTC = timezone.utc

BASE = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)


def make_candles(tf: str, n: int, start: datetime = BASE,
                 price: Decimal = None) -> list:
    """Generate n valid, closed, ascending candles for a timeframe."""
    sec = dv.TIMEFRAME_SECONDS[tf]
    price = price or Decimal("100")
    out = []
    for i in range(n):
        ts = start + timedelta(seconds=sec * i)
        o = price
        c = price + Decimal("0.1")
        out.append(Candle(ts, o, c + Decimal("0.05"), o - Decimal("0.05"), c, Decimal("1000")))
        price = c
    return out


# ---------------------------------------------------------------------------
# Closed-candle cutoff
# ---------------------------------------------------------------------------

def test_cutoff_removes_forming_candle():
    """The last candle, if its close time > now, must be dropped."""
    now = BASE + timedelta(hours=3, minutes=30)  # 3h30m into a 1h world
    candles = make_candles("1h", 6)
    # Force the last candle to be "forming": its open is at 3h, close at 4h > now
    forming = Candle(
        now - timedelta(seconds=60),  # open at 3:00 boundary
        Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), Decimal("1000"),
    )
    closed_only = dv.ensure_closed(candles, "1h", now)
    # The last real closed candle for 1h is the one that opened at 2h (closes 3h)
    assert closed_only[-1].timestamp == BASE + timedelta(hours=2)


def test_cutoff_uses_close_time_not_open_time():
    """Cutoff rule is close_time <= T, not open_time <= T."""
    now = BASE + timedelta(hours=1, seconds=30)  # 1h30m: the 1h candle opening at 0h closed
    candles = make_candles("1h", 4)
    # 0h candle closes at 1h <= now -> keep; 1h candle closes at 2h > now -> drop
    closed_only = dv.ensure_closed(candles, "1h", now)
    assert closed_only[-1].timestamp == BASE + timedelta(hours=0)


def test_unfinished_candle_never_reaches_indicators():
    """The currently-forming 5M candle must not appear in the closed set."""
    now = BASE + timedelta(seconds=750)  # 12m30s
    candles = make_candles("5m", 10)
    closed_only = dv.ensure_closed(candles, "5m", now)
    # closed means close_time(open+300) <= 750 -> open <= 450s -> last open at 300s
    assert closed_only[-1].timestamp == BASE + timedelta(seconds=300)


# ---------------------------------------------------------------------------
# Multi-timeframe synchronization
# ---------------------------------------------------------------------------

def test_sync_all_aligns_to_common_decision_point():
    """All three timeframes must be cut to the same closed decision point."""
    now = BASE + timedelta(hours=5, minutes=25)
    hf = make_candles("1h", 10)
    setup = make_candles("15m", 40)
    trigger = make_candles("5m", 120)

    synced = dv.sync_all(hf, setup, trigger, decision_time=now)
    hf_c = synced["hf"]
    setup_c = synced["setup"]
    trigger_c = synced["trigger"]

    # Decision point = close time of the last closed trigger (5m) candle
    decision = dv.common_decision_point(trigger_c, "5m")
    assert decision is not None

    # Every returned candle's close time must be <= decision point
    for c in hf_c:
        assert dv.candle_close_time(c, "1h") <= decision
    for c in setup_c:
        assert dv.candle_close_time(c, "15m") <= decision
    for c in trigger_c:
        assert dv.candle_close_time(c, "5m") <= decision

    # And the last trigger candle must be exactly the decision point
    assert dv.candle_close_time(trigger_c[-1], "5m") == decision


def test_common_decision_point_is_close_time():
    """decision point = close time of the most recent closed trigger candle."""
    now = BASE + timedelta(hours=1)
    trigger = make_candles("5m", 12)
    closed = dv.ensure_closed(trigger, "5m", now)
    decision = dv.common_decision_point(closed, "5m")
    assert decision == dv.candle_close_time(closed[-1], "5m")


# ---------------------------------------------------------------------------
# Malformed candle rejection
# ---------------------------------------------------------------------------

def test_reject_nan_close():
    candles = make_candles("5m", 10)
    bad = Candle(candles[0].timestamp, Decimal("100"), Decimal("101"),
                 Decimal("99"), Decimal("NaN"), Decimal("1000"))
    candles.append(bad)
    with pytest.raises(DataValidationError):
        dv.validate_series(candles, "5m", min_history=1)


def test_reject_infinite_high():
    candles = make_candles("5m", 10)
    bad = Candle(candles[0].timestamp, Decimal("100"), Decimal("Infinity"),
                 Decimal("99"), Decimal("100"), Decimal("1000"))
    candles.append(bad)
    with pytest.raises(DataValidationError):
        dv.validate_series(candles, "5m", min_history=1)


def test_reject_negative_price():
    candles = make_candles("5m", 10)
    bad = Candle(candles[0].timestamp, Decimal("-1"), Decimal("101"),
                 Decimal("99"), Decimal("100"), Decimal("1000"))
    candles.append(bad)
    with pytest.raises(DataValidationError):
        dv.validate_series(candles, "5m", min_history=1)


def test_reject_negative_volume():
    candles = make_candles("5m", 10)
    bad = Candle(candles[0].timestamp, Decimal("100"), Decimal("101"),
                 Decimal("99"), Decimal("100"), Decimal("-5"))
    candles.append(bad)
    with pytest.raises(DataValidationError):
        dv.validate_series(candles, "5m", min_history=1)


def test_reject_high_below_low():
    candles = make_candles("5m", 10)
    bad = Candle(candles[0].timestamp, Decimal("100"), Decimal("90"),
                 Decimal("110"), Decimal("100"), Decimal("1000"))
    candles.append(bad)
    with pytest.raises(DataValidationError):
        dv.validate_series(candles, "5m", min_history=1)


def test_reject_close_outside_high_low():
    candles = make_candles("5m", 10)
    bad = Candle(candles[0].timestamp, Decimal("100"), Decimal("101"),
                 Decimal("99"), Decimal("105"), Decimal("1000"))  # close > high
    candles.append(bad)
    with pytest.raises(DataValidationError):
        dv.validate_series(candles, "5m", min_history=1)


def test_valid_candle_passes():
    candles = make_candles("5m", 10)
    dv.validate_series(candles, "5m", min_history=1)  # no raise


# ---------------------------------------------------------------------------
# Duplicate / unordered
# ---------------------------------------------------------------------------

def test_reject_duplicate_timestamps():
    candles = make_candles("5m", 10)
    dup = Candle(candles[0].timestamp, Decimal("100"), Decimal("101"),
                 Decimal("99"), Decimal("100"), Decimal("1000"))
    candles.append(dup)
    with pytest.raises(DataValidationError):
        dv.validate_series(candles, "5m", min_history=1)


def test_reject_unordered_timestamps():
    candles = make_candles("5m", 10)
    # Out-of-order: move the last candle to the front
    last = candles.pop()
    candles.insert(0, last)
    with pytest.raises(DataValidationError):
        dv.validate_series(candles, "5m", min_history=1)


# ---------------------------------------------------------------------------
# Insufficient history
# ---------------------------------------------------------------------------

def test_insufficient_history_rejects():
    candles = make_candles("5m", 100)  # far below the 600-candle EMA200 warmup
    with pytest.raises(DataValidationError):
        dv.validate_series(candles, "5m",
                           min_history=dv.min_history_for_indicators())


def test_sufficient_history_passes():
    candles = make_candles("5m", 700)
    dv.validate_series(candles, "5m", min_history=dv.min_history_for_indicators())


# ---------------------------------------------------------------------------
# Freshness — based on CANDLE CLOSE TIME, not API response time
# ---------------------------------------------------------------------------

def test_freshness_uses_candle_close_time():
    """A recent API response can still be stale if the latest CLOSED candle is old."""
    now = BASE + timedelta(hours=5, minutes=0)
    # Last closed 1h candle closes at 4h (open 3h). Age = 1h = 3600s.
    candles = make_candles("1h", 6)
    closed = dv.ensure_closed(candles, "1h", now)
    # Stale rule: age must be <= timeframe (3600s) to be considered current
    assert dv.candle_close_time(closed[-1], "1h") <= now
    # If now is within the candle's window, validate_series (min_history=1) passes
    dv.validate_series(closed, "1h", min_history=1, now=now, max_age_seconds=30)


def test_stale_1h_candle_rejected():
    """A 1h candle closed 3 hours ago is stale relative to now."""
    now = BASE + timedelta(hours=8)  # last closed 1h closes at 5h, age = 3h > 1.5*3600
    candles = make_candles("1h", 6)
    closed = dv.ensure_closed(candles, "1h", now)
    assert len(closed) >= 2
    with pytest.raises(DataValidationError):
        dv.validate_series(closed, "1h", min_history=1, now=now, max_age_seconds=30)


def test_stale_5m_candle_rejected():
    """A 5m feed that lags > 1.5 windows behind now is stale.

    15 candles end at open=70m (closes 75m). With now=90m, only candles
    closing <= 90m are kept -> last closed closes at 75m, age = 15m > 450s.
    """
    now = BASE + timedelta(minutes=90)
    candles = make_candles("5m", 15)
    closed = dv.ensure_closed(candles, "5m", now)
    assert len(closed) >= 2
    with pytest.raises(DataValidationError):
        dv.validate_series(closed, "5m", min_history=1, now=now, max_age_seconds=30)


# ---------------------------------------------------------------------------
# API shape: normalize_klines -> Candle
# ---------------------------------------------------------------------------

def test_api_timeframes_mapping():
    assert dv.API_TIMEFRAMES["5m"] == "5m"
    assert dv.API_TIMEFRAMES["15m"] == "15m"
    assert dv.API_TIMEFRAMES["1h"] == "1h"


def test_timeframe_seconds_values():
    assert dv.TIMEFRAME_SECONDS["5m"] == 300
    assert dv.TIMEFRAME_SECONDS["15m"] == 900
    assert dv.TIMEFRAME_SECONDS["1h"] == 3600
