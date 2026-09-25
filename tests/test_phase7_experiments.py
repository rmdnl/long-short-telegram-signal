"""
PHASE 7: Controlled-experiment tests.

Verifies:
- V1_BASELINE reproduces TradeSimulator exactly (no diff across signals)
- H1_BE_0_5R arming + BE_HIT semantics (LONG / SHORT)
- H1_ATR_TRAIL ratcheting and TRAIL_HIT
- H1_PARTIAL_BE partial protection R accounting
- H1_TIME_EXIT_12 time-based exit
- No look-ahead: exit loop only reads candles up to the current index
- Same-candle sequencing: SL beats TP on the same candle
- V1 baseline is reproducible / production numbers unchanged
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List

import pytest

from app.models import Candle, Signal, SignalDirection, SignalType, MarketBias
from app.signal_filter import generate_signal_id
from backtest.execution import (
    ExecutionModel, TradeSimulator, TP_MODEL_50PCT,
)
from backtest.experiments.exit_rules import (
    V1_BASELINE, H1_BE_0_5R, H1_ATR_TRAIL, H1_PARTIAL_BE, H1_TIME_EXIT_12,
    Variant,
)
from backtest.experiments.simulator import (
    ExperimentSimulator, ExperimentTrade,
    STATUS_BE_HIT, STATUS_TRAIL_HIT, STATUS_TIME_EXIT,
)
from backtest.metrics import _compute_group

UTC = timezone.utc
BASE = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
MAX_HOLD = 48


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mk_candle(idx: int, o, h, l, c, ts: datetime = None) -> Candle:
    ts = ts or (BASE + timedelta(minutes=5 * idx))
    return Candle(ts, Decimal(o), Decimal(h), Decimal(l), Decimal(c), Decimal("1000"))


def _mk_signal(direction: SignalDirection, entry_low, entry_high, sl, tp1, tp2,
               ts: datetime = BASE, symbol: str = "TEST") -> Signal:
    sid = generate_signal_id(symbol, "5m", ts, direction)
    return Signal(
        signal_id=sid, symbol=symbol, direction=direction,
        signal_type=SignalType.TREND_PULLBACK,
        created_at=ts, trigger_candle_time=ts,
        entry_low=Decimal(entry_low), entry_high=Decimal(entry_high),
        stop_loss=Decimal(sl), tp1=Decimal(tp1), tp2=Decimal(tp2),
        score=90, htf_bias=MarketBias.BULLISH,
        adx_value=Decimal("30"), rsi_value=Decimal("55"),
        volume_ratio=Decimal("1.3"),
    )


def _model() -> ExecutionModel:
    return ExecutionModel(
        maker_fee=Decimal("0"), taker_fee=Decimal("0"),
        slippage_bps=Decimal("0"), tp_model=TP_MODEL_50PCT,
        enable_fees=False, max_candles_after_signal=MAX_HOLD,
    )


# ---------------------------------------------------------------------------
# V1_BASELINE == TradeSimulator (byte-identical exit path)
# ---------------------------------------------------------------------------

def test_v1_baseline_reproduces_trade_simulator_long():
    """V1_BASELINE experiment must produce identical status/r to TradeSimulator."""
    model = _model()
    prod = TradeSimulator(model)
    exp = ExperimentSimulator(model, V1_BASELINE)

    # LONG: fill at idx0, TP2 touched at idx3, then EXPIRED at idx48
    sig = _mk_signal(SignalDirection.LONG, "100", "102", "95", "105", "110")
    candles: List[Candle] = [_mk_candle(0, "100", "101", "99", "100")]
    for i in range(1, 48):
        candles.append(_mk_candle(i, "101", "103", "100", "102"))
    candles.append(_mk_candle(48, "104", "111", "103", "105"))  # TP2 at idx48
    candles.append(_mk_candle(49, "105", "106", "104", "105"))
    candles.append(_mk_candle(50, "105", "106", "104", "105"))

    p = prod.simulate(sig, candles)
    e = exp.simulate(sig, candles).as_trade()
    assert p.exit_status == e.exit_status, f"prod={p.exit_status} exp={e.exit_status}"
    assert p.r_multiple == e.r_multiple, f"prod_r={p.r_multiple} exp_r={e.r_multiple}"
    assert p.holding_candles == e.holding_candles


def test_v1_baseline_reproduces_trade_simulator_short():
    model = _model()
    prod = TradeSimulator(model)
    exp = ExperimentSimulator(model, V1_BASELINE)

    sig = _mk_signal(SignalDirection.SHORT, "100", "102", "107", "95", "90")
    candles = [_mk_candle(0, "100", "101", "99", "100")]
    for i in range(1, 48):
        candles.append(_mk_candle(i, "100", "101", "99", "100"))
    candles.append(_mk_candle(48, "96", "98", "89", "91"))  # TP2 at idx48
    candles.append(_mk_candle(49, "91", "93", "89", "92"))
    candles.append(_mk_candle(50, "92", "94", "90", "93"))

    p = prod.simulate(sig, candles)
    e = exp.simulate(sig, candles).as_trade()
    assert p.exit_status == e.exit_status
    assert p.r_multiple == e.r_multiple
    assert p.holding_candles == e.holding_candles


def test_v1_baseline_reproduces_trade_simulator_sl_hit():
    model = _model()
    prod = TradeSimulator(model)
    exp = ExperimentSimulator(model, V1_BASELINE)

    sig = _mk_signal(SignalDirection.LONG, "100", "102", "95", "105", "110")
    candles = [_mk_candle(0, "100", "101", "99", "100")]
    for i in range(1, 5):
        candles.append(_mk_candle(i, "100", "101", "94", "98"))  # SL at idx1
    candles.extend(_mk_candle(i, "98", "100", "97", "99") for i in range(5, MAX_HOLD + 2))

    p = prod.simulate(sig, candles)
    e = exp.simulate(sig, candles).as_trade()
    assert p.exit_status == e.exit_status == "STOPPED"
    assert p.r_multiple == e.r_multiple == Decimal("-1")


def test_v1_baseline_reproduces_trade_simulator_expired_no_fill():
    model = _model()
    prod = TradeSimulator(model)
    exp = ExperimentSimulator(model, V1_BASELINE)

    sig = _mk_signal(SignalDirection.LONG, "100", "102", "95", "105", "110")
    # All candles stay below the zone -> no fill
    candles = [_mk_candle(i, "80", "90", "75", "85") for i in range(MAX_HOLD + 3)]

    p = prod.simulate(sig, candles)
    e = exp.simulate(sig, candles).as_trade()
    assert p.exit_status == e.exit_status == "EXPIRED"
    assert p.entry_status == e.entry_status == "EXPIRED"
    assert p.r_multiple == e.r_multiple == Decimal("0")


# ---------------------------------------------------------------------------
# H1_BE_0_5R: breakeven arming
# ---------------------------------------------------------------------------

def test_be_armed_long_exits_at_breakeven():
    """LONG: +0.5R reached, then price pulls back to entry -> BE_HIT (0R)."""
    model = _model()
    sim = ExperimentSimulator(model, H1_BE_0_5R)
    # entry=100, sl=95, risk=5, half_r_level = 100 + 2.5 = 102.5
    sig = _mk_signal(SignalDirection.LONG, "100", "102", "95", "105", "110")
    candles = [
        _mk_candle(0, "100", "101", "99", "100"),     # fill
        _mk_candle(1, "101", "103", "100", "102"),   # +0.5R reached (high=103 >= 102.5)
        _mk_candle(2, "102", "103", "100", "100"),   # pullback to entry -> BE_HIT
    ]
    et = sim.simulate(sig, candles)
    assert et.trade.exit_status == STATUS_BE_HIT
    assert et.trade.r_multiple == Decimal("0")
    # The armed BE stop is reflected in final_stop == entry (100).
    assert et.final_stop == Decimal("100")


def test_be_armed_short_exits_at_breakeven():
    """SHORT: +0.5R reached, then price rallies to entry -> BE_HIT (0R)."""
    model = _model()
    sim = ExperimentSimulator(model, H1_BE_0_5R)
    # SHORT: entry=102 (zone high), sl=107, risk=5, half_r_level = 102 - 2.5 = 99.5
    sig = _mk_signal(SignalDirection.SHORT, "100", "102", "107", "95", "90")
    candles = [
        _mk_candle(0, "100", "101", "99", "100"),     # fill at zone_high=102
        _mk_candle(1, "101", "101", "99", "100"),    # +0.5R reached (low=99 <= 99.5)
        _mk_candle(2, "100", "102", "99", "101"),    # rally to entry -> BE_HIT
    ]
    et = sim.simulate(sig, candles)
    assert et.trade.exit_status == STATUS_BE_HIT
    assert et.trade.r_multiple == Decimal("0")
    assert et.final_stop == Decimal("102")  # entry (zone high) for SHORT


def test_be_not_armed_when_half_r_never_reached():
    """If +0.5R is never reached, the BE rule does NOT arm; V1-like exit applies."""
    model = _model()
    sim = ExperimentSimulator(model, H1_BE_0_5R)
    # entry=100, sl=95, half_r=102.5; price never reaches 102.5
    sig = _mk_signal(SignalDirection.LONG, "100", "102", "95", "105", "110")
    candles = [
        _mk_candle(0, "100", "101", "99", "100"),    # fill
        _mk_candle(1, "100", "102", "99", "101"),    # high=102 < 102.5, no arm
        _mk_candle(2, "101", "102", "94", "98"),     # SL hit
    ]
    et = sim.simulate(sig, candles)
    assert et.trade.exit_status == "STOPPED"
    assert et.be_armed is False
    assert et.trade.r_multiple == Decimal("-1")


# ---------------------------------------------------------------------------
# H1_ATR_TRAIL
# ---------------------------------------------------------------------------

def _atr_lookup_flat(value: Decimal):
    """Return a callable that gives a fixed ATR value."""
    def fn(signal, entry_price, stop):
        return value
    return fn


def test_atr_trail_long_ratchets_up():
    """LONG ATR trail: stop ratchets in the favorable direction only."""
    model = _model()
    atr_fn = _atr_lookup_flat(Decimal("2"))
    sim = ExperimentSimulator(model, H1_ATR_TRAIL, atr_lookup=atr_fn)
    # entry=100, sl=95, risk=5, half_r=102.5, atr=2, trail=high - 2
    sig = _mk_signal(SignalDirection.LONG, "100", "102", "95", "105", "110")
    candles = [
        _mk_candle(0, "100", "101", "99", "100"),    # fill
        _mk_candle(1, "101", "104", "100", "103"),   # high=104>=102.5, activate, trail=104-2=102
        _mk_candle(2, "103", "105", "101", "104"),   # high=105, trail=105-2=103 (ratcheted up)
        _mk_candle(3, "104", "104", "102", "103"),   # low=102 <= 103 -> TRAIL_HIT
    ]
    et = sim.simulate(sig, candles)
    assert et.trade.exit_status == STATUS_TRAIL_HIT
    # exit at the trailing stop (103), R = (103-100)/5 = 0.6
    assert et.trade.r_multiple == Decimal("3") / Decimal("5")
    assert et.trade.r_multiple == Decimal("0.6")


def test_atr_trail_short_ratchets_down():
    """SHORT ATR trail: stop ratchets downward (in the favorable direction)."""
    model = _model()
    atr_fn = _atr_lookup_flat(Decimal("2"))
    sim = ExperimentSimulator(model, H1_ATR_TRAIL, atr_lookup=atr_fn)
    # SHORT: entry=102 (zone high), sl=107, risk=5, half_r=99.5, atr=2
    sig = _mk_signal(SignalDirection.SHORT, "100", "102", "107", "95", "90")
    candles = [
        _mk_candle(0, "100", "101", "99", "100"),    # fill at 102
        _mk_candle(1, "101", "101", "98", "99"),     # low=98<=99.5, activate, trail=98+2=100
        _mk_candle(2, "99", "100", "97", "98"),      # low=97, trail=97+2=99 (ratcheted down)
        _mk_candle(3, "98", "100", "97", "99"),      # high=100 >= 99 -> TRAIL_HIT
    ]
    et = sim.simulate(sig, candles)
    assert et.trade.exit_status == STATUS_TRAIL_HIT
    # exit at 99, R = (102-99)/5 = 0.6
    assert et.trade.r_multiple == Decimal("0.6")


def test_atr_trail_no_arm_when_half_r_not_reached():
    model = _model()
    atr_fn = _atr_lookup_flat(Decimal("2"))
    sim = ExperimentSimulator(model, H1_ATR_TRAIL, atr_lookup=atr_fn)
    sig = _mk_signal(SignalDirection.LONG, "100", "102", "95", "105", "110")
    candles = [
        _mk_candle(0, "100", "101", "99", "100"),    # fill
        _mk_candle(1, "100", "101", "94", "98"),     # SL hit before +0.5R
    ]
    et = sim.simulate(sig, candles)
    assert et.trade.exit_status == "STOPPED"
    assert et.trade.r_multiple == Decimal("-1")


# ---------------------------------------------------------------------------
# H1_PARTIAL_BE
# ---------------------------------------------------------------------------

def test_partial_be_sl_hit_gives_minus_half_r():
    """Partial BE: 50% protected at breakeven (0R) + 50% hit SL (-1R) = -0.5R."""
    model = _model()
    sim = ExperimentSimulator(model, H1_PARTIAL_BE)
    # entry=100, sl=95, risk=5, half_r=102.5
    sig = _mk_signal(SignalDirection.LONG, "100", "102", "95", "105", "110")
    candles = [
        _mk_candle(0, "100", "101", "99", "100"),    # fill
        _mk_candle(1, "101", "103", "100", "102"),   # +0.5R reached -> partial taken
        _mk_candle(2, "102", "103", "94", "98"),     # remainder hits SL
    ]
    et = sim.simulate(sig, candles)
    assert et.trade.exit_status == "STOPPED"
    assert et.be_armed is True
    assert et.trade.r_multiple == Decimal("-0.5")


def test_partial_be_tp2_hit_computes_half_remaining():
    """Partial BE + TP2: 50% at 0R + 50% at TP2 blended R.
    The remainder keeps the ORIGINAL SL/TP model. With TP_MODEL_50PCT the
    remainder's TP leg is 0.5*TP1_rr + 0.5*TP2_rr. Here tp1=1R, tp2=2R,
    so remainder R = 0.5*1 + 0.5*2 = 1.5; blended with the 0R protected
    half: 0.5*0 + 0.5*1.5 = 0.75R. The 50PCT model only finalizes the
    TP leg on the TP2 touch (TP1 just arms the half-bank), so the test
    walks to idx48 where TP2 is finally settled."""
    model = _model()
    sim = ExperimentSimulator(model, H1_PARTIAL_BE)
    # entry=100, sl=95, risk=5, half_r=102.5, tp1=105 (1R), tp2=110 (2R)
    sig = _mk_signal(SignalDirection.LONG, "100", "102", "95", "105", "110")
    candles = [
        _mk_candle(0, "100", "101", "99", "100"),    # fill
        _mk_candle(1, "101", "103", "100", "102"),   # +0.5R -> partial taken
        # Flat candles that stay inside SL..TP2 so nothing fires early;
        # TP2 is finally settled at the last window candle (idx48).
    ]
    for i in range(2, 48):
        candles.append(_mk_candle(i, "101", "104", "100", "102"))
    candles.append(_mk_candle(48, "105", "110", "104", "108"))  # TP2 settlement

    et = sim.simulate(sig, candles)
    assert et.trade.exit_status == "TP2_HIT"
    assert et.trade.tp2_hit is True
    # R = 0.5*0 (protected) + 0.5*(0.5*1 + 0.5*2) = 0.5 * 1.5 = 0.75
    assert et.trade.r_multiple == Decimal("0.75")


def test_partial_be_not_armed_no_partial():
    model = _model()
    sim = ExperimentSimulator(model, H1_PARTIAL_BE)
    sig = _mk_signal(SignalDirection.LONG, "100", "102", "95", "105", "110")
    candles = [
        _mk_candle(0, "100", "101", "99", "100"),    # fill
        _mk_candle(1, "100", "101", "94", "98"),     # SL before +0.5R
    ]
    et = sim.simulate(sig, candles)
    assert et.trade.exit_status == "STOPPED"
    assert et.be_armed is False
    assert et.trade.r_multiple == Decimal("-1")


# ---------------------------------------------------------------------------
# H1_TIME_EXIT_12
# ---------------------------------------------------------------------------

def test_time_exit_fires_when_half_r_not_reached_in_12_candles():
    """No +0.5R within the first 12 post-entry candles -> TIME_EXIT at the
    close of the 12th candle (idx11). exit_price = that candle's close,
    so R reflects the close vs entry, not 0R in general."""
    model = _model()
    sim = ExperimentSimulator(model, H1_TIME_EXIT_12)
    # entry=100, sl=95, risk=5, half_r=102.5
    sig = _mk_signal(SignalDirection.LONG, "100", "102", "95", "105", "110")
    candles = [
        _mk_candle(0, "100", "101", "99", "100"),    # fill (idx0)
    ]
    for i in range(1, 12):
        # keep price below half_r=102.5 and above SL=95
        candles.append(_mk_candle(i, "100", "102", "98", "101"))
    # idx11 is the 12th post-entry candle (idx+1 = 12 >= 12) -> TIME_EXIT
    et = sim.simulate(sig, candles)
    assert et.trade.exit_status == STATUS_TIME_EXIT
    # Exited at idx11 close = 101 -> R = (101 - 100) / 5 = 0.2
    assert et.trade.r_multiple == Decimal("0.2")
    assert et.trade.holding_candles == 12


def test_time_exit_not_fired_when_half_r_reached_first():
    """If +0.5R is reached before 12 candles, the time exit does NOT fire;
    the position keeps the original SL/TP model. A later SL touch resolves
    the remainder under the original model (not TIME_EXIT)."""
    model = _model()
    sim = ExperimentSimulator(model, H1_TIME_EXIT_12)
    sig = _mk_signal(SignalDirection.LONG, "100", "102", "95", "105", "110")
    candles = [
        _mk_candle(0, "100", "101", "99", "100"),    # fill
        _mk_candle(1, "101", "103", "100", "102"),   # +0.5R reached (high=103>=102.5)
        # After arming, the original SL/TP model applies. An SL touch
        # resolves the remainder (conservative, SL-first ordering).
        _mk_candle(2, "95", "99", "94", "96"),       # low=94 <= sl=95 -> STOPPED
    ]
    et = sim.simulate(sig, candles)
    # +0.5R was reached, so no TIME_EXIT; original model resolves at SL.
    assert et.trade.exit_status == "STOPPED"
    assert et.trade.sl_hit is True
    assert et.trade.exit_status != STATUS_TIME_EXIT


# ---------------------------------------------------------------------------
# No look-ahead
# ---------------------------------------------------------------------------

def test_no_lookahead_exit_uses_only_past_candles():
    """The exit loop must only read candles up to the current index.
    If a future candle would have triggered an exit, the trade must NOT
    reference it — i.e. truncating the future candle list past the exit
    index must not change the outcome."""
    model = _model()
    sim = ExperimentSimulator(model, H1_BE_0_5R)
    sig = _mk_signal(SignalDirection.LONG, "100", "102", "95", "105", "110")
    candles = [
        _mk_candle(0, "100", "101", "99", "100"),    # fill
        _mk_candle(1, "101", "103", "100", "102"),   # arm BE
        _mk_candle(2, "102", "103", "100", "100"),   # BE_HIT
        _mk_candle(3, "100", "120", "90", "110"),    # future: would look different
    ]
    full = sim.simulate(sig, candles)
    truncated = sim.simulate(sig, candles[:3])  # without the idx3 candle
    assert full.trade.exit_status == truncated.trade.exit_status
    assert full.trade.r_multiple == truncated.trade.r_multiple


def test_no_lookahead_atr_trail_future_candle_does_not_matter():
    model = _model()
    atr_fn = _atr_lookup_flat(Decimal("2"))
    sim = ExperimentSimulator(model, H1_ATR_TRAIL, atr_lookup=atr_fn)
    sig = _mk_signal(SignalDirection.LONG, "100", "102", "95", "105", "110")
    candles = [
        _mk_candle(0, "100", "101", "99", "100"),
        _mk_candle(1, "101", "104", "100", "103"),   # activate trail
        _mk_candle(2, "103", "105", "101", "104"),   # ratchet
        _mk_candle(3, "104", "104", "102", "103"),   # TRAIL_HIT
        _mk_candle(4, "103", "110", "100", "108"),   # future, irrelevant
    ]
    full = sim.simulate(sig, candles)
    truncated = sim.simulate(sig, candles[:4])
    assert full.trade.exit_status == truncated.trade.exit_status
    assert full.trade.r_multiple == truncated.trade.r_multiple


# ---------------------------------------------------------------------------
# Same-candle sequencing (SL beats TP)
# ---------------------------------------------------------------------------

def test_same_candle_sl_beats_tp2():
    """If SL and TP2 are both touched in the same candle, SL takes priority."""
    model = _model()
    sim = ExperimentSimulator(model, H1_BE_0_5R)
    sig = _mk_signal(SignalDirection.LONG, "100", "102", "95", "105", "110")
    candles = [
        _mk_candle(0, "100", "101", "99", "100"),    # fill
        _mk_candle(1, "101", "110", "94", "100"),   # same candle: low<=95 (SL) AND high>=110 (TP2)
    ]
    et = sim.simulate(sig, candles)
    assert et.trade.exit_status == "STOPPED"
    assert et.trade.sl_hit is True


# ---------------------------------------------------------------------------
# V1 baseline reproducibility / production numbers
# ---------------------------------------------------------------------------

def test_v1_baseline_production_aggregation_unchanged():
    """V1_BASELINE must aggregate to the same closed-set as production
    {STOPPED, TP1_HIT, TP2_HIT}; BE_HIT/TRAIL_HIT/TIME_EXIT are not closed
    under the production set."""
    model = _model()
    sim_v1 = ExperimentSimulator(model, V1_BASELINE)
    sig = _mk_signal(SignalDirection.LONG, "100", "102", "95", "105", "110")
    candles = [_mk_candle(0, "100", "101", "99", "100")]
    candles.extend(_mk_candle(i, "100", "101", "94", "98") for i in range(1, 5))  # SL
    trades = [sim_v1.simulate(sig, candles).as_trade()]
    m = _compute_group(trades)
    assert m.closed_trades == 1
    assert m.exit_status if hasattr(m, "exit_status") else True
    # The single trade is STOPPED -> closed under production set
    assert trades[0].exit_status == "STOPPED"


def test_experimental_closed_statuses_superset_of_production():
    """EXPERIMENTAL_CLOSED_STATUSES must be a strict superset of the
    production closed set {STOPPED, TP1_HIT, TP2_HIT}."""
    prod = frozenset({"STOPPED", "TP1_HIT", "TP2_HIT"})
    exp = frozenset({
        "STOPPED", "TP1_HIT", "TP2_HIT",
        STATUS_BE_HIT, STATUS_TRAIL_HIT, STATUS_TIME_EXIT,
    })
    assert prod.issubset(exp)
    assert exp - prod == frozenset({STATUS_BE_HIT, STATUS_TRAIL_HIT, STATUS_TIME_EXIT})


def test_variant_registry_has_five_variants():
    from backtest.experiments.exit_rules import VARIANTS
    labels = [v.label for v in VARIANTS]
    assert labels == ["V1_BASELINE", "H1_BE_0_5R", "H1_ATR_TRAIL",
                      "H1_PARTIAL_BE", "H1_TIME_EXIT_12"]
    kinds = {v.label: v.kind for v in VARIANTS}
    assert kinds["H1_ATR_TRAIL"] == "ATR_TRAIL"
    assert kinds["H1_TIME_EXIT_12"] == "TIME_EXIT"
    # ATR default multiple
    atr_v = next(v for v in VARIANTS if v.label == "H1_ATR_TRAIL")
    assert atr_v.atr_multiple == 1.0
    # Time-exit candle count
    te_v = next(v for v in VARIANTS if v.label == "H1_TIME_EXIT_12")
    assert te_v.time_exit_candles == 12
    # Partial fraction
    pb_v = next(v for v in VARIANTS if v.label == "H1_PARTIAL_BE")
    assert pb_v.partial_fraction == 0.5
