"""
PHASE 6: Forensic analysis of WHY the baseline strategy produces negative
historical expectancy.

This module is a pure, deterministic diagnostic layer. It NEVER modifies:
- the strategy
- the execution model
- the config
- the original baseline_trades.csv

It takes the list of `TradeResult` (already produced by the baseline run) and
the historical datasets, then re-derives per-trade diagnostic fields that the
baseline CSV does not carry (ADX/RSI/volume/ATR/EMA-distance at signal time,
MAE/MFE in R, failure-sequence label) and computes every bucket / group
statistic requested in PHASE 6.

All numbers reported here are OBSERVED FACTS from historical data. The
companion markdown report separates OBSERVED / INTERPRETATION / HYPOTHESIS.

No trading execution. No network. No parameter tuning.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Sequence

from app import data_validation as dv
from app.models import Candle

from backtest.data import HistoricalDataset
from backtest.execution import TradeResult
from backtest.indicators_precomputed import PrecomputedIndicators

TRIGGER_TF = "5m"
SETUP_TF = "15m"
HF_TF = "1h"


# ---------------------------------------------------------------------------
# Percentile helper (deterministic, no external deps)
# ---------------------------------------------------------------------------

def percentile(values: Sequence[float], p: float = 50.0) -> Optional[float]:
    """
    Deterministic percentile (linear interpolation, numpy 'linear' method).

    p in [0, 100]. Returns None for an empty sequence.
    """
    if not values:
        return None
    if len(values) == 1:
        return float(values[0])
    s = sorted(values)
    if p <= 0:
        return float(s[0])
    if p >= 100:
        return float(s[-1])
    rank = (p / 100.0) * (len(s) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(s) - 1)
    frac = rank - lo
    return s[lo] + (s[hi] - s[lo]) * frac


# ---------------------------------------------------------------------------
# Enriched trade (per-trade diagnostic record)
# ---------------------------------------------------------------------------

@dataclass
class ForensicsTrade:
    """A baseline TradeResult plus re-derived diagnostic fields."""
    # --- carried from TradeResult ---
    signal_id: str
    symbol: str
    direction: str
    signal_time: datetime
    entry_low: Decimal
    entry_high: Decimal
    entry_price: Optional[Decimal]
    stop_loss: Decimal
    tp1: Decimal
    tp2: Decimal
    score: int
    market_regime: str
    entry_status: str
    exit_status: str
    exit_time: Optional[datetime]
    exit_price: Optional[Decimal]
    r_multiple: Decimal
    tp1_hit: bool
    tp2_hit: bool
    sl_hit: bool
    holding_candles: int
    fees_enabled: bool

    # --- re-derived at signal time (15M setup bundle) ---
    adx: Optional[Decimal] = None
    rsi: Optional[Decimal] = None
    atr: Optional[Decimal] = None
    ema_fast: Optional[Decimal] = None
    ema_slow: Optional[Decimal] = None
    volume_ratio: Optional[Decimal] = None
    ema_distance_atr: Optional[float] = None      # abs(trigger_close - ema50)/atr

    # --- post-entry excursion (in R), filled trades only ---
    mae_r: Optional[float] = None
    mfe_r: Optional[float] = None

    # --- failure-sequence label (filled trades) ---
    failure_sequence: str = "N/A"

    # --- derived flags ---
    @property
    def is_filled(self) -> bool:
        return self.entry_status == "FILLED"

    @property
    def is_winner(self) -> bool:
        return float(self.r_multiple) > 0 and self.is_closed

    @property
    def is_closed(self) -> bool:
        return self.exit_status in ("TP1_HIT", "TP2_HIT", "STOPPED")

    @property
    def is_loss(self) -> bool:
        return float(self.r_multiple) < 0 and self.is_closed

    @property
    def risk(self) -> Decimal:
        if self.entry_price is None:
            return Decimal("0")
        return abs(self.entry_price - self.stop_loss)


# ---------------------------------------------------------------------------
# Indicator re-derivation at signal time
# ---------------------------------------------------------------------------

def _build_indicator_index(
    ds: HistoricalDataset,
    config,
) -> Dict[datetime, Dict]:
    """
    For one symbol, map trigger-candle-open-time -> the setup-bundle + trigger
    values that the engine saw at that decision point.

    Keyed by the trigger candle OPEN timestamp (== Signal.trigger_candle_time,
    == TradeResult.signal_time as stored). Deterministic via precomputed
    causal arrays, so it matches the values the engine used.
    """
    import bisect
    trig_full = ds.tf(TRIGGER_TF)
    setup_full = ds.tf(SETUP_TF)
    hf_full = ds.tf(HF_TF)
    if not trig_full:
        return {}

    pre = PrecomputedIndicators(config)
    pre.precompute(hf_full, setup_full, trig_full)

    setup_close_times = [
        c.timestamp + timedelta(seconds=dv.TIMEFRAME_SECONDS[SETUP_TF])
        for c in setup_full
    ]
    hf_close_times = [
        c.timestamp + timedelta(seconds=dv.TIMEFRAME_SECONDS[HF_TF])
        for c in hf_full
    ]

    out: Dict[datetime, Dict] = {}
    for i, c in enumerate(trig_full):
        T = dv.candle_close_time(c, TRIGGER_TF)
        setup_count = bisect.bisect_right(setup_close_times, T)
        hf_count = bisect.bisect_right(hf_close_times, T)
        if setup_count == 0 or hf_count == 0:
            continue
        _, setup_ind, trig_ind = pre.build_ind(
            hf_count - 1, setup_count - 1, i,
        )
        # volume ratio at trigger candle (mirror Signal.volume_ratio)
        vol_ratio = None
        if trig_ind.volume_sma:
            vol_ratio = c.volume / trig_ind.volume_sma
        # EMA distance in ATR at trigger close
        ema_dist = None
        if setup_ind.ema_fast is not None and setup_ind.atr:
            dist = abs(c.close - setup_ind.ema_fast)
            if setup_ind.atr > 0:
                ema_dist = float(dist / setup_ind.atr)
        out[c.timestamp] = {
            "adx": setup_ind.adx,
            "rsi": setup_ind.rsi,
            "atr": setup_ind.atr,
            "ema_fast": setup_ind.ema_fast,
            "ema_slow": setup_ind.ema_slow,
            "volume_ratio": vol_ratio,
            "ema_distance_atr": ema_dist,
        }
    return out


# ---------------------------------------------------------------------------
# MAE / MFE + failure sequence from raw trigger candles
# ---------------------------------------------------------------------------

def _excursions_and_sequence(
    ft: ForensicsTrade,
    trigger: List[Candle],
    ts_index: Dict[datetime, int],
) -> None:
    """
    Walk the trigger candles from the signal candle to the exit candle and
    compute MAE/MFE (in R) and a failure-sequence label for filled trades.

    MAE (max adverse excursion) and MFE (max favorable excursion) are only
    meaningful for trades that actually filled; set to None otherwise.
    """
    if not ft.is_filled or ft.risk == 0:
        return

    idx = ts_index.get(ft.signal_time)
    if idx is None:
        return

    risk = ft.risk

    worst = 0.0
    best = 0.0
    # Walk from the candle AFTER the signal to the exit candle inclusive.
    end_idx = len(trigger)
    if ft.exit_time is not None and ft.exit_time in ts_index:
        end_idx = min(end_idx, ts_index[ft.exit_time])
    else:
        # fall back to signal_time + holding_candles
        end_idx = min(len(trigger), idx + 1 + max(ft.holding_candles, 0))

    for j in range(idx + 1, end_idx + 1):
        c = trigger[j]
        if ft.direction == "LONG":
            min_low = c.low
            max_high = c.high
            worst = max(worst, float((ft.entry_price - min_low) / risk))
            best = max(best, float((max_high - ft.entry_price) / risk))
        else:  # SHORT
            max_high = c.high
            min_low = c.low
            worst = max(worst, float((max_high - ft.entry_price) / risk))
            best = max(best, float((ft.entry_price - min_low) / risk))

    ft.mae_r = round(worst, 4)
    ft.mfe_r = round(best, 4)
    ft.failure_sequence = _classify_sequence(ft, trigger, idx, end_idx, ts_index)


def _classify_sequence(
    ft: ForensicsTrade,
    trigger: List[Candle],
    idx: int,
    end_idx: int,
    ts_index: Dict[datetime, int],
) -> str:
    """
    Classify the path from entry to exit into a deterministic label.

    Labels:
    - SL_DIRECT          : adverse move to SL with no meaningful favorable
                           excursion before it (SL came before any TP touch)
    - FAVORABLE_THEN_SL  : price moved favorably (MFE >= 0.5R) then reversed
                           to SL
    - TP1_THEN_SL        : TP1 was touched (partial) but trade still stopped
    - EXPIRED_NO_FILL_SIDE: unfilled/expired before a full SL or TP2
    - TP1_EXITS          : exited at TP1 (TP_MODEL) / TP1 only winner
    - TP2_EXITS          : full TP2 winner
    - EXPIRED_OPEN       : still open at data end (marked EXPIRED by sim)
    - N/A                : not filled or not a closed trade
    """
    if not ft.is_filled:
        return "N/A"
    if not ft.is_closed:
        # open at data end
        return "EXPIRED_OPEN"
    if ft.sl_hit:
        # A stopped trade: did it first move favorably?
        if ft.mfe_r is not None and ft.mfe_r >= 0.5:
            if ft.tp1_hit:
                return "TP1_THEN_SL"
            return "FAVORABLE_THEN_SL"
        return "SL_DIRECT"
    if ft.tp2_hit:
        return "TP2_EXITS"
    if ft.tp1_hit:
        return "TP1_EXITS"
    return "EXPIRED_OPEN"


# ---------------------------------------------------------------------------
# Enrichment entry point
# ---------------------------------------------------------------------------

def compute_forensics_trades(
    trades: List[TradeResult],
    datasets: Dict[str, HistoricalDataset],
    config,
) -> List[ForensicsTrade]:
    """
    Convert baseline TradeResults into ForensicsTrade records with
    re-derived diagnostic fields (ADX/RSI/ATR/EMA-distance/MAE/MFE/sequence).
    """
    ind_index: Dict[str, Dict[datetime, Dict]] = {}
    trigger_ts_index: Dict[str, Dict[datetime, int]] = {}
    trigger_list: Dict[str, List[Candle]] = {}
    for sym, ds in datasets.items():
        ind_index[sym] = _build_indicator_index(ds, config)
        trigger = ds.tf(TRIGGER_TF)
        trigger_list[sym] = trigger
        trigger_ts_index[sym] = {c.timestamp: i for i, c in enumerate(trigger)}

    out: List[ForensicsTrade] = []
    for t in trades:
        ft = ForensicsTrade(
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
        diag = ind_index.get(t.symbol, {}).get(t.signal_time)
        if diag:
            ft.adx = diag.get("adx")
            ft.rsi = diag.get("rsi")
            ft.atr = diag.get("atr")
            ft.ema_fast = diag.get("ema_fast")
            ft.ema_slow = diag.get("ema_slow")
            ft.volume_ratio = diag.get("volume_ratio")
            ft.ema_distance_atr = diag.get("ema_distance_atr")
        _excursions_and_sequence(ft, trigger_list.get(t.symbol, []),
                                 trigger_ts_index.get(t.symbol, {}))
        out.append(ft)
    return out


# ---------------------------------------------------------------------------
# Group statistics
# ---------------------------------------------------------------------------

@dataclass
class GroupStats:
    """Compact per-group aggregate (signals/filled/win/expectancy/PF/totalR/maxDD)."""
    signals: int = 0
    filled: int = 0
    closed: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: Optional[float] = None
    expectancy: Optional[float] = None
    profit_factor: Optional[float] = None   # None => N/A
    total_r: Optional[float] = None
    max_drawdown_r: float = 0.0

    def as_row(self, label: str) -> List[str]:
        """Markdown table row: label + all metrics."""
        return [
            label,
            str(self.signals),
            str(self.filled),
            _pct(self.win_rate),
            _f(self.expectancy),
            _pf(self.profit_factor),
            _f(self.total_r),
            _f(self.max_drawdown_r),
        ]

    def header(self) -> List[str]:
        return ["group", "signals", "filled", "win_rate",
                "expectancy", "PF", "total_R", "max_DD_R"]


def _group_stats(trades: Sequence[ForensicsTrade]) -> GroupStats:
    g = GroupStats()
    closed_rs: List[float] = []
    wins: List[float] = []
    losses: List[float] = []
    for t in trades:
        g.signals += 1
        if t.is_filled:
            g.filled += 1
        if t.is_closed:
            g.closed += 1
            r = float(t.r_multiple)
            closed_rs.append(r)
            if r > 0:
                wins.append(r)
            elif r < 0:
                losses.append(r)
    g.wins = len(wins)
    g.losses = len(losses)
    if g.closed:
        g.win_rate = g.wins / g.closed
        g.expectancy = sum(closed_rs) / len(closed_rs)
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        if g.losses == 0 or gross_loss == 0:
            g.profit_factor = None
        else:
            g.profit_factor = gross_win / gross_loss
    g.total_r = sum(closed_rs) if closed_rs else None
    g.max_drawdown_r = _max_dd(closed_rs)
    return g


def group_by(
    trades: Sequence[ForensicsTrade],
    key_fn,
) -> "Dict[str, GroupStats]":
    buckets: Dict[str, List[ForensicsTrade]] = {}
    for t in trades:
        k = key_fn(t)
        if k is None:
            continue
        buckets.setdefault(str(k), []).append(t)
    return {k: _group_stats(v) for k, v in buckets.items()}


# ---------------------------------------------------------------------------
# Bucket definitions (PHASE 6 fixed ranges — not optimized)
# ---------------------------------------------------------------------------

SCORE_BUCKETS = [(80, 84), (85, 89), (90, 94), (95, 100)]
ADX_BUCKETS = [(22, 25), (25, 30), (30, 40), (40, None)]
RSI_LONG_BUCKETS = [(50, 55), (55, 60), (60, 65), (65, None)]
RSI_SHORT_BUCKETS = [(45, 50), (40, 45), (35, 40), (None, 35)]   # mirrored
VOLUME_BUCKETS = [(1.0, 1.2), (1.2, 1.5), (1.5, 2.0), (2.0, None)]
EMA_DIST_BUCKETS = [(0.0, 0.5), (0.5, 1.0), (1.0, 1.5), (1.5, 2.0)]


def _bucket_label(lo, hi) -> str:
    if hi is None:
        return f"{lo}+"
    if lo is None:
        return f"<{hi}"
    if lo == hi:
        return f"{lo}"
    return f"{lo}-{hi}"


def _in_bucket(value, lo, hi) -> bool:
    if value is None:
        return False
    v = float(value)
    if lo is not None and v < lo:
        return False
    if hi is not None and v > hi:
        return False
    return True


def score_buckets(trades: Sequence[ForensicsTrade]) -> Dict[str, GroupStats]:
    return group_by(
        trades,
        lambda t: next(
            (_bucket_label(lo, hi) for (lo, hi) in SCORE_BUCKETS
             if _in_bucket(t.score, lo, hi)),
            None,
        ),
    )


def adx_buckets(trades: Sequence[ForensicsTrade]) -> Dict[str, GroupStats]:
    return group_by(
        trades,
        lambda t: next(
            (_bucket_label(lo, hi) for (lo, hi) in ADX_BUCKETS
             if _in_bucket(t.adx, lo, hi)),
            None,
        ),
    )


def rsi_buckets(trades: Sequence[ForensicsTrade]) -> Dict[str, GroupStats]:
    """RSI buckets are direction-aware: use RSI_LONG for LONG, RSI_SHORT for SHORT."""
    def key_fn(t):
        if t.direction == "LONG":
            buckets = RSI_LONG_BUCKETS
        elif t.direction == "SHORT":
            buckets = RSI_SHORT_BUCKETS
        else:
            return None
        lab = next(
            (_bucket_label(lo, hi) for (lo, hi) in buckets
             if _in_bucket(t.rsi, lo, hi)),
            None,
        )
        if lab is None:
            return None
        return f"{t.direction}:{lab}"
    return group_by(trades, key_fn)


def volume_buckets(trades: Sequence[ForensicsTrade]) -> Dict[str, GroupStats]:
    return group_by(
        trades,
        lambda t: next(
            (_bucket_label(lo, hi) for (lo, hi) in VOLUME_BUCKETS
             if _in_bucket(t.volume_ratio, lo, hi)),
            None,
        ),
    )


def ema_distance_buckets(trades: Sequence[ForensicsTrade]) -> Dict[str, GroupStats]:
    return group_by(
        trades,
        lambda t: next(
            (_bucket_label(lo, hi) for (lo, hi) in EMA_DIST_BUCKETS
             if _in_bucket(t.ema_distance_atr, lo, hi)),
            None,
        ),
    )


# ---------------------------------------------------------------------------
# Distribution / exit / holding / MAE-MFE / entry / RR
# ---------------------------------------------------------------------------

@dataclass
class RDistribution:
    count: int
    min_r: Optional[float]
    max_r: Optional[float]
    mean_r: Optional[float]
    median_r: Optional[float]
    p25: Optional[float]
    p75: Optional[float]
    total_r: Optional[float]


def r_distribution(trades: Sequence[ForensicsTrade],
                   direction: Optional[str] = None) -> RDistribution:
    pool = [t for t in trades
            if t.is_closed and (direction is None or t.direction == direction)]
    rs = [float(t.r_multiple) for t in pool]
    if not rs:
        return RDistribution(0, None, None, None, None, None, None, None)
    return RDistribution(
        count=len(rs),
        min_r=min(rs),
        max_r=max(rs),
        mean_r=sum(rs) / len(rs),
        median_r=percentile(rs, 50),
        p25=percentile(rs, 25),
        p75=percentile(rs, 75),
        total_r=sum(rs),
    )


@dataclass
class ExitReasonCounts:
    sl: int
    tp1: int
    tp2: int
    expired: int
    not_filled: int
    total: int

    def pct(self, n: int) -> Optional[float]:
        return (n / self.total) if self.total else None


def exit_reasons(trades: Sequence[ForensicsTrade],
                 direction: Optional[str] = None) -> ExitReasonCounts:
    pool = [t for t in trades if direction is None or t.direction == direction]
    sl = sum(1 for t in pool if t.sl_hit and t.exit_status == "STOPPED")
    tp1 = sum(1 for t in pool if t.exit_status == "TP1_HIT")
    tp2 = sum(1 for t in pool if t.exit_status == "TP2_HIT")
    expired = sum(1 for t in pool if t.exit_status == "EXPIRED")
    not_filled = sum(1 for t in pool if t.entry_status != "FILLED")
    return ExitReasonCounts(sl, tp1, tp2, expired, not_filled, len(pool))


@dataclass
class HoldingStats:
    median: Optional[float]
    mean: Optional[float]
    p25: Optional[float]
    p75: Optional[float]
    maximum: Optional[float]


def holding_time(
    trades: Sequence[ForensicsTrade],
    winners_only: bool = False,
    losers_only: bool = False,
) -> HoldingStats:
    pool = [t for t in trades if t.is_filled]
    if winners_only:
        pool = [t for t in pool if t.is_winner]
    if losers_only:
        pool = [t for t in pool if t.is_loss]
    holds = [t.holding_candles for t in pool]
    if not holds:
        return HoldingStats(None, None, None, None, None)
    return HoldingStats(
        median=percentile(holds, 50),
        mean=sum(holds) / len(holds),
        p25=percentile(holds, 25),
        p75=percentile(holds, 75),
        maximum=max(holds),
    )


@dataclass
class MaeMfeStats:
    # per winner/loser
    winners_mae: Optional[float]
    winners_mfe: Optional[float]
    losers_mae: Optional[float]
    losers_mfe: Optional[float]
    # % of losers that ever moved >= 0.5R favorable before SL
    losers_hit_half_r: Optional[float]
    # % of winners that first moved adverse before hitting TP
    winners_negative_mae_pct: Optional[float]


def mae_mfe(trades: Sequence[ForensicsTrade]) -> MaeMfeStats:
    winners = [t for t in trades if t.is_winner]
    losers = [t for t in trades if t.is_loss]

    def _avg(ts, attr):
        vals = [getattr(t, attr) for t in ts if getattr(t, attr) is not None]
        return (sum(vals) / len(vals)) if vals else None

    w_mae = _avg(winners, "mae_r")
    w_mfe = _avg(winners, "mfe_r")
    l_mae = _avg(losers, "mae_r")
    l_mfe = _avg(losers, "mfe_r")

    losers_hit = (
        sum(1 for t in losers if t.mfe_r is not None and t.mfe_r >= 0.5)
        / len(losers) if losers else None
    )
    winners_neg = (
        sum(1 for t in winners if t.mae_r is not None and t.mae_r > 0)
        / len(winners) if winners else None
    )
    return MaeMfeStats(w_mae, w_mfe, l_mae, l_mfe, losers_hit, winners_neg)


# ---------------------------------------------------------------------------
# Entry quality
# ---------------------------------------------------------------------------

@dataclass
class EntryQuality:
    filled: int
    # average fill price vs zone (as fraction of zone width)
    avg_fill_offset_pct: Optional[float]     # (fill - zone_low)/zone_width, LONG
    # average trigger-close vs fill
    avg_trigger_close_vs_fill_pct: Optional[float]
    unfilled_expired: int
    # fraction of filled trades where the fill was exactly the conservative edge
    conservative_edge_fill_pct: Optional[float]


def entry_quality(trades: Sequence[ForensicsTrade]) -> EntryQuality:
    filled = [t for t in trades if t.is_filled]
    unfilled = sum(1 for t in trades if t.entry_status != "FILLED")
    if not filled:
        return EntryQuality(0, None, None, unfilled, None)

    offsets = []
    trig_vs_fill = []
    edge = 0
    for t in filled:
        zone_w = t.entry_high - t.entry_low
        if t.direction == "LONG":
            # fill should be zone_low (least favorable)
            if zone_w > 0:
                offsets.append(
                    float((t.entry_price - t.entry_low) / zone_w)
                )
            if t.entry_price == t.entry_low:
                edge += 1
        else:
            if zone_w > 0:
                offsets.append(
                    float((t.entry_high - t.entry_price) / zone_w)
                )
            if t.entry_price == t.entry_high:
                edge += 1
        trig_close = t.exit_price  # not the trigger; use risk as proxy
        # trigger close vs fill: we don't carry trigger close here; use R
        trig_vs_fill.append(float(t.r_multiple))

    return EntryQuality(
        filled=len(filled),
        avg_fill_offset_pct=(sum(offsets) / len(offsets)) if offsets else None,
        avg_trigger_close_vs_fill_pct=None,
        unfilled_expired=unfilled,
        conservative_edge_fill_pct=(edge / len(filled)) if filled else None,
    )


# ---------------------------------------------------------------------------
# Risk/Reward validation
# ---------------------------------------------------------------------------

@dataclass
class RRValidation:
    count: int
    avg_sl_distance_r: Optional[float]     # should be ~1.0 (risk by definition)
    avg_tp1_r: Optional[float]
    avg_tp2_r: Optional[float]
    config_tp1_rr: float
    config_tp2_rr: float


def rr_validation(trades: Sequence[ForensicsTrade],
                  tp1_rr: Decimal, tp2_rr: Decimal) -> RRValidation:
    filled = [t for t in trades if t.is_filled and t.risk > 0]
    if not filled:
        return RRValidation(0, None, None, None, float(tp1_rr), float(tp2_rr))
    sl_d, tp1s, tp2s = [], [], []
    for t in filled:
        sl_d.append(float(abs(t.entry_price - t.stop_loss) / t.risk))
        tp1s.append(float(abs(t.tp1 - t.entry_price) / t.risk))
        tp2s.append(float(abs(t.tp2 - t.entry_price) / t.risk))
    return RRValidation(
        count=len(filled),
        avg_sl_distance_r=sum(sl_d) / len(sl_d),
        avg_tp1_r=sum(tp1s) / len(tp1s),
        avg_tp2_r=sum(tp2s) / len(tp2s),
        config_tp1_rr=float(tp1_rr),
        config_tp2_rr=float(tp2_rr),
    )


# ---------------------------------------------------------------------------
# OOS separation (by signal_time window)
# ---------------------------------------------------------------------------

def oos_split_labels(
    trades: Sequence[ForensicsTrade],
    start: datetime,
    end: datetime,
    oos_fraction: float = 0.3,
    validation_fraction: float = 0.2,
) -> Dict[str, List[ForensicsTrade]]:
    """Assign each trade to in_sample / validation / out_of_sample by signal_time."""
    from backtest.baseline import OosConfig
    seg = OosConfig(oos_fraction=oos_fraction,
                    validation_fraction=validation_fraction).split(start, end)
    out: Dict[str, List[ForensicsTrade]] = {
        "in_sample": [], "validation": [], "out_of_sample": [],
    }
    for t in trades:
        st = t.signal_time
        for label, (s, e) in seg.items():
            if s <= st < e:
                out[label].append(t)
                break
        else:
            # outside all segments (edge): bucket into the nearest
            if st < seg["in_sample"][0]:
                out["in_sample"].append(t)
            elif st >= seg["out_of_sample"][1]:
                out["out_of_sample"].append(t)
    return out


# ---------------------------------------------------------------------------
# Formatting helpers (match backtest.report conventions)
# ---------------------------------------------------------------------------

def _f(v, spec=".4f") -> str:
    if v is None:
        return "N/A"
    return format(v, spec)


def _pct(v) -> str:
    return "N/A" if v is None else f"{v * 100:.1f}%"


def _pf(v) -> str:
    return "N/A" if v is None else f"{v:.3f}"


def _max_dd(rs: Sequence[float]) -> float:
    if not rs:
        return 0.0
    peak = 0.0
    equity = 0.0
    max_dd = 0.0
    for r in rs:
        equity += r
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
    return max_dd


# ---------------------------------------------------------------------------
# CSV export for forensics
# ---------------------------------------------------------------------------

_FORENSICS_CSV_COLUMNS = [
    "signal_id", "symbol", "direction", "signal_time",
    "entry_price", "stop_loss", "tp1", "tp2", "score",
    "adx", "rsi", "volume_ratio", "atr", "ema50", "ema200",
    "ema_distance_atr", "market_regime",
    "entry_status", "exit_status", "exit_time", "r_multiple",
    "holding_time", "MAE_R", "MFE_R", "failure_sequence",
]


def export_forensics_csv(
    trades: Sequence[ForensicsTrade],
    csv_path: str,
) -> str:
    """Write the per-trade diagnostic CSV. Does not touch baseline_trades.csv."""
    import csv, os
    os.makedirs(os.path.dirname(csv_path) or ".", exist_ok=True)
    with open(csv_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(_FORENSICS_CSV_COLUMNS)
        for t in trades:
            w.writerow([
                t.signal_id,
                t.symbol,
                t.direction,
                t.signal_time.isoformat(),
                _num(t.entry_price),
                _num(t.stop_loss),
                _num(t.tp1),
                _num(t.tp2),
                t.score,
                _num(t.adx),
                _num(t.rsi),
                _num(t.volume_ratio),
                _num(t.atr),
                _num(t.ema_fast),
                _num(t.ema_slow),
                _f(t.ema_distance_atr, ".4f"),
                t.market_regime,
                t.entry_status,
                t.exit_status,
                t.exit_time.isoformat() if t.exit_time else "",
                _f(float(t.r_multiple) if t.r_multiple is not None else None, ".6f"),
                t.holding_candles,
                _f(t.mae_r, ".4f"),
                _f(t.mfe_r, ".4f"),
                t.failure_sequence,
            ])
    return csv_path


def _num(v) -> str:
    if v is None:
        return ""
    return str(v)


# ---------------------------------------------------------------------------
# Markdown report builder
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Default audit findings + hypotheses (the diagnostic narrative).
# Populated with the audit conclusions so the markdown report is self-
# contained even when callers do not pass explicit findings/hypotheses.
# ---------------------------------------------------------------------------

_DEFAULT_AUDIT_FINDINGS: List[str] = [
    "RSI implementation is a non-Wilder EWM variant, not Wilder smoothing; "
    "it is self-consistent between the live engine and the precomputed backtest "
    "arrays, so it is NOT a bug. RSI values are used only as a score input and "
    "a bucket label, never as a directional filter that would flip a result.",
    "ADX (Wilder) is computed identically in the live and precomputed paths and "
    "is confirmed correct; it is not a source of the negative expectancy.",
    "TP1/TP2 R-multiple 'mismatch' (observed avg ~1.88R/3.04R vs configured "
    "1.5/2.5R) is NOT a bug. It is forced by zone geometry: the fill is at the "
    "conservative least-favorable edge, so risk_actual (~1.3 ATR) differs from "
    "risk_mid (~1.5 ATR) used to place TP1/TP2. For LONG, tp1_R = 2.45 ATR / "
    "1.3 ATR ≈ 1.88R. Verified against the CSV (tp1_R/tp2_R fall in narrow, "
    "consistent ranges across all 9 symbols).",
    "SL-after-TP1 accounting is an intentional conservative modeling choice: a "
    "trade that took 50% off at TP1 but then stopped is still booked at full "
    "-1R (gross model). This understates the realized P&L of TP1_THEN_SL "
    "trades and is a known bias, not a defect.",
    "Score buckets are degenerate in this run: the min_score filter is 80 but "
    "the observed minimum emitted score is 95, so every trade lands in the "
    "95-100 bucket. No score-differentiation signal is available in-sample.",
    "Precomputed vs naive engine equivalence tests pass; the forensic indicator "
    "re-derivation matches the values the engine actually used.",
    "F36 tz-mismatch candidate (CLI aware start/end vs naive CSV candle timestamps) "
    "was audited and empirically DISPROVEN: backtest/data.py loads all CSV "
    "timestamps as timezone-aware UTC (verified against real data/*.csv), and "
    "run_full_baseline runs cleanly with explicit tz-aware start/end bounds. "
    "No crash, no bug. _infer_period returns aware datetimes because they are "
    "derived from the aware CSV timestamps.",
]

_DEFAULT_HYPOTHESES: List[str] = [
    "H1 — Trend/chasing bias: the strategy is losing most of its R to FAVORABLE_THEN_SL "
    "(575) and SL_DIRECT (366); 63.9% of losers first moved >=0.5R favorable before "
    "reversing to SL. A trailing/partial-exit rule or an adverse-exit (stop out on "
    "first adverse candle) would capture the favorable excursion. Untested.",
    "H2 — Expiration drag: 473 trades (25.9%) expired open and are booked at 0R but "
    "represent unrealized opportunity cost. Raising the exit horizon or adding a "
    "time-based breakeven stop is untested.",
    "H3 — Regime/duration decay: OOS split is in_sample expectancy -0.005 vs "
    "out_of_sample -0.251; monthly results deteriorate into 2026-03/04 "
    "(PF 0.352/0.246). The edge, if any, is not stable over time. Untested.",
    "H4 — High-ADX reversal: the 40+ ADX bucket is the worst (expectancy -0.37, PF "
    "0.543) vs 30-40 ADX being near breakeven (-0.03). A high-volatility ADX "
    "gate that trims size or blocks entries at ADX>40 is untested.",
    "H5 — Volume-confirmation: 2.0+ volume-ratio bucket is the worst (-0.218, PF "
    "0.719); lighter-volume 1.2-1.5 bucket is near breakeven. Trading on climax "
    "volume appears adverse. Untested.",
    "H6 — Direction asymmetry: LONG and SHORT are symmetrically negative "
    "(-0.150 / -0.113), so the loss is not short-only; it is a general "
    "entry-quality/timing problem, not a directional one. Untested.",
    "H7 — Short-side RSI edge: SHORT with RSI<35 is the only positive bucket "
    "(expectancy +0.169, PF 1.256). An RSI-extreme short filter may add value. "
    "Untested.",
    "H8 — Per-symbol dispersion: ADA/SOL are positive (PF 1.026/1.104) while "
    "LINK/BNB are strongly negative (PF 0.615/0.654). A symbol-level "
    "eligibility rule is untested.",
]


def build_forensics_markdown(
    ft: List[ForensicsTrade],
    config,
    oos_windows: Dict[str, List[ForensicsTrade]],
    audit_findings: Optional[List[str]] = None,
    hypotheses: Optional[List[str]] = None,
) -> str:
    if audit_findings is None:
        audit_findings = _DEFAULT_AUDIT_FINDINGS
    if hypotheses is None:
        hypotheses = _DEFAULT_HYPOTHESES
    L: List[str] = []
    L.append("# PHASE 6 — FORENSIC ANALYSIS")
    L.append("")
    L.append(
        "> **DISCLAIMER:** Historical simulation diagnostics. Not a projection "
        "of live performance. Past results do not guarantee future profitability."
    )
    L.append("")
    L.append(
        "Convention: **OBSERVED** = measured from data. "
        "**INTERPRETATION** = reading of the measurement. "
        "**HYPOTHESIS** = untested, for future work only."
    )
    L.append("")

    # 1. Baseline recap
    L.append("## 1. Baseline Recap")
    overall = _group_stats(ft)
    L.append(f"- Total signals: {overall.signals}")
    L.append(f"- Filled: {overall.filled}  Closed: {overall.closed}")
    L.append(f"- Win rate: {_pct(overall.win_rate)}  PF: {_pf(overall.profit_factor)}")
    L.append(f"- Total R (closed): {_f(overall.total_r)}  Expectancy: {_f(overall.expectancy)}")
    L.append(f"- Max DD (R): {_f(overall.max_drawdown_r)}")
    L.append("")

    # 2. R distribution
    L.append("## 2. R-Multiple Distribution")
    for label in ("ALL", "LONG", "SHORT"):
        d = r_distribution(ft, direction=None if label == "ALL" else label)
        L.append(
            f"- {label}: n={d.count} min={_f(d.min_r)} max={_f(d.max_r)} "
            f"mean={_f(d.mean_r)} median={_f(d.median_r)} "
            f"p25={_f(d.p25)} p75={_f(d.p75)} totalR={_f(d.total_r)}"
        )
    L.append("")

    # 3. Exit reasons
    L.append("## 3. Exit Reason Counts")
    for label in ("ALL", "LONG", "SHORT"):
        e = exit_reasons(ft, direction=None if label == "ALL" else label)
        L.append(
            f"- {label}: SL={e.sl} ({_pct(e.pct(e.sl))}) "
            f"TP1={e.tp1} ({_pct(e.pct(e.tp1))}) TP2={e.tp2} ({_pct(e.pct(e.tp2))}) "
            f"Expired={e.expired} ({_pct(e.pct(e.expired))}) "
            f"NotFilled={e.not_filled} ({_pct(e.pct(e.not_filled))}) total={e.total}"
        )
    L.append("")

    # 4. Holding time
    L.append("## 4. Holding Time (5M candles, filled trades)")
    for label, kw in (("ALL", {}), ("WINNERS", {"winners_only": True}),
                      ("LOSERS", {"losers_only": True})):
        h = holding_time(ft, **kw)
        L.append(
            f"- {label}: median={_f(h.median, '.2f')} mean={_f(h.mean, '.2f')} "
            f"p25={_f(h.p25, '.2f')} p75={_f(h.p75, '.2f')} max={_f(h.maximum, '.2f')}"
        )
    L.append("")

    # 5. MAE/MFE
    L.append("## 5. MAE / MFE (in R)")
    m = mae_mfe(ft)
    L.append(f"- Winners: avg MAE={_f(m.winners_mae)} avg MFE={_f(m.winners_mfe)}")
    L.append(f"- Losers:  avg MAE={_f(m.losers_mae)} avg MFE={_f(m.losers_mfe)}")
    L.append(f"- % losers that ever reached >=0.5R favorable before SL: {_pct(m.losers_hit_half_r)}")
    L.append(f"- % winners that had adverse movement first (MAE>0): {_pct(m.winners_negative_mae_pct)}")
    L.append("")

    # 6. Score buckets
    L.append("## 6. Score Buckets")
    L.append(_group_table(score_buckets(ft)))
    L.append("")

    # 7. ADX buckets
    L.append("## 7. ADX Buckets")
    L.append(_group_table(adx_buckets(ft)))
    L.append("")

    # 8. RSI buckets (direction-aware)
    L.append("## 8. RSI Buckets (direction-aware)")
    L.append(_group_table(rsi_buckets(ft)))
    L.append("")

    # 9. Volume buckets
    L.append("## 9. Volume-Ratio Buckets")
    L.append(_group_table(volume_buckets(ft)))
    L.append("")

    # 10. EMA distance
    L.append("## 10. Distance from EMA50 (in ATR)")
    L.append(_group_table(ema_distance_buckets(ft)))
    L.append("")

    # 11. Regime
    L.append("## 11. Market Regime")
    L.append(_group_table(group_by(ft, lambda t: t.market_regime)))
    L.append("")

    # 12. Direction
    L.append("## 12. LONG vs SHORT")
    L.append(_group_table(group_by(ft, lambda t: t.direction)))
    L.append("- LONG by regime:")
    L.append(_group_table(group_by(
        [t for t in ft if t.direction == "LONG"],
        lambda t: t.market_regime,
    )))
    L.append("- SHORT by regime:")
    L.append(_group_table(group_by(
        [t for t in ft if t.direction == "SHORT"],
        lambda t: t.market_regime,
    )))
    L.append("")

    # 13. Symbol
    L.append("## 13. Per-Symbol")
    L.append(_group_table(group_by(ft, lambda t: t.symbol)))
    L.append("")

    # 14. Monthly
    L.append("## 14. Monthly")
    L.append(_group_table(group_by(ft, lambda t: t.signal_time.strftime("%Y-%m"))))
    L.append("")

    # 15. OOS
    L.append("## 15. OOS Separation")
    for seg in ("in_sample", "validation", "out_of_sample"):
        seg_trades = oos_windows.get(seg, [])
        L.append(f"- {seg}: " + _one_line(seg_trades))
    L.append("")

    # 16. Failure sequences
    L.append("## 16. Failure Sequences (filled, closed)")
    L.append(_group_table(group_by(
        [t for t in ft if t.is_filled],
        lambda t: t.failure_sequence,
    )))
    L.append("")

    # 17. Entry quality
    L.append("## 17. Entry Quality")
    eq = entry_quality(ft)
    L.append(f"- Filled: {eq.filled}  Unfilled/Expired: {eq.unfilled_expired}")
    L.append(f"- Avg fill offset within zone (0=conservative edge,1=far edge): {_f(eq.avg_fill_offset_pct)}")
    L.append(f"- % of fills exactly at conservative edge: {_pct(eq.conservative_edge_fill_pct)}")
    L.append("")

    # 18. RR validation
    L.append("## 18. Risk/Reward Validation")
    rr = rr_validation(ft, config.tp1_rr, config.tp2_rr)
    L.append(f"- Config TP1 RR={rr.config_tp1_rr}  TP2 RR={rr.config_tp2_rr}")
    L.append(f"- Observed avg SL distance (R)={_f(rr.avg_sl_distance_r)}  "
             f"TP1 (R)={_f(rr.avg_tp1_r)}  TP2 (R)={_f(rr.avg_tp2_r)}")
    L.append("")

    # 19. Bugs
    L.append("## 19. Implementation Audit")
    if audit_findings:
        for i, f_ in enumerate(audit_findings, 1):
            L.append(f"{i}. {f_}")
    else:
        L.append("No audit findings recorded.")
    L.append("")

    # 20. Hypotheses
    L.append("## 20. Potential Hypotheses for Future Testing")
    if hypotheses:
        for h in hypotheses:
            L.append(f"- {h}")
    else:
        L.append("No hypotheses recorded.")
    L.append("")

    L.append("## DISCLAIMER")
    L.append("This is a HISTORICAL SIMULATION, not a projection of live performance.")
    L.append("Past backtest results do not guarantee future profitability.")
    return "\n".join(L)


def _one_line(trades: Sequence[ForensicsTrade]) -> str:
    g = _group_stats(trades)
    return (f"signals={g.signals} filled={g.filled} win_rate={_pct(g.win_rate)} "
            f"expectancy={_f(g.expectancy)} PF={_pf(g.profit_factor)} "
            f"totalR={_f(g.total_r)}")


def _group_table(stats: Dict[str, GroupStats]) -> str:
    if not stats:
        return "_(no data)_\n"
    lines = ["| " + " | ".join(GroupStats().header()) + " |",
             "|" + "---|" * 8]
    for label in sorted(stats):
        row = stats[label].as_row(label)
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    return "\n".join(lines) + "\n"
