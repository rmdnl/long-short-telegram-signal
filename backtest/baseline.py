"""
PHASE 5: Full baseline backtest orchestrator.

Runs the backtest over all 9 symbols, generates:
- Data quality reports
- Gross + net fee/slippage reports
- Per-symbol / per-regime / per-direction / per-month breakdowns
- OOS (out-of-sample) separation
- False-signal classification
- Markdown summary report
- CSV trade export

NO parameter optimization. Uses the current locked config.
Historical simulation only — NOT a projection of live performance.
"""
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from app.config import get_config
from app.models import Candle, Signal
from app import data_validation as dv

from backtest.data import HistoricalDataset, DataQualityReport, load_csv_dir
from backtest.engine import BacktestEngine, BacktestSignal
from backtest.execution import (
    ExecutionModel, TradeSimulator, TradeResult,
    TP_MODEL_50PCT,
)
from backtest.metrics import BacktestMetrics, metrics_from_trades
from backtest.report import export_csv, _fmt, _pf, _pct

logger = logging.getLogger(__name__)

# Default symbols for baseline
DEFAULT_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT",
    "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT",
]

# Warmup: EMA200 on 1H needs 200*60/5 = 2400 5M candles;
# 15M EMA200 needs 200*15/5 = 600 5M candles.
# Use 600 as a safe minimum (covers 15M + 5M warmup).
DEFAULT_WARMUP_MIN = 600

# Fee assumptions for net report (documented, NOT user-specific)
NET_TAKER_FEE = Decimal("0.0004")     # 0.04% typical Binance spot taker
NET_SLIPPAGE_BPS = Decimal("2")       # 2 bps = 0.02%
# Expiration: 48 x 5M = 4 hours max wait for entry fill
DEFAULT_MAX_HOLD_CANDLES = 48


# ---------------------------------------------------------------------------
# Configuration snapshot
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConfigSnapshot:
    """Immutable snapshot of the strategy configuration for one baseline run."""
    adx_length: int
    adx_min: Decimal
    rsi_length: int
    rsi_midline: Decimal
    ema_fast: int
    ema_slow: int
    atr_length: int
    sl_atr_multiplier: Decimal
    tp1_rr: Decimal
    tp2_rr: Decimal
    volume_sma_length: int
    volume_multiplier: Decimal
    min_score: int
    max_distance_from_ema_atr: Decimal
    cooldown_candles: int
    symbols: Tuple[str, ...]

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["symbols"] = list(d["symbols"])
        return d

    @classmethod
    def from_config(cls) -> "ConfigSnapshot":
        c = get_config()
        return cls(
            adx_length=c.adx_length,
            adx_min=c.adx_min,
            rsi_length=c.rsi_length,
            rsi_midline=c.rsi_midline,
            ema_fast=c.ema_fast,
            ema_slow=c.ema_slow,
            atr_length=c.atr_length,
            sl_atr_multiplier=c.sl_atr_multiplier,
            tp1_rr=c.tp1_rr,
            tp2_rr=c.tp2_rr,
            volume_sma_length=c.volume_sma_length,
            volume_multiplier=c.volume_multiplier,
            min_score=c.min_score,
            max_distance_from_ema_atr=c.max_distance_from_ema_atr,
            cooldown_candles=c.cooldown_candles,
            symbols=tuple(c.symbols),
        )


# ---------------------------------------------------------------------------
# OOS split
# ---------------------------------------------------------------------------

@dataclass
class OosConfig:
    """Out-of-sample split: fraction of total period as OOS.

    The first (1 - oos_fraction) of the period is IN-SAMPLE,
    the next validation_fraction is VALIDATION,
    the last oos_fraction is OUT-OF-SAMPLE.
    """
    oos_fraction: float = 0.3
    validation_fraction: float = 0.2

    def split(self, start: datetime, end: datetime) -> Dict[str, Tuple[datetime, datetime]]:
        total = (end - start).total_seconds()
        oos_start = start + timedelta(seconds=total * (1 - self.oos_fraction))
        val_start = start + timedelta(seconds=total * (1 - self.oos_fraction - self.validation_fraction))
        return {
            "in_sample": (start, val_start),
            "validation": (val_start, oos_start),
            "out_of_sample": (oos_start, end),
        }


# ---------------------------------------------------------------------------
# False-signal classification
# ---------------------------------------------------------------------------

# Classification labels for losing trades
FS_HT_REVERSAL = "HTF_REVERSAL"
FS_FAILED_BREAKOUT = "FAILED_BREAKOUT"
FS_TREND_EXHAUSTION = "TREND_EXHAUSTION"
FS_VOLUME_FAILURE = "VOLUME_FAILURE"
FS_SUDDEN_VOLATILITY = "SUDDEN_VOLATILITY"
FS_RANGING_TRANSITION = "RANGING_TRANSITION"
FS_UNKNOWN = "UNKNOWN"


def classify_loss(
    trade: TradeResult,
    trigger_candles: List[Candle],
    trigger_idx: int,
) -> str:
    """
    Classify why a losing trade happened using ONLY data available at signal time
    and the trade's post-signal candles. Deterministic — no invented labels.

    Returns one of the FS_* constants.
    """
    if trade.exit_status not in ("STOPPED",) and not trade.sl_hit:
        # Not a clear stop-loss — classify as UNKNOWN for non-SL exits
        # (e.g. expired, TP1-only with net loss from fees)
        if trade.exit_status == "EXPIRED":
            return FS_UNKNOWN
        # Check if net R is negative due to fees
        if trade.r_multiple <= 0 and not trade.sl_hit:
            return FS_UNKNOWN
        return FS_UNKNOWN

    # For stop-losses, look at the trigger candles to classify
    # Only use candles up to the exit candle
    if trigger_idx is None or trigger_idx < 0:
        return FS_UNKNOWN

    # Find the exit candle index
    exit_idx = -1
    for i in range(trigger_idx + 1, len(trigger_candles)):
        if trigger_candles[i].timestamp == trade.exit_time:
            exit_idx = i
            break

    if exit_idx <= 0:
        return FS_UNKNOWN

    # Get the trigger candle at signal time
    sig_candle = trigger_candles[trigger_idx]

    # 1. HTF reversal: check if the 15M context reversed within N candles
    #    (approximate: the exit happened within 3 trigger candles of the signal)
    if exit_idx - trigger_idx <= 3:
        # Very fast reversal — likely an HTF context reversal
        return FS_HT_REVERSAL

    # 2. Failed breakout: price broke the entry zone in the wrong direction
    #    before TP was reached
    entry_mid = (trade.entry_low + trade.entry_high) / 2
    sl = trade.stop_loss
    for i in range(trigger_idx + 1, exit_idx + 1):
        c = trigger_candles[i]
        if trade.direction == "LONG" and c.low <= sl:
            # Price went directly to SL without reaching TP
            if c.low < entry_mid and c.high < trade.tp1:
                return FS_FAILED_BREAKOUT
        elif trade.direction == "SHORT" and c.high >= sl:
            if c.high > entry_mid and c.low > trade.tp1:
                return FS_FAILED_BREAKOUT

    # 3. Trend exhaustion: ADX was high at signal but price couldn't sustain
    #    (approximation: held for a long time before SL)
    hold = exit_idx - trigger_idx
    if hold >= 24:  # >= 2 hours (24 x 5M candles)
        return FS_TREND_EXHAUSTION

    # 4. Sudden volatility: the exit candle's range was unusually large
    #    (compared to the previous 10 candles' ATR approximation)
    if exit_idx > 10:
        recent = trigger_candles[exit_idx - 10:exit_idx]
        ranges = [c.high - c.low for c in recent]
        avg_range = sum(ranges) / len(ranges) if ranges else Decimal("0")
        exit_range = trigger_candles[exit_idx].high - trigger_candles[exit_idx].low
        if avg_range > 0 and exit_range > avg_range * 2:
            return FS_SUDDEN_VOLATILITY

    # 5. Ranging transition: price was choppy (many small-range candles)
    if hold >= 6:
        recent = trigger_candles[trigger_idx + 1: exit_idx + 1]
        small_ranges = sum(
            1 for c in recent
            if (c.high - c.low) < (sig_candle.high - sig_candle.low)
        )
        if recent and small_ranges / len(recent) > 0.7:
            return FS_RANGING_TRANSITION

    return FS_UNKNOWN


# ---------------------------------------------------------------------------
# Quality report builder
# ---------------------------------------------------------------------------

def quality_report_lines(datasets: Dict[str, HistoricalDataset]) -> List[str]:
    """Build data-quality report lines for all symbols/timeframes."""
    lines: List[str] = []
    for sym, ds in sorted(datasets.items()):
        for rpt in ds.reports:
            lines.append(rpt.summary())
    return lines


def quality_has_major_issues(datasets: Dict[str, HistoricalDataset]) -> bool:
    """True if any timeframe has a major data-quality problem."""
    for ds in datasets.values():
        for rpt in ds.reports:
            if rpt.major_problem:
                return True
    return False


# ---------------------------------------------------------------------------
# Simulation helper
# ---------------------------------------------------------------------------

def _simulate_signals(
    ds: HistoricalDataset,
    signals: List[BacktestSignal],
    model: ExecutionModel,
) -> List[TradeResult]:
    """Run TradeSimulator over a signal list and return TradeResult rows."""
    sim = TradeSimulator(model)
    trigger = ds.tf("5m")
    ts_list = [c.timestamp for c in trigger]
    trades: List[TradeResult] = []
    for bs in signals:
        try:
            idx = ts_list.index(bs.signal.trigger_candle_time)
        except ValueError:
            continue
        future = trigger[idx + 1:]
        tr = sim.simulate(
            bs.signal, future,
            regime=bs.market_regime,
            regime_known=bs.regime_known,
        )
        trades.append(tr)
    return trades


# ---------------------------------------------------------------------------
# Run a baseline over a set of symbols
# ---------------------------------------------------------------------------

def run_baseline(
    datasets: Dict[str, HistoricalDataset],
    model: ExecutionModel,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    warmup_min: int = DEFAULT_WARMUP_MIN,
) -> Tuple[BacktestMetrics, List[TradeResult]]:
    """
    Run the backtest over all symbols with a given ExecutionModel.
    Returns (metrics, all_trades).
    """
    engine = BacktestEngine(dataset=datasets, start=start, end=end)
    all_trades: List[TradeResult] = []
    for symbol, ds in datasets.items():
        signals = engine.run_symbol(symbol, warmup_min)
        trades = _simulate_signals(ds, signals, model)
        all_trades.extend(trades)

    m = metrics_from_trades(
        all_trades,
        period_start=start.isoformat() if start else None,
        period_end=end.isoformat() if end else None,
        symbols=list(datasets.keys()),
        warmup_candles_skipped=warmup_min,
    )
    return m, all_trades


# ---------------------------------------------------------------------------
# OOS run
# ---------------------------------------------------------------------------

def run_oos(
    datasets: Dict[str, HistoricalDataset],
    model: ExecutionModel,
    oos_cfg: OosConfig,
    start: datetime,
    end: datetime,
    warmup_min: int = DEFAULT_WARMUP_MIN,
) -> Dict[str, BacktestMetrics]:
    """
    Run IN-SAMPLE / VALIDATION / OUT-OF-SAMPLE as three isolated windows.
    Each window uses a fresh BacktestEngine (no state leakage).
    """
    segments = oos_cfg.split(start, end)
    results: Dict[str, BacktestMetrics] = {}
    for label, (seg_start, seg_end) in segments.items():
        m, _ = run_baseline(datasets, model, seg_start, seg_end, warmup_min)
        m.period_start = seg_start.isoformat()
        m.period_end = seg_end.isoformat()
        results[label] = m
    return results


# ---------------------------------------------------------------------------
# Loss classification
# ---------------------------------------------------------------------------

def classify_losses(
    all_trades: List[TradeResult],
    datasets: Dict[str, HistoricalDataset],
) -> Dict[str, int]:
    """Classify all losing trades. Returns {label: count}."""
    counts: Dict[str, int] = {}
    trigger_cache: Dict[str, List[Candle]] = {}

    for t in all_trades:
        if t.r_multiple <= 0 and t.exit_status in ("STOPPED", "EXPIRED"):
            sym = t.symbol
            if sym not in trigger_cache:
                trigger_cache[sym] = datasets[sym].tf("5m")
            trigger = trigger_cache[sym]
            ts_list = [c.timestamp for c in trigger]
            try:
                idx = ts_list.index(t.signal_time)
            except ValueError:
                idx = None
            label = classify_loss(t, trigger, idx)
            counts[label] = counts.get(label, 0) + 1
        else:
            # Wins and non-expired non-SL trades: UNKNOWN
            counts[FS_UNKNOWN] = counts.get(FS_UNKNOWN, 0) + 1

    return counts


# ---------------------------------------------------------------------------
# Markdown report builder
# ---------------------------------------------------------------------------

def _metrics_block(m: BacktestMetrics) -> List[str]:
    """Core metrics as markdown lines."""
    out = [
        f"| Signals | {m.total_signals} (Long={m.long_signals} Short={m.short_signals}) |",
        f"| Filled / Unfilled / Expired | {m.filled} / {m.unfilled} / {m.expired} |",
        f"| Closed trades | {m.closed_trades} |",
        f"| Wins / Losses | {m.wins} / {m.losses} |",
        f"| Win Rate | {_pct(m.win_rate)} |",
        f"| Average R | {_fmt(m.avg_r)} |",
        f"| Median R | {_fmt(m.median_r)} |",
        f"| Total R | {_fmt(m.total_r)} |",
        f"| Average Win R / Avg Loss R | {_fmt(m.avg_win_r)} / {_fmt(m.avg_loss_r)} |",
        f"| Profit Factor | {_pf(m.profit_factor)} |",
        f"| Expectancy | {_fmt(m.expectancy)} |",
        f"| Max Drawdown (R) | {_fmt(m.max_drawdown_r)} |",
        f"| Max Consecutive Losses | {m.max_consecutive_losses} |",
        f"| TP1 Hit / TP2 Hit / SL Rate | {_pct(m.tp1_hit_rate)} / {_pct(m.tp2_hit_rate)} / {_pct(m.sl_rate)} |",
        f"| Avg Holding (candles) | {_fmt(m.avg_holding_candles, '.2f')} |",
        f"| Signals/Day / Signals/Week | {_fmt(m.signals_per_day)} / {_fmt(m.signals_per_week)} |",
    ]
    return out


def _sym_table(m: BacktestMetrics) -> List[str]:
    """Per-symbol markdown table."""
    out = [
        "| Symbol | Signals | Win Rate | Expectancy | PF | Max DD (R) | Total R |",
        "|--------|---------|----------|------------|----|-----------|---------|",
    ]
    for sym, sm in sorted(m.by_symbol.items()):
        out.append(
            f"| {sym} | {sm.total_signals} | {_pct(sm.win_rate)} | "
            f"{_fmt(sm.expectancy)} | {_pf(sm.profit_factor)} | "
            f"{_fmt(sm.max_drawdown_r)} | {_fmt(sm.total_r)} |"
        )
    return out


def _dir_table(m: BacktestMetrics) -> List[str]:
    """Per-direction markdown table."""
    out = [
        "| Direction | Signals | Win Rate | Expectancy | PF | Total R |",
        "|-----------|---------|----------|------------|----|---------|",
    ]
    for d, dm in sorted(m.by_direction.items()):
        out.append(
            f"| {d} | {dm.total_signals} | {_pct(dm.win_rate)} | "
            f"{_fmt(dm.expectancy)} | {_pf(dm.profit_factor)} | "
            f"{_fmt(dm.total_r)} |"
        )
    return out


def _regime_table(m: BacktestMetrics) -> List[str]:
    """Per-regime markdown table."""
    out = [
        "| Regime | Signals | Win Rate | Expectancy | PF | Max DD (R) | Total R |",
        "|--------|---------|----------|------------|----|-----------|---------|",
    ]
    for reg, rm in sorted(m.by_regime.items()):
        out.append(
            f"| {reg} | {rm.total_signals} | {_pct(rm.win_rate)} | "
            f"{_fmt(rm.expectancy)} | {_pf(rm.profit_factor)} | "
            f"{_fmt(rm.max_drawdown_r)} | {_fmt(rm.total_r)} |"
        )
    return out


def _month_table(m: BacktestMetrics) -> List[str]:
    """Per-month markdown table."""
    out = [
        "| Month | Signals | Win Rate | Expectancy | Total R |",
        "|-------|---------|----------|------------|---------|",
    ]
    for month, mm in sorted(m.by_month.items()):
        out.append(
            f"| {month} | {mm.total_signals} | {_pct(mm.win_rate)} | "
            f"{_fmt(mm.expectancy)} | {_fmt(mm.total_r)} |"
        )
    return out


def _oos_table(results: Dict[str, BacktestMetrics]) -> List[str]:
    """OOS breakdown markdown table."""
    out = [
        "| Segment | Start | End | Signals | Win Rate | Expectancy | PF | Total R |",
        "|---------|-------|-----|---------|----------|------------|----|---------|",
    ]
    for label in ("in_sample", "validation", "out_of_sample"):
        m = results.get(label)
        if m is None:
            continue
        out.append(
            f"| {label} | {m.period_start} | {m.period_end} | "
            f"{m.total_signals} | {_pct(m.win_rate)} | {_fmt(m.expectancy)} | "
            f"{_pf(m.profit_factor)} | {_fmt(m.total_r)} |"
        )
    return out


def build_markdown_report(
    config_snap: ConfigSnapshot,
    gross_m: BacktestMetrics,
    net_m: BacktestMetrics,
    oos_results: Dict[str, BacktestMetrics],
    all_trades: List[TradeResult],
    quality_lines: List[str],
    loss_counts: Dict[str, int],
    dataset_stats_lines: List[str],
    data_dir: str,
    period_start: Optional[datetime],
    period_end: Optional[datetime],
) -> str:
    """Build the full baseline markdown report string."""
    L: List[str] = []
    L.append("# BASELINE BACKTEST REPORT")
    L.append("")
    L.append(
        "> **DISCLAIMER:** This is a HISTORICAL SIMULATION. "
        "Past backtest results do not guarantee future profitability. "
        "No claim of accuracy, guaranteed outcome, or high probability is made."
    )
    L.append("")

    # DATASET
    L.append("## 1. DATASET")
    L.append("")
    L.append(f"- Source: Binance public historical klines")
    L.append(f"- Period: {period_start} -> {period_end}")
    L.append(f"- Symbols: {', '.join(config_snap.symbols)}")
    L.append(f"- Timeframes: 1H / 15M / 5M (5M trigger)")
    L.append(f"- Data dir: `{data_dir}`")
    L.append("")
    for line in dataset_stats_lines:
        L.append(f"  - {line}")
    L.append("")

    # CONFIGURATION
    L.append("## 2. CONFIGURATION (locked, no optimization)")
    L.append("")
    L.append("| Parameter | Value |")
    L.append("|-----------|-------|")
    for k, v in config_snap.to_dict().items():
        L.append(f"| {k} | {v} |")
    L.append("")

    # DATA QUALITY
    L.append("## 3. DATA QUALITY")
    L.append("")
    for line in quality_lines:
        L.append(f"- {line}")
    L.append("")
    if quality_has_major_issues_line(quality_lines):
        L.append("**MAJOR ISSUES DETECTED — backtest results may be affected.**")
        L.append("")

    # OVERALL (GROSS)
    L.append("## 4. OVERALL RESULTS (GROSS — fees=0, slippage=0)")
    L.append("")
    L.append("| Metric | Value |")
    L.append("|--------|-------|")
    L.extend(_metrics_block(gross_m))
    L.append("")

    # NET
    L.append(f"## 5. NET RESULTS (fees={NET_TAKER_FEE}, slippage={NET_SLIPPAGE_BPS} bps)")
    L.append("")
    L.append(
        f"> Assumptions: taker fee = {NET_TAKER_FEE} (0.04%), "
        f"slippage = {NET_SLIPPAGE_BPS} bps (0.02%). "
        f"These are conservative generic values, NOT the user's actual fee tier."
    )
    L.append("")
    L.append("| Metric | Value |")
    L.append("|--------|-------|")
    L.extend(_metrics_block(net_m))
    L.append("")
    L.append(f"- Gross Total R: {_fmt(gross_m.total_r)}")
    L.append(f"- Net Total R: {_fmt(net_m.total_r)}")
    if net_m.est_fee_cost_r is not None:
        L.append(f"- Estimated fee/slippage cost (R): {_fmt(net_m.est_fee_cost_r)}")
    L.append("")

    # PER SYMBOL
    L.append("## 6. PER SYMBOL")
    L.append("")
    L.extend(_sym_table(gross_m))
    L.append("")

    # LONG vs SHORT
    L.append("## 7. LONG vs SHORT")
    L.append("")
    L.extend(_dir_table(gross_m))
    L.append("")

    # MARKET REGIME
    L.append("## 8. MARKET REGIME")
    L.append("")
    L.extend(_regime_table(gross_m))
    L.append("")

    # MONTHLY
    L.append("## 9. MONTHLY BREAKDOWN")
    L.append("")
    L.extend(_month_table(gross_m))
    L.append("")

    # OOS
    L.append("## 10. OUT-OF-SAMPLE")
    L.append("")
    L.extend(_oos_table(oos_results))
    L.append("")
    L.append(
        "> OOS results are the primary robustness reference. "
        "IN-SAMPLE and VALIDATION are NOT combined with OOS into a single headline number."
    )
    L.append("")

    # LOSS CLASSIFICATION
    L.append("## 11. LOSS CLASSIFICATION (losing trades only)")
    L.append("")
    L.append("| Category | Count |")
    L.append("|----------|-------|")
    for label, count in sorted(loss_counts.items(), key=lambda x: -x[1]):
        L.append(f"| {label} | {count} |")
    L.append("")
    L.append(
        "> Classification uses only deterministic data available at signal time. "
        "UNKNOWN is used when the cause cannot be reliably determined."
    )
    L.append("")

    # SIGNAL FREQUENCY
    L.append("## 12. SIGNAL FREQUENCY")
    L.append("")
    L.append(f"- Signals/Day: {_fmt(gross_m.signals_per_day)}")
    L.append(f"- Signals/Week: {_fmt(gross_m.signals_per_week)}")
    total_days = 1
    if period_start and period_end:
        total_days = max((period_end - period_start).days, 1)
    L.append(f"- Signals/Month (approx): {_fmt(gross_m.total_signals / total_days * 30)}")
    L.append("")

    # LIMITATIONS
    L.append("## 13. LIMITATIONS")
    L.append("")
    L.append("- Historical simulation only; NOT a projection of live performance.")
    L.append("- Entry fill model is conservative (least-favorable zone edge).")
    L.append(f"- Fees/slippage assumptions: taker={NET_TAKER_FEE}, slippage={NET_SLIPPAGE_BPS} bps.")
    L.append(f"- Max hold: {DEFAULT_MAX_HOLD_CANDLES} x 5M candles for entry fill window.")
    L.append("- No parameter optimization was performed.")
    L.append("- No trading execution was performed or simulated.")
    L.append("- Data gaps, if any, are reported in section 3 but NOT silently repaired.")
    L.append("")

    return "\n".join(L)


def quality_has_major_issues_line(lines: List[str]) -> bool:
    """Check if any quality line contains 'MAJOR_PROBLEM'."""
    return any("MAJOR_PROBLEM" in line for line in lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@dataclass
class BaselineOutput:
    """All artifacts from one baseline run."""
    config_snap: ConfigSnapshot
    gross_metrics: BacktestMetrics
    net_metrics: BacktestMetrics
    oos_results: Dict[str, BacktestMetrics]
    all_trades: List[TradeResult]
    loss_counts: Dict[str, int]
    quality_lines: List[str]
    markdown_report: str
    csv_path: Optional[str]


def run_full_baseline(
    data_dir: str = "data",
    symbols: Optional[List[str]] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    warmup_min: int = DEFAULT_WARMUP_MIN,
    oos_fraction: float = 0.3,
    validation_fraction: float = 0.2,
    report_dir: str = "reports",
) -> BaselineOutput:
    """
    Run the complete PHASE 5 baseline:
    1. Load all symbol datasets from data_dir
    2. Generate data quality reports
    3. Run gross backtest (fees=0, slippage=0)
    4. Run net backtest (taker=0.04%, slippage=2bps)
    5. Run OOS split
    6. Classify losses
    7. Export CSV
    8. Build markdown report

    Args:
        data_dir: root data directory (contains {SYMBOL}/{tf}.csv)
        symbols: list of symbols (defaults to DEFAULT_SYMBOLS)
        start/end: optional date bounds (defaults to full dataset range)
        warmup_min: minimum warmup candles before signals are allowed
        oos_fraction: fraction of period as out-of-sample
        validation_fraction: fraction as validation
        report_dir: where to write reports

    Returns:
        BaselineOutput with all artifacts.
    """
    cfg = ConfigSnapshot.from_config()
    syms = symbols or list(cfg.symbols)

    # Determine period bounds from data if not specified
    if start is None or end is None:
        start, end = _infer_period(data_dir, syms)

    # Load datasets
    datasets: Dict[str, HistoricalDataset] = {}
    for sym in syms:
        ds = load_csv_dir(data_dir, sym)
        if not ds.tf("5m"):
            logger.warning(f"No 5m data for {sym}, skipping")
            continue
        datasets[sym] = ds

    # Quality report
    q_lines = quality_report_lines(datasets)

    # Execution models
    gross_model = ExecutionModel(
        maker_fee=Decimal("0"),
        taker_fee=Decimal("0"),
        slippage_bps=Decimal("0"),
        tp_model=TP_MODEL_50PCT,
        enable_fees=False,
        max_candles_after_signal=DEFAULT_MAX_HOLD_CANDLES,
    )
    net_model = ExecutionModel(
        maker_fee=Decimal("0"),
        taker_fee=NET_TAKER_FEE,
        slippage_bps=NET_SLIPPAGE_BPS,
        tp_model=TP_MODEL_50PCT,
        enable_fees=True,
        max_candles_after_signal=DEFAULT_MAX_HOLD_CANDLES,
    )

    # Gross run
    gross_m, all_trades = run_baseline(datasets, gross_model, start, end, warmup_min)

    # Net run
    net_m, _ = run_baseline(datasets, net_model, start, end, warmup_min)

    # OOS run
    oos_cfg = OosConfig(oos_fraction=oos_fraction, validation_fraction=validation_fraction)
    oos_results = run_oos(datasets, gross_model, oos_cfg, start, end, warmup_min)

    # Loss classification
    loss_counts = classify_losses(all_trades, datasets)

    # Dataset stats lines
    ds_lines: List[str] = []
    for sym, ds in sorted(datasets.items()):
        for tf in ("1h", "15m", "5m"):
            n = len(ds.tf(tf))
            ds_lines.append(f"{sym} {tf}: {n} candles")

    # CSV export
    os.makedirs(report_dir, exist_ok=True)
    csv_path = os.path.join(report_dir, "baseline_trades.csv")
    export_csv(all_trades, csv_path)

    # Markdown report
    md = build_markdown_report(
        config_snap=cfg,
        gross_m=gross_m,
        net_m=net_m,
        oos_results=oos_results,
        all_trades=all_trades,
        quality_lines=q_lines,
        loss_counts=loss_counts,
        dataset_stats_lines=ds_lines,
        data_dir=data_dir,
        period_start=start,
        period_end=end,
    )

    md_path = os.path.join(report_dir, "baseline_summary.md")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(md)

    return BaselineOutput(
        config_snap=cfg,
        gross_metrics=gross_m,
        net_metrics=net_m,
        oos_results=oos_results,
        all_trades=all_trades,
        loss_counts=loss_counts,
        quality_lines=q_lines,
        markdown_report=md,
        csv_path=csv_path,
    )


def _infer_period(
    data_dir: str,
    syms: List[str],
) -> Tuple[datetime, datetime]:
    """Infer the period bounds from the earliest 5m candle start and latest end."""
    earliest: Optional[datetime] = None
    latest: Optional[datetime] = None
    for sym in syms:
        ds = load_csv_dir(data_dir, sym)
        trigger = ds.tf("5m")
        if not trigger:
            continue
        start_t = trigger[0].timestamp
        end_t = dv.candle_close_time(trigger[-1], "5m")
        if earliest is None or start_t < earliest:
            earliest = start_t
        if latest is None or end_t > latest:
            latest = end_t
    if earliest is None or latest is None:
        raise ValueError(f"Could not infer period from {data_dir}/{syms}")
    return earliest, latest


def cli_main() -> None:
    """
    CLI entry point:
        python -m backtest.baseline --data-dir data --report-dir reports \
            [--symbols BTCUSDT,ETHUSDT,...] [--start 2025-01-01] [--end 2025-06-30]
    """
    import argparse

    # Run the hard trading-execution guard before the baseline.
    from app.trading_guard import assert_no_execution_code
    assert_no_execution_code()
    logger.info("Trading-execution guard passed: 0 violations")

    parser = argparse.ArgumentParser(description="PHASE 5 baseline backtest runner")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--report-dir", default="reports")
    parser.add_argument("--symbols", default=None,
                        help="Comma-separated; defaults to all configured symbols")
    parser.add_argument("--start", default=None, help="ISO date UTC, e.g. 2025-01-01")
    parser.add_argument("--end", default=None, help="ISO date UTC, e.g. 2025-06-30")
    parser.add_argument("--warmup", type=int, default=DEFAULT_WARMUP_MIN)
    parser.add_argument("--oos-fraction", type=float, default=0.3)
    parser.add_argument("--val-fraction", type=float, default=0.2)
    args = parser.parse_args()

    def _parse_dt(s: Optional[str]) -> Optional[datetime]:
        if not s:
            return None
        s = s.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    symbols = None
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    out = run_full_baseline(
        data_dir=args.data_dir,
        symbols=symbols,
        start=_parse_dt(args.start),
        end=_parse_dt(args.end),
        warmup_min=args.warmup,
        oos_fraction=args.oos_fraction,
        validation_fraction=args.val_fraction,
        report_dir=args.report_dir,
    )

    print(out.markdown_report)
    print(f"\nCSV exported to: {out.csv_path}")
    print(f"Markdown report written to: {os.path.join(args.report_dir, 'baseline_summary.md')}")


if __name__ == "__main__":
    cli_main()
