"""
PHASE 4: Walk-forward boundary tests.

Uses deterministic synthetic data. Walk-forward must keep TRAIN /
VALIDATION / OUT-OF-SAMPLE strictly isolated (no cross-window state)
and respect the configured arbitrary date ranges.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from backtest.data import load_rows
from backtest.engine import BacktestEngine
from backtest.walk_forward import WalkForwardConfig, run_walk_forward
from tests.conftest_backtest import make_candles, make_dataset, _rows_from, BASE

UTC = timezone.utc


def _datasets(symbol="WF"):
    """Return the {symbol: HistoricalDataset} mapping run_walk_forward expects."""
    return {symbol: make_dataset(symbol, n_hf=700, n_setup=700, n_trigger=700)}


def test_walk_forward_windows_are_isolated():
    """Each window runs a FRESH engine; no signal leaks across windows."""
    ds = _datasets()
    cfg = WalkForwardConfig(
        train=(BASE, BASE + timedelta(days=30)),
        validation=(BASE + timedelta(days=30), BASE + timedelta(days=60)),
        out_of_sample=(BASE + timedelta(days=60), BASE + timedelta(days=90)),
    )
    result = run_walk_forward(ds, cfg, warmup_min=0)
    assert set(result.keys()) == {"train", "validation", "out_of_sample"}

    # No signal emitted by one window may share a signal_id with another
    ids = {w.label: {s.signal_id for s in w.signals} for w in result.values()}
    overlap_tv = ids["train"] & ids["validation"]
    overlap_vs = ids["validation"] & ids["out_of_sample"]
    overlap_ts = ids["train"] & ids["out_of_sample"]
    assert not overlap_tv
    assert not overlap_vs
    assert not overlap_ts


def test_walk_forward_respects_date_bounds():
    """Signals in each window must have trigger times within the window."""
    ds = _datasets()
    train_end = BASE + timedelta(days=30)
    oos_start = BASE + timedelta(days=60)
    oos_end = BASE + timedelta(days=90)
    cfg = WalkForwardConfig(
        train=(BASE, train_end),
        validation=(train_end, oos_start),
        out_of_sample=(oos_start, oos_end),
    )
    result = run_walk_forward(ds, cfg, warmup_min=0)
    for s in result["train"].signals:
        assert s.decision_time <= train_end
    for s in result["out_of_sample"].signals:
        assert oos_start < s.decision_time <= oos_end


def test_walk_forward_out_of_sample_is_primary():
    """OOS result is computed independently and must not include train data."""
    ds = _datasets()
    cfg = WalkForwardConfig(
        train=(BASE, BASE + timedelta(days=10)),
        validation=(BASE + timedelta(days=10), BASE + timedelta(days=20)),
        out_of_sample=(BASE + timedelta(days=20), BASE + timedelta(days=30)),
    )
    result = run_walk_forward(ds, cfg, warmup_min=0)
    oos = result["out_of_sample"]
    assert oos.start == BASE + timedelta(days=20)
    assert oos.end == BASE + timedelta(days=30)
    # OOS signal times strictly inside [20d, 30d]
    for s in oos.signals:
        assert BASE + timedelta(days=20) < s.decision_time <= BASE + timedelta(days=30)


def test_walk_forward_accepts_iso_string_ranges():
    """WalkForwardConfig accepts ISO strings, not just datetimes."""
    ds = _datasets()
    start = "2024-01-01"
    end = "2024-01-15"
    cfg = WalkForwardConfig(
        train=(start, end),
        validation=(end, "2024-02-01"),
        out_of_sample=("2024-02-01", "2024-03-01"),
    )
    result = run_walk_forward(ds, cfg, warmup_min=0)
    assert result["out_of_sample"].start == datetime(2024, 2, 1, tzinfo=UTC)
    assert result["out_of_sample"].end == datetime(2024, 3, 1, tzinfo=UTC)
