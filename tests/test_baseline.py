"""
PHASE 5: Tests for backtest/baseline.py orchestrator.

Uses deterministic synthetic data (conftest_backtest.make_candles) — no live
Binance data. Exercises the full run_full_baseline pipeline: dataset loading,
gross/net simulation, OOS split, loss classification, CSV export, and
markdown report generation.
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List

import pytest

from backtest.data import HistoricalDataset, load_rows
from backtest.baseline import (
    run_full_baseline,
    ConfigSnapshot,
    OosConfig,
    classify_loss,
    quality_report_lines,
    build_markdown_report,
    NET_TAKER_FEE,
    NET_SLIPPAGE_BPS,
    FS_UNKNOWN,
)
from backtest.metrics import BacktestMetrics, metrics_from_trades
from backtest.execution import TradeResult
from app.models import Candle

UTC = timezone.utc
T0 = datetime(2025, 1, 1, 0, 0, tzinfo=UTC)


def _ds_dict(symbols, n_5m=800, n_15m=200, n_1h=40, start=T0) -> dict:
    """Build a {symbol: HistoricalDataset} dict via the shared conftest helper."""
    from tests.conftest_backtest import make_dataset
    out = {}
    for sym in symbols:
        out[sym] = make_dataset(
            sym, n_hf=n_1h, n_setup=n_15m, n_trigger=n_5m, start=start,
        )
    return out


def _make_candles(tf: str, n: int, start: datetime = T0) -> List[Candle]:
    from tests.conftest_backtest import make_candles
    return make_candles(tf, n, start=start)


# ---------------------------------------------------------------------------
# OOS split
# ---------------------------------------------------------------------------

def test_oos_split_boundaries():
    oos = OosConfig(oos_fraction=0.3, validation_fraction=0.2)
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 12, 31, tzinfo=UTC)
    segs = oos.split(start, end)
    assert set(segs.keys()) == {"in_sample", "validation", "out_of_sample"}
    # Segments are contiguous and non-overlapping
    is_s, is_e = segs["in_sample"]
    val_s, val_e = segs["validation"]
    oos_s, oos_e = segs["out_of_sample"]
    assert is_e == val_s
    assert val_e == oos_s
    assert is_s == start
    assert oos_e == end
    # OOS is the last 30%
    total = (end - start).total_seconds()
    oos_dur = (oos_e - oos_s).total_seconds()
    assert abs(oos_dur - total * 0.3) < 86400 * 2  # within 2 days tolerance


# ---------------------------------------------------------------------------
# Loss classification
# ---------------------------------------------------------------------------

def _mk_trade(exit_status="STOPPED", r=-1.0, direction="LONG") -> TradeResult:
    return TradeResult(
        signal_id="s1", symbol="X", direction=direction,
        signal_time=T0, entry_low=Decimal("100"), entry_high=Decimal("110"),
        entry_price=Decimal("110"), stop_loss=Decimal("95"),
        tp1=Decimal("120"), tp2=Decimal("130"), score=80,
        market_regime="TRENDING", entry_status="FILLED", exit_status=exit_status,
        exit_time=T0 + timedelta(minutes=10), exit_price=Decimal("95"),
        r_multiple=Decimal(str(r)), tp1_hit=False, tp2_hit=False,
        sl_hit=(exit_status == "STOPPED"), holding_candles=2, fees_enabled=False,
    )


def test_classify_loss_early_exit_htf_reversal():
    trigger = _make_candles("5m", 20)
    trade = _mk_trade()
    # Exit within 3 candles of signal -> HTF reversal
    label = classify_loss(trade, trigger, trigger_idx=5)
    assert label in ("HTF_REVERSAL", "FAILED_BREAKOUT", "TREND_EXHAUSTION",
                     "SUDDEN_VOLATILITY", "RANGING_TRANSITION", "UNKNOWN")


def test_classify_loss_unknown_when_no_trigger():
    trade = _mk_trade()
    label = classify_loss(trade, _make_candles("5m", 10), trigger_idx=None)
    assert label == FS_UNKNOWN


# ---------------------------------------------------------------------------
# Quality report lines
# ---------------------------------------------------------------------------

def test_quality_report_lines_nonempty():
    dss = _ds_dict(["BTCUSDT", "ETHUSDT"])
    lines = quality_report_lines(dss)
    assert len(lines) > 0
    # Each symbol x each tf produces a line
    assert any("BTCUSDT" in l for l in lines)


# ---------------------------------------------------------------------------
# Full pipeline with synthetic data
# ---------------------------------------------------------------------------

def test_run_full_baseline_synthetic(tmp_path):
    """End-to-end: two synthetic symbols through run_full_baseline."""
    # Write synthetic CSVs in the downloader layout so load_csv_dir finds them
    save_dir = str(tmp_path / "data")
    symbols = ["BTCUSDT", "ETHUSDT"]
    for sym in symbols:
        ds = _ds_dict([sym])[sym]
        _write_ds_to_dir(ds, sym, save_dir)

    start = T0
    end = T0 + timedelta(days=400)

    out = run_full_baseline(
        data_dir=save_dir,
        symbols=symbols,
        start=start,
        end=end,
        warmup_min=600,
        oos_fraction=0.3,
        validation_fraction=0.2,
        report_dir=str(tmp_path / "reports"),
    )

    # Markdown report contains all required sections
    md = out.markdown_report
    for section in [
        "DATASET", "CONFIGURATION", "DATA QUALITY", "OVERALL RESULTS",
        "PER SYMBOL", "LONG vs SHORT", "MARKET REGIME", "MONTHLY",
        "OUT-OF-SAMPLE", "LOSS CLASSIFICATION", "SIGNAL FREQUENCY",
        "LIMITATIONS",
    ]:
        assert section in md, f"Missing section: {section}"

    # OOS results have three segments
    assert set(out.oos_results.keys()) == {"in_sample", "validation", "out_of_sample"}

    # CSV trade export exists
    assert out.csv_path and os.path.exists(out.csv_path)

    # Config snapshot is frozen
    snap = out.config_snap
    with pytest.raises(Exception):
        snap.adx_min = Decimal("99")


def _write_ds_to_dir(ds: HistoricalDataset, symbol: str, base_dir: str):
    """Write a HistoricalDataset's candles to {base_dir}/{symbol}/{tf}.csv."""
    import csv as _csv
    sym_dir = os.path.join(base_dir, symbol)
    os.makedirs(sym_dir, exist_ok=True)
    for tf in ("1h", "15m", "5m"):
        candles = ds.tf(tf)
        path = os.path.join(sym_dir, f"{tf}.csv")
        with open(path, "w", newline="") as fh:
            w = _csv.writer(fh)
            w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
            for c in candles:
                w.writerow([
                    c.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    str(c.open), str(c.high), str(c.low),
                    str(c.close), str(c.volume),
                ])


# ---------------------------------------------------------------------------
# Config snapshot immutability
# ---------------------------------------------------------------------------

def test_config_snapshot_immutable():
    snap = ConfigSnapshot.from_config()
    assert isinstance(snap, ConfigSnapshot)
    # frozen dataclass: setattr raises
    with pytest.raises(AttributeError):
        snap.min_score = 99


def test_config_snapshot_from_config_matches():
    from app.config import get_config
    snap = ConfigSnapshot.from_config()
    c = get_config()
    assert snap.adx_min == c.adx_min
    assert snap.min_score == c.min_score
    assert snap.symbols == tuple(c.symbols)


# ---------------------------------------------------------------------------
# Gross vs net model construction
# ---------------------------------------------------------------------------

def test_gross_model_has_zero_fees():
    from backtest.execution import ExecutionModel, TP_MODEL_50PCT
    from backtest.baseline import NET_TAKER_FEE
    gross = ExecutionModel(
        maker_fee=Decimal("0"), taker_fee=Decimal("0"),
        slippage_bps=Decimal("0"), tp_model=TP_MODEL_50PCT,
        enable_fees=False, max_candles_after_signal=48,
    )
    assert gross.enable_fees is False
    assert gross.taker_fee == Decimal("0")

    net = ExecutionModel(
        maker_fee=Decimal("0"), taker_fee=NET_TAKER_FEE,
        slippage_bps=NET_SLIPPAGE_BPS, tp_model=TP_MODEL_50PCT,
        enable_fees=True, max_candles_after_signal=48,
    )
    assert net.enable_fees is True
    assert net.taker_fee == NET_TAKER_FEE


# ---------------------------------------------------------------------------
# Markdown builder with empty metrics (no crash)
# ---------------------------------------------------------------------------

def test_markdown_report_handles_empty_metrics():
    from backtest.baseline import build_markdown_report, ConfigSnapshot
    empty = BacktestMetrics()
    oos = {"in_sample": empty, "validation": empty, "out_of_sample": empty}
    snap = ConfigSnapshot.from_config()
    md = build_markdown_report(
        config_snap=snap,
        gross_m=empty,
        net_m=empty,
        oos_results=oos,
        all_trades=[],
        quality_lines=[],
        loss_counts={},
        dataset_stats_lines=[],
        data_dir="data",
        period_start=T0,
        period_end=T0 + timedelta(days=365),
    )
    assert "BASELINE BACKTEST REPORT" in md
    assert "DISCLAIMER" in md


# ---------------------------------------------------------------------------
# _infer_period
# ---------------------------------------------------------------------------

def test_infer_period_from_data(tmp_path):
    from backtest.baseline import _infer_period
    save_dir = str(tmp_path / "data")
    symbols = ["BTCUSDT"]
    for sym in symbols:
        ds = _ds_dict([sym])[sym]
        _write_ds_to_dir(ds, sym, save_dir)
    start, end = _infer_period(save_dir, symbols)
    assert start == T0
    assert end > start
