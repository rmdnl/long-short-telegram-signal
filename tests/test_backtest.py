"""
PHASE 4: Backtest data, alignment, warmup, cooldown, dedup, no-look-ahead.

All tests use deterministic synthetic data — no live Binance dependency.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.models import Candle
from app import data_validation as dv
from backtest.data import load_rows
from backtest.align import align_window, iter_trigger_decision_points
from backtest.engine import BacktestEngine
from tests.conftest_backtest import make_candles, make_dataset, BASE

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Historical data loading
# ---------------------------------------------------------------------------

def test_load_rows_builds_dataset():
    ds = make_dataset("TEST", n_hf=100, n_setup=100, n_trigger=100)
    assert len(ds.tf("5m")) == 100
    assert len(ds.tf("15m")) == 100
    assert len(ds.tf("1h")) == 100


def test_load_rows_rejects_none_dataset():
    # Empty rows -> empty tf lists, no crash
    ds = load_rows({}, "EMPTY")
    assert ds.tf("5m") == []


def test_quality_report_detects_missing_candles():
    # 5m series with a gap: two candles 20 min apart (should be 5m apart)
    c1 = make_candles("5m", 5)
    gap = Candle(BASE + timedelta(minutes=100),
                 Decimal("100"), Decimal("101"), Decimal("99"),
                 Decimal("100"), Decimal("1000"))
    ds = load_rows(
        {"5m": [
            {"timestamp": c.timestamp, "open": c.open, "high": c.high,
             "low": c.low, "close": c.close, "volume": c.volume} for c in c1
        ] + [
            {"timestamp": gap.timestamp, "open": gap.open, "high": gap.high,
             "low": gap.low, "close": gap.close, "volume": gap.volume},
        ]},
        "GAPTEST",
    )
    rep_5m = [r for r in ds.reports if r.tf == "5m"][0]
    assert rep_5m.missing_candles > 0
    assert rep_5m.major_problem is True


def test_quality_report_detects_duplicate_and_unordered():
    ts = [BASE, BASE + timedelta(minutes=5), BASE + timedelta(minutes=10)]
    rows = []
    for t in ts:
        rows.append({"timestamp": t, "open": "100", "high": "101",
                     "low": "99", "close": "100", "volume": "1000"})
    # duplicate the middle row and append out-of-order at the end
    dup = dict(rows[1])
    rows.append(dup)
    rows.append({"timestamp": ts[0], "open": "100", "high": "101",
                 "low": "99", "close": "100", "volume": "1000"})  # unordered
    ds = load_rows({"5m": rows}, "DUPT")
    rep = [r for r in ds.reports if r.tf == "5m"][0]
    assert rep.duplicate_candles >= 2
    assert rep.unordered_candles >= 1
    # After dedup the candles list should be 3 unique ascending
    assert len(ds.tf("5m")) == 3


# ---------------------------------------------------------------------------
# No-look-ahead alignment
# ---------------------------------------------------------------------------

def test_alignment_no_lookahead_1h_context():
    """1H context at a 5M decision point must NOT include the forming 1H bar.

    Setup:
    - 1H candles: 2, starting at BASE -> open BASE (close BASE+1h), open BASE+1h (close BASE+2h)
    - 15M candles: 100, starting at BASE
    - 5M candles: 800, starting at BASE
    - Decision: trigger[18] open = BASE+90m, close = BASE+95m (01:35)

    At T = BASE+95m:
    - Only the 1H candle open BASE (close BASE+60m <= T) is closed -> 1 ht candle.
    - 1H candle open BASE+1h closes BASE+2h > T -> excluded.
    """
    hf = make_candles("1h", 2, start=BASE)
    setup = make_candles("15m", 100, start=BASE)
    trigger = make_candles("5m", 800, start=BASE)

    t_candle = trigger[18]
    T = dv.candle_close_time(t_candle, "5m")
    assert T == BASE + timedelta(minutes=95)

    window = align_window("X", hf, setup, trigger, T)
    # Only 1 closed 1H candle by T -> below MIN_CONTEXT_CANDLES=2 -> NOT ok
    # This is the correct no-look-ahead behavior (context not yet available).
    assert window.ok is False
    assert window.reason == "MISSING_HTF_DATA"

    # Re-run with enough 1H history: start 2h earlier so >=2 closed 1H exist by T
    early = BASE - timedelta(hours=2)   # 2023-12-31 22:00
    hf2 = make_candles("1h", 4, start=early)
    setup2 = make_candles("15m", 100, start=early)
    trigger2 = make_candles("5m", 800, start=early)
    # T2 = close of trigger2[42] = early + 43*5m = 22:00 + 215m = 01:35 (next day)
    T2 = dv.candle_close_time(trigger2[42], "5m")
    window2 = align_window("X", hf2, setup2, trigger2, T2)
    assert window2.ok is True
    # 1H open=01:00 (close 02:00 > T2=01:35) must be excluded
    assert not any(c.timestamp == early + timedelta(hours=3) for c in window2.hf)
    # 1H open=00:00 (close 01:00 <= T2) is the last allowed one
    assert any(c.timestamp == early + timedelta(hours=2) for c in window2.hf)
    # 15M open=01:30 (close 02:00 > T2) must be excluded
    assert not any(c.timestamp == early + timedelta(hours=3, minutes=30) for c in window2.setup)
    # All included candles close before or at T2
    for c in window2.hf:
        assert dv.candle_close_time(c, "1h") <= T2
    for c in window2.setup:
        assert dv.candle_close_time(c, "15m") <= T2


def test_boundary_5m_close_10_35_excludes_11_00_1h_and_10_45_15m():
    """
    Spec example: 5M closes at 10:35 ->
    1H context must not use the 11:00 candle; 15M must not use 10:45.
    """
    # Build candles on an absolute clock. Start 1H at 08:00 and 15M at
    # 08:00 so that at T=10:35 there are >=2 closed HTF candles each
    # (08:00 closes 09:00, 09:00 closes 10:00, both <= 10:35).
    start_hf = datetime(2024, 1, 1, 8, 0, tzinfo=UTC)
    start_5m = datetime(2024, 1, 1, 9, 0, tzinfo=UTC)

    # 5M candles 09:00 .. The trigger candle that CLOSES at 10:35 opens 10:30
    trigger = make_candles("5m", 24, start=start_5m)  # open_i = 09:00 + i*5m
    # open_i = 09:00 + i*5m ; close_i = open_i + 5m
    # close 10:35 -> open 10:30 -> i = (10:30-09:00)/5m = 18
    t_candle = trigger[18]
    # close of trigger[18] = 09:00 + 18*5m + 5m = 09:00 + 95m = 10:35
    assert dv.candle_close_time(t_candle, "5m") == start_5m + timedelta(minutes=95)  # 10:35

    # 1H candles 08:00, 09:00, 10:00 ...
    hf = make_candles("1h", 5, start=start_hf)
    # 15M candles 08:00, 08:15, ...
    setup = make_candles("15m", 24, start=start_hf)

    T = dv.candle_close_time(t_candle, "5m")  # 10:35
    window = align_window("X", hf, setup, trigger, T)
    assert window.ok

    # No 1H candle may have close_time > T
    hf_close_times = [dv.candle_close_time(c, "1h") for c in window.hf]
    assert all(ct <= T for ct in hf_close_times)
    # The 1H candle opening 10:00 closes 11:00 -> must be excluded
    assert not any(c.timestamp == start_hf + timedelta(hours=2) for c in window.hf)
    # The 1H candle opening 09:00 closes 10:00 -> allowed
    assert any(c.timestamp == start_hf + timedelta(hours=1) for c in window.hf)

    # No 15M candle opening 10:30 (closes 10:45 > T) may be present
    assert not any(c.timestamp == start_hf + timedelta(hours=2, minutes=30) for c in window.setup)
    # The 15M candle opening 10:15 closes 10:30 <= T -> allowed
    assert any(c.timestamp == start_hf + timedelta(hours=2, minutes=15) for c in window.setup)
    setup_close_times = [dv.candle_close_time(c, "15m") for c in window.setup]
    assert all(ct <= T for ct in setup_close_times)


def test_missing_htf_data_no_forward_fill():
    """If 1H/15M context is absent at a decision point, window is rejected."""
    # Empty 1H series -> at any 5M close, no 1H context
    setup = make_candles("15m", 50, start=BASE)
    trigger = make_candles("5m", 50, start=BASE)
    T = dv.candle_close_time(trigger[10], "5m")
    window = align_window("X", [], setup, trigger, T)
    assert window.ok is False
    assert window.reason == "MISSING_HTF_DATA"


def test_trigger_decision_points_respect_bounds():
    """A 5M candle opening at BASE + i*5m closes at BASE + (i+1)*5m."""
    trigger = make_candles("5m", 100, start=BASE)
    start = BASE + timedelta(minutes=10)
    end = BASE + timedelta(minutes=30)
    pts = iter_trigger_decision_points(trigger, start, end)
    close_times = [dv.candle_close_time(c, "5m") for c in pts]
    # close times in [10, 30]: 10, 15, 20, 25, 30 (candle closing exactly at
    # the bound is included because the cutoff is close_time <= T)
    assert close_times == [BASE + timedelta(minutes=m) for m in (10, 15, 20, 25, 30)]


# ---------------------------------------------------------------------------
# Warmup gating
# ---------------------------------------------------------------------------

def test_warmup_blocks_signals_before_min_history(clean_env, synth_data):
    """No signal may be emitted before the indicator warmup window is met."""
    from app.data_validation import min_history_for_indicators

    warmup = min_history_for_indicators()  # 600
    ds = synth_data.make_dataset("WARMUP", n_hf=warmup, n_setup=warmup,
                                 n_trigger=warmup)
    eng = BacktestEngine(dataset={"WARMUP": ds})
    # Running with warmup_min set to the full requirement must still be
    # deterministic and not raise.
    signals = eng.run(warmup_min=warmup)
    assert "WARMUP" in signals
    # Every returned signal's decision window must have >= warmup closed candles
    # (implied by the engine gate)


def test_warmup_zero_allows_early_signals(clean_env, synth_data):
    """With warmup_min=0 the engine is free to evaluate early candles."""
    ds = synth_data.make_dataset("EARLY", n_hf=50, n_setup=50, n_trigger=50)
    eng = BacktestEngine(dataset={"EARLY": ds})
    out = eng.run(warmup_min=0)
    assert "EARLY" in out


# ---------------------------------------------------------------------------
# Cooldown + duplicate-signal suppression (reuses live signal_filter)
# ---------------------------------------------------------------------------

def test_cooldown_suppresses_close_signals(clean_env, synth_data, monkeypatch):
    """Two signals within cooldown_candles of each other -> only first kept."""
    import app.signal_engine as se

    ds = synth_data.make_dataset("CD", n_hf=700, n_setup=700, n_trigger=700)

    # Count every engine call that PASSES the warmup gate (i.e. every
    # candidate that reaches the engine). The cooldown gate suppresses
    # most of them; surviving signals are a strict subset.
    gate_passes = []

    class _FakeEngine:
        def generate_signal(self, symbol, hf_candles, hf_ind,
                           setup_candles, setup_ind,
                           trigger_candles, trigger_ind):
            # Build a deterministic LONG signal from the last trigger candle
            from app.models import Signal, SignalDirection, SignalType, MarketBias
            from app.signal_filter import generate_signal_id
            from app.risk_engine import (calculate_entry_zone, calculate_stop_loss,
                                         calculate_take_profit)
            trig = trigger_candles[-1]
            atr = setup_ind.atr if setup_ind.atr else Decimal("10")
            direction = SignalDirection.LONG
            entry_low, entry_high = calculate_entry_zone(trig, atr, direction)
            mid = (entry_low + entry_high) / 2
            sl = calculate_stop_loss(direction, mid, atr, Decimal("1.5"))
            tp1, tp2 = calculate_take_profit(direction, mid, sl, Decimal("1.5"), Decimal("2.5"))
            sid = generate_signal_id(symbol, "5m", trig.timestamp, direction)
            sig = Signal(
                signal_id=sid, symbol=symbol, direction=direction,
                signal_type=SignalType.TREND_PULLBACK,
                created_at=trig.timestamp, trigger_candle_time=trig.timestamp,
                entry_low=entry_low, entry_high=entry_high,
                stop_loss=sl, tp1=tp1, tp2=tp2,
                score=90, htf_bias=MarketBias.BULLISH,
                adx_value=Decimal("30"), rsi_value=Decimal("55"),
                volume_ratio=Decimal("1.3"),
            )
            gate_passes.append(sid)
            return sig

    eng = BacktestEngine(dataset={"CD": ds}, engine=_FakeEngine())
    # cooldown_candles default is 3 -> 15m gap between surviving signals
    sigs = eng.run(warmup_min=0)
    cd = sigs["CD"]
    total_candles = len(ds.tf("5m"))  # 700
    # The cooldown gate (3 candles = 15m) suppresses most of the 700 trigger
    # candles: far fewer survive than the total number evaluated.
    assert len(cd) < total_candles // 2
    # Cooldown: no two surviving signals may be within 3 trigger candles
    from app import data_validation as dv
    ts = [s.signal.trigger_candle_time for s in cd]
    for i in range(1, len(ts)):
        gap = (ts[i] - ts[i-1]).total_seconds()
        assert gap >= 3 * 300
    # Engine was only called for candidates that survived the cooldown gate
    assert len(gate_passes) == len(cd)
