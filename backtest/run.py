"""
PHASE 4 CLI runner for the backtest engine.

Usage:
    python -m backtest.run --data-dir data/BTCUSDT --symbol BTCUSDT \
        [--start 2024-01-01] [--end 2026-01-01] [--warmup 600]
        [--tp-model TP1_50PCT] [--fees] [--taker 0.0004] [--slippage-bps 2]
        [--max-hold 48] [--csv out/BTCUSDT_trades.csv]
        [--walk-forward train=2024-01-01,2024-12-31 validation=2025-01-01,2025-06-30 oos=2025-07-01,2025-12-31]

Loads a symbol's 1H/15M/5M CSV dataset, runs the SAME live signal engine,
simulates execution deterministically, and prints + optionally exports a
report and per-trade CSV.

No trading execution. SIGNAL-ONLY by construction.
"""
import argparse
import os
from datetime import datetime, timezone
from decimal import Decimal

from backtest.data import load_csv
from backtest.engine import BacktestEngine
from backtest.execution import ExecutionModel, TradeSimulator, TP_MODEL_50PCT, TP_MODEL_ALL
from backtest.metrics import metrics_from_trades
from backtest.report import render_report, export_csv
from backtest.walk_forward import WalkForwardConfig, run_walk_forward


def _to_dt(s: str):
    if s is None:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_range(spec: str):
    """Parse 'start,end' (either may be empty) -> (start_dt, end_dt)."""
    start_s, _, end_s = spec.partition(",")
    return _to_dt(start_s.strip() or None), _to_dt(end_s.strip() or None)


def build_model(args) -> ExecutionModel:
    return ExecutionModel(
        maker_fee=Decimal("0"),
        taker_fee=Decimal(str(args.taker)),
        slippage_bps=Decimal(str(args.slippage_bps)),
        tp_model=TP_MODEL_ALL if args.tp_model == "TP1_ALL" else TP_MODEL_50PCT,
        enable_fees=args.fees,
        max_candles_after_signal=args.max_hold,
    )


def _simulate_signals(ds, signals, model) -> list:
    """Run TradeSimulator over a signal list and return TradeResult rows."""
    sim = TradeSimulator(model)
    trigger = ds.tf("5m")
    ts_list = [c.timestamp for c in trigger]
    trades = []
    for bs in signals:
        try:
            idx = ts_list.index(bs.signal.trigger_candle_time)
        except ValueError:
            continue
        future = trigger[idx + 1:]
        trades.append(sim.simulate(bs.signal, future,
                                   regime=bs.market_regime,
                                   regime_known=bs.regime_known))
    return trades


def run_single_symbol(symbol: str, data_dir: str, args) -> None:
    ds = load_csv(data_dir, symbol)
    print("DATA QUALITY:")
    for line in ds.quality_report():
        print("  ", line)
    print()

    engine = BacktestEngine(
        dataset={symbol: ds},
        start=_to_dt(args.start),
        end=_to_dt(args.end),
    )
    m = engine.run_full(warmup_min=args.warmup, execution_model=build_model(args))
    print(render_report(m))

    if args.csv:
        signals = engine.run_symbol(symbol, args.warmup)
        trades = _simulate_signals(ds, signals, build_model(args))
        export_csv(trades, args.csv)
        print(f"\nExported {len(trades)} trade row(s) to {args.csv}")


def run_walk_forward_cli(symbol: str, data_dir: str, args, wf_spec: str) -> None:
    """Walk-forward with the same engine over three isolated windows."""
    ds = load_csv(data_dir, symbol)
    mapping = {}
    for p in wf_spec.split():
        key, _, rng = p.partition("=")
        mapping[key] = _parse_range(rng)
    cfg = WalkForwardConfig(
        train=mapping.get("train", (None, None)),
        validation=mapping.get("validation", (None, None)),
        out_of_sample=mapping.get("oos", (None, None)),
    )
    result = run_walk_forward({symbol: ds}, cfg, warmup_min=args.warmup,
                              execution_model=build_model(args))
    for label in ("train", "validation", "out_of_sample"):
        w = result[label]
        trades = _simulate_signals(ds, w.signals, build_model(args))
        wm = metrics_from_trades(trades)
        print(f"\n=== {label.upper()} [{w.start} -> {w.end}] "
              f"signals={len(w.signals)} ===")
        print(render_report(wm))


def main() -> None:
    parser = argparse.ArgumentParser(description="PHASE 4 backtest runner")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--data-dir", required=True,
                        help="Dir with {symbol}_1h.csv, {symbol}_15m.csv, {symbol}_5m.csv")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--warmup", type=int, default=600)
    parser.add_argument("--tp-model", default="TP1_50PCT",
                        choices=["TP1_50PCT", "TP1_ALL"])
    parser.add_argument("--fees", action="store_true",
                        help="Enable taker fee + slippage in R calculation")
    parser.add_argument("--taker", default="0.0004")
    parser.add_argument("--slippage-bps", default="2")
    parser.add_argument("--max-hold", type=int, default=0,
                        help="Max 5M candles to wait for entry fill (0 = no expiry)")
    parser.add_argument("--csv", default=None,
                        help="Optional path to export per-trade CSV")
    parser.add_argument("--walk-forward", default=None, metavar="SPEC",
                        help="e.g. 'train=2024-01-01,2024-12-31 "
                             "validation=2025-01-01,2025-06-30 oos=2025-07-01,2025-12-31'")

    args = parser.parse_args()
    if args.walk_forward:
        run_walk_forward_cli(args.symbol, args.data_dir, args, args.walk_forward)
    else:
        run_single_symbol(args.symbol, args.data_dir, args)


if __name__ == "__main__":
    main()
