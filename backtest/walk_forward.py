"""
PHASE 4: Walk-forward evaluation.

Accepts arbitrary date ranges for TRAIN / VALIDATION / OUT-OF-SAMPLE and
runs the SAME BacktestEngine independently over each window so the three
segments are NEVER combined into one headline number. Out-of-sample is the
primary robustness reference.

No parameter optimization happens here — each window uses the current,
locked configuration (requirement #22).
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from backtest.engine import BacktestEngine, BacktestSignal


@dataclass
class WalkForwardConfig:
    """Arbitrary walk-forward windows (ISO date strings or datetime)."""
    train: Tuple[Optional[datetime], Optional[datetime]]
    validation: Tuple[Optional[datetime], Optional[datetime]]
    out_of_sample: Tuple[Optional[datetime], Optional[datetime]]


def _to_dt(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    s = str(value)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class WalkForwardWindow:
    label: str
    start: Optional[datetime]
    end: Optional[datetime]
    signals: List[BacktestSignal]
    trades: List = field(default_factory=list)


def run_walk_forward(
    dataset: Dict[str, "HistoricalDataset"],
    config: WalkForwardConfig,
    warmup_min: int = 0,
    execution_model=None,
) -> Dict[str, WalkForwardWindow]:
    """
    Run the SAME backtest engine over three isolated windows.

    Each window gets a FRESH engine (no state leakage across windows) so the
    out-of-sample result is not contaminated by train/validation history.

    Returns:
        {"train": WalkForwardWindow, "validation": ..., "out_of_sample": ...}
    """
    result: Dict[str, WalkForwardWindow] = {}
    segments = [
        ("train", config.train),
        ("validation", config.validation),
        ("out_of_sample", config.out_of_sample),
    ]
    for label, (start, end) in segments:
        engine = BacktestEngine(
            dataset=dataset,
            start=_to_dt(start),
            end=_to_dt(end),
        )
        signals_by_symbol = engine.run(warmup_min=warmup_min)
        all_signals: List[BacktestSignal] = []
        for sym, sigs in signals_by_symbol.items():
            all_signals.extend(sigs)
        result[label] = WalkForwardWindow(
            label=label,
            start=_to_dt(start),
            end=_to_dt(end),
            signals=all_signals,
        )
    return result
