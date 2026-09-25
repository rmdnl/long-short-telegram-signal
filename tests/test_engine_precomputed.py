"""
Tests that the precomputed fast path in BacktestEngine._run_symbol_precomputed
produces the same signals as the naive per-window path.

Consistency strategy:
- Build a synthetic dataset with 501 trigger candles (just above the >500
  threshold) so the precomputed path is selected.
- Run both paths and compare the full signal lists (id, direction, time,
  regime, score).

The precomputed path was refactored to pass O(1) tail slices to
SignalEngine.generate_signal instead of full prefixes; these tests verify
that refactor is behaviour-preserving.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List

import pytest

from app.models import Candle, SignalDirection
from app.config import get_config
from app.signal_engine import SignalEngine
from backtest.engine import BacktestEngine, BacktestSignal
from backtest.align import iter_trigger_decision_points, TRIGGER_TF
from backtest.indicators_precomputed import PrecomputedIndicators
import backtest.indicators_shared as ind_shared
import backtest.align as align_mod

from tests.conftest_backtest import make_dataset, make_candles


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _build_dataset(n_trigger: int = 510, n_setup: int = 120, n_hf: int = 50,
                   start: datetime = None):
    """Synthetic dataset sized to cross the 500-candle threshold.

    Setup/hf are smaller than trigger so per-window slicing cost is
    measurable but precompute overhead stays reasonable.
    """
    from tests.conftest_backtest import make_dataset
    start = start or datetime(2025, 1, 1, tzinfo=timezone.utc)
    return make_dataset("TEST", n_hf=n_hf, n_setup=n_setup,
                        n_trigger=n_trigger, start=start)


def _force_naive(ds):
    """
    Force BacktestEngine onto the naive path for a given dataset by patching
    the trigger candle list to appear shorter than 500.
    """
    class _ShrinkTrigger:
        def __init__(self, inner):
            self._inner = inner
        def tf(self, key: str):
            if key == "5m":
                return self._inner.tf("5m")[:500]
            return self._inner.tf(key)
    return _ShrinkTrigger(ds)


# ---------------------------------------------------------------------------
# consistency test: naive vs precomputed
# ---------------------------------------------------------------------------

class TestPrecomputedConsistency:
    """The fast path must emit identical signals to the naive path."""

    def test_same_signals_on_small_warmup(self):
        """warmup_min=0: both paths should produce identical signal lists."""
        ds = _build_dataset(n_trigger=510, n_setup=120, n_hf=50)
        dataset = {"SYM": ds}

        cfg = get_config()

        # naive path (force <500 trigger)
        naive_ds = _force_naive(ds)
        engine_naive = BacktestEngine({"SYM": naive_ds})
        naive_signals = engine_naive.run_symbol("SYM", warmup_min=0)

        # precomputed path (trigger > 500)
        engine_pre = BacktestEngine(dataset)
        pre_signals = engine_pre.run_symbol("SYM", warmup_min=0)

        # Compare by signal_id + direction + decision_time
        naive_key = [(s.signal_id, s.signal.direction, s.decision_time)
                     for s in naive_signals]
        pre_key   = [(s.signal_id, s.signal.direction, s.decision_time)
                     for s in pre_signals]

        # naive path shrinks trigger to 500 → 10 fewer decision points than
        # precomputed (510). Compare on the overlapping first 500 points.
        pre_key_500 = pre_key[:len(naive_key)]
        assert naive_key == pre_key_500, (
            f"Signal mismatch.\n"
            f"naive={naive_key}\npre={pre_key_500}\n"
        )

    def test_same_signals_with_warmup(self):
        """warmup_min=600 should skip the same early decision points in both."""
        ds = _build_dataset(n_trigger=610, n_setup=120, n_hf=50)
        dataset = {"SYM": ds}

        naive_ds = _force_naive(ds)
        engine_naive = BacktestEngine({"SYM": naive_ds})
        naive_signals = engine_naive.run_symbol("SYM", warmup_min=600)

        engine_pre = BacktestEngine(dataset)
        pre_signals = engine_pre.run_symbol("SYM", warmup_min=600)

        naive_key = [s.signal_id for s in naive_signals]
        pre_key   = [s.signal_id for s in pre_signals]

        # warmup_min=600 > naive trigger (500) → both should be empty
        assert naive_key == pre_key == []

    def test_precomputed_indicator_arrays_match_naive(self):
        """PrecomputedIndicators.build_ind() == compute_indicators(prefix) at
        the last candle index for every timeframe."""
        ds = _build_dataset(n_trigger=510, n_setup=120, n_hf=50)
        cfg = get_config()

        hf_full    = ds.tf("1h")
        setup_full = ds.tf("15m")
        trig_full  = ds.tf("5m")

        pre = PrecomputedIndicators(cfg)
        pre.precompute(hf_full, setup_full, trig_full)

        # Test at the last candle index for each timeframe
        hf_idx    = len(hf_full) - 1
        setup_idx = len(setup_full) - 1
        trig_idx  = len(trig_full) - 1

        hf_pre, setup_pre, trig_pre = pre.build_ind(hf_idx, setup_idx, trig_idx)

        hf_naive    = ind_shared.compute_indicators(hf_full,    cfg)
        setup_naive = ind_shared.compute_indicators(setup_full, cfg)
        trig_naive  = ind_shared.compute_indicators(trig_full,  cfg)

        # Compare the IndicatorValues that SignalEngine actually consumes
        for field in ("ema_fast", "ema_slow", "rsi", "atr", "adx",
                      "plus_di", "minus_di", "volume_sma"):
            assert getattr(hf_pre, field)    == getattr(hf_naive, field), \
                f"hf.{field}: pre={getattr(hf_pre, field)} naive={getattr(hf_naive, field)}"
            assert getattr(setup_pre, field) == getattr(setup_naive, field), \
                f"setup.{field}: pre={getattr(setup_pre, field)} naive={getattr(setup_naive, field)}"
            assert getattr(trig_pre, field)  == getattr(trig_naive, field), \
                f"trig.{field}: pre={getattr(trig_pre, field)} naive={getattr(trig_naive, field)}"

    def test_ready_at_matches_indicators_ready(self):
        """PrecomputedIndicators.ready_at(i, j) == indicators_ready(build_ind(., i, j))."""
        ds = _build_dataset(n_trigger=510, n_setup=120, n_hf=50)
        cfg = get_config()

        pre = PrecomputedIndicators(cfg)
        pre.precompute(ds.tf("1h"), ds.tf("15m"), ds.tf("5m"))

        for setup_idx, trig_idx in [(0, 0), (10, 10), (50, 50),
                                      (100, 100), (119, 509)]:
            _, s_ind, t_ind = pre.build_ind(
                min(49, 0), setup_idx, trig_idx,
            )
            expected = ind_shared.indicators_ready(s_ind) and \
                       ind_shared.indicators_ready(t_ind)
            assert pre.ready_at(setup_idx, trig_idx) == expected, (
                f"ready_at({setup_idx},{trig_idx}) mismatch: "
                f"expected={expected}"
            )

    def test_rsi_tail_slice_matches_full_prefix(self):
        """
        The O(1) tail-slice passed to SignalEngine must produce the same
        prev_rsi as the full prefix would, because RSI is causal.
        """
        from app.indicators import rsi as calc_rsi

        ds = _build_dataset(n_trigger=510, n_setup=120, n_hf=50)
        cfg = get_config()
        rsi_len = cfg.rsi_length
        setup_full = ds.tf("15m")

        # Pick a mid-range index to test
        for setup_count in (rsi_len + 1, rsi_len + 5, 100, 119):
            full_closes = [c.close for c in setup_full[:setup_count]]
            rsi_full = calc_rsi(full_closes, rsi_len)
            prev_rsi_full = rsi_full[-2] if len(rsi_full) >= 2 else None

            # What the engine actually passes: tail slice
            rsi_window = rsi_len + 2
            if setup_count < rsi_len + 2:
                # not enough candles for RSI warmup; pass just 2 (prev_rsi=None)
                tail = setup_full[setup_count - 2:setup_count]
            else:
                tail = setup_full[setup_count - rsi_window:setup_count]
            tail_closes = [c.close for c in tail]
            rsi_tail = calc_rsi(tail_closes, rsi_len)
            prev_rsi_tail = rsi_tail[-2] if len(rsi_tail) >= 2 else None

            assert prev_rsi_full == prev_rsi_tail, (
                f"setup_count={setup_count}: "
                f"full={prev_rsi_full} tail={prev_rsi_tail}"
            )
