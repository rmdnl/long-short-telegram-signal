"""
PHASE 6: Forensics runner.

Reconstructs the baseline trade list (identical to the PHASE 5 baseline run),
enriches each trade with per-signal diagnostics (ADX/RSI/volume/ATR/EMA/MAE/MFE/failure-sequence),
and writes:

- reports/forensics_trades.csv   (per-trade diagnostic CSV)
- reports/forensics.md           (forensic report)

No strategy change. No parameter tuning. No execution. No Telegram.

Usage:
    python -m backtest.forensics_run
"""
import argparse
import logging
import os
from datetime import datetime

from app.config import get_config
from app.trading_guard import assert_no_execution_code

from backtest.data import load_csv_dir
from backtest.baseline import (
    DEFAULT_SYMBOLS,
    DEFAULT_WARMUP_MIN,
    run_baseline,
    OosConfig,
)
from backtest.execution import ExecutionModel, TP_MODEL_50PCT
from backtest import forensics
from backtest.forensics import (
    compute_forensics_trades,
    export_forensics_csv,
    build_forensics_markdown,
    oos_split_labels,
)

logger = logging.getLogger(__name__)

GROSS_MODEL = ExecutionModel(
    maker_fee=__import__("decimal").Decimal("0"),
    taker_fee=__import__("decimal").Decimal("0"),
    slippage_bps=__import__("decimal").Decimal("0"),
    tp_model=TP_MODEL_50PCT,
    enable_fees=False,
    max_candles_after_signal=48,
)


def run_forensics(
    data_dir: str = "data",
    report_dir: str = "reports",
    symbols=None,
    start: datetime = None,
    end: datetime = None,
    warmup_min: int = DEFAULT_WARMUP_MIN,
    oos_fraction: float = 0.3,
    validation_fraction: float = 0.2,
) -> dict:
    cfg = get_config()
    syms = symbols or DEFAULT_SYMBOLS

    from backtest.baseline import _infer_period
    if start is None or end is None:
        start, end = _infer_period(data_dir, syms)

    datasets = {}
    for sym in syms:
        ds = load_csv_dir(data_dir, sym)
        if not ds.tf("5m"):
            logger.warning(f"No 5m data for {sym}, skipping")
            continue
        datasets[sym] = ds

    # Reconstruct the exact baseline trade list (gross, no fees)
    _gross_m, all_trades = run_baseline(datasets, GROSS_MODEL, start, end, warmup_min)

    # Enrich
    ft = compute_forensics_trades(all_trades, datasets, cfg)

    # OOS windows
    oos_windows = oos_split_labels(ft, start, end, oos_fraction, validation_fraction)

    # CSV
    os.makedirs(report_dir, exist_ok=True)
    csv_path = os.path.join(report_dir, "forensics_trades.csv")
    export_forensics_csv(ft, csv_path)

    # Markdown
    md = build_forensics_markdown(ft, cfg, oos_windows)
    md_path = os.path.join(report_dir, "forensics.md")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(md)

    return {
        "total_signals": len(ft),
        "csv_path": csv_path,
        "md_path": md_path,
    }


def cli_main() -> None:
    assert_no_execution_code()
    logger.info("Trading-execution guard passed: 0 violations")

    parser = argparse.ArgumentParser(description="PHASE 6 forensics runner")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--report-dir", default="reports")
    parser.add_argument("--symbols", default=None)
    parser.add_argument("--warmup", type=int, default=DEFAULT_WARMUP_MIN)
    args = parser.parse_args()

    symbols = None
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    out = run_forensics(
        data_dir=args.data_dir,
        report_dir=args.report_dir,
        symbols=symbols,
        warmup_min=args.warmup,
    )
    print(f"Forensics complete: {out['total_signals']} trades")
    print(f"CSV: {out['csv_path']}")
    print(f"MD:  {out['md_path']}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cli_main()
