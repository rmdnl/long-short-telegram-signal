"""
PHASE 4: Backtest metrics with documented formulas.

All metrics are computed from a list of TradeResult. Formulas are stated
explicitly. Undefined cases return N/A (never infinity, never 0-by-default
hidden).
"""
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Optional, Sequence

from backtest.execution import TradeResult

NA = "N/A"


def _q(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


@dataclass
class BacktestMetrics:
    """Aggregate + grouped backtest metrics."""
    # Signal-level
    total_signals: int = 0
    long_signals: int = 0
    short_signals: int = 0
    filled: int = 0
    unfilled: int = 0
    expired: int = 0
    closed_trades: int = 0
    open_trades: int = 0

    # Outcome-level
    wins: int = 0
    losses: int = 0
    win_rate: Optional[float] = None
    avg_r: Optional[float] = None
    median_r: Optional[float] = None
    total_r: Optional[float] = None
    avg_win_r: Optional[float] = None
    avg_loss_r: Optional[float] = None
    profit_factor: Optional[float] = None   # N/A when zero losses
    expectancy: Optional[float] = None
    max_drawdown_r: Optional[float] = None
    max_consecutive_losses: int = 0
    tp1_hit_rate: Optional[float] = None
    tp2_hit_rate: Optional[float] = None
    sl_rate: Optional[float] = None
    avg_holding_candles: Optional[float] = None
    signals_per_day: Optional[float] = None
    signals_per_week: Optional[float] = None

    # Fees
    fees_enabled: bool = False
    gross_r_total: Optional[float] = None
    net_r_total: Optional[float] = None
    est_fee_cost_r: Optional[float] = None

    # Grouped metrics (symbol / direction / regime / month / year)
    by_symbol: Dict[str, "BacktestMetrics"] = field(default_factory=dict)
    by_direction: Dict[str, "BacktestMetrics"] = field(default_factory=dict)
    by_regime: Dict[str, "BacktestMetrics"] = field(default_factory=dict)
    by_month: Dict[str, "BacktestMetrics"] = field(default_factory=dict)
    by_year: Dict[str, "BacktestMetrics"] = field(default_factory=dict)

    period_start: Optional[str] = None
    period_end: Optional[str] = None
    symbols: List[str] = field(default_factory=list)
    warmup_candles_skipped: int = 0

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        return d


def _median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def _max_drawdown_r(r_series: Sequence[Decimal]) -> float:
    """Peak-to-trough drawdown of a cumulative-R equity curve (in R)."""
    if not r_series:
        return 0.0
    peak: Decimal = Decimal("0")
    equity: Decimal = Decimal("0")
    max_dd: Decimal = Decimal("0")
    for r in r_series:
        equity += r
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
    return float(max_dd)


def _compute_group(trades: List[TradeResult]) -> BacktestMetrics:
    """Compute core metrics for a single group of trades."""
    m = BacktestMetrics()
    m.total_signals = len(trades)
    m.long_signals = sum(1 for t in trades if t.direction == "LONG")
    m.short_signals = sum(1 for t in trades if t.direction == "SHORT")
    m.filled = sum(1 for t in trades if t.entry_status == "FILLED")
    m.unfilled = sum(1 for t in trades if t.entry_status != "FILLED")
    m.expired = sum(1 for t in trades if t.exit_status == "EXPIRED")
    m.closed_trades = sum(
        1 for t in trades if t.exit_status in ("TP1_HIT", "TP2_HIT", "STOPPED")
    )
    m.open_trades = sum(1 for t in trades if t.exit_status == "OPEN")
    m.fees_enabled = any(t.fees_enabled for t in trades)

    closed_rs = [float(t.r_multiple) for t in trades
                 if t.exit_status in ("TP1_HIT", "TP2_HIT", "STOPPED")]
    all_rs = [float(t.r_multiple) for t in trades]

    m.total_r = sum(closed_rs) if closed_rs else None

    if closed_rs:
        m.avg_r = sum(closed_rs) / len(closed_rs)
        m.median_r = _median(closed_rs)
        m.expectancy = m.avg_r  # avg R per closed trade
        wins = [r for r in closed_rs if r > 0]
        losses = [r for r in closed_rs if r < 0]
        m.wins = len(wins)
        m.losses = len(losses)
        m.win_rate = (m.wins / m.closed_trades) if m.closed_trades else None
        m.avg_win_r = (sum(wins) / len(wins)) if wins else None
        m.avg_loss_r = (sum(losses) / len(losses)) if losses else None

        # Profit factor = gross winning R / abs(gross losing R); N/A if no losses
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        if m.losses == 0:
            m.profit_factor = None  # N/A (documented)
        elif gross_loss == 0:
            m.profit_factor = None
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

    # Touched-rate metrics over all signals
    if trades:
        m.tp1_hit_rate = sum(1 for t in trades if t.tp1_hit) / len(trades)
        m.tp2_hit_rate = sum(1 for t in trades if t.tp2_hit) / len(trades)
        m.sl_rate = sum(1 for t in trades if t.sl_hit) / len(trades)

    # Holding time (filled trades only)
    fill_holds = [t.holding_candles for t in trades if t.entry_status == "FILLED"]
    m.avg_holding_candles = (sum(fill_holds) / len(fill_holds)) if fill_holds else None

    # Signals per day / week (using signal_time span)
    times = [t.signal_time for t in trades]
    if len(times) >= 2:
        span_days = max((max(times) - min(times)).total_seconds(), 3600) / 86400.0
        m.signals_per_day = len(times) / span_days
        m.signals_per_week = m.signals_per_day * 7.0

    # Max drawdown over closed-trade R sequence (in R, not %).
    m.max_drawdown_r = _max_drawdown_r(
        [Decimal(str(r)) for r in closed_rs]
    )

    # Consecutive losses
    cons = 0
    max_cons = 0
    for r in closed_rs:
        if r < 0:
            cons += 1
            max_cons = max(max_cons, cons)
        else:
            cons = 0
    m.max_consecutive_losses = max_cons

    return m


def _to_float(v) -> Optional[float]:
    if v is None:
        return None
    return float(v)


def metrics_from_trades(
    trades: List[TradeResult],
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    symbols: Optional[List[str]] = None,
    warmup_candles_skipped: int = 0,
) -> BacktestMetrics:
    """Build a full BacktestMetrics from a list of simulated trades."""
    m = _compute_group(trades)
    m.period_start = period_start
    m.period_end = period_end
    m.symbols = symbols or sorted({t.symbol for t in trades})
    m.warmup_candles_skipped = warmup_candles_skipped

    # Grouped breakdowns
    def group_by(key_fn):
        out: Dict[str, List[TradeResult]] = {}
        for t in trades:
            out.setdefault(key_fn(t), []).append(t)
        return {k: _compute_group(v) for k, v in out.items()}

    m.by_symbol = group_by(lambda t: t.symbol)
    m.by_direction = group_by(lambda t: t.direction)
    m.by_regime = group_by(lambda t: t.market_regime)
    m.by_month = group_by(lambda t: t.signal_time.strftime("%Y-%m"))
    m.by_year = group_by(lambda t: t.signal_time.strftime("%Y"))

    # Fees: net vs gross R
    if m.fees_enabled:
        net_rs = [float(t.r_multiple) for t in trades
                  if t.exit_status in ("TP1_HIT", "TP2_HIT", "STOPPED")]
        m.net_r_total = sum(net_rs)
        m.gross_r_total = m.net_r_total  # net already has fees subtracted
        m.est_fee_cost_r = m.net_r_total - m.total_r if m.total_r is not None else None

    return m
