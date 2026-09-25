"""
PHASE 7: Controlled-experiment runner + report builder.

Runs every exit-management variant on the SAME historical dataset, signal
generation, entry model, fees/slippage, and OOS split. Only the post-entry
exit logic differs. Produces a markdown report with:
- per-variant primary metrics (ALL + LONG/SHORT)
- IS / validation / OOS separated
- OOS-only metrics block (PF / expectancy / totalR / maxDD / win_rate)
- baseline deltas
- robustness breakdowns (per-symbol OOS, monthly OOS, regime OOS)
- H4 / H5 / H7 diagnostics (ADX>=40, vol>=2.0, SHORT RSI<35)

No parameter optimization. No production strategy changes. No trading.
"""
import os
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional

from app.config import get_config
from backtest.data import HistoricalDataset, load_csv_dir
from backtest.engine import BacktestEngine
from backtest.execution import ExecutionModel, TP_MODEL_50PCT, TradeResult
from backtest.metrics import BacktestMetrics, _compute_group
from backtest.baseline import (
    OosConfig,
    ConfigSnapshot,
    DEFAULT_WARMUP_MIN,
    DEFAULT_MAX_HOLD_CANDLES,
)
from backtest.forensics import ForensicsTrade

from backtest.experiments.exit_rules import VARIANTS, Variant
from backtest.experiments.simulator import (
    ExperimentSimulator,
    ExperimentTrade,
    EXPERIMENTAL_CLOSED_STATUSES,
)

# Production closed set (the exact set backtest.metrics._compute_group uses).
# V1_BASELINE is ALWAYS aggregated with this set, so its numbers remain
# identical to the PHASE 6 baseline.
_PRODUCTION_CLOSED = frozenset({"STOPPED", "TP1_HIT", "TP2_HIT"})


# ---------------------------------------------------------------------------
# ATR lookup (per signal) for the ATR-trailing variant
# ---------------------------------------------------------------------------

def _atr_index(ds: HistoricalDataset, config) -> Dict[datetime, Optional[Decimal]]:
    """Map trigger-candle-open-time -> setup-timeframe ATR (same source the
    engine used), keyed by signal_time."""
    from backtest.indicators_precomputed import PrecomputedIndicators
    from datetime import timedelta
    from app import data_validation as dv
    import bisect

    trig = ds.tf("5m")
    setup = ds.tf("15m")
    hf = ds.tf("1h")
    if not trig:
        return {}
    pre = PrecomputedIndicators(config)
    pre.precompute(hf, setup, trig)
    setup_close = [c.timestamp + timedelta(seconds=dv.TIMEFRAME_SECONDS["15m"]) for c in setup]
    out: Dict[datetime, Optional[Decimal]] = {}
    for i, c in enumerate(trig):
        T = dv.candle_close_time(c, "5m")
        setup_count = bisect.bisect_right(setup_close, T)
        if setup_count == 0:
            out[c.timestamp] = None
            continue
        _, setup_ind, _ = pre.build_ind(len(hf) - 1, setup_count - 1, i)
        out[c.timestamp] = setup_ind.atr
    return out


def _build_atr_lookup(datasets: Dict[str, HistoricalDataset], config) -> Dict[str, Dict[datetime, Optional[Decimal]]]:
    return {sym: _atr_index(ds, config) for sym, ds in datasets.items()}


# ---------------------------------------------------------------------------
# Variant run
# ---------------------------------------------------------------------------

def _run_variant(
    datasets: Dict[str, HistoricalDataset],
    variant: Variant,
    model: ExecutionModel,
    start: Optional[datetime],
    end: Optional[datetime],
    warmup_min: int,
    atr_lookup: Dict[str, Dict[datetime, Optional[Decimal]]],
) -> List[ExperimentTrade]:
    """Run one variant over all symbols; returns per-trade ExperimentTrades.

    Reuses the identical live engine + entry fill model; only the post-entry
    exit rule differs. Signal lists are cached per variant so every variant
    sees exactly the same set of signals on the same candles.
    """
    engine = BacktestEngine(dataset=datasets, start=start, end=end)
    all_trades: List[ExperimentTrade] = []
    for sym, ds in datasets.items():
        signals = engine.run_symbol(sym, warmup_min)
        trigger = ds.tf("5m")
        ts_index = {c.timestamp: i for i, c in enumerate(trigger)}
        atr_fn = None
        if variant.kind == "ATR_TRAIL":
            lookup = atr_lookup.get(sym)
            def atr_fn(s, e, sl, _sym=sym, _lookup=lookup):
                return _lookup.get(s.trigger_candle_time) if _lookup else None
        sim = ExperimentSimulator(model, variant, atr_lookup=atr_fn)
        for bs in signals:
            idx = ts_index.get(bs.signal.trigger_candle_time)
            if idx is None:
                continue
            future = trigger[idx + 1:]
            et = sim.simulate(
                bs.signal, future,
                regime=bs.market_regime,
                regime_known=bs.regime_known,
            )
            all_trades.append(et)
    return all_trades


# ---------------------------------------------------------------------------
# OOS assignment
# ---------------------------------------------------------------------------

def _assign_oos(trades: List[ExperimentTrade], oos_cfg: OosConfig,
                start: datetime, end: datetime) -> Dict[str, List[ExperimentTrade]]:
    seg = oos_cfg.split(start, end)
    out: Dict[str, List[ExperimentTrade]] = {
        "in_sample": [], "validation": [], "out_of_sample": [],
    }
    for et in trades:
        st = et.trade.signal_time
        for label, (s, e) in seg.items():
            if s <= st < e:
                out[label].append(et)
                break
        else:
            if st < seg["in_sample"][0]:
                out["in_sample"].append(et)
            elif st >= seg["out_of_sample"][1]:
                out["out_of_sample"].append(et)
    return out


# ---------------------------------------------------------------------------
# Metric helpers
# ---------------------------------------------------------------------------

def _compute_group_exp(trades: List[TradeResult],
                       closed_statuses: Optional[frozenset] = None,
                       group_by: bool = False,
                       ) -> BacktestMetrics:
    """Parallel to backtest.metrics._compute_group but parameterizable over
    the closed-status set.

    Production (_compute_group) hardcodes the production closed set
    {STOPPED, TP1_HIT, TP2_HIT}. Experimental variants may close via
    {BE_HIT, TRAIL_HIT, TIME_EXIT} as well, so we pass the larger set when
    aggregating experimental variants. V1_BASELINE is always aggregated
    with the PRODUCTION set, so its numbers remain identical to the PHASE 6
    baseline. This keeps metrics.py (production) untouched.
    """
    if closed_statuses is None:
        closed_statuses = EXPERIMENTAL_CLOSED_STATUSES
    m = BacktestMetrics()
    m.total_signals = len(trades)
    m.long_signals = sum(1 for t in trades if t.direction == "LONG")
    m.short_signals = sum(1 for t in trades if t.direction == "SHORT")
    m.filled = sum(1 for t in trades if t.entry_status == "FILLED")
    m.unfilled = sum(1 for t in trades if t.entry_status != "FILLED")
    m.expired = sum(1 for t in trades if t.exit_status == "EXPIRED")
    m.closed_trades = sum(1 for t in trades if t.exit_status in closed_statuses)
    m.open_trades = sum(1 for t in trades if t.exit_status == "OPEN")
    m.fees_enabled = any(t.fees_enabled for t in trades)

    closed_rs = [float(t.r_multiple) for t in trades if t.exit_status in closed_statuses]
    m.total_r = sum(closed_rs) if closed_rs else None
    if closed_rs:
        m.avg_r = sum(closed_rs) / len(closed_rs)
        s = sorted(closed_rs)
        n = len(s)
        m.median_r = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
        m.expectancy = m.avg_r
        wins = [r for r in closed_rs if r > 0]
        losses = [r for r in closed_rs if r < 0]
        m.wins = len(wins)
        m.losses = len(losses)
        m.win_rate = (m.wins / m.closed_trades) if m.closed_trades else None
        m.avg_win_r = (sum(wins) / len(wins)) if wins else None
        m.avg_loss_r = (sum(losses) / len(losses)) if losses else None
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        if m.losses == 0 or gross_loss == 0:
            m.profit_factor = None  # N/A (production convention)
        else:
            m.profit_factor = gross_win / gross_loss
    else:
        m.wins = 0
        m.losses = 0
        m.win_rate = None
        m.avg_r = None
        m.median_r = None
        m.expectancy = None
        m.avg_win_r = None
        m.avg_loss_r = None
        m.profit_factor = None

    if trades:
        m.tp1_hit_rate = sum(1 for t in trades if t.tp1_hit) / len(trades)
        m.tp2_hit_rate = sum(1 for t in trades if t.tp2_hit) / len(trades)
        m.sl_rate = sum(1 for t in trades if t.sl_hit) / len(trades)

    fill_holds = [t.holding_candles for t in trades if t.entry_status == "FILLED"]
    m.avg_holding_candles = (sum(fill_holds) / len(fill_holds)) if fill_holds else None

    times = [t.signal_time for t in trades]
    if len(times) >= 2:
        span_days = max((max(times) - min(times)).total_seconds(), 3600) / 86400.0
        m.signals_per_day = len(times) / span_days
        m.signals_per_week = m.signals_per_day * 7.0

    from backtest.metrics import _max_drawdown_r
    m.max_drawdown_r = _max_drawdown_r([Decimal(str(r)) for r in closed_rs])

    cons = 0
    max_cons = 0
    for r in closed_rs:
        if r < 0:
            cons += 1
            max_cons = max(max_cons, cons)
        else:
            cons = 0
    m.max_consecutive_losses = max_cons

    # Grouped breakdowns (top-level call only) — avoids infinite recursion.
    if group_by:
        def _group_by(key_fn):
            out: Dict[str, List[TradeResult]] = {}
            for t in trades:
                out.setdefault(str(key_fn(t)), []).append(t)
            # Nested groups are NOT further grouped (they are leaf metrics).
            return {k: _compute_group_exp(v, closed_statuses, group_by=False)
                    for k, v in out.items()}
        m.by_symbol = _group_by(lambda t: t.symbol)
        m.by_direction = _group_by(lambda t: t.direction)
        m.by_regime = _group_by(lambda t: t.market_regime)
        m.by_month = _group_by(lambda t: t.signal_time.strftime("%Y-%m"))
        m.by_year = _group_by(lambda t: t.signal_time.strftime("%Y"))
    return m


def _variant_metrics(trades: List[ExperimentTrade]) -> BacktestMetrics:
    """Aggregate a variant's trades. V1_BASELINE uses the PRODUCTION closed
    set (identical to PHASE 6); experimental variants use the extended
    closed set so BE_HIT / TRAIL_HIT / TIME_EXIT count as closed."""
    label = trades[0].variant if trades else ""
    closed = _PRODUCTION_CLOSED if label == "V1_BASELINE" else None
    return _compute_group_exp([et.trade for et in trades], closed, group_by=True)


def _group_metrics(trades: List[ExperimentTrade], key_fn) -> Dict[str, BacktestMetrics]:
    out: Dict[str, List[ExperimentTrade]] = {}
    for et in trades:
        k = key_fn(et.trade)
        if k is None:
            continue
        out.setdefault(str(k), []).append(et)
    label = trades[0].variant if trades else ""
    closed = _PRODUCTION_CLOSED if label == "V1_BASELINE" else None
    return {k: _compute_group_exp([e.trade for e in v], closed, group_by=False)
            for k, v in out.items()}


def _group_block(trades: List[ExperimentTrade], key_fn, title: str) -> str:
    stats = _group_metrics(trades, key_fn)
    L = [f"#### {title}",
         "| group | signals | win_rate | expectancy | PF | total_R |",
         "|---|---|---|---|---|---|"]
    for k in sorted(stats):
        m = stats[k]
        L.append(
            f"| {k} | {m.total_signals} | {_pct(m.win_rate)} | "
            f"{_f(m.expectancy)} | {_pf(m.profit_factor)} | {_f(m.total_r)} |"
        )
    L.append("")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------
# H4 / H5 / H7 diagnostics (diagnostic only — no parameter changes)
# ---------------------------------------------------------------------------

def _diag_split_labels(trades, oos_cfg, start, end):
    """Split ForensicsTrades into in_sample / validation / out_of_sample."""
    seg = OosConfig(oos_cfg.oos_fraction, oos_cfg.validation_fraction).split(start, end)
    out = {"in_sample": [], "validation": [], "out_of_sample": []}
    for t in trades:
        for label, (s, e) in seg.items():
            if s <= t.signal_time < e:
                out[label].append(t)
                break
    return out


def _diag_summary(label, stats):
    """Emit one bullet block for an IS/Val/OOS split of a diagnostic pool."""
    lines = [f"#### {label}"]
    for seg_name in ("in_sample", "validation", "out_of_sample"):
        m = _variant_metrics_from_forensics(stats.get(seg_name, []))
        lines.append(
            f"- {seg_name}: signals={m.total_signals} filled={m.filled} "
            f"closed={m.closed_trades} win_rate={_pct(m.win_rate)} "
            f"expectancy={_f(m.expectancy)} PF={_pf(m.profit_factor)} "
            f"total_R={_f(m.total_r)}"
        )
    lines.append("")
    return lines


def _variant_metrics_from_forensics(trades) -> BacktestMetrics:
    """Compute metrics over a list of ForensicsTrade (r_multiple + entry/exit fields).

    ForensicsTrade uses the V1 exit statuses (STOPPED / TP1_HIT / TP2_HIT /
    EXPIRED), so the production _compute_group applies unchanged.
    """
    synthetic: List[TradeResult] = []
    for t in trades:
        tr = TradeResult(
            signal_id=t.signal_id,
            symbol=t.symbol,
            direction=t.direction,
            signal_time=t.signal_time,
            entry_low=t.entry_low,
            entry_high=t.entry_high,
            entry_price=t.entry_price,
            stop_loss=t.stop_loss,
            tp1=t.tp1,
            tp2=t.tp2,
            score=t.score,
            market_regime=t.market_regime,
            entry_status=t.entry_status,
            exit_status=t.exit_status,
            exit_time=t.exit_time,
            exit_price=t.exit_price,
            r_multiple=t.r_multiple,
            tp1_hit=t.tp1_hit,
            tp2_hit=t.tp2_hit,
            sl_hit=t.sl_hit,
            holding_candles=t.holding_candles,
            fees_enabled=t.fees_enabled,
        )
        synthetic.append(tr)
    return _compute_group(synthetic) if synthetic else _compute_group([])


def _diag_adx_40plus(ft_list, oos_cfg, start, end) -> Dict:
    """H4: ADX >= 40 trades, split by LONG/SHORT and by IS/Val/OOS."""
    pool = [t for t in ft_list if t.adx is not None and float(t.adx) >= 40]
    seg = _diag_split_labels(pool, oos_cfg, start, end)
    seg["LONG"] = [t for t in pool if t.direction == "LONG"]
    seg["SHORT"] = [t for t in pool if t.direction == "SHORT"]
    seg["count"] = len(pool)
    return seg


def _diag_vol_2plus(ft_list, oos_cfg, start, end) -> Dict:
    """H5: volume-ratio >= 2.0 trades, does poor performance persist OOS?"""
    pool = [t for t in ft_list if t.volume_ratio is not None and float(t.volume_ratio) >= 2.0]
    seg = _diag_split_labels(pool, oos_cfg, start, end)
    seg["count"] = len(pool)
    return seg


def _diag_short_rsi_lt35(ft_list, oos_cfg, start, end) -> Dict:
    """H7: SHORT with RSI < 35. Do NOT assume the positive bucket is a real edge."""
    pool = [t for t in ft_list if t.direction == "SHORT"
            and t.rsi is not None and float(t.rsi) < 35]
    seg = _diag_split_labels(pool, oos_cfg, start, end)
    seg["count"] = len(pool)
    return seg


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

@dataclass
class ExperimentRun:
    data_dir: str
    symbols: List[str]
    start: datetime
    end: datetime
    oos_cfg: OosConfig
    model: ExecutionModel
    variants: Dict[str, List[ExperimentTrade]]
    atr_lookup: Dict[str, Dict[datetime, Optional[Decimal]]]
    forensics_trades: List[ForensicsTrade]


def run_experiments(
    data_dir: str = "data",
    symbols: Optional[List[str]] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    warmup_min: int = DEFAULT_WARMUP_MIN,
    oos_fraction: float = 0.3,
    validation_fraction: float = 0.2,
    data: Optional[Dict[str, HistoricalDataset]] = None,
) -> ExperimentRun:
    cfg = get_config()
    syms = symbols or list(cfg.symbols)

    if data is None:
        from backtest.baseline import _infer_period
        if start is None or end is None:
            start, end = _infer_period(data_dir, syms)
        data = {}
        for s in syms:
            ds = load_csv_dir(data_dir, s)
            if not ds.tf("5m"):
                continue
            data[s] = ds
    else:
        if start is None or end is None:
            earliest = min(min(c.timestamp for c in ds.tf("5m")) for ds in data.values())
            from app import data_validation as dv
            latest = max(dv.candle_close_time(ds.tf("5m")[-1], "5m") for ds in data.values())
            start, end = earliest, latest

    oos_cfg = OosConfig(oos_fraction=oos_fraction, validation_fraction=validation_fraction)
    model = ExecutionModel(
        maker_fee=Decimal("0"),
        taker_fee=Decimal("0"),
        slippage_bps=Decimal("0"),
        tp_model=TP_MODEL_50PCT,
        enable_fees=False,
        max_candles_after_signal=DEFAULT_MAX_HOLD_CANDLES,
    )
    atr_lookup = _build_atr_lookup(data, cfg)
    variants = {
        v.label: _run_variant(data, v, model, start, end, warmup_min, atr_lookup)
        for v in VARIANTS
    }
    # Forensics trades are only needed for the H4/H5/H7 diagnostics; we
    # enrich the V1 baseline variant (the untouched control) so the diagnostic
    # buckets are computed on exactly the baseline set.
    from backtest.forensics import compute_forensics_trades
    ft_list = compute_forensics_trades(
        [et.trade for et in variants["V1_BASELINE"]], data, cfg,
    )
    return ExperimentRun(
        data_dir=data_dir, symbols=list(data.keys()), start=start, end=end,
        oos_cfg=oos_cfg, model=model, variants=variants, atr_lookup=atr_lookup,
        forensics_trades=ft_list,
    )


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------

def _f(v, spec=".4f"):
    return "N/A" if v is None else format(v, spec)


def _pct(v):
    return "N/A" if v is None else f"{v * 100:.1f}%"


def _pf(v):
    return "N/A" if v is None else f"{v:.3f}"


def _metrics_row(label: str, m: BacktestMetrics) -> List[str]:
    return [
        label,
        str(m.total_signals),
        str(m.filled),
        str(m.closed_trades),
        _pct(m.win_rate),
        _f(m.expectancy),
        _pf(m.profit_factor),
        _f(m.total_r),
        _f(m.max_drawdown_r),
        str(m.max_consecutive_losses),
        _pct(m.tp1_hit_rate),
        _pct(m.tp2_hit_rate),
        _pct(m.sl_rate),
        _pct(m.expired / m.total_signals if m.total_signals else None),
        _f(m.avg_holding_candles, ".2f"),
    ]


_METRIC_HEADERS = [
    "variant", "signals", "filled", "closed", "win_rate",
    "expectancy", "PF", "total_R", "max_DD_R", "max_consec_losses",
    "TP1", "TP2", "SL", "expired", "avg_holding",
]


def _segment_metrics_block(run: ExperimentRun, label: str, seg: str) -> str:
    trades = _assign_oos(run.variants[label], run.oos_cfg, run.start, run.end)[seg]
    m = _variant_metrics(trades)
    return (
        f"| {label} | {seg} | {m.total_signals} | {m.filled} | {m.closed_trades} | "
        f"{_pct(m.win_rate)} | {_f(m.expectancy)} | {_pf(m.profit_factor)} | "
        f"{_f(m.total_r)} | {_f(m.max_drawdown_r)} | {m.max_consecutive_losses} |"
    )


def _segment_rows(run: ExperimentRun, seg: str) -> List[str]:
    L = ["| " + " | ".join(
        ["variant", "segment", "signals", "filled", "closed", "win_rate",
         "expectancy", "PF", "total_R", "max_DD_R", "max_consec_losses"]) + " |",
         "|" + "---|" * 11]
    for v in VARIANTS:
        L.append(_segment_metrics_block(run, v.label, seg))
    return L


def _delta(cur, base):
    if cur is None or base is None:
        return "N/A"
    return f"{(cur - base):+.4f}"


def _dir_block(by_direction, d):
    m = by_direction.get(d)
    if m is None:
        return "n/a"
    return f"{_pct(m.win_rate)} / PF {_pf(m.profit_factor)} / total_R {_f(m.total_r)}"


def build_report(run: ExperimentRun, cfg) -> str:
    L: List[str] = []
    L.append("# PHASE 7 — CONTROLLED EXIT-MANAGEMENT EXPERIMENTS")
    L.append("")
    L.append("> **DISCLAIMER:** Historical simulation. No winner is declared, "
             "no strategy is changed, and no live trading is performed. "
             "Variants are compared to the V1 baseline only. Numbers are reported "
             "as measurements, not rankings.")
    L.append("")
    L.append(f"- Dataset: `{run.data_dir}`, symbols={run.symbols}")
    L.append(f"- Period: {run.start} -> {run.end}")
    L.append(
        f"- OOS split: in_sample={1 - run.oos_cfg.oos_fraction - run.oos_cfg.validation_fraction:.1f} "
        f"/ validation={run.oos_cfg.validation_fraction:.1f} / "
        f"out_of_sample={run.oos_cfg.oos_fraction:.1f}"
    )
    L.append("- Execution model: gross (fees=0, slippage=0), TP_MODEL_50PCT, "
             f"max_hold={DEFAULT_MAX_HOLD_CANDLES}")
    L.append("- Same signals / entry model / OOS split for every variant; "
             "only post-entry exit management differs.")
    L.append("- Experimental closed-set = {STOPPED, TP1_HIT, TP2_HIT, BE_HIT, "
             "TRAIL_HIT, TIME_EXIT}. V1_BASELINE uses the production "
             "{STOPPED, TP1_HIT, TP2_HIT} set, so its numbers are identical "
             "to the PHASE 6 baseline.")
    L.append("")

    # 1. Configuration
    L.append("## 1. Variant Configurations")
    L.append("")
    L.append("| variant | kind | ATR_multiple | time_exit_candles | partial_fraction |")
    L.append("|---|---|---|---|---|")
    for v in VARIANTS:
        L.append(
            f"| {v.label} | {v.kind} | {v.atr_multiple if v.atr_multiple is not None else '—'} | "
            f"{v.time_exit_candles or '—'} | {v.partial_fraction if v.partial_fraction is not None else '—'} |"
        )
    L.append("")
    L.append("V1_BASELINE is the untouched production exit model "
             "(SL / TP1 / TP2 / EXPIRED, TP_MODEL_50PCT). Other rows are "
             "experimental exit-management variants.")
    L.append("")

    # 2. Primary metrics
    L.append("## 2. Primary Metrics (ALL period)")
    L.append("")
    L.append("| " + " | ".join(_METRIC_HEADERS) + " |")
    L.append("|" + "---|" * len(_METRIC_HEADERS))
    for v in VARIANTS:
        m = _variant_metrics(run.variants[v.label])
        L.append("| " + " | ".join(_metrics_row(v.label, m)) + " |")
    L.append("")

    # 3. Direction split
    L.append("## 3. LONG / SHORT Split (ALL)")
    L.append("")
    for v in VARIANTS:
        m = _variant_metrics(run.variants[v.label])
        L.append(f"- **{v.label}**  LONG: {_dir_block(m.by_direction, 'LONG')}")
        L.append(f"  SHORT: {_dir_block(m.by_direction, 'SHORT')}")
    L.append("")

    # 4. IS / Val / OOS separated
    L.append("## 4. OOS Separation (IS / Validation / OOS, never combined)")
    L.append("")
    for seg in ("in_sample", "validation", "out_of_sample"):
        L.append(f"### {seg}")
        L.append("")
        L.extend(_segment_rows(run, seg))
        L.append("")
    L.append("> **OOS rows are the primary robustness reference.** "
             "In-sample and validation rows are reported separately and "
             "never combined with OOS into a single headline number.")
    L.append("")

    # 5. OOS-only metrics block
    L.append("## 5. OOS-Only Metrics (out_of_sample window)")
    L.append("")
    L.extend(_segment_rows(run, "out_of_sample"))
    L.append("")

    # 6. Baseline deltas
    L.append("## 6. Baseline Deltas (vs V1_BASELINE)")
    L.append("")
    base_all = _variant_metrics(run.variants["V1_BASELINE"])
    base_oos = _variant_metrics(_assign_oos(run.variants["V1_BASELINE"],
                                             run.oos_cfg, run.start, run.end)["out_of_sample"])
    L.append("| variant | ΔPF (all) | Δexpect (all) | Δtotal_R (all) | ΔmaxDD (all) | Δwin_rate (all) | "
             "ΔPF (OOS) | Δexpect (OOS) | Δtotal_R (OOS) | ΔmaxDD (OOS) | Δwin_rate (OOS) |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for v in VARIANTS:
        if v.label == "V1_BASELINE":
            L.append("| " + v.label + " | — | — | — | — | — | — | — | — | — | — |")
            continue
        m_all = _variant_metrics(run.variants[v.label])
        m_oos = _variant_metrics(_assign_oos(run.variants[v.label],
                                              run.oos_cfg, run.start, run.end)["out_of_sample"])
        L.append("| " + v.label + " | "
                 + _delta(m_all.profit_factor, base_all.profit_factor) + " | "
                 + _delta(m_all.expectancy, base_all.expectancy) + " | "
                 + _delta(m_all.total_r, base_all.total_r) + " | "
                 + _delta(m_all.max_drawdown_r, base_all.max_drawdown_r) + " | "
                 + _delta(m_all.win_rate, base_all.win_rate) + " | "
                 + _delta(m_oos.profit_factor, base_oos.profit_factor) + " | "
                 + _delta(m_oos.expectancy, base_oos.expectancy) + " | "
                 + _delta(m_oos.total_r, base_oos.total_r) + " | "
                 + _delta(m_oos.max_drawdown_r, base_oos.max_drawdown_r) + " | "
                 + _delta(m_oos.win_rate, base_oos.win_rate) + " |")
    L.append("")
    L.append("> Deltas are simple differences vs V1_BASELINE (experimental − baseline). "
             "Not a ranking. A variant is a CANDIDATE only if the OOS delta is "
             "positive and not driven by a single month or symbol (see §7).")
    L.append("")

    # 7. Robustness OOS breakdowns
    L.append("## 7. Robustness — OOS Breakdowns")
    L.append("")
    L.append("> An experimental rule is only a CANDIDATE if the improvement is "
             "visible IN OOS and NOT driven by a single month/symbol/regime.")
    L.append("")
    for v in VARIANTS:
        L.append(f"### {v.label}")
        L.append("")
        oos_trades = _assign_oos(run.variants[v.label], run.oos_cfg, run.start, run.end)["out_of_sample"]
        L.append(_group_block(oos_trades, lambda t: t.symbol, "Per-symbol OOS"))
        L.append(_group_block(oos_trades, lambda t: t.signal_time.strftime("%Y-%m"), "Monthly OOS"))
        L.append(_group_block(oos_trades, lambda t: t.market_regime, "Regime OOS"))

    # 8. H4 / H5 / H7 diagnostics
    L.append("## 8. Diagnostics (diagnostic only, no parameter changes)")
    L.append("")
    L.append("### 8.1 H4 — ADX >= 40")
    L.append("")
    L.append("_Diagnostic: report trades where ADX>=40, separated by LONG/SHORT and IS/Val/OOS._")
    L.append("")
    adx40 = _diag_adx_40plus(run.forensics_trades, run.oos_cfg, run.start, run.end)
    L.append(f"- Total ADX>=40 signals: **{adx40['count']}**")
    L.append(f"- LONG: **{len(adx40.get('LONG', []))}**   SHORT: **{len(adx40.get('SHORT', []))}**")
    L.extend(_diag_summary("ADX>=40", adx40))
    L.append("")

    L.append("### 8.2 H5 — Volume ratio >= 2.0")
    L.append("")
    L.append("_Diagnostic: report trades where volume_ratio>=2.0; does poor performance persist OOS?_")
    L.append("")
    vol2 = _diag_vol_2plus(run.forensics_trades, run.oos_cfg, run.start, run.end)
    L.append(f"- Total vol>=2.0 signals: **{vol2['count']}**")
    L.extend(_diag_summary("Volume>=2.0x", vol2))
    L.append("")

    L.append("### 8.3 H7 — SHORT RSI < 35")
    L.append("")
    L.append("_Diagnostic: sample size / in-sample / validation / OOS for SHORT RSI<35. "
             "Do NOT assume the positive bucket represents a real edge._")
    L.append("")
    h7 = _diag_short_rsi_lt35(run.forensics_trades, run.oos_cfg, run.start, run.end)
    L.append(f"- Total SHORT RSI<35 signals: **{h7['count']}**")
    L.extend(_diag_summary("SHORT RSI<35", h7))
    L.append("")

    L.append("## 9. NO-WINNER STATEMENT")
    L.append("")
    L.append("No variant is declared the 'best', 'winning', 'optimal', or "
             "'guaranteed improvement'. The only claim is that each variant "
             "produced the measurements above relative to V1_BASELINE. A "
             "human decision is required before any variant becomes V2.")
    L.append("")

    L.append("## DISCLAIMER")
    L.append("Historical simulation only. No parameter optimization was "
             "performed. No new indicators were introduced. No Telegram "
             "integration. No trading execution. Production strategy files "
             "are unchanged.")
    return "\n".join(L)


def write_report(run: ExperimentRun, cfg, report_dir: str = "reports") -> str:
    md = build_report(run, cfg)
    os.makedirs(report_dir, exist_ok=True)
    path = os.path.join(report_dir, "phase7_experiments.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(md)
    return path


def cli_main() -> None:
    """CLI entry:
        python -m backtest.experiments.run --data-dir data --report-dir reports
    """
    import argparse
    from app.trading_guard import assert_no_execution_code
    assert_no_execution_code()
    parser = argparse.ArgumentParser("PHASE 7 controlled experiments runner")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--report-dir", default="reports")
    parser.add_argument("--symbols", default=None)
    parser.add_argument("--warmup", type=int, default=DEFAULT_WARMUP_MIN)
    parser.add_argument("--oos-fraction", type=float, default=0.3)
    parser.add_argument("--val-fraction", type=float, default=0.2)
    args = parser.parse_args()
    symbols = None
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    run = run_experiments(
        data_dir=args.data_dir,
        symbols=symbols,
        warmup_min=args.warmup,
        oos_fraction=args.oos_fraction,
        validation_fraction=args.val_fraction,
    )
    path = write_report(run, get_config(), args.report_dir)
    print(f"PHASE 7 experiments report written to: {path}")
    for label, et_list in run.variants.items():
        m = _variant_metrics(et_list)
        print(f"  {label}: signals={m.total_signals} closed={m.closed_trades} "
              f"win_rate={_pct(m.win_rate)} PF={_pf(m.profit_factor)} "
              f"total_R={_f(m.total_r)}")


if __name__ == "__main__":
    cli_main()
