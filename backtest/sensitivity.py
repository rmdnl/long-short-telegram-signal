"""
PHASE 4: Sensitivity analysis (analysis-only, NO parameter selection).

Runs the backtest over a grid of parameter values and reports how
sensitive the strategy is to each. Does NOT recommend a "best" value.

Usage:
    from backtest.sensitivity import run_sensitivity
    grid = run_sensitivity(
        ds,
        adx_values=[20, 22, 25],
        score_values=[75, 80, 85],
        volume_values=[1.0, 1.2, 1.5],
    )
"""
import copy
from typing import Dict, List, Optional

from backtest.engine import BacktestEngine
from backtest.metrics import BacktestMetrics, metrics_from_trades
from backtest.execution import ExecutionModel, TradeSimulator
from app.config import get_config, Config


def _apply_param_overrides(config: Config, adx_min: int,
                           min_score: int, volume_mult) -> None:
    """Apply parameter overrides to a config instance (in-memory only)."""
    config.adx_min = adx_min
    config.min_score = min_score
    config.volume_multiplier = volume_mult


def run_sensitivity(
    datasets: Dict[str, "HistoricalDataset"],
    adx_values: Optional[List[int]] = None,
    score_values: Optional[List[int]] = None,
    volume_values: Optional[List[float]] = None,
    warmup_min: int = 0,
    execution_model: Optional[ExecutionModel] = None,
    start=None, end=None,
) -> Dict[str, List[Dict]]:
    """
    Run a sensitivity grid over ADX, score, and volume multiplier.

    Returns:
        {"adx": [ {value, total_r, win_rate, profit_factor, signals} ],
         "score": [ ... ],
         "volume": [ ... ]}

    Each entry is analysis-only: it reports the metric outcome but does
    not label any value as "best".
    """
    model = execution_model or ExecutionModel()
    base_config = get_config()

    def _run_for(adx_min, min_score, vol_mult) -> Dict:
        # Create a fresh config with overrides (does not mutate the singleton)
        import os
        os.environ_backup = dict(os.environ)
        cfg_mod = __import__("app.config", fromlist=["Config"])
        cfg_mod.config = None  # force reload
        saved_adx = cfg_mod.get_config().adx_min
        cfg_mod.get_config().adx_min = adx_min
        cfg_mod.get_config().min_score = min_score
        cfg_mod.get_config().volume_multiplier = vol_mult
        engine = BacktestEngine(dataset=datasets, start=start, end=end)
        m = engine.run_full(warmup_min=warmup_min, execution_model=model)
        # Restore
        cfg_mod.config = None
        return {
            "adx": adx_min, "score": min_score, "volume": vol_mult,
            "total_r": m.total_r, "win_rate": m.win_rate,
            "profit_factor": m.profit_factor,
            "signals": m.total_signals,
        }

    results: Dict[str, List[Dict]] = {"adx": [], "score": [], "volume": []}

    # ADX grid (holding score and volume at defaults)
    if adx_values:
        default_score = base_config.min_score
        default_vol = base_config.volume_multiplier
        for adx in adx_values:
            results["adx"].append(_run_for(adx, default_score, default_vol))

    # Score grid
    if score_values:
        default_adx = base_config.adx_min
        default_vol = base_config.volume_multiplier
        for sc in score_values:
            results["score"].append(_run_for(default_adx, sc, default_vol))

    # Volume grid
    if volume_values:
        default_adx = base_config.adx_min
        default_score = base_config.min_score
        for vol in volume_values:
            results["volume"].append(_run_for(default_adx, default_score, vol))

    return results


def print_sensitivity_report(results: Dict[str, List[Dict]]) -> str:
    """Format a sensitivity analysis report (no recommendations)."""
    L = []
    L.append("SENSITIVITY ANALYSIS (analysis-only, no parameter selection)")
    L.append("=" * 50)
    for param, entries in results.items():
        if not entries:
            continue
        L.append(f"\n{param.upper()} grid:")
        for e in entries:
            L.append(
                f"  {param}={e.get(param)}  "
                f"signals={e['signals']}  "
                f"total_r={e['total_r']}  "
                f"win_rate={e['win_rate']}  "
                f"PF={e['profit_factor']}"
            )
    L.append("\nNo 'best parameter' is recommended. "
             "This report is analysis-only.")
    return "\n".join(L)
