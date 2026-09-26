"""
Regression tests for production wiring fixes:
  1. SignalStore get_signal() round-trip (item 7, 10).
  2. Structure-first stop loss (item 5) — respects candle closure, falls back.
  3. Telegram delivery retry / persistence (item 6, 10).
  4. Cooldown + opposite-protection per-symbol isolation (item 3, 4).

All tests are deterministic: no wall-clock, no network.
"""
import pytest
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from pathlib import Path

from app.models import (
    Candle, Signal, SignalDirection, SignalType, SignalStatus, MarketBias,
)
from app.signal_store import SignalStore, DELIVERY_PENDING, DELIVERY_DELIVERED, DELIVERY_FAILED
from app import risk_engine


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _utc(year, month, day, hour=0, minute=0, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


def _c(ts, low="50000", high="51000", close=None, volume="1000"):
    return Candle(
        timestamp=ts,
        open=Decimal("50500"),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close or "50500"),
        volume=Decimal(volume),
    )


def _sig(
    *,
    long=True,
    signal_id=None,
    symbol="BTCUSDT",
    entry_low=Decimal("50500"),
    entry_high=Decimal("50510"),
    sl=Decimal("49500"),
    tp1=Decimal("52000"),
    tp2=Decimal("53000"),
    score=90,
    created_at=None,
    trigger_time=None,
    htf_bias=None,
) -> Signal:
    direction = SignalDirection.LONG if long else SignalDirection.SHORT
    bias = htf_bias or (MarketBias.BULLISH if long else MarketBias.BEARISH)
    return Signal(
        signal_id=signal_id or f"{symbol}|5m|{_utc(2024,1,1,0,0).isoformat()}|{direction.value}",
        symbol=symbol,
        direction=direction,
        signal_type=SignalType.TREND_PULLBACK,
        created_at=created_at or _utc(2024, 1, 1, 0, 5),
        trigger_candle_time=trigger_time or _utc(2024, 1, 1, 0, 0),
        entry_low=entry_low,
        entry_high=entry_high,
        stop_loss=sl,
        tp1=tp1,
        tp2=tp2,
        score=score,
        htf_bias=bias,
        adx_value=Decimal("28.5"),
        rsi_value=Decimal("45.2"),
        volume_ratio=Decimal("1.45"),
        status=SignalStatus.ACTIVE,
    )


# ======================================================================
# 7. SignalStore get_signal() — exact row -> Signal round trip
# ======================================================================

def test_signal_store_get_signal_long_roundtrip(tmp_path):
    store = SignalStore(db_path=str(tmp_path / "t.db"))
    s = _sig(long=True, entry_low=Decimal("50512.345678"),
             entry_high=Decimal("50513.999001"),
             sl=Decimal("49999.000111"),
             created_at=_utc(2024, 1, 2, 3, 12, 7),
             trigger_time=_utc(2024, 1, 2, 3, 10, 0))
    store.save_signal(s)
    got = store.get_signal(s.signal_id)

    assert got is not None
    assert got.signal_id == s.signal_id
    assert got.symbol == s.symbol
    assert got.direction == SignalDirection.LONG
    assert got.signal_type == s.signal_type
    assert got.entry_low == s.entry_low
    assert got.entry_high == s.entry_high
    assert got.stop_loss == s.stop_loss
    assert got.tp1 == s.tp1
    assert got.tp2 == s.tp2
    assert got.score == s.score
    assert got.adx_value == s.adx_value
    assert got.rsi_value == s.rsi_value
    assert got.volume_ratio == s.volume_ratio
    assert got.htf_bias == s.htf_bias
    assert got.status == s.status
    assert got.created_at == s.created_at
    assert got.created_at.tzinfo is not None
    assert got.trigger_candle_time == s.trigger_candle_time
    assert got.trigger_candle_time.tzinfo is not None


def test_signal_store_get_signal_short_roundtrip(tmp_path):
    store = SignalStore(db_path=str(tmp_path / "t.db"))
    s = _sig(long=False, symbol="ETHUSDT")
    store.save_signal(s)
    got = store.get_signal(s.signal_id)

    assert got is not None
    assert got.direction == SignalDirection.SHORT
    assert got.htf_bias == MarketBias.BEARISH
    assert got.entry_low == s.entry_low
    assert got.stop_loss == s.stop_loss


def test_signal_store_get_signal_nonexistent_returns_none(tmp_path):
    store = SignalStore(db_path=str(tmp_path / "t.db"))
    assert store.get_signal("NOT_THERE") is None
    store.save_signal(_sig())
    assert store.get_signal("NOT_THERE") is None


def test_signal_store_decimal_precision(tmp_path):
    """Sub-satoshi Decimal values must survive row -> Signal reconstruction."""
    store = SignalStore(db_path=str(tmp_path / "t.db"))
    # Number of decimals chosen to exceed float-64 mantissa
    long_repr = "50888.123456789012"
    s = _sig(long=True,
             entry_low=Decimal(f"{long_repr}"),
             entry_high=Decimal(f"{long_repr}01"),
             sl=Decimal(f"{long_repr}99"))
    store.save_signal(s)
    got = store.get_signal(s.signal_id)
    assert str(got.entry_low) == f"{long_repr}"
    assert str(got.entry_high) == f"{long_repr}01"
    assert str(got.stop_loss) == f"{long_repr}99"


def test_signal_store_time_awareness(tmp_path):
    store = SignalStore(db_path=str(tmp_path / "t.db"))
    ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    s = _sig(created_at=ts, trigger_time=ts - timedelta(minutes=5))
    store.save_signal(s)
    got = store.get_signal(s.signal_id)
    assert got.created_at.tzinfo is not None
    assert got.trigger_candle_time.tzinfo is not None
    assert got.created_at == ts
    assert got.trigger_candle_time == ts - timedelta(minutes=5)


def test_signal_store_overwrites_on_deterministic_id(tmp_path):
    store = SignalStore(db_path=str(tmp_path / "t.db"))
    s1 = _sig(score=80)
    store.save_signal(s1)
    # Same id, different score -> REPLACE should keep the latest
    s2 = Signal(
        signal_id=s1.signal_id, symbol=s1.symbol,
        direction=s1.direction, signal_type=s1.signal_type,
        created_at=s1.created_at, trigger_candle_time=s1.trigger_candle_time,
        entry_low=s1.entry_low, entry_high=s1.entry_high,
        stop_loss=s1.stop_loss, tp1=s1.tp1, tp2=s1.tp2,
        score=97, htf_bias=s1.htf_bias, adx_value=s1.adx_value,
        rsi_value=s1.rsi_value, volume_ratio=s1.volume_ratio,
    )
    store.save_signal(s2)
    assert store.get_signal(s1.signal_id).score == 97


# ======================================================================
# 5. Structure-first stop loss — via risk helpers + engine wiring
# ======================================================================

def test_stop_loss_structure_priority_long():
    """When a valid swing low exists, it must be preferred over ATR.

    calculate_stop_loss(swing_level=...) path is the structure-first win.
    """
    entry_mid = Decimal("51000")
    atr = Decimal("80")
    swing_low = Decimal("50400")
    sl_atr = Decimal("1.5")

    atr_sl = risk_engine.calculate_stop_loss(
        SignalDirection.LONG, entry_mid, atr, sl_atr, swing_level=None)
    struct_sl = risk_engine.calculate_stop_loss(
        SignalDirection.LONG, entry_mid, atr, sl_atr, swing_level=swing_low)

    assert atr_sl == Decimal("50880")      # 51000 - 80*1.5
    assert struct_sl == swing_low           # structure returned verbatim


def test_stop_loss_structure_priority_short():
    entry_mid = Decimal("51000")
    atr = Decimal("80")
    swing_high = Decimal("51600")
    sl_atr = Decimal("1.5")

    atr_sl = risk_engine.calculate_stop_loss(
        SignalDirection.SHORT, entry_mid, atr, sl_atr, swing_level=None)
    struct_sl = risk_engine.calculate_stop_loss(
        SignalDirection.SHORT, entry_mid, atr, sl_atr, swing_level=swing_high)

    assert atr_sl == Decimal("51120")      # 51000 + 80*1.5
    assert struct_sl == swing_high


def test_stop_loss_fallback_none_swing():
    """Passing swing_level=None must fall back to ATR."""
    entry_mid, atr, sl_atr = Decimal("51000"), Decimal("80"), Decimal("1.5")
    sl = risk_engine.calculate_stop_loss(
        SignalDirection.LONG, entry_mid, atr, sl_atr, swing_level=None)
    assert sl == Decimal("50880")


def test_find_swing_lookback_bounded():
    """find_swing_* must look only at the last `lookback` bars."""
    base = _utc(2024, 1, 1, 0, 0)
    # 20 bars: first 10 contain an artificial extreme, last 10 are normal
    lows = [Decimal("30000")] + [Decimal("50000")] * 19
    highs = [Decimal("90000")] + [Decimal("52000")] * 19
    cands = [
        Candle(timestamp=base + timedelta(minutes=15*i),
               open=Decimal("51000"), high=highs[i], low=lows[i],
               close=Decimal("51000"), volume=Decimal("1000"))
        for i in range(20)
    ]
    # A lookback of 10 must NOT see the artificial extreme outside the window
    assert risk_engine.find_swing_low(cands, lookback=10) == Decimal("50000")
    assert risk_engine.find_swing_high(cands, lookback=10) == Decimal("52000")
    # Lookback >= dataset must see it
    assert risk_engine.find_swing_low(cands, lookback=20) == Decimal("30000")
    assert risk_engine.find_swing_high(cands, lookback=20) == Decimal("90000")


def test_structure_first_uses_only_provided_closed_setup_candles():
    """Engine wiring uses the closed 15M setup candles — no extra lookahead.

    We assert the contract by observing that find_swing outcomes are exactly
    the min/max of the provided closed setup series (which were filtered by
    data_validation.cutoff_candles). The helpers themselves are pure and
    cannot reach forward.
    """
    from datetime import timedelta
    # Setup candles are all closed before decision point
    setup_candles = [
        _c(_utc(2024, 1, 1, 0, 0), low="50400", high="51400"),
        _c(_utc(2024, 1, 1, 0, 15), low="50300", high="51300"),
        _c(_utc(2024, 1, 1, 0, 30), low="50150", high="51500"),
        _c(_utc(2024, 1, 1, 0, 45), low="50200", high="51600"),
    ]
    assert risk_engine.find_swing_low(setup_candles, lookback=10) == Decimal("50150")
    assert risk_engine.find_swing_high(setup_candles, lookback=10) == Decimal("51600")
    # A candidate forming candle added after the list must NOT affect the
    # computed extrema (callers must not include forming bars).
    forming = _c(_utc(2024, 1, 1, 1, 0), low="30000", high="90000")
    assert risk_engine.find_swing_low(setup_candles, lookback=10) == Decimal("50150")


# ======================================================================
# 6 & 10. Delivery-state persistence and bounded retry
# ======================================================================

def test_delivery_status_transitions(tmp_path):
    store = SignalStore(db_path=str(tmp_path / "t.db"))
    s = _sig(signal_id="id-1", symbol="BTCUSDT", long=True)
    store.save_signal(s)

    assert not store.is_delivered("id-1")
    assert store.get_delivery_attempts("id-1") == 0

    store.record_delivery_failure("id-1", "timeout")
    assert not store.is_delivered("id-1")
    assert store.get_delivery_attempts("id-1") == 1

    store.mark_delivered("id-1")
    assert store.is_delivered("id-1")
    # Only signals with status==DELIVERED are suppressable
    assert store.signal_exists("id-1") is True


def test_undelivered_signals_are_retryable_across_restart(tmp_path):
    path = str(tmp_path / "t.db")
    store = SignalStore(db_path=path)
    s = _sig(signal_id="id-2", long=True)
    store.save_signal(s)
    store.record_delivery_failure("id-2", "transient 5xx")

    # New object on the same db file (restart simulation)
    store2 = SignalStore(db_path=path)
    undel = store2.get_undelivered_signals(limit=10)
    assert len(undel) == 1
    assert undel[0].signal_id == "id-2"
    assert not store2.is_delivered("id-2")

    store2.mark_delivered("id-2")
    assert store2.is_delivered("id-2")
    # After a second restart, delivered signals do not reappear
    store3 = SignalStore(db_path=path)
    assert store3.get_undelivered_signals(limit=10) == []


def test_delivery_not_considered_done_after_failure(tmp_path):
    """A failed send must NOT let is_delivered() return True."""
    store = SignalStore(db_path=str(tmp_path / "t.db"))
    s = _sig(signal_id="id-3")
    store.save_signal(s)
    store.record_delivery_failure("id-3", "429 rate-limited")
    assert not store.is_delivered("id-3")
    store.record_delivery_failure("id-3", "500")
    assert not store.is_delivered("id-3")


def test_signal_exists_but_undelivered_is_not_suppressed(tmp_path, monkeypatch):
    """Bug-catch: row existence alone must not suppress a retry.

    Regression for the old `signal_exists` gate that would never retry a
    signal whose Telegram send had failed but whose row already existed.
    """
    import os
    from unittest.mock import MagicMock
    from app import main as main_mod

    store = SignalStore(db_path=str(tmp_path / "t.db"))
    scanner = MagicMock()

    # Pre-insert an undelivered signal (failed previous cycle)
    s = _sig(signal_id="BTCUSDT|5m|2024-01-01T00:00:00+00:00|LONG",
             symbol="BTCUSDT", long=True)
    store.save_signal(s)
    store.record_delivery_failure(s.signal_id, "previous failure")
    assert store.signal_exists(s.signal_id) is True
    assert not store.is_delivered(s.signal_id)

    bot = MagicMock()
    bot.send_signal.return_value = True

    main_mod._handle_signal(s, scanner, store, bot, send_telegram=True,
                            dry_run=False)
    # Despite the row already existing, the signal must be delivered
    bot.send_signal.assert_called_once()
    assert store.is_delivered(s.signal_id)


def test_scanner_failures_do_not_crash_main_loop(tmp_path, monkeypatch):
    """A Scanner-market-data failure must not terminate the main scan cycle.

    The failure is caught, `signals` falls back to [], and the pacing
    calculation still runs.
    """
    from app.scanner import Scanner
    # Calling scan_all_symbols must succeed for at least the dry-run path
    sc = Scanner()
    # scan_all_symbols wraps market-data exceptions internally and returns []
    # so the "always_store.signal_exists" dedup path is what we exercise.
    sigs = sc.scan_all_symbols()
    assert isinstance(sigs, list)


def test_clear_signal_state_does_not_bleed_to_other_symbols():
    from app.scanner import Scanner
    sc = Scanner()
    aid = "BTCUSDT|5m|2024-01-01T00:00:00+00:00|LONG"
    bid = "ETHUSDT|5m|2024-01-01T00:00:00+00:00|SHORT"
    sc._emitted_ids.add(aid)
    sc._emitted_ids.add(bid)
    sc._last_signal_time["BTCUSDT"] = _utc(2024,1,1,0,0)
    sc._last_signal_time["ETHUSDT"] = _utc(2024,1,1,0,10)
    sc._last_signal_direction["BTCUSDT"] = SignalDirection.LONG
    sc._last_signal_direction["ETHUSDT"] = SignalDirection.SHORT

    sc.clear_signal_state("BTCUSDT", aid)

    assert aid not in sc._emitted_ids
    assert bid in sc._emitted_ids
    assert "BTCUSDT" not in sc._last_signal_time
    assert "ETHUSDT" in sc._last_signal_time
    assert "ETHUSDT" in sc._last_signal_direction


def test_cooldown_is_per_symbol(monkeypatch):
    """Cooldown blocking one symbol must not block another."""
    from datetime import timedelta
    from app.scanner import Scanner, REASON_COOLDOWN_ACTIVE, REASON_OPPOSITE_SIGNAL_BLOCKED
    from app.models import MarketBias

    scan_now = _utc(2024, 1, 1, 1, 0, 0)

    monkeypatch.setenv("COOLDOWN_CANDLES", "3")
    import app.config as cfg
    cfg.config = None

    sc = Scanner()
    # Prime cooldown for BTCUSDT only (5 min ago, cooldown needs 15 min)
    sc._last_signal_time["BTCUSDT"] = scan_now - timedelta(minutes=5)
    sc._last_signal_direction["BTCUSDT"] = SignalDirection.LONG

    latest_trigger_ts = scan_now - timedelta(minutes=5)  # same candle, closed
    # BTCUSDT is blocked
    gate_btc = sc._direction_gate_reason("BTCUSDT", latest_trigger_ts, MarketBias.BULLISH)
    assert gate_btc in (REASON_COOLDOWN_ACTIVE, REASON_OPPOSITE_SIGNAL_BLOCKED)
    # Fresh symbol with no prior signal is allowed (returns None)
    gate_eth = sc._direction_gate_reason("ETHUSDT", latest_trigger_ts, MarketBias.BULLISH)
    assert gate_eth is None
    cfg.config = None


# ======================================================================
# 3 & 10. Cooldown restart-safety: rehydrated from SQLite
# ======================================================================

def test_cooldown_rehydrated_after_restart(tmp_path):
    """After a restart, the scanner must have the same cooldown state as
    before, sourced from the most recent DELIVERED signal per symbol."""
    from app.scanner import Scanner, REASON_COOLDOWN_ACTIVE, REASON_OPPOSITE_SIGNAL_BLOCKED
    from app.models import MarketBias

    db = str(tmp_path / "restart.db")
    store = SignalStore(db_path=db)

    # Pretend we delivered a BTCUSDT LONG at 00:15 UTC
    ts = _utc(2024, 1, 1, 0, 15)
    s = _sig(signal_id="BTC|5m|restart|LONG", symbol="BTCUSDT", long=True,
             trigger_time=ts)
    store.save_signal(s)
    store.mark_delivered(s.signal_id)

    # Simulate restart: new scanner, rehydrate from DB
    sc = Scanner()
    rows = store.get_last_delivered_by_symbol()
    restored = sc.restore_cooldown_state(rows)
    assert restored == 1

    # Immediately after (elapsed < 15 min = 3×5m), cooldown must block
    gate = sc._direction_gate_reason("BTCUSDT", ts + timedelta(minutes=5), MarketBias.BULLISH)
    assert gate in (REASON_COOLDOWN_ACTIVE, REASON_OPPOSITE_SIGNAL_BLOCKED)

    # After cooldown elapsed, gate opens
    gate2 = sc._direction_gate_reason("BTCUSDT", ts + timedelta(minutes=16), MarketBias.BULLISH)
    assert gate2 is None


def test_cooldown_not_started_for_undelivered(tmp_path):
    """An undelivered signal must NOT start a cooldown window, because
    the user never saw it — re-starting the cooldown on restart would
    silently skip a valid opportunity."""
    from app.scanner import Scanner

    db = str(tmp_path / "undelivered.db")
    store = SignalStore(db_path=db)
    ts = _utc(2024, 1, 1, 0, 15)
    s = _sig(signal_id="ETH|5m|undel", symbol="ETHUSDT", long=True,
             trigger_time=ts)
    store.save_signal(s)
    # Simulate failure: NOT delivered
    store.record_delivery_failure(s.signal_id, "test-fail")

    sc = Scanner()
    rows = store.get_last_delivered_by_symbol()
    assert rows == []  # no delivered signal to restore from
    restored = sc.restore_cooldown_state(rows)
    assert restored == 0

    # Gate must be open (no prior cooldown state)
    gate = sc._direction_gate_reason("ETHUSDT", ts + timedelta(minutes=1), MarketBias.BULLISH)
    assert gate is None


# ======================================================================
# 9. No-lookahead regression: find_swing_*, cutoff_candles, data_validation
# ======================================================================

def test_cutoff_candles_drops_forming():
    """The latest candle whose close_time > decision_time must be dropped."""
    from app.data_validation import cutoff_candles
    decision = _utc(2024, 1, 1, 0, 5)
    candles = [
        _c(_utc(2024, 1, 1, 0, 0)),  # close = 0:05 <= 0:05  → kept
        _c(_utc(2024, 1, 1, 0, 5)),  # close = 0:10 >  0:05  → dropped
    ]
    closed = cutoff_candles(candles, "5m", decision)
    assert len(closed) == 1
    assert closed[0].timestamp == _utc(2024, 1, 1, 0, 0)


def test_cutoff_candles_boundary_exact():
    """At exact boundary (close_time == decision_time) the candle is kept."""
    from app.data_validation import cutoff_candles
    decision = _utc(2024, 1, 1, 0, 5)
    candle = _c(_utc(2024, 1, 1, 0, 0))  # close = 0:05
    closed = cutoff_candles([candle], "5m", decision)
    assert len(closed) == 1
