"""
Scanner: monitors multiple symbols and produces signal DECISIONS.

PHASE 3 (dry-run) responsibilities:
- Fetch public OHLCV for 1H / 15M / 5M
- Deterministic closed-candle + multi-timeframe synchronization
- Data validation (structure, warmup, freshness)
- Signal engine evaluation
- Per-symbol decision with a primary REJECTION REASON when no signal
- Signal observability (SIGNAL_GENERATED with full fields)
- State: never re-evaluate the same closed 5M candle
- Per-symbol cooldown gate (COOLDOWN_CANDLES x 5m trigger candles)
- Per-symbol opposite-signal protection (only within the cooldown window)
- Multi-symbol isolation: one bad symbol never aborts the scan

No order creation. Signal-only.
"""
from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Dict, Optional, Set
from datetime import datetime, timezone

from app.models import Signal, Candle, IndicatorValues, MarketBias, SignalDirection
from app.market_data import BinanceMarketData, MarketDataError, RateLimitError, TransientError
from app.signal_engine import SignalEngine
from app.config import get_config
from app import indicators, strategy, data_validation, signal_filter
from app.logger import get_logger

logger = get_logger(__name__)

# Reason codes for NO-SIGNAL decisions (logged for live validation)
REASON_HTF_NEUTRAL = "HTF_NEUTRAL"
REASON_SETUP_INVALID = "SETUP_INVALID"
REASON_TRIGGER_NOT_CONFIRMED = "TRIGGER_NOT_CONFIRMED"
REASON_LOW_VOLUME = "LOW_VOLUME"
REASON_OVEREXTENDED = "OVEREXTENDED"
REASON_SCORE_BELOW_THRESHOLD = "SCORE_BELOW_THRESHOLD"
REASON_INVALID_RR = "INVALID_RR"
REASON_ADX_TOO_LOW = "ADX_TOO_LOW"
REASON_WRONG_DI_DIRECTION = "WRONG_DI_DIRECTION"
REASON_RSI_NOT_RECOVERED = "RSI_NOT_RECOVERED"
REASON_INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
REASON_STALE_DATA = "STALE_DATA"
REASON_INVALID_DATA = "INVALID_DATA"
REASON_MARKET_DATA_ERROR = "MARKET_DATA_ERROR"
REASON_RATE_LIMITED = "RATE_LIMITED"
REASON_COOLDOWN_ACTIVE = "COOLDOWN_ACTIVE"
REASON_OPPOSITE_SIGNAL_BLOCKED = "OPPOSITE_SIGNAL_BLOCKED"
REASON_CANDLE_NOT_CLOSED = "CANDLE_NOT_CLOSED"
REASON_UNKNOWN = "UNKNOWN"


@dataclass
class ScanDecision:
    """Result of scanning one symbol in one cycle."""
    symbol: str
    decision_time: Optional[datetime]
    bias: MarketBias
    regime: Optional[str]
    setup_state: str            # VALID / INVALID / SKIPPED
    trigger_state: str         # VALID / WAIT / SKIPPED
    adx: Optional[float]
    rsi: Optional[float]
    atr: Optional[float]
    volume_ratio: Optional[float]
    score: int
    decision: str              # "SIGNAL" | "NO_SIGNAL"
    reason: str               # rejection reason code (empty when decision=SIGNAL)
    signal: Optional[Signal] = None
    processed_trigger_ts: Optional[datetime] = None  # state key

    def log_line(self) -> str:
        """Single compact log line — never dump OHLCV arrays."""
        rr = ""
        if self.signal:
            rr = f" | SL={self.signal.stop_loss} TP1={self.signal.tp1} TP2={self.signal.tp2}"
        return (
            f"{self.symbol} bias={self.bias.value} regime={self.regime} "
            f"setup={self.setup_state} trigger={self.trigger_state} "
            f"ADX={self.adx} RSI={self.rsi} ATR={self.atr} Vol={self.volume_ratio}x "
            f"score={self.score} -> {self.decision}"
            + (f" [{self.reason}]" if self.reason else "")
            + (f" {rr}" if rr else "")
        )


class Scanner:
    """
    Multi-symbol scanner with deterministic candle synchronization.
    """

    def __init__(self, market_data: Optional[BinanceMarketData] = None,
                 engine: Optional[SignalEngine] = None):
        self.config = get_config()
        self.market_data = market_data or BinanceMarketData(
            request_timeout_seconds=self.config.request_timeout_seconds,
            max_retries=self.config.max_retries,
            backoff_base_seconds=float(self.config.backoff_base_seconds),
        )
        self.signal_engine = engine or SignalEngine()
        # state: symbol -> (last processed trigger candle ts, set of emitted signal ids)
        self._last_trigger_ts: Dict[str, datetime] = {}
        self._emitted_ids: Set[str] = set()
        # Per-symbol cooldown / opposite-direction state.
        # Last ACCEPTED signal's trigger candle OPEN time and direction, keyed
        # by symbol. Only committed when a signal is actually accepted.
        self._last_signal_time: Dict[str, datetime] = {}
        self._last_signal_direction: Dict[str, SignalDirection] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def scan_all_symbols(self) -> List[Signal]:
        """
        Scan all configured symbols. One bad symbol never aborts the rest.

        Returns:
            List of valid signals (empty if none). Decisions are logged.
        """
        signals: List[Signal] = []
        for symbol in self.config.symbols:
            try:
                decision = self.scan_symbol(symbol)
                if decision.signal:
                    signals.append(decision.signal)
            except Exception as e:
                # Isolation: log and continue to next symbol
                logger.error(f"{symbol}: unexpected scan error, continuing: {e}")
                continue
        return signals

    def scan_symbol(self, symbol: str) -> ScanDecision:
        """
        Scan a single symbol; returns a ScanDecision (never raises on data issues).
        """
        now = datetime.now(timezone.utc)

        # --- Fetch 1H / 15M / 5M ---
        try:
            hf_candles = self._fetch(symbol, "1h")
            setup_candles = self._fetch(symbol, "15m")
            trigger_candles = self._fetch(symbol, "5m")
        except RateLimitError as e:
            logger.warning(f"{symbol}: rate limited: {e}")
            return self._reject(symbol, None, REASON_RATE_LIMITED, now)
        except TransientError as e:
            logger.warning(f"{symbol}: transient data error: {e}")
            return self._reject(symbol, None, REASON_MARKET_DATA_ERROR, now)
        except MarketDataError as e:
            logger.warning(f"{symbol}: market data error: {e}")
            return self._reject(symbol, None, REASON_MARKET_DATA_ERROR, now)

        # --- Multi-timeframe synchronization to a common closed decision point ---
        synced = data_validation.sync_all(hf_candles, setup_candles, trigger_candles)
        decision_time = data_validation.common_decision_point(trigger_candles, "5m")
        hf_candles = synced["hf"]
        setup_candles = synced["setup"]
        trigger_candles = synced["trigger"]

        if not hf_candles or not setup_candles or not trigger_candles:
            return self._reject(symbol, decision_time, REASON_INSUFFICIENT_HISTORY, now)

        # --- Data validation (structure, warmup, freshness) ---
        min_history = data_validation.min_history_for_indicators(
            ema_slow_period=self.config.ema_slow,
            adx_period=self.config.adx_length,
            rsi_period=self.config.rsi_length,
            atr_period=self.config.atr_length,
            volume_sma_period=self.config.volume_sma_length,
        )
        # Freshness reference = wall-clock `now` (the observation instant).
        # Staleness of each series is judged as now - latest_close_time, using
        # the per-timeframe rule (age > 1.5*tf -> STALE):
        #   - a freshly-closed 1H candle (closed up to 1h ago, now still in the
        #     next hour) has age <= tf -> NOT stale.
        #   - a 5M candle whose last close is > 7.5 minutes old -> STALE.
        # This is the production semantics: a lagging 5M feed is caught, while a
        # normally-lagging 1H/15M context series is not mislabeled.
        validation_error = self._first_validation_error(
            hf_candles, "1h", setup_candles, "15m", trigger_candles, "5m",
            min_history, now, self.config.max_data_age_seconds,
        )
        if validation_error is not None:
            reason = REASON_STALE_DATA if validation_error.startswith("STALE") else \
                     (REASON_INSUFFICIENT_HISTORY if validation_error.startswith("INSUFFICIENT") else
                      REASON_INVALID_DATA)
            logger.warning(f"{symbol}: data rejected: {validation_error}")
            return self._reject(symbol, decision_time, reason, now)

        # --- State guard: never re-evaluate the same closed 5M candle ---
        latest_trigger_ts = trigger_candles[-1].timestamp
        last = self._last_trigger_ts.get(symbol)
        if last is not None and latest_trigger_ts <= last:
            # Same (or older) closed 5M candle already evaluated -> skip new signal
            logger.debug(f"{symbol}: trigger candle {latest_trigger_ts} already processed, skipping")
            return self._reject(symbol, decision_time, "REPEATED_CANDLE", now)
        self._last_trigger_ts[symbol] = latest_trigger_ts

        # --- Indicators ---
        hf_ind = self._calc_indicators(hf_candles)
        setup_ind = self._calc_indicators(setup_candles)
        trigger_ind = self._calc_indicators(trigger_candles)

        # --- Cooldown + opposite-direction gate (production signal gate) ---
        # Only a CONFIRMED closed trigger candle passes; any direction is blocked
        # while the cooldown window is active. State is committed only when a
        # signal is actually accepted below (restart-safe: SQLite is the
        # source of truth for dedup; in-memory cooldown is re-derived on each
        # restart from the last accepted signal's trigger candle open time).
        gate_reason = self._direction_gate_reason(
            symbol, trigger_candles[-1].timestamp,
            self._bias(hf_ind, hf_candles),
        )
        if gate_reason is not None:
            return self._reject(symbol, decision_time, gate_reason, now)

        # --- Decision reason classification (for observability) ---
        reason = self._classify_rejection(
            symbol, hf_ind, hf_candles, setup_ind, setup_candles,
            trigger_ind, trigger_candles,
        )

        # --- Signal engine ---
        signal = self.signal_engine.generate_signal(
            symbol, hf_candles, hf_ind, setup_candles, setup_ind,
            trigger_candles, trigger_ind,
        )

        volume_ratio = self._volume_ratio(trigger_candles[-1], trigger_ind)

        if signal is not None:
            if signal.signal_id in self._emitted_ids:
                logger.debug(f"{symbol}: duplicate signal {signal.signal_id} suppressed")
                signal = None
            else:
                self._emitted_ids.add(signal.signal_id)
                # Commit per-symbol cooldown / direction state (restart-safe:
                # the gate is re-applied on the next evaluation of this candle;
                # if delivery later fails, the restart-safety path in main.py
                # clears this state so the signal can be re-generated once).
                self._last_signal_time[symbol] = signal.trigger_candle_time
                self._last_signal_direction[symbol] = signal.direction
                logger.info(
                    f"SIGNAL_GENERATED {symbol} {signal.direction.value} "
                    f"score={signal.score} entry={signal.entry_low}-{signal.entry_high} "
                    f"SL={signal.stop_loss} TP1={signal.tp1} TP2={signal.tp2} "
                    f"RR1={signal.rr_tp1} RR2={signal.rr_tp2} "
                    f"trigger={signal.trigger_candle_time.isoformat()}"
                )

        decision = ScanDecision(
            symbol=symbol,
            decision_time=decision_time,
            bias=self._bias(hf_ind, hf_candles),
            regime=self._regime(setup_ind),
            setup_state="VALID" if self._setup_state(symbol, hf_ind, hf_candles, setup_ind, setup_candles) else "INVALID",
            trigger_state="VALID" if self._trigger_state(symbol, hf_ind, hf_candles, setup_ind, setup_candles, trigger_candles) else "WAIT",
            adx=float(setup_ind.adx) if setup_ind.adx is not None else None,
            rsi=float(setup_ind.rsi) if setup_ind.rsi is not None else None,
            atr=float(setup_ind.atr) if setup_ind.atr is not None else None,
            volume_ratio=volume_ratio,
            score=signal.score if signal else 0,
            decision="SIGNAL" if signal else "NO_SIGNAL",
            reason="" if signal else reason,
            signal=signal,
            processed_trigger_ts=latest_trigger_ts,
        )

        if self.config.dry_run:
            logger.info(f"[DRY-RUN] {decision.log_line()}")
        else:
            logger.info(decision.log_line())

        return decision

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _fetch(self, symbol: str, tf: str) -> List[Candle]:
        """Fetch + structural-validate one timeframe."""
        api_tf = data_validation.API_TIMEFRAMES[tf]
        candles = self.market_data.fetch_klines(symbol, api_tf, limit=700)
        self.market_data.validate_candles(candles, tf=tf)
        return candles

    def _direction_gate_reason(
        self,
        symbol: str,
        latest_trigger_ts: datetime,
        current_bias: MarketBias,
    ) -> Optional[str]:
        """
        Production cooldown + opposite-direction gate.

        COOLDOWN_CANDLES (default 3) × 5m trigger candles = 15 minutes.
        While the window is active ANY new signal for the same symbol is
        blocked (same-direction AND opposite-direction).

        Deterministic: the gate compares closed-candle open times — the
        last accepted signal's trigger candle open time (T_last) vs. the
        latest closed trigger candle open time (T).  No wall-clock calls.

        A new trigger candle T is allowed when:
            (T - T_last) / tf_seconds > cooldown_candles

        The reason code distinguishes same vs. opposite direction so logs
        are actionable, but the gate outcome (block/allow) is identical.
        """
        last_time = self._last_signal_time.get(symbol)
        if last_time is None:
            return None

        cooldown = self.config.cooldown_candles
        if cooldown <= 0:
            return None

        # Cooldown is measured in CLOSED 5m trigger candles: both the last
        # accepted signal's trigger candle open time and the current latest
        # closed trigger candle open time share the same time base, so the
        # elapsed candle count is exact and restart-deterministic.
        #
        # Delegates to signal_filter.check_cooldown so live mode uses the
        # SAME boundary semantics as the backtest and the test suite
        # (allow when elapsed >= cooldown_candles x 5m, i.e. `>=` not `>`).
        if signal_filter.check_cooldown(
            last_time,
            latest_trigger_ts,
            data_validation.TIMEFRAME_SECONDS["5m"],
            cooldown,
        ):
            # Fully outside the cooldown window: allow (duplicate protection
            # is handled separately by _emitted_ids / signal_exists).
            return None

        # Within the cooldown window: block.  Use the current 1H bias as a
        # deterministic direction hint — the signal engine will emit a LONG
        # only when bias is BULLISH and a SHORT only when bias is BEARISH.
        last_dir = self._last_signal_direction.get(symbol)
        hint = self._direction_hint_from_bias(current_bias)
        if hint is not None and last_dir is not None and \
           signal_filter.check_opposite_signal_protection(last_dir, hint):
            return REASON_OPPOSITE_SIGNAL_BLOCKED
        return REASON_COOLDOWN_ACTIVE

    @staticmethod
    def _direction_hint_from_bias(bias: MarketBias) -> Optional[SignalDirection]:
        """Map the 1H bias to the direction the signal engine would emit."""
        if bias == MarketBias.BULLISH:
            return SignalDirection.LONG
        if bias == MarketBias.BEARISH:
            return SignalDirection.SHORT
        return None

    def restore_cooldown_state(self, rows) -> int:
        """
        Rehydrate per-symbol cooldown / direction state from SQLite.

        Called once at startup. Without this, a restart would wipe the
        in-memory cooldown state and immediately re-open the cooldown window
        for every symbol, letting a restart double-signal the same setup.

        Args:
            rows: Iterable of (symbol, trigger_candle_time, direction) tuples
                as returned by SignalStore.get_last_delivered_by_symbol().

        Returns:
            Number of symbols restored.
        """
        restored = 0
        for symbol, trigger_ts, direction in rows:
            ts = trigger_ts if isinstance(trigger_ts, datetime) else \
                datetime.fromisoformat(trigger_ts)
            self._last_signal_time[symbol] = ts
            try:
                self._last_signal_direction[symbol] = SignalDirection(direction)
            except ValueError:
                logger.warning(f"Unknown stored direction {direction!r} for {symbol}")
            restored += 1
        if restored:
            logger.info(f"Restored cooldown state for {restored} symbol(s) from SQLite")
        return restored

    def clear_signal_state(self, symbol: str, signal_id: Optional[str] = None) -> None:
        """
        Reset per-symbol cooldown / direction state for restart-safety.

        Called by main.py when a previously accepted signal FAILED to deliver
        via Telegram so the signal can be re-generated exactly once on the
        next scan (SQLite delivery state remains pending/failed until a
        retry succeeds, preventing duplicate sends).

        Only resets the state for the given symbol. Other symbols' cooldown,
        direction and dedup state are untouched (multi-symbol isolation).

        Args:
            symbol: Symbol whose cooldown/gate state is reset.
            signal_id: Optional failed signal id to drop from the in-memory
                dedup set so the retry regenerating the identical id is not
                suppressed. Other ids are preserved.
        """
        self._last_signal_time.pop(symbol, None)
        self._last_signal_direction.pop(symbol, None)
        self._last_trigger_ts.pop(symbol, None)
        # Signal ids are deterministic (symbol + trigger candle open time),
        # so the in-memory dedup set must NOT be polluted by a failed send:
        # the retry will regenerate the identical id. Drop only this id;
        # other symbols' (and this symbol's older) ids stay suppressed.
        if signal_id is not None:
            self._emitted_ids.discard(signal_id)
        else:
            # Backwards-compatible fallback: drop this symbol's ids only.
            self._emitted_ids = {
                sid for sid in self._emitted_ids
                if not sid.startswith(f"{symbol}|")
            }

    def _reject(self, symbol: str, decision_time: Optional[datetime],
                reason: str, now: datetime) -> ScanDecision:
        d = ScanDecision(
            symbol=symbol, decision_time=decision_time,
            bias=MarketBias.NEUTRAL, regime="UNKNOWN",
            setup_state="SKIPPED", trigger_state="SKIPPED",
            adx=None, rsi=None, atr=None, volume_ratio=None,
            score=0, decision="NO_SIGNAL", reason=reason, signal=None,
        )
        if self.config.dry_run:
            logger.info(f"[DRY-RUN] {d.log_line()}")
        else:
            logger.info(d.log_line())
        return d

    def _first_validation_error(
        self,
        hf: List[Candle], hf_tf: str,
        setup: List[Candle], setup_tf: str,
        trigger: List[Candle], trigger_tf: str,
        min_history: int, now: datetime, max_age: int,
    ) -> Optional[str]:
        """Return the first validation error code across all three series, or None."""
        for series, tf, mh in (
            (hf, hf_tf, min_history),
            (setup, setup_tf, min_history),
            (trigger, trigger_tf, min_history),
        ):
            try:
                data_validation.validate_series(
                    series, tf, mh, now=now, max_age_seconds=max_age,
                )
            except data_validation.DataValidationError as e:
                return str(e)
        return None

    @staticmethod
    def _bias(hf_ind: IndicatorValues, hf_candles: List[Candle]) -> MarketBias:
        return strategy.determine_hf_bias(hf_ind, hf_candles[-1].close)

    @staticmethod
    def _regime(setup_ind: IndicatorValues) -> str:
        from app.market_regime import detect_regime, MarketRegimeError
        if setup_ind.adx is None:
            return "UNKNOWN"
        try:
            cfg = get_config()
            return detect_regime(setup_ind.adx, cfg.adx_min).value
        except MarketRegimeError:
            return "UNKNOWN"

    def _setup_state(self, symbol, hf_ind, hf_candles, setup_ind, setup_candles) -> bool:
        bias = self._bias(hf_ind, hf_candles)
        if bias == MarketBias.NEUTRAL:
            return False
        prev_rsi = self._prev_rsi(setup_candles)
        if bias == MarketBias.BULLISH:
            valid, _ = strategy.check_long_setup(
                hf_ind, hf_candles[-1].close, setup_ind, prev_rsi,
                self.config.adx_min, self.config.rsi_midline)
        else:
            valid, _ = strategy.check_short_setup(
                hf_ind, hf_candles[-1].close, setup_ind, prev_rsi,
                self.config.adx_min, self.config.rsi_midline)
        return valid

    def _trigger_state(self, symbol, hf_ind, hf_candles, setup_ind, setup_candles, trigger_candles) -> bool:
        if not self._setup_state(symbol, hf_ind, hf_candles, setup_ind, setup_candles):
            return False
        bias = self._bias(hf_ind, hf_candles)
        if bias == MarketBias.BULLISH:
            return strategy.check_long_trigger(trigger_candles)
        return strategy.check_short_trigger(trigger_candles)

    def _calc_indicators(self, candles: List[Candle]) -> IndicatorValues:
        """Compute the full indicator bundle for a candle series."""
        closes = [c.close for c in candles]
        high_arr = [c.high for c in candles]
        low_arr = [c.low for c in candles]
        vols = [c.volume for c in candles]
        c = self.config

        ema_fast = indicators.ema(closes, c.ema_fast)
        ema_slow = indicators.ema(closes, c.ema_slow)
        rsi_val = indicators.rsi(closes, c.rsi_length)
        atr_val = indicators.atr(candles, c.atr_length)
        adx_val, plus_di, minus_di = indicators.adx(candles, c.adx_length)
        vol_sma = indicators.volume_sma(candles, c.volume_sma_length)

        def _last(arr, index=-1):
            try:
                if index < 0:
                    return arr[index]
                return arr[index]
            except (IndexError, TypeError):
                return None

        return IndicatorValues(
            ema_fast=_last(ema_fast),
            ema_slow=_last(ema_slow),
            rsi=_last(rsi_val),
            atr=_last(atr_val),
            adx=_last(adx_val),
            plus_di=_last(plus_di),
            minus_di=_last(minus_di),
            volume_sma=_last(vol_sma),
        )

    @staticmethod
    def _prev_rsi(setup_candles: List[Candle]) -> Optional[Decimal]:
        if len(setup_candles) < 2:
            return None
        vals = indicators.rsi([c.close for c in setup_candles], get_config().rsi_length)
        return vals[-2] if vals else None

    @staticmethod
    def _volume_ratio(candle: Candle, ind: IndicatorValues) -> Optional[float]:
        if ind.volume_sma is None or ind.volume_sma == 0:
            return None
        return float(candle.volume / ind.volume_sma)

    def _classify_rejection(
        self, symbol, hf_ind, hf_candles, setup_ind, setup_candles,
        trigger_ind, trigger_candles,
    ) -> str:
        """
        Primary rejection reason for observability when no signal produced.
        Walks the same gate order as the engine and returns the FIRST failing reason.
        """
        bias = self._bias(hf_ind, hf_candles)
        if bias == MarketBias.NEUTRAL:
            return REASON_HTF_NEUTRAL

        # ADX gate
        if setup_ind.adx is None or setup_ind.adx < self.config.adx_min:
            return REASON_ADX_TOO_LOW

        prev_rsi = self._prev_rsi(setup_candles)
        # DI direction
        if bias == MarketBias.BULLISH:
            if setup_ind.ema_fast is None or setup_ind.ema_slow is None or setup_ind.ema_fast <= setup_ind.ema_slow:
                return REASON_SETUP_INVALID
            if setup_ind.plus_di is None or setup_ind.minus_di is None or setup_ind.plus_di <= setup_ind.minus_di:
                return REASON_WRONG_DI_DIRECTION
            if not strategy.check_rsi_recovery(prev_rsi, setup_ind.rsi, self.config.rsi_midline):
                return REASON_RSI_NOT_RECOVERED
        else:
            if setup_ind.ema_fast is None or setup_ind.ema_slow is None or setup_ind.ema_fast >= setup_ind.ema_slow:
                return REASON_SETUP_INVALID
            if setup_ind.plus_di is None or setup_ind.minus_di is None or setup_ind.minus_di <= setup_ind.plus_di:
                return REASON_WRONG_DI_DIRECTION
            if not strategy.check_rsi_breakdown(prev_rsi, setup_ind.rsi, self.config.rsi_midline):
                return REASON_RSI_NOT_RECOVERED

        # Trigger
        if bias == MarketBias.BULLISH:
            if not strategy.check_long_trigger(trigger_candles):
                return REASON_TRIGGER_NOT_CONFIRMED
        else:
            if not strategy.check_short_trigger(trigger_candles):
                return REASON_TRIGGER_NOT_CONFIRMED

        # Overextension
        if setup_ind.ema_fast is not None and setup_ind.atr is not None:
            if not strategy.check_overextension(
                trigger_candles[-1].close, setup_ind.ema_fast,
                setup_ind.atr, self.config.max_distance_from_ema_atr,
            ):
                return REASON_OVEREXTENDED

        # Volume
        ratio = self._volume_ratio(trigger_candles[-1], trigger_ind)
        if ratio is not None and ratio < self.config.volume_multiplier:
            return REASON_LOW_VOLUME

        # Default: the engine rejected on score or RR (or passed gates but low score)
        return REASON_SCORE_BELOW_THRESHOLD
