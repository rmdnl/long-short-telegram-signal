"""
PHASE 4: Readable backtest report + CSV export.

render_report: human-readable summary + per-symbol + per-regime + per-period.
export_csv: one row per simulated signal/trade; NO secrets included.

This module only FORMATS results — it never adds strategy logic.
"""
import csv
import os
from typing import List, Optional

from backtest.metrics import BacktestMetrics
from backtest.execution import TradeResult

_CSV_COLUMNS = [
    "signal_id", "symbol", "direction", "signal_time",
    "entry_low", "entry_high", "entry_price", "stop_loss",
    "tp1", "tp2", "score", "market_regime",
    "entry_status", "exit_status", "exit_time", "exit_price",
    "r_multiple", "tp1_hit", "tp2_hit", "sl_hit", "holding_candles",
]


def _fmt(v, spec=".4f"):
    if v is None:
        return "N/A"
    if isinstance(v, float):
        return format(v, spec)
    return str(v)


def _line_metrics(m: BacktestMetrics) -> str:
    out = []
    out.append(f"Signals: {m.total_signals} (Long={m.long_signals} Short={m.short_signals})")
    out.append(f"Filled: {m.filled}  Unfilled: {m.unfilled}  Expired: {m.expired}")
    out.append(f"Closed trades: {m.closed_trades}  Open: {m.open_trades}")
    out.append(f"Wins: {m.wins}  Losses: {m.losses}  Win Rate: {_pct(m.win_rate)}")
    out.append(f"Average R: {_fmt(m.avg_r)}  Median R: {_fmt(m.median_r)}  Total R: {_fmt(m.total_r)}")
    out.append(f"Average Win R: {_fmt(m.avg_win_r)}  Average Loss R: {_fmt(m.avg_loss_r)}")
    out.append(f"Profit Factor: {_pf(m.profit_factor)}  Expectancy: {_fmt(m.expectancy)}")
    out.append(f"Max Drawdown (R): {_fmt(m.max_drawdown_r)}  Max Consec Losses: {m.max_consecutive_losses}")
    out.append(f"TP1 Hit Rate: {_pct(m.tp1_hit_rate)}  TP2 Hit Rate: {_pct(m.tp2_hit_rate)}  SL Rate: {_pct(m.sl_rate)}")
    out.append(f"Avg Holding (candles): {_fmt(m.avg_holding_candles, '.2f')}  "
               f"Signals/Day: {_fmt(m.signals_per_day)}  Signals/Week: {_fmt(m.signals_per_week)}")
    if m.fees_enabled:
        out.append(f"Fees: enabled (gross R={_fmt(m.gross_r_total)} net R={_fmt(m.net_r_total)} "
                   f"est cost R={_fmt(m.est_fee_cost_r)})")
    else:
        out.append("Fees/slippage: DISABLED")
    return "\n".join(out)


def _pct(v):
    return "N/A" if v is None else f"{v * 100:.1f}%"


def _pf(v):
    # Per spec: zero losses -> N/A, not infinity.
    return "N/A" if v is None else f"{v:.3f}"


def render_report(m: BacktestMetrics) -> str:
    """Render a full multi-section report string."""
    L: List[str] = []
    L.append("=" * 60)
    L.append("BACKTEST SUMMARY")
    L.append("=" * 60)
    L.append(f"Period: {m.period_start or 'N/A'} -> {m.period_end or 'N/A'}")
    L.append(f"Symbols: {', '.join(m.symbols) if m.symbols else 'N/A'}")
    L.append("Timeframes: 1H / 15M / 5M (5M trigger)")
    L.append(f"Warmup candles skipped: {m.warmup_candles_skipped}")
    L.append("-" * 60)
    L.append(_line_metrics(m))
    L.append("")

    # PER SYMBOL
    L.append("=" * 60)
    L.append("PER SYMBOL")
    L.append("=" * 60)
    for sym, sm in m.by_symbol.items():
        L.append(f"\n{sym}")
        L.append(f"Signals: {sm.total_signals}  Win Rate: {_pct(sm.win_rate)}  "
                 f"Expectancy: {_fmt(sm.expectancy)}  PF: {_pf(sm.profit_factor)}  "
                 f"Max DD (R): {_fmt(sm.max_drawdown_r)}  Total R: {_fmt(sm.total_r)}")
    L.append("")

    # PER REGIME
    L.append("=" * 60)
    L.append("PER REGIME")
    L.append("=" * 60)
    for regime, rm in m.by_regime.items():
        L.append(f"\n{regime}")
        L.append(f"Signals: {rm.total_signals}  Win Rate: {_pct(rm.win_rate)}  "
                 f"Expectancy: {_fmt(rm.expectancy)}  PF: {_pf(rm.profit_factor)}")
    L.append("")

    # PER PERIOD (year + month)
    L.append("=" * 60)
    L.append("PER PERIOD")
    L.append("=" * 60)
    for year, ym in m.by_year.items():
        L.append(f"\n{year}: signals={ym.total_signals} "
                 f"win_rate={_pct(ym.win_rate)} total_r={_fmt(ym.total_r)}")
    L.append("")

    L.append("=" * 60)
    L.append("DISCLAIMER")
    L.append("=" * 60)
    L.append("This is a HISTORICAL SIMULATION, not a projection of live performance.")
    L.append("Past backtest results do not guarantee future profitability.")
    return "\n".join(L)


def export_csv(trades: List[TradeResult], csv_path: str) -> str:
    """Export every simulated signal/trade to CSV. No secrets included."""
    os.makedirs(os.path.dirname(csv_path) or ".", exist_ok=True)
    with open(csv_path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(_CSV_COLUMNS)
        for t in trades:
            writer.writerow([
                t.signal_id,
                t.symbol,
                t.direction,
                t.signal_time.isoformat() if t.signal_time else "",
                t.entry_low, t.entry_high, t.entry_price,
                t.stop_loss, t.tp1, t.tp2,
                t.score,
                t.market_regime,
                t.entry_status, t.exit_status,
                t.exit_time.isoformat() if t.exit_time else "",
                t.exit_price,
                t.r_multiple,
                t.tp1_hit, t.tp2_hit, t.sl_hit,
                t.holding_candles,
            ])
    return csv_path
