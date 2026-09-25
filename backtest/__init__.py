"""
PHASE 4: Backtest + walk-forward validation engine.

Reusable decision path (NO logic duplication with live):
- data_validation.cutoff_candles / sync_timeframe  (no-look-ahead slicing)
- app.indicators + shared indicator bundle          (same indicator math)
- SignalEngine.generate_signal(...)                 (the LIVE strategy engine)
- risk_engine.* (inside the engine)                 (exact entry/SL/TP)
- signal_filter.check_cooldown / generate_signal_id (same dedup + cooldown)

The backtest only adds: historical data, a deterministic execution model,
and metrics/reporting. It never invents new strategy rules.

No trading execution. SIGNAL-ONLY by construction.
"""
from backtest.data import HistoricalDataset, DataQualityReport, load_csv, load_json
from backtest.align import AlignedWindow, align_window, WindowReport
from backtest.engine import BacktestEngine, BacktestSignal
from backtest.execution import ExecutionModel, TradeResult, TP_MODEL_50PCT, TP_MODEL_ALL
from backtest.metrics import BacktestMetrics, metrics_from_trades
from backtest.walk_forward import WalkForwardConfig, WalkForwardWindow, run_walk_forward
from backtest.report import render_report, export_csv
from backtest.sensitivity import run_sensitivity, print_sensitivity_report
from backtest.indicators_shared import compute_indicators, indicators_ready

__all__ = [
    "HistoricalDataset", "DataQualityReport", "load_csv", "load_json",
    "AlignedWindow", "align_window", "WindowReport",
    "BacktestEngine", "BacktestSignal",
    "ExecutionModel", "TradeResult", "TP_MODEL_50PCT", "TP_MODEL_ALL",
    "BacktestMetrics", "metrics_from_trades",
    "WalkForwardConfig", "WalkForwardWindow", "run_walk_forward",
    "render_report", "export_csv",
    "run_sensitivity", "print_sensitivity_report",
    "compute_indicators", "indicators_ready",
]
