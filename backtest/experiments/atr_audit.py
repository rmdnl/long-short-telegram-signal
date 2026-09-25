"""
PHASE 7.5: H1_ATR_TRAIL OOS stability audit.

Stability audit of the Phase 7 candidate `H1_ATR_TRAIL` on the locked OOS
split. This module is a PURE diagnostic layer. It:

- reuses the EXACT Phase 7 infrastructure (run_experiments), so the dataset,
  period, symbols, entry model, fees/slippage, OOS split, decision points,
  and no-lookahead rules are identical to Phase 7;
- does NOT download a new dataset;
- does NOT change any ATR-trail parameter, entry logic, or production file;
- does NOT grid-search or evaluate alternative trail/activation values;
- reports H1_ATR_TRAIL only as a "candidate" / "candidate V2" — it is never
  called a winner / best / optimal / proven profitable.

All numbers are measurements on OOS, compared descriptively against
V1_BASELINE. A human decides whether the candidate becomes V2.
"""
import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from backtest.experiments.runner import (
    ExperimentRun,
    run_experiments,
    _assign_oos,
    _compute_group_exp,
    _PRODUCTION_CLOSED,
)
from backtest.experiments.simulator import (
    ExperimentTrade,
    EXPERIMENTAL_CLOSED_STATUSES,
)
from backtest.forensics import compute_forensics_trades
from backtest.metrics import _max_drawdown_r

# ADX regime buckets (Phase 7.5 fixed ranges — not optimized)
ADX_REGIME_BUCKETS: List[Tuple[str, Optional[float], Optional[float]]] = [
    ("ADX<22", None, 22.0),
    ("22-30", 22.0, 30.0),
    ("30-40", 30.0, 40.0),
    ("ADX>=40", 40.0, None),
]

# ATR-trail activation threshold (Phase 7 candidate value). Read-only.
ATR_ACTIVATION_MULTIPLE = Decimal("2.0")


# ---------------------------------------------------------------------------
# Output containers
# ---------------------------------------------------------------------------

@dataclass
class RDistributable:
    """R-multiple distribution summary over a list of closed trades."""
    closed_trades: int
    mean: Optional[float]
    median: Optional[float]
    min_r: Optional[float]
    max_r: Optional[float]
    p10: Optional[float]
    p25: Optional[float]
    p75: Optional[float]
    p90: Optional[float]
    stddev: Optional[float]
    count_lt_neg1: int
    count_eq_0: int
    count_gt_0: int
    count_ge_1: int
    count_ge_2: int
    out_of_bounds: int


@dataclass
class DDRecord:
    """Drawdown record for a closed-trade R sequence."""
    closed_trades: int
    max_drawdown_r: Optional[float]
    max_dd_duration_candles: int
    max_dd_duration_hours: float
    recovery_candles: Optional[int]
    recovery_hours: Optional[float]
    cumulative_r_end: Optional[float]


@dataclass
class MonthSlice:
    """Metrics for one group (month / symbol / regime)."""
    label: str
    closed: int
    total_r: Optional[float]
    expectancy: Optional[float]
    pf: Optional[float]
    win_rate: Optional[float]
    max_dd_r: Optional[float]


@dataclass
class RobustnessRow:
    scenario: str
    closed: int
    total_r: Optional[float]
    pf: Optional[float]
    expectancy: Optional[float]


@dataclass
class SourceOfImprovement:
    v1_sl_count: int
    atr_sl_count: int
    v1_avg_loss_r: Optional[float]
    atr_avg_loss_r: Optional[float]
    v1_favorable_exit_count: int
    atr_favorable_exit_count: int
    v1_avg_holding: Optional[float]
    atr_avg_holding: Optional[float]
    v1_median_r: Optional[float]
    atr_median_r: Optional[float]
    v1_p25_r: Optional[float]
    atr_p25_r: Optional[float]
    atr_top5_contrib_r: Optional[float]
    atr_top5_share: Optional[float]
    atr_monthly_pos_count: int
    atr_monthly_neg_count: int
    atr_symbol_pos_count: int
    atr_symbol_neg_count: int
    atr_adx_bucket_best: str
    atr_adx_bucket_best_r: Optional[float]


@dataclass
class AuditResult:
    run: ExperimentRun
    oos_window: Tuple[datetime, datetime]
    v1_oos: List[ExperimentTrade]
    atr_oos: List[ExperimentTrade]
    v1_closed: frozenset
    atr_closed: frozenset
    v1_rdist: RDistributable
    atr_rdist: RDistributable
    v1_monthly: List[MonthSlice]
    atr_monthly: List[MonthSlice]
    v1_symbol: List[MonthSlice]
    atr_symbol: List[MonthSlice]
    atr_symbol_improvement: int
    atr_symbol_deterioration: int
    v1_adx: List[MonthSlice]
    atr_adx: List[MonthSlice]
    v1_dd: DDRecord
    atr_dd: DDRecord
    soi: SourceOfImprovement
    robustness: Dict


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def _rdist(trades: List[ExperimentTrade], closed: frozenset) -> RDistributable:
    rs = [float(t.trade.r_multiple) for t in trades if t.trade.exit_status in closed]
    n = len(rs)
    if n == 0:
        return RDistributable(
            closed_trades=0, mean=None, median=None, min_r=None, max_r=None,
            p10=None, p25=None, p75=None, p90=None, stddev=None,
            count_lt_neg1=0, count_eq_0=0, count_gt_0=0, count_ge_1=0,
            count_ge_2=0, out_of_bounds=0,
        )
    s = sorted(rs)
    mean = sum(rs) / n
    var = sum((r - mean) ** 2 for r in rs) / n
    return RDistributable(
        closed_trades=n,
        mean=mean,
        median=s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2,
        min_r=s[0], max_r=s[-1],
        p10=s[int(0.10 * (n - 1))],
        p25=s[int(0.25 * (n - 1))],
        p75=s[int(0.75 * (n - 1))],
        p90=s[int(0.90 * (n - 1))],
        stddev=math.sqrt(var),
        count_lt_neg1=sum(1 for r in rs if r < -1.0),
        count_eq_0=sum(1 for r in rs if r == 0.0),
        count_gt_0=sum(1 for r in rs if r > 0.0),
        count_ge_1=sum(1 for r in rs if r >= 1.0),
        count_ge_2=sum(1 for r in rs if r >= 2.0),
        out_of_bounds=sum(1 for r in rs if r > 10.0),
    )


def _dd_record(trades: List[ExperimentTrade], closed: frozenset) -> DDRecord:
    """Drawdown over the sequential closed-trade R sequence (OOS order)."""
    ordered = sorted(
        [t for t in trades if t.trade.exit_status in closed],
        key=lambda t: t.trade.signal_time,
    )
    rs = [float(t.trade.r_multiple) for t in ordered]
    n = len(rs)
    if n == 0:
        return DDRecord(0, None, 0, 0.0, None, None, None)
    cum = 0.0
    peak = 0.0
    peak_idx = 0
    cur_dd = 0.0
    dd_start_idx = 0
    max_dd = 0.0
    max_dd_dur = 0
    max_dd_start_idx = 0
    for i, r in enumerate(rs):
        cum += r
        if cum > peak:
            peak = cum
            peak_idx = i
        dd = peak - cum
        if dd > cur_dd:
            cur_dd = dd
            dd_start_idx = peak_idx
        if cum >= peak:
            cur_dd = 0.0
        if cur_dd > max_dd:
            max_dd = cur_dd
            max_dd_start_idx = dd_start_idx
            max_dd_dur = i - dd_start_idx + 1
    recovery_candles = None
    recovery_hours = None
    if max_dd > 0 and max_dd_start_idx >= 0:
        cum_at_trough = sum(rs[:max_dd_start_idx + 1])
        target = sum(rs[:max_dd_start_idx]) + max_dd if max_dd_start_idx > 0 else max_dd
        running = cum_at_trough
        for j in range(max_dd_start_idx + 1, n):
            running += rs[j]
            if running >= target:
                recovery_candles = j - max_dd_start_idx
                break
    max_dd_duration_hours = max_dd_dur * 5.0 / 60.0
    if recovery_candles is not None:
        recovery_hours = recovery_candles * 5.0 / 60.0
    return DDRecord(
        closed_trades=n,
        max_drawdown_r=round(max_dd, 6),
        max_dd_duration_candles=max_dd_dur,
        max_dd_duration_hours=round(max_dd_duration_hours, 4),
        recovery_candles=recovery_candles,
        recovery_hours=recovery_hours,
        cumulative_r_end=round(cum, 6),
    )


def _group_slices(trades: List[ExperimentTrade], key_fn, closed: frozenset,
                  label_fn=None) -> List[MonthSlice]:
    out: Dict[str, List[ExperimentTrade]] = {}
    for t in trades:
        k = key_fn(t.trade)
        if k is None:
            continue
        out.setdefault(str(k), []).append(t)
    slices: List[MonthSlice] = []
    for k in sorted(out):
        m = _compute_group_exp([e.trade for e in out[k]], closed, group_by=False)
        s = MonthSlice(
            label=label_fn(k) if label_fn else k,
            closed=m.closed_trades,
            total_r=m.total_r,
            expectancy=m.expectancy,
            pf=m.profit_factor,
            win_rate=m.win_rate,
            max_dd_r=m.max_drawdown_r,
        )
        slices.append(s)
    return slices


def _adx_bucket(r) -> Optional[str]:
    if r.adx is None:
        return None
    adx = float(r.adx)
    for name, lo, hi in ADX_REGIME_BUCKETS:
        if (lo is None or adx >= lo) and (hi is None or adx < hi):
            return name
    return "ADX>=40"


def _build_adx_map(forensics_trades) -> Dict[Tuple, Optional[Decimal]]:
    """Map (symbol, signal_time) -> ADX value from forensics enrichment."""
    out: Dict[Tuple, Optional[Decimal]] = {}
    for ft in forensics_trades:
        out[(ft.symbol, ft.signal_time)] = ft.adx
    return out


def _adx_group_sliced(adx_map, trades, closed, buckets) -> List[MonthSlice]:
    out: Dict[str, List[ExperimentTrade]] = {}
    for t in trades:
        adx = adx_map.get((t.trade.symbol, t.trade.signal_time))
        if adx is None:
            continue
        adx_f = float(adx)
        for name, lo, hi in buckets:
            if (lo is None or adx_f >= lo) and (hi is None or adx_f < hi):
                out.setdefault(name, []).append(t)
                break
    slices: List[MonthSlice] = []
    for name, _lo, _hi in buckets:
        bucket = out.get(name, [])
        if not bucket:
            continue
        m = _compute_group_exp([e.trade for e in bucket], closed, group_by=False)
        slices.append(MonthSlice(
            label=name,
            closed=m.closed_trades,
            total_r=m.total_r,
            expectancy=m.expectancy,
            pf=m.profit_factor,
            win_rate=m.win_rate,
            max_dd_r=m.max_drawdown_r,
        ))
    return slices


def _source_of_improvement(
    v1_oos, atr_oos, v1_closed, atr_closed,
    atr_monthly, atr_symbol, atr_adx,
) -> SourceOfImprovement:
    v1_rs = [float(t.trade.r_multiple) for t in v1_oos if t.trade.exit_status in v1_closed]
    atr_rs = [float(t.trade.r_multiple) for t in atr_oos if t.trade.exit_status in atr_closed]

    v1_losses = [r for r in v1_rs if r < 0]
    atr_losses = [r for r in atr_rs if r < 0]
    v1_avg_loss = sum(v1_losses) / len(v1_losses) if v1_losses else None
    atr_avg_loss = sum(atr_losses) / len(atr_losses) if atr_losses else None

    v1_favorable = sum(1 for t in v1_oos
                       if t.trade.exit_status in {"TP1_HIT", "TP2_HIT"})
    atr_fav_statuses = {"BE_HIT", "TRAIL_HIT", "TP1_HIT", "TP2_HIT"}
    atr_favorable = sum(1 for t in atr_oos if t.trade.exit_status in atr_fav_statuses)

    v1_holding = [t.trade.holding_candles for t in v1_oos
                  if t.trade.entry_status == "FILLED"]
    atr_holding = [t.trade.holding_candles for t in atr_oos
                   if t.trade.entry_status == "FILLED"]
    v1_avg_h = sum(v1_holding) / len(v1_holding) if v1_holding else None
    atr_avg_h = sum(atr_holding) / len(atr_holding) if atr_holding else None

    def _median(data):
        if not data:
            return None
        s = sorted(data)
        n = len(s)
        return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2

    def _p25(data):
        if not data:
            return None
        s = sorted(data)
        n = len(s)
        return s[int(0.25 * (n - 1))]

    atr_monthly_pos = sum(1 for r in atr_monthly if (r.total_r or 0) > 0)
    atr_monthly_neg = sum(1 for r in atr_monthly if (r.total_r or 0) < 0)
    atr_symbol_pos = sum(1 for r in atr_symbol if (r.total_r or 0) > 0)
    atr_symbol_neg = sum(1 for r in atr_symbol if (r.total_r or 0) < 0)

    best_label = "N/A"
    best_r: Optional[float] = None
    for r in atr_adx:
        tr = r.total_r or 0.0
        if best_r is None or tr > best_r:
            best_r = tr
            best_label = r.label

    atr_top5 = sorted(atr_rs, reverse=True)[:5]
    atr_top5_contrib = sum(atr_top5) if atr_top5 else 0.0
    atr_total = sum(atr_rs) if atr_rs else 0.0
    atr_top5_share = atr_top5_contrib / atr_total if atr_total != 0 else None

    return SourceOfImprovement(
        v1_sl_count=sum(1 for r in v1_rs if r <= -1.0),
        atr_sl_count=sum(1 for r in atr_rs if r <= -1.0),
        v1_avg_loss_r=v1_avg_loss,
        atr_avg_loss_r=atr_avg_loss,
        v1_favorable_exit_count=v1_favorable,
        atr_favorable_exit_count=atr_favorable,
        v1_avg_holding=v1_avg_h,
        atr_avg_holding=atr_avg_h,
        v1_median_r=_median(v1_rs),
        atr_median_r=_median(atr_rs),
        v1_p25_r=_p25(v1_rs),
        atr_p25_r=_p25(atr_rs),
        atr_top5_contrib_r=atr_top5_contrib,
        atr_top5_share=atr_top5_share,
        atr_monthly_pos_count=atr_monthly_pos,
        atr_monthly_neg_count=atr_monthly_neg,
        atr_symbol_pos_count=atr_symbol_pos,
        atr_symbol_neg_count=atr_symbol_neg,
        atr_adx_bucket_best=best_label,
        atr_adx_bucket_best_r=best_r,
    )


def _robustness(trades: List[ExperimentTrade], closed: frozenset) -> Dict:
    rows: List[RobustnessRow] = []
    all_rs = [float(t.trade.r_multiple)
              for t in trades if t.trade.exit_status in closed]
    all_total = sum(all_rs) if all_rs else None

    def _agg(subset: List[ExperimentTrade], label: str) -> RobustnessRow:
        rs = [float(t.trade.r_multiple)
              for t in subset if t.trade.exit_status in closed]
        if not rs:
            return RobustnessRow(label, 0, None, None, None)
        m = _compute_group_exp([t.trade for t in subset], closed, group_by=False)
        return RobustnessRow(label, m.closed_trades, m.total_r, m.profit_factor, m.expectancy)

    # scenario: exclude best month
    def month_total(trades_list):
        out = {}
        for t in trades_list:
            if t.trade.exit_status not in closed:
                continue
            key = t.trade.signal_time.strftime("%Y-%m")
            out[key] = out.get(key, 0.0) + float(t.trade.r_multiple)
        return out

    mt = month_total(trades)
    if mt:
        best_month = max(mt, key=lambda k: mt[k])
        excluded = [t for t in trades
                    if t.trade.signal_time.strftime("%Y-%m") != best_month]
        rows.append(_agg(excluded, f"exclude_best_month_{best_month}"))
    # scenario: exclude worst month
    if mt:
        worst_month = min(mt, key=lambda k: mt[k])
        excluded = [t for t in trades
                    if t.trade.signal_time.strftime("%Y-%m") != worst_month]
        rows.append(_agg(excluded, f"exclude_worst_month_{worst_month}"))
    # scenario: exclude best symbol
    st: Dict[str, float] = {}
    for t in trades:
        if t.trade.exit_status not in closed:
            continue
        st[t.trade.symbol] = st.get(t.trade.symbol, 0.0) + float(t.trade.r_multiple)
    if st:
        best_sym = max(st, key=lambda k: st[k])
        excluded = [t for t in trades if t.trade.symbol != best_sym]
        rows.append(_agg(excluded, f"exclude_best_symbol_{best_sym}"))
    # scenario: exclude worst symbol
    if st:
        worst_sym = min(st, key=lambda k: st[k])
        excluded = [t for t in trades if t.trade.symbol != worst_sym]
        rows.append(_agg(excluded, f"exclude_worst_symbol_{worst_sym}"))

    best_month = max(mt, key=lambda k: mt[k]) if mt else None
    worst_month = min(mt, key=lambda k: mt[k]) if mt else None
    best_sym = max(st, key=lambda k: st[k]) if st else None
    worst_sym = min(st, key=lambda k: st[k]) if st else None
    detail = {
        "best_month": best_month,
        "worst_month": worst_month,
        "best_symbol": best_sym,
        "worst_symbol": worst_sym,
        "best_month_total_r": mt.get(best_month) if best_month else None,
        "worst_month_total_r": mt.get(worst_month) if worst_month else None,
        "best_symbol_total_r": st.get(best_sym) if best_sym else None,
        "worst_symbol_total_r": st.get(worst_sym) if worst_sym else None,
    }
    return {"rows": rows, "detail": detail}


def run_audit(data_dir: str = "data") -> AuditResult:
    """Run the locked Phase 7 experiment set, isolate the OOS window, and
    compute every Phase 7.5 audit quantity for H1_ATR_TRAIL vs V1_BASELINE."""
    run = run_experiments(data_dir=data_dir)
    oos_window = run.oos_cfg.split(run.start, run.end)["out_of_sample"]

    v1_oos = _assign_oos(run.variants["V1_BASELINE"], run.oos_cfg,
                         run.start, run.end)["out_of_sample"]
    atr_oos = _assign_oos(run.variants["H1_ATR_TRAIL"], run.oos_cfg,
                          run.start, run.end)["out_of_sample"]

    v1_closed = _PRODUCTION_CLOSED
    atr_closed = EXPERIMENTAL_CLOSED_STATUSES

    v1_rdist = _rdist(v1_oos, v1_closed)
    atr_rdist = _rdist(atr_oos, atr_closed)

    def month_key(tr):
        return tr.signal_time.strftime("%Y-%m")

    v1_monthly = _group_slices(v1_oos, month_key, v1_closed)
    atr_monthly = _group_slices(atr_oos, month_key, atr_closed)
    v1_symbol = _group_slices(v1_oos, lambda tr: tr.symbol, v1_closed)
    atr_symbol = _group_slices(atr_oos, lambda tr: tr.symbol, atr_closed)

    v1_by_sym = {r.label: r for r in v1_symbol}
    atr_by_sym = {r.label: r for r in atr_symbol}
    atr_symbol_improvement = sum(
        1 for sym, ar in atr_by_sym.items()
        if (ar.total_r or 0) > (v1_by_sym.get(sym, ar).total_r or 0)
    )
    atr_symbol_deterioration = sum(
        1 for sym, ar in atr_by_sym.items()
        if (ar.total_r or 0) < (v1_by_sym.get(sym, ar).total_r or 0)
    )

    adx_map = _build_adx_map(run.forensics_trades)
    v1_adx = _adx_group_sliced(adx_map, v1_oos, v1_closed, ADX_REGIME_BUCKETS)
    atr_adx = _adx_group_sliced(adx_map, atr_oos, atr_closed, ADX_REGIME_BUCKETS)

    v1_dd = _dd_record(v1_oos, v1_closed)
    atr_dd = _dd_record(atr_oos, atr_closed)

    soi = _source_of_improvement(
        v1_oos, atr_oos, v1_closed, atr_closed,
        atr_monthly, atr_symbol, atr_adx,
    )
    robustness = _robustness(atr_oos, atr_closed)

    return AuditResult(
        run=run,
        oos_window=oos_window,
        v1_oos=v1_oos,
        atr_oos=atr_oos,
        v1_closed=v1_closed,
        atr_closed=atr_closed,
        atr_monthly=atr_monthly,
        v1_monthly=v1_monthly,
        atr_symbol=atr_symbol,
        v1_symbol=v1_symbol,
        atr_symbol_improvement=atr_symbol_improvement,
        atr_symbol_deterioration=atr_symbol_deterioration,
        atr_adx=atr_adx,
        v1_adx=v1_adx,
        atr_rdist=atr_rdist,
        v1_rdist=v1_rdist,
        atr_dd=atr_dd,
        v1_dd=v1_dd,
        soi=soi,
        robustness=robustness,
    )


def _f(v, spec=".4f"):
    return "N/A" if v is None else format(v, spec)


def _pct(v):
    return "N/A" if v is None else f"{v * 100:.1f}%"


def _pf(v):
    return "N/A" if v is None else f"{v:.3f}"


def _sign(v, spec=".2f"):
    if v is None:
        return "N/A"
    return format(v, "+" + spec)


def build_audit_report(res: AuditResult) -> str:
    """Render the Phase 7.5 H1_ATR_TRAIL OOS stability audit as markdown.

    Every number is a measurement on the OOS window, compared descriptively
    against V1_BASELINE. The candidate is never declared a winner.
    """
    run = res.run
    L: List[str] = []
    L.append("# PHASE 7.5 — H1_ATR_TRAIL OOS STABILITY AUDIT")
    L.append("")
    L.append("> **DISCLAIMER:** This is a stability audit of a single "
             "Phase 7 candidate (`H1_ATR_TRAIL`) on the locked OOS split. "
             "No new dataset was downloaded, no ATR-trail parameter was "
             "changed, no alternative trail/activation value was "
             "grid-searched, and no production file was modified. "
             "`H1_ATR_TRAIL` is reported only as a CANDIDATE / CANDIDATE-V2. "
             "It is never declared a winner, best, optimal, or proven "
             "profitable. A human decision is required before it becomes V2.")
    L.append("")
    L.append(f"- Dataset: `{run.data_dir}`, symbols={run.symbols}")
    L.append(f"- Period: {run.start} -> {run.end}")
    L.append(
        f"- OOS window: {res.oos_window[0]} -> {res.oos_window[1]} "
        f"(in_sample={1 - run.oos_cfg.oos_fraction - run.oos_cfg.validation_fraction:.1f} / "
        f"validation={run.oos_cfg.validation_fraction:.1f} / "
        f"out_of_sample={run.oos_cfg.oos_fraction:.1f})"
    )
    L.append("- Execution model: gross (fees=0, slippage=0), TP_MODEL_50PCT.")
    L.append("- V1_BASELINE is aggregated with the production closed-set "
             "{STOPPED, TP1_HIT, TP2_HIT}; H1_ATR_TRAIL is aggregated with "
             "the experimental closed-set that adds "
             "{BE_HIT, TRAIL_HIT, TIME_EXIT}.")
    L.append("")

    # 1. Headline comparison
    L.append("## 1. Headline OOS Comparison (V1 vs H1_ATR_TRAIL)")
    L.append("")
    L.append("| metric | V1_BASELINE | H1_ATR_TRAIL | Δ (ATR − V1) |")
    L.append("|---|---|---|---|")
    v1_total_r = sum(float(t.trade.r_multiple)
                     for t in res.v1_oos if t.trade.exit_status in res.v1_closed)
    atr_total_r = sum(float(t.trade.r_multiple)
                      for t in res.atr_oos if t.trade.exit_status in res.atr_closed)
    for label, v1v, atrv in [
        ("closed_signals", res.v1_rdist.closed_trades, res.atr_rdist.closed_trades),
        ("total_R", v1_total_r, atr_total_r),
        ("expectancy", res.v1_rdist.mean, res.atr_rdist.mean),
        ("median_R", res.v1_rdist.median, res.atr_rdist.median),
        ("mean_R", res.v1_rdist.mean, res.atr_rdist.mean),
        ("stddev_R", res.v1_rdist.stddev, res.atr_rdist.stddev),
        ("max_DD_R", res.v1_dd.max_drawdown_r, res.atr_dd.max_drawdown_r),
    ]:
        d = (atrv - v1v) if (v1v is not None and atrv is not None) else None
        L.append(f"| {label} | {_f(v1v)} | {_f(atrv)} | {_sign(d, '.4f')} |")
    L.append("")
    L.append("> Deltas are descriptive, not rankings. A positive total_R "
             "delta does NOT by itself make the candidate viable; see the "
             "robustness and source-of-improvement sections.")
    L.append("")

    # 2. R-distribution audit
    L.append("## 2. R-Distribution Audit (OOS, closed trades)")
    L.append("")
    L.append("| stat | V1_BASELINE | H1_ATR_TRAIL |")
    L.append("|---|---|---|")
    for key, label in [
        ("min_r", "min_R"), ("p10", "P10"), ("p25", "P25"), ("median", "median"),
        ("p75", "P75"), ("p90", "P90"), ("max_r", "max_R"),
        ("mean", "mean"), ("stddev", "stddev"),
    ]:
        L.append(f"| {label} | {_f(getattr(res.v1_rdist, key))} | "
                 f"{_f(getattr(res.atr_rdist, key))} |")
    L.append("| count(R < -1) | " + str(res.v1_rdist.count_lt_neg1) + " | "
             + str(res.atr_rdist.count_lt_neg1) + " |")
    L.append("| count(R = 0) | " + str(res.v1_rdist.count_eq_0) + " | "
             + str(res.atr_rdist.count_eq_0) + " |")
    L.append("| count(R > 0) | " + str(res.v1_rdist.count_gt_0) + " | "
             + str(res.atr_rdist.count_gt_0) + " |")
    L.append("| count(R >= 1) | " + str(res.v1_rdist.count_ge_1) + " | "
             + str(res.atr_rdist.count_ge_1) + " |")
    L.append("| count(R >= 2) | " + str(res.v1_rdist.count_ge_2) + " | "
             + str(res.atr_rdist.count_ge_2) + " |")
    L.append("| out-of-bound_R | " + str(res.v1_rdist.out_of_bounds) + " | "
             + str(res.atr_rdist.out_of_bounds) + " |")
    L.append("")
    L.append("> ATR trail can ratchet past the TP2 blended ceiling, so its "
             "max_R may exceed V1's. V1's closed-set max is the TP2 blended "
             "R; the ATR closed-set adds TRAIL_HIT and can reach higher. "
             "A floor of -1R (SL) applies to both.")
    L.append("")

    # 3. Monthly OOS audit
    L.append("## 3. Monthly OOS Audit")
    L.append("")
    L.append("### V1_BASELINE")
    L.append("")
    L.append("| month | closed | win_rate | expectancy | PF | total_R | max_DD_R |")
    L.append("|---|---|---|---|---|---|---|")
    for r in res.v1_monthly:
        L.append(f"| {r.label} | {r.closed} | {_pct(r.win_rate)} | "
                 f"{_f(r.expectancy)} | {_pf(r.pf)} | {_f(r.total_r)} | {_f(r.max_dd_r)} |")
    L.append("")
    L.append("### H1_ATR_TRAIL")
    L.append("")
    L.append("| month | closed | win_rate | expectancy | PF | total_R | max_DD_R |")
    L.append("|---|---|---|---|---|---|---|")
    for r in res.atr_monthly:
        L.append(f"| {r.label} | {r.closed} | {_pct(r.win_rate)} | "
                 f"{_f(r.expectancy)} | {_pf(r.pf)} | {_f(r.total_r)} | {_f(r.max_dd_r)} |")
    L.append("")
    L.append("> A candidate is NOT viable if its OOS edge is concentrated in "
             "a single month. Positive months: "
             f"{res.soi.atr_monthly_pos_count}, negative months: "
             f"{res.soi.atr_monthly_neg_count}.")
    L.append("")

    # 4. Symbol OOS audit
    L.append("## 4. Per-Symbol OOS Audit")
    L.append("")
    L.append("### V1_BASELINE")
    L.append("")
    L.append("| symbol | closed | win_rate | expectancy | PF | total_R | max_DD_R |")
    L.append("|---|---|---|---|---|---|---|")
    for r in res.v1_symbol:
        L.append(f"| {r.label} | {r.closed} | {_pct(r.win_rate)} | "
                 f"{_f(r.expectancy)} | {_pf(r.pf)} | {_f(r.total_r)} | {_f(r.max_dd_r)} |")
    L.append("")
    L.append("### H1_ATR_TRAIL")
    L.append("")
    L.append("| symbol | closed | win_rate | expectancy | PF | total_R | max_DD_R |")
    L.append("|---|---|---|---|---|---|---|")
    for r in res.atr_symbol:
        L.append(f"| {r.label} | {r.closed} | {_pct(r.win_rate)} | "
                 f"{_f(r.expectancy)} | {_pf(r.pf)} | {_f(r.total_r)} | {_f(r.max_dd_r)} |")
    L.append("")
    L.append(f"Symbols improving vs V1: **{res.atr_symbol_improvement}**. "
             f"Symbols deteriorating vs V1: **{res.atr_symbol_deterioration}**.")
    L.append("")
    L.append("> A candidate is NOT viable if its OOS edge is concentrated in "
             "a single symbol. Positive symbols: "
             f"{res.soi.atr_symbol_pos_count}, negative symbols: "
             f"{res.soi.atr_symbol_neg_count}.")
    L.append("")

    # 5. ADX regime OOS audit
    L.append("## 5. ADX Regime OOS Audit")
    L.append("")
    L.append("> ADX is read from the forensics enrichment (signal-time ADX). "
             "Trades with missing ADX are excluded from the buckets.")
    L.append("")
    L.append("### V1_BASELINE")
    L.append("")
    L.append("| regime | closed | PF | total_R |")
    L.append("|---|---|---|---|")
    for r in res.v1_adx:
        L.append(f"| {r.label} | {r.closed} | {_pf(r.pf)} | {_f(r.total_r)} |")
    L.append("")
    L.append("### H1_ATR_TRAIL")
    L.append("")
    L.append("| regime | closed | PF | total_R |")
    L.append("|---|---|---|---|")
    for r in res.atr_adx:
        L.append(f"| {r.label} | {r.closed} | {_pf(r.pf)} | {_f(r.total_r)} |")
    L.append("")
    L.append(f"Best ADX bucket for H1_ATR_TRAIL: **{res.soi.atr_adx_bucket_best}** "
             f"(total_R={_f(res.soi.atr_adx_bucket_best_r)}). "
             "If the improvement is confined to one regime, it is NOT robust.")
    L.append("")

    # 6. Drawdown audit
    L.append("## 6. Drawdown Audit (OOS, closed-trade R sequence)")
    L.append("")
    L.append("| metric | V1_BASELINE | H1_ATR_TRAIL |")
    L.append("|---|---|---|")
    L.append(f"| max_DD_R | {_f(res.v1_dd.max_drawdown_r)} | {_f(res.atr_dd.max_drawdown_r)} |")
    L.append(f"| max_DD_duration_candles | {res.v1_dd.max_dd_duration_candles} | {res.atr_dd.max_dd_duration_candles} |")
    L.append(f"| max_DD_duration_hours | {res.v1_dd.max_dd_duration_hours} | {res.atr_dd.max_dd_duration_hours} |")
    rec_v1 = res.v1_dd.recovery_candles if res.v1_dd.recovery_candles is not None else "N/A"
    rec_atr = res.atr_dd.recovery_candles if res.atr_dd.recovery_candles is not None else "N/A"
    L.append(f"| recovery_candles | {rec_v1} | {rec_atr} |")
    rh_v1 = res.v1_dd.recovery_hours if res.v1_dd.recovery_hours is not None else "N/A"
    rh_atr = res.atr_dd.recovery_hours if res.atr_dd.recovery_hours is not None else "N/A"
    L.append(f"| recovery_hours | {rh_v1} | {rh_atr} |")
    L.append(f"| cumulative_R_end | {_f(res.v1_dd.cumulative_r_end)} | {_f(res.atr_dd.cumulative_r_end)} |")
    L.append(f"| closed_trades | {res.v1_dd.closed_trades} | {res.atr_dd.closed_trades} |")
    L.append("")
    L.append("> Drawdown duration is measured in 5-minute trigger candles "
             "(1 candle = 5 minutes). N/A recovery means the equity never "
             "returned to the pre-trough peak within the OOS window.")
    L.append("")

    # 7. Source-of-improvement diagnostics
    L.append("## 7. Source-of-Improvement Diagnostics")
    L.append("")
    L.append("| # | diagnostic | V1_BASELINE | H1_ATR_TRAIL | interpretation |")
    L.append("|---|---|---|---|---|")
    L.append(f"| 1 | SL_count (R<=-1) | {res.soi.v1_sl_count} | {res.soi.atr_sl_count} | fewer large SLs? |")
    L.append(f"| 2 | avg_loss_R | {_f(res.soi.v1_avg_loss_r)} | {_f(res.soi.atr_avg_loss_r)} | softer losses? |")
    L.append(f"| 3 | favorable_exit_count | {res.soi.v1_favorable_exit_count} | {res.soi.atr_favorable_exit_count} | more TRAIL/TP2? |")
    L.append(f"| 4 | avg_holding_candles | {_f(res.soi.v1_avg_holding, '.2f')} | {_f(res.soi.atr_avg_holding, '.2f')} | faster exit? |")
    L.append(f"| 5 | median_R | {_f(res.soi.v1_median_r)} | {_f(res.soi.atr_median_r)} | R-shift? |")
    L.append(f"| 6 | P25_R | {_f(res.soi.v1_p25_r)} | {_f(res.soi.atr_p25_r)} | left-tail shift? |")
    L.append(f"| 7 | top5_contribution_R | N/A | {_f(res.soi.atr_top5_contrib_r)} | top-5 R sum |")
    L.append(f"| 8 | top5_share_of_total_R | N/A | {_f(res.soi.atr_top5_share, '.2f')} | concentration flag |")
    L.append("")
    L.append("> A candidate improvement that is driven primarily by "
             "(a) fewer large SLs, (b) more favorable exits, (c) shorter "
             "holding time, (d) a rightward R-shift, or (e) a small set of "
             "large winners is described here. It is NOT a proof of edge.")
    L.append("")

    # 8. Robustness exclusions
    L.append("## 8. Robustness — Exclusion Scenarios (H1_ATR_TRAIL, OOS)")
    L.append("")
    L.append("| scenario | PF | total_R | expectancy | closed |")
    L.append("|---|---|---|---|---|")
    for row in res.robustness["rows"]:
        L.append(f"| {row.scenario} | {_pf(row.pf)} | {_f(row.total_r)} | "
                 f"{_f(row.expectancy)} | {row.closed} |")
    L.append("")
    det = res.robustness["detail"]
    L.append(f"- Best month: {det['best_month']} (total_R={_f(det['best_month_total_r'])})")
    L.append(f"- Worst month: {det['worst_month']} (total_R={_f(det['worst_month_total_r'])})")
    L.append(f"- Best symbol: {det['best_symbol']} (total_R={_f(det['best_symbol_total_r'])})")
    L.append(f"- Worst symbol: {det['worst_symbol']} (total_R={_f(det['worst_symbol_total_r'])})")
    L.append("")
    L.append("> If the candidate edge disappears when a single month or "
             "symbol is excluded, it is NOT robust. A candidate is viable "
             "only if the edge persists across all four exclusion scenarios "
             "with a positive PF.")
    L.append("")

    L.append("## NO-CANDIDATE-CONFIRMATION STATEMENT")
    L.append("")
    L.append("No candidate is confirmed. `H1_ATR_TRAIL` remains a CANDIDATE "
             "for a human decision. The measurements above are "
             "descriptive. A human must decide whether it becomes V2.")
    L.append("")
    L.append("## DISCLAIMER")
    L.append("Historical simulation only. No parameter optimization. No new "
             "indicators. No Telegram integration. No trading execution. "
             "Production strategy files are unchanged.")
    return "\n".join(L)


def write_audit_report(res: AuditResult, report_dir: str = "reports") -> str:
    import os
    os.makedirs(report_dir, exist_ok=True)
    path = os.path.join(report_dir, "phase7_5_atr_trail_audit.md")
    md = build_audit_report(res)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(md)
    return path


def cli_main() -> None:
    import argparse
    from app.trading_guard import assert_no_execution_code
    assert_no_execution_code()
    parser = argparse.ArgumentParser("PHASE 7.5 H1_ATR_TRAIL OOS stability audit")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--report-dir", default="reports")
    args = parser.parse_args()
    res = run_audit(data_dir=args.data_dir)
    path = write_audit_report(res, args.report_dir)
    print(f"PHASE 7.5 H1_ATR_TRAIL OOS stability audit written to: {path}")
    print(f"  OOS window: {res.oos_window[0]} -> {res.oos_window[1]}")
    print(f"  V1 OOS closed: {res.v1_rdist.closed_trades}  total_R: "
          f"{_f(res.v1_rdist.mean * res.v1_rdist.closed_trades if res.v1_rdist.mean is not None else None)}")
    print(f"  ATR OOS closed: {res.atr_rdist.closed_trades}  total_R: "
          f"{_f(res.atr_rdist.mean * res.atr_rdist.closed_trades if res.atr_rdist.mean is not None else None)}")
    print(f"  ATR max_DD_R: {res.atr_dd.max_drawdown_r}  V1 max_DD_R: {res.v1_dd.max_drawdown_r}")


if __name__ == "__main__":
    cli_main()
