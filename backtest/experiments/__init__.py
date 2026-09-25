"""
PHASE 7: Controlled exit-management experiments.

This sub-package is an ISOLATED experimental layer. It reuses:
- the same historical datasets (backtest.data)
- the same live SignalEngine driven by BacktestEngine (backtest.engine)
- the same OOS split (backtest.baseline.OosConfig)
- the same entry-fill model and conservative same-candle rules
  (backtest.execution constants / entry logic)

It ONLY varies the post-entry EXIT MANAGEMENT. Production strategy files
(app/strategy.py, app/signal_engine.py, app/risk_engine.py, app/signal_filter.py)
and the baseline artifacts are NOT modified here.

No trading execution. No new indicators. No parameter grid search.
"""
from backtest.experiments.exit_rules import (
    Variant,
    VARIANTS,
    make_variant,
    V1_BASELINE,
    H1_BE_0_5R,
    H1_ATR_TRAIL,
    H1_PARTIAL_BE,
    H1_TIME_EXIT_12,
)
from backtest.experiments.simulator import ExperimentSimulator

__all__ = [
    "Variant",
    "VARIANTS",
    "make_variant",
    "V1_BASELINE",
    "H1_BE_0_5R",
    "H1_ATR_TRAIL",
    "H1_PARTIAL_BE",
    "H1_TIME_EXIT_12",
    "ExperimentSimulator",
]
