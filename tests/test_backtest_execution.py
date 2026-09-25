"""
PHASE 4: Deterministic execution-model tests.

Covers: entry fill, entry-not-filled (expired), SL, TP1, TP2,
same-candle SL/TP ambiguity (conservative ordering), R-multiples,
fees/slippage, and drawdown.

Model: entry candle OPENS the position; SL/TP are evaluated from the NEXT
candle onward. The fill candle itself never triggers an exit.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.models import Candle, Signal, SignalDirection, SignalType, MarketBias
from app.signal_filter import generate_signal_id
from backtest.execution import (
    ExecutionModel, TradeSimulator, TP_MODEL_50PCT, TP_MODEL_ALL,
    STATUS_STOPPED, STATUS_TP1_HIT, STATUS_TP2_HIT, STATUS_EXPIRED,
)

UTC = timezone.utc
BASE = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)


def _mk_candle(ts, o, h, l, c):
    return Candle(ts, o, h, l, c, Decimal("1000"))


def _mk_signal(direction, entry_low, entry_high, sl, tp1, tp2, ts=BASE):
    sid = generate_signal_id("TEST", "5m", ts, direction)
    return Signal(
        signal_id=sid, symbol="TEST", direction=direction,
        signal_type=SignalType.TREND_PULLBACK,
        created_at=ts, trigger_candle_time=ts,
        entry_low=entry_low, entry_high=entry_high,
        stop_loss=sl, tp1=tp1, tp2=tp2,
        score=90, htf_bias=MarketBias.BULLISH,
        adx_value=Decimal("30"), rsi_value=Decimal("55"),
        volume_ratio=Decimal("1.3"),
    )


def _c(t_idx, o, h, l, c):
    """5M candle at open time BASE + t_idx*5m."""
    return _mk_candle(BASE + timedelta(minutes=5 * t_idx),
                      Decimal(o), Decimal(h), Decimal(l), Decimal(c))


# --- Helpers: LONG signal, zone [100,102], SL 95, TP1 105, TP2 110 ---
LONG_SIG_PARAMS = dict(direction=SignalDirection.LONG,
                       entry_low="100", entry_high="102",
                       sl="95", tp1="105", tp2="110")


def _long_futures(fill_c, *exits):
    """Candle 0 = fill, then exit candles at idx 1,2,..."""
    out = [_c(0, "100", "101", "99", "100")]  # intersects zone -> fill
    for i, c in enumerate(exits, start=1):
        out.append(c)
    return out


# ---------------------------------------------------------------------------
# Entry fill / not filled
# ---------------------------------------------------------------------------

def test_entry_filled_when_zone_intersects():
    """LONG: a candle whose range intersects the zone fills at the zone edge."""
    sig = _mk_signal(**LONG_SIG_PARAMS)
    sim = TradeSimulator(ExecutionModel())
    tr = sim.simulate(sig, [_c(0, "98", "103", "97", "101")])
    assert tr.entry_status == "FILLED"
    assert tr.entry_price == Decimal("100")  # least-favorable fill = zone low
    assert tr.exit_status == STATUS_EXPIRED   # still open at end of data


def test_entry_not_filled_expires():
    """If no future candle reaches the zone, the signal expires without fill."""
    sig = _mk_signal(SignalDirection.LONG, "200", "202", "195", "205", "210")
    futures = [
        _c(0, "100", "110", "95", "105"),   # far below the zone [200,202]
        _c(1, "100", "110", "95", "105"),
    ]
    sim = TradeSimulator(ExecutionModel(max_candles_after_signal=2))
    tr = sim.simulate(sig, futures)
    assert tr.entry_status == STATUS_EXPIRED
    assert tr.exit_status == STATUS_EXPIRED
    assert tr.r_multiple == Decimal("0")


# ---------------------------------------------------------------------------
# SL / TP1 / TP2
# ---------------------------------------------------------------------------

def test_stop_loss_hit():
    """LONG: a later candle's low pierces SL -> STOPPED at -1R."""
    sig = _mk_signal(**LONG_SIG_PARAMS)
    futures = _long_futures(
        None,
        _c(1, "100", "103", "94", "96"),    # low 94 <= SL 95 -> stopped
    )
    sim = TradeSimulator(ExecutionModel())
    tr = sim.simulate(sig, futures)
    assert tr.entry_status == "FILLED"
    assert tr.sl_hit is True
    assert tr.exit_status == STATUS_STOPPED
    assert tr.exit_price == Decimal("95")
    assert tr.r_multiple == Decimal("-1")


def test_tp1_hit_50pct_model_rides_to_tp2():
    """Default 50% model: TP1 hit then TP2 hit -> blended R."""
    sig = _mk_signal(**LONG_SIG_PARAMS)
    futures = _long_futures(
        None,
        _c(1, "100", "105", "99", "105"),   # high 105 = TP1 -> tp1_hit
        _c(2, "105", "111", "104", "110"),  # high 111 >= TP2 110 -> tp2_hit
    )
    sim = TradeSimulator(ExecutionModel())
    tr = sim.simulate(sig, futures)
    assert tr.tp1_hit is True
    assert tr.tp2_hit is True
    assert tr.exit_status == STATUS_TP2_HIT
    # entry=100, risk=|100-95|=5
    # tp1_rr=|105-100|/5=1, tp2_rr=|110-100|/5=2
    # blended = 0.5*1 + 0.5*2 = 1.5
    assert tr.r_multiple == Decimal("1.5")


def test_tp1_all_model_closes_at_tp1():
    """TP1_ALL model: whole position exits at TP1; TP2 never considered."""
    sig = _mk_signal(**LONG_SIG_PARAMS)
    futures = _long_futures(
        None,
        _c(1, "100", "112", "99", "110"),   # both TP1(105) and TP2(110) touched
    )
    sim = TradeSimulator(ExecutionModel(tp_model=TP_MODEL_ALL))
    tr = sim.simulate(sig, futures)
    assert tr.exit_status == STATUS_TP1_HIT
    assert tr.tp1_hit is True
    assert tr.exit_price == Decimal("105")
    # risk=5, tp1_rr=|105-100|/5=1
    assert tr.r_multiple == Decimal("1")


# ---------------------------------------------------------------------------
# Same-candle ambiguity: conservative ordering
# ---------------------------------------------------------------------------

def test_same_candle_sl_and_tp_assumes_sl_first():
    """A single open-candle that pierces BOTH SL and TP -> assume SL first."""
    sig = _mk_signal(**LONG_SIG_PARAMS)
    futures = _long_futures(
        None,
        _c(1, "101", "112", "93", "110"),   # low 93 <= SL, high 112 >= TP2 -> SL first
    )
    sim = TradeSimulator(ExecutionModel())
    tr = sim.simulate(sig, futures)
    assert tr.sl_hit is True
    assert tr.exit_status == STATUS_STOPPED
    assert tr.r_multiple == Decimal("-1")


def test_same_candle_entry_and_sl_conservative():
    """
    On the fill candle, the position opens at the least-favorable edge; SL/TP
    are NOT evaluated on that candle. A later candle that pierces SL
    resolves conservatively to STOPPED.
    """
    sig = _mk_signal(**LONG_SIG_PARAMS)
    futures = _long_futures(
        None,
        _c(1, "100", "101", "93", "96"),    # low 93 <= SL -> stopped
    )
    sim = TradeSimulator(ExecutionModel())
    tr = sim.simulate(sig, futures)
    assert tr.sl_hit is True
    assert tr.exit_status == STATUS_STOPPED


# ---------------------------------------------------------------------------
# R-multiple + fees / slippage
# ---------------------------------------------------------------------------

def test_r_multiple_is_size_independent():
    """R is normalized to risk; same shape at different price levels -> same R."""
    # Both shapes: fill -> candle touching TP2 (no TP1-first ambiguity, single TP2)
    sig_a = _mk_signal(SignalDirection.LONG, "10", "11", "9", "12", "13")
    sig_b = _mk_signal(SignalDirection.LONG, "100", "110", "90", "120", "130")

    fa = [_c(0, Decimal("10"), Decimal("11"), Decimal("9"), Decimal("10")),
          _c(1, Decimal("10"), Decimal("14"), Decimal("9.5"), Decimal("13"))]
    fb = [_c(0, Decimal("100"), Decimal("110"), Decimal("90"), Decimal("100")),
          _c(1, Decimal("100"), Decimal("140"), Decimal("95"), Decimal("130"))]

    sim = TradeSimulator(ExecutionModel())
    tr_a = sim.simulate(sig_a, fa)
    tr_b = sim.simulate(sig_b, fb)
    # Both: 50% at TP1, 50% at TP2
    assert tr_a.r_multiple == tr_b.r_multiple


def test_fees_reduce_net_r_when_enabled():
    """Enabling fees lowers the net R below the gross R."""
    sig = _mk_signal(**LONG_SIG_PARAMS)
    futures = _long_futures(
        None,
        _c(1, "100", "105", "99", "105"),   # TP1 hit
        _c(2, "105", "111", "104", "110"),  # TP2 hit
    )
    sim_no_fee = TradeSimulator(ExecutionModel())
    sim_fee = TradeSimulator(ExecutionModel(enable_fees=True, taker_fee=Decimal("0.001")))
    tr_no = sim_no_fee.simulate(sig, futures)
    tr_yes = sim_fee.simulate(sig, futures)
    assert tr_yes.r_multiple < tr_no.r_multiple
    assert tr_yes.fees_enabled is True


def test_fees_disabled_reports_no_cost():
    sig = _mk_signal(**LONG_SIG_PARAMS)
    futures = _long_futures(
        None,
        _c(1, "100", "105", "99", "105"),
        _c(2, "105", "111", "104", "110"),
    )
    sim = TradeSimulator(ExecutionModel())  # enable_fees=False by default
    tr = sim.simulate(sig, futures)
    assert tr.fees_enabled is False
    assert tr.r_multiple == Decimal("1.5")


# ---------------------------------------------------------------------------
# Drawdown (via metrics on a synthetic sequence)
# ---------------------------------------------------------------------------

def test_max_drawdown_r():
    """Peak-to-trough drawdown of a cumulative-R curve in R units."""
    from backtest.metrics import _max_drawdown_r
    series = [Decimal("1"), Decimal("1"), Decimal("-3"), Decimal("2")]
    # equity: 1, 2, -1, 1 -> peak 2, trough -1 -> dd 3
    dd = _max_drawdown_r(series)
    assert dd == pytest.approx(3.0)


def test_max_drawdown_empty_is_zero():
    from backtest.metrics import _max_drawdown_r
    assert _max_drawdown_r([]) == 0.0
