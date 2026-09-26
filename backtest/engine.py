"""
PHASE 4: Backtest signal engine.

Drives the LIVE SignalEngine over historical 5M decision points using
closed-only, no-look-ahead windows (backtest.align). Applies the SAME
cooldown and duplicate-signal logic as live mode (signal_filter).

Reused live components (no duplication):
- app.signal_engine.SignalEngine.generate_signal
- backtest.indicators_shared.compute_indicators (== Scanner._calc_indicators)
- app.data_validation.cutoff_candles (no-look-ahead slicing)
- app.signal_filter.check_cooldown / generate_signal_id

New backtest-only additions:
- per-symbol cooldown gate (signal_filter.check_cooldown)
- warmup gate (no signals while indicators are None)
- deterministic walk (no wall-clock, no network)
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Optional

from app.models import Candle, Signal, SignalDirection, IndicatorValues
from app.config import get_config
from app import data_validation as dv
from app import signal_filter
from app.signal_engine import SignalEngine
from app.market_regime import detect_regime, MarketRegimeError

from backtest.align import (
    AlignedWindow, align_window, iter_trigger_decision_points, TRIGGER_TF,
)
from backtest.indicators_shared import compute_indicators, indicators_ready
from backtest.indicators_precomputed import PrecomputedIndicators


@dataclass
class BacktestSignal:
    """A signal emitted by the backtest, with regime + decision metadata."""
    signal: Signal
    symbol: str
    decision_time: datetime
    market_regime: str          # RANGING / WEAK_TREND / TRENDING / UNKNOWN
    score: int
    regime_known: bool = True

    @property
    def signal_id(self) -> str:
        return self.signal.signal_id


@dataclass
class _SymbolState:
    last_signal_time: Optional[datetime] = None
    emitted_ids: set = field(default_factory=set)


class BacktestEngine:
    """
    Historical backtest that reuses the live SignalEngine.

    For each closed 5M trigger candle in [start, end] (per symbol) it builds
    a closed-only multi-timeframe window and calls SignalEngine. It applies
    the same cooldown + duplicate-id suppression as live mode.
    """

    def __init__(self, dataset: Dict[str, "HistoricalDataset"] = None,
                 engine: Optional[SignalEngine] = None,
                 start: Optional[datetime] = None,
                 end: Optional[datetime] = None):
        """
        Args:
            dataset: {symbol: HistoricalDataset}. Required to run().
            engine: injected SignalEngine (defaults to SignalEngine()).
            start/end: inclusive-ish bounds on the 5M decision close times.
        """
        self._datasets = dataset or {}
        self.config = get_config()
        self.engine = engine or SignalEngine()
        self.start = start
        self.end = end

        self._state: Dict[str, _SymbolState] = {}

    # ------------------------------------------------------------------
    def _state_for(self, symbol: str) -> _SymbolState:
        st = self._state.get(symbol)
        if st is None:
            st = _SymbolState()
            self._state[symbol] = st
        return st

    def _cooldown_ok(self, symbol: str, trigger_open_time: datetime) -> bool:
        """Cooldown gate on the trigger candle OPEN time.

        Both last_signal_time and the current trigger open time are on the same
        time base (candle open), so `cooldown_candles * interval` is the true
        number of trigger candles between two signals. Mirrors live mode.

        check_cooldown returns True when the cooldown has fully elapsed
        (allow) and False when the signal is still within cooldown (block).
        """
        st = self._state_for(symbol)
        interval = dv.TIMEFRAME_SECONDS[TRIGGER_TF]
        # Pass trigger_open_time as `now`: this is the same time base as
        # last_signal_time (candle open), so the elapsed time correctly
        # represents how many candles have passed.
        return signal_filter.check_cooldown(
            st.last_signal_time, trigger_open_time, interval,
            self.config.cooldown_candles,
        )

    def _regime(self, setup_ind: IndicatorValues) -> tuple[str, bool]:
        if setup_ind.adx is None:
            return "UNKNOWN", False
        try:
            return detect_regime(setup_ind.adx, self.config.adx_min).value, True
        except MarketRegimeError:
            return "UNKNOWN", False

    # ------------------------------------------------------------------
    def evaluate_window(self, symbol: str, window: AlignedWindow,
                        warmup_min: int) -> Optional[BacktestSignal]:
        """Evaluate one aligned window; returns BacktestSignal or None."""
        if not window.ok:
            return None

        # Cooldown gate (same as live): use the trigger candle open time so
        # the comparison is on the same time base as last_signal_time.
        if not self._cooldown_ok(symbol, window.trigger_candle.timestamp):
            return None

        # Warmup gate: require enough closed candles AND all indicators ready
        if (len(window.trigger) < warmup_min
                or len(window.setup) < warmup_min
                or len(window.hf) < warmup_min):
            return None

        hf_ind = compute_indicators(window.hf, self.config)
        setup_ind = compute_indicators(window.setup, self.config)
        trigger_ind = compute_indicators(window.trigger, self.config)

        if not (indicators_ready(setup_ind) and indicators_ready(trigger_ind)):
            return None

        signal = self.engine.generate_signal(
            symbol,
            window.hf, hf_ind,
            window.setup, setup_ind,
            window.trigger, trigger_ind,
        )
        if signal is None:
            return None

        # Duplicate-signal gate (same id -> suppress)
        st = self._state_for(symbol)
        if signal.signal_id in st.emitted_ids:
            return None

        # Commit state (only for accepted signals, mirroring live scanner)
        st.emitted_ids.add(signal.signal_id)
        st.last_signal_time = signal.trigger_candle_time

        regime, known = self._regime(setup_ind)
        return BacktestSignal(
            signal=signal,
            symbol=symbol,
            decision_time=window.decision_time,
            market_regime=regime,
            score=signal.score,
            regime_known=known,
        )

    # ------------------------------------------------------------------
    def run_symbol(self, symbol: str, warmup_min: int) -> List[BacktestSignal]:
        """
        Run the backtest for one symbol.

        Uses precomputed indicator arrays (O(1) per window) when the dataset
        is large enough to justify them (> 500 trigger candles); falls back
        to the per-window compute_indicators path for small synthetic test
        datasets where the overhead would exceed the savings.
        """
        ds = self._datasets[symbol]
        trigger = ds.tf("5m")
        points = iter_trigger_decision_points(trigger, self.start, self.end)

        # For large datasets, precompute indicators once per timeframe
        if len(trigger) > 500:
            return self._run_symbol_precomputed(symbol, warmup_min, points)
        else:
            return self._run_symbol_naive(symbol, warmup_min, points)

    def _run_symbol_naive(
        self, symbol: str, warmup_min: int, points: List["Candle"],
    ) -> List[BacktestSignal]:
        """Original per-window compute_indicators path (small datasets)."""
        ds = self._datasets[symbol]
        signals: List[BacktestSignal] = []
        for c in points:
            decision_time = dv.candle_close_time(c, TRIGGER_TF)
            window = align_window(symbol, ds.tf("1h"), ds.tf("15m"),
                                  ds.tf("5m"), decision_time)
            sig = self.evaluate_window(symbol, window, warmup_min)
            if sig is not None:
                signals.append(sig)
        return signals

    def _run_symbol_precomputed(
        self, symbol: str, warmup_min: int, points: List["Candle"],
    ) -> List[BacktestSignal]:
        """
        Fast path: precompute indicator arrays once; per-window is O(1).

        For each trigger candle C at index i:
        - T = C.close_time
        - hf_idx    = last 1H  candle with close_time <= T  (binary search)
        - setup_idx = last 15M candle with close_time <= T  (binary search)
        - trigger_idx = i  (trigger candle at index i is itself the cutoff)

        All indicator values are read from precomputed arrays — equivalent
        to compute_indicators(prefix_up_to_i) for causal indicators.

        SignalEngine only reads hf[-1], setup[-2:], trigger[-1], so we pass
        small tail slices instead of full lists to avoid O(n) copies.
        """
        import bisect

        ds = self._datasets[symbol]
        hf_full    = ds.tf("1h")
        setup_full = ds.tf("15m")
        trig_full  = ds.tf("5m")

        # Precompute indicator arrays for all three timeframes
        pre = PrecomputedIndicators(self.config)
        pre.precompute(hf_full, setup_full, trig_full)

        # Build close-time arrays for binary search
        def _close_times(candles: List["Candle"], tf: str) -> List[datetime]:
            secs = dv.TIMEFRAME_SECONDS[tf]
            return [c.timestamp + timedelta(seconds=secs) for c in candles]

        hf_close_times    = _close_times(hf_full, "1h")
        setup_close_times = _close_times(setup_full, "15m")

        # Build timestamp -> index map for trigger candles (O(n) once)
        trig_ts_to_idx: Dict[datetime, int] = {
            cc.timestamp: j for j, cc in enumerate(trig_full)
        }

        # SignalEngine reads (after our patches):
        #   hf_candles[-1].close
        #   setup_candles[-2:]         (O(1) — we pass just 2 candles)
        #   self._rsi_override[-2]     (O(1) — precomputed RSI array slice)
        #   trigger_candles[-2:]       (for check_long_trigger / check_short_trigger)
        #   trigger_candles[-1]       (for entry / risk / volume)
        #
        # Precompute RSI once over the full 15M setup series and share a
        # list-slice as the engine's _rsi_override — SignalEngine then reads
        # prev_rsi = _rsi_override[-2] in O(1) instead of calling calc_rsi
        # (pandas EWM) on every window.

        from app.indicators import rsi as calc_rsi
        setup_closes_all = [c.close for c in setup_full]
        setup_rsi_full   = calc_rsi(setup_closes_all, self.config.rsi_length)

        # Install _rsi_override on the engine instance.
        # SignalEngine reads it as (rsi_full_array, setup_count) tuple —
        # prev_rsi = rsi_full_array[setup_count - 2] in O(1) per window,
        # no list copies.
        engine = self.engine

        # Pre-allocated mutable slots; the tuple is reassigned each window
        # so we use a mutable wrapper to avoid 31k tuple allocations.
        _rsi_ref = [setup_rsi_full, 0]  # [rsi_array, setup_count]
        engine._rsi_override = _rsi_ref

        # Pre-allocate the 2-candle trigger / setup tail slots
        # We mutate the same list objects in-place per window to avoid
        # 31k list allocations.
        sig_tail = [None, None]          # setup tail (2 candles)
        trig_tail = [None, None]         # trigger tail (2 candles)
        hf_last  = [None]                # 1H tail (1 candle)

        signals: List[BacktestSignal] = []

        for c in points:
            T = dv.candle_close_time(c, TRIGGER_TF)
            trig_idx = trig_ts_to_idx.get(c.timestamp)
            if trig_idx is None:
                continue

            # Binary search: last candle index with close_time <= T
            hf_count    = bisect.bisect_right(hf_close_times, T)
            setup_count = bisect.bisect_right(setup_close_times, T)
            trig_count  = trig_idx + 1

            # MIN_CONTEXT + warmup gates (same rules as align_window)
            if hf_count < 2 or setup_count < 2:
                continue
            if trig_count < warmup_min:
                continue

            # Cooldown gate (same as live): use trigger candle open time
            if not self._cooldown_ok(symbol, c.timestamp):
                continue

            # Read indicator values at the cutoff indices
            hf_ind, setup_ind, trig_ind = pre.build_ind(
                hf_count - 1, setup_count - 1, trig_idx,
            )
            if not (indicators_ready(setup_ind) and indicators_ready(trig_ind)):
                continue

            # RSI override: update the shared 2-slot ref in O(1).
            # SignalEngine reads _rsi_ref[0][setup_count-2] — O(1) index.
            _rsi_ref[1] = setup_count

            # O(1) in-place tail mutations (no new list objects per window)
            hf_last[0] = hf_full[hf_count - 1]
            sig_tail[0] = setup_full[setup_count - 2]
            sig_tail[1] = setup_full[setup_count - 1]
            trig_tail[0] = trig_full[trig_idx - 1] if trig_idx >= 1 else None
            trig_tail[1] = trig_full[trig_idx]

            signal = engine.generate_signal(
                symbol,
                hf_last,   hf_ind,
                sig_tail,  setup_ind,
                trig_tail, trig_ind,
            )
            if signal is None:
                continue

            st = self._state_for(symbol)
            if signal.signal_id in st.emitted_ids:
                continue

            st.emitted_ids.add(signal.signal_id)
            st.last_signal_time = signal.trigger_candle_time

            regime, known = self._regime(setup_ind)
            signals.append(BacktestSignal(
                signal=signal,
                symbol=symbol,
                decision_time=T,
                market_regime=regime,
                score=signal.score,
                regime_known=known,
            ))

        return signals

    def run(self, warmup_min: int = 0) -> Dict[str, List[BacktestSignal]]:
        """Run the backtest over every symbol; returns {symbol: [signals]}."""
        out: Dict[str, List[BacktestSignal]] = {}
        for symbol in self._datasets:
            out[symbol] = self.run_symbol(symbol, warmup_min)
        return out

    # ------------------------------------------------------------------
    def run_full(self, warmup_min: int = 0,
                 execution_model: "ExecutionModel | None" = None
                 ) -> "BacktestMetrics":
        """
        Run the backtest AND simulate execution, returning full metrics.

        Uses TradeSimulator over each signal's post-trigger 5M candles and
        metrics_from_trades for the aggregate report.
        """
        from backtest.execution import TradeSimulator, ExecutionModel
        from backtest.metrics import metrics_from_trades

        model = execution_model or ExecutionModel()
        sim = TradeSimulator(model)

        all_trades = []
        warmup_skipped = 0
        for symbol in self._datasets:
            ds = self._datasets[symbol]
            signals = self.run_symbol(symbol, warmup_min)
            trigger = ds.tf("5m")
            ts_list = [c.timestamp for c in trigger]
            for bs in signals:
                trigger_ts = bs.signal.trigger_candle_time
                try:
                    idx = ts_list.index(trigger_ts)
                except ValueError:
                    continue
                future = trigger[idx + 1:]
                tr = sim.simulate(
                    bs.signal, future,
                    regime=bs.market_regime,
                    regime_known=bs.regime_known,
                )
                all_trades.append(tr)
            warmup_skipped += max(0, warmup_min)

        return metrics_from_trades(
            all_trades,
            period_start=self.start.isoformat() if self.start else None,
            period_end=self.end.isoformat() if self.end else None,
            symbols=list(self._datasets.keys()),
            warmup_candles_skipped=warmup_skipped,
        )
