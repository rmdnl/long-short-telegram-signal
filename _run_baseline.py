"""Run the full PHASE 5 baseline and print a compact summary."""
import logging
import sys
import time

logging.basicConfig(level=logging.WARNING)

from backtest.baseline import run_full_baseline

t0 = time.time()
out = run_full_baseline(
    data_dir="data",
    report_dir="reports",
    warmup_min=600,
    oos_fraction=0.3,
    validation_fraction=0.2,
)
elapsed = time.time() - t0

g = out.gross_metrics
n = out.net_metrics

print(f"Baseline run completed in {elapsed:.1f}s")
print(f"Signals: {g.total_signals}  Filled: {g.filled}  Closed: {g.closed_trades}")
print(f"Win rate: {g.win_rate}  Expectancy: {g.expectancy}  PF: {g.profit_factor}")
print(f"Gross total R: {g.total_r}  Net total R: {n.total_r}")
print(f"Max DD (gross R): {g.max_drawdown_r}  Net: {n.max_drawdown_r}")
print(f"OOS total R: {out.oos_results['out_of_sample'].total_r}  "
      f"(in-sample: {out.oos_results['in_sample'].total_r}, "
      f"validation: {out.oos_results['validation'].total_r})")
print(f"CSV: {out.csv_path}")
print(f"MD:  reports/baseline_summary.md")
