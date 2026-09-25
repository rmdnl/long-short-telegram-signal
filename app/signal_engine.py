"""
Signal engine: orchestrates all logic to produce signals.

Entry point for generating signals from multi-timeframe data.
"""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, List

from app.models import (
    Candle,
    Signal,
    SignalDirection,
    SignalType,
    MarketBias,
    IndicatorValues,
    ScoreComponents,
)
from app.config import get_config
from app import strategy, market_regime, risk_engine, signal_filter
from app.logger import get_logger

logger = get_logger(__name__)


class SignalEngineError(Exception):
    """Raised when signal generation fails"""
    pass


class SignalEngine:
    """
    Core signal generation engine.

    Combines:
    - 1H market bias
    - 15M setup conditions
    - 5M trigger confirmation
    - Score calculation
    - Risk/Reward calculation
    - Filters (overextension, volume, etc)
    """

    def __init__(self, _rsi_override: Optional[List] = None):
        self.config = get_config()
        # Optional pre-computed RSI value list. When set, generate_signal()
        # reads prev_rsi from this list instead of calling calc_rsi() on the
        # candle closes — enables O(1) backtest precomputed fast path.
        # Format: list of Optional[Decimal], one entry per setup candle.
        # If index out of range or value is None, prev_rsi stays None.
        self._rsi_override = _rsi_override

    def generate_signal(
        self,
        symbol: str,
        hf_candles: List[Candle],
        hf_ind: IndicatorValues,
        setup_candles: List[Candle],
        setup_ind: IndicatorValues,
        trigger_candles: List[Candle],
        trigger_ind: IndicatorValues,
    ) -> Optional[Signal]:
        """
        Generate signal if all conditions met.

        Args:
            symbol: Trading pair
            hf_candles: 1H candles
            hf_ind: 1H indicator values
            setup_candles: 15M candles
            setup_ind: 15M indicator values
            trigger_candles: 5M candles
            trigger_ind: 5M indicator values

        Returns:
            Signal object or None
        """
        # Step 1: HTF Bias
        hf_close = hf_candles[-1].close
        bias = strategy.determine_hf_bias(hf_ind, hf_close)

        if bias == MarketBias.NEUTRAL:
            logger.debug(f"{symbol}: HTF bias NEUTRAL, no signal")
            return None

        # Step 2: Market regime on 15M
        regime = market_regime.detect_regime(setup_ind.adx, self.config.adx_min)

        # Step 3: Check setup on 15M
        # Extract previous RSI from setup candles (second-to-last indicator)
        # In production, this would come from indicator calculation
        prev_rsi = None
        if self._rsi_override is not None:
            # Backtest precomputed path: _rsi_override is a 2-element list
            # [rsi_full_array, setup_count] updated in-place each window.
            # prev_rsi = rsi_full_array[setup_count - 2] in O(1).
            rsi_arr = self._rsi_override[0]
            setup_count = self._rsi_override[1]
            if setup_count >= 2:
                prev_rsi = rsi_arr[setup_count - 2]
        elif len(setup_candles) >= 2:
            # Calculate RSI for previous candle using full series
            from app.indicators import rsi as calc_rsi
            closes = [c.close for c in setup_candles]
            rsi_vals = calc_rsi(closes, self.config.rsi_length)
            if len(rsi_vals) >= 2:
                prev_rsi = rsi_vals[-2]

        if bias == MarketBias.BULLISH:
            setup_valid, reason = strategy.check_long_setup(
                hf_ind,
                hf_close,
                setup_ind,
                prev_rsi,
                self.config.adx_min,
                self.config.rsi_midline,
            )
            direction = SignalDirection.LONG if setup_valid else None
        else:
            setup_valid, reason = strategy.check_short_setup(
                hf_ind,
                hf_close,
                setup_ind,
                prev_rsi,
                self.config.adx_min,
                self.config.rsi_midline,
            )
            direction = SignalDirection.SHORT if setup_valid else None

        if not setup_valid:
            logger.debug(f"{symbol}: Setup invalid on 15M")
            return None

        # Step 4: Trigger on 5M
        if direction == SignalDirection.LONG:
            trigger_valid = strategy.check_long_trigger(trigger_candles)
        else:
            trigger_valid = strategy.check_short_trigger(trigger_candles)

        if not trigger_valid:
            logger.debug(f"{symbol}: Trigger invalid on 5M")
            return None

        # Step 5: Overextension filter
        if not strategy.check_overextension(
            trigger_candles[-1].close,
            setup_ind.ema_fast,
            setup_ind.atr,
            self.config.max_distance_from_ema_atr,
        ):
            logger.debug(f"{symbol}: Price overextended from EMA50")
            return None

        # Step 6: Volume filter
        if not self._check_volume(trigger_candles[-1], trigger_ind):
            logger.debug(f"{symbol}: Volume too low")
            return None

        # Step 7: Risk/Reward calculation
        trigger_candle = trigger_candles[-1]
        entry_low, entry_high = risk_engine.calculate_entry_zone(
            trigger_candle,
            setup_ind.atr,
            direction,
        )
        entry_mid = (entry_low + entry_high) / 2

        stop_loss = risk_engine.calculate_stop_loss(
            direction,
            entry_mid,
            setup_ind.atr,
            self.config.sl_atr_multiplier,
        )

        tp1, tp2 = risk_engine.calculate_take_profit(
            direction,
            entry_mid,
            stop_loss,
            self.config.tp1_rr,
            self.config.tp2_rr,
        )

        # Step 8: Score calculation
        score_components = self._calculate_score(
            bias,
            setup_ind,
            trigger_ind,
            trigger_valid,
            entry_mid,
            stop_loss,
            tp1,
            tp2,
        )

        if score_components.total < self.config.min_score:
            logger.debug(f"{symbol}: Score {score_components.total} < {self.config.min_score}")
            return None

        # Step 9: Create signal
        signal_type = strategy.classify_signal_type(direction, trigger_valid)

        signal_id = signal_filter.generate_signal_id(
            symbol,
            "5m",
            trigger_candle.timestamp,
            direction,
        )

        now = datetime.now(timezone.utc)

        signal = Signal(
            signal_id=signal_id,
            symbol=symbol,
            direction=direction,
            signal_type=signal_type,
            created_at=now,
            trigger_candle_time=trigger_candle.timestamp,
            entry_low=entry_low,
            entry_high=entry_high,
            stop_loss=stop_loss,
            tp1=tp1,
            tp2=tp2,
            score=score_components.total,
            htf_bias=bias,
            adx_value=setup_ind.adx,
            rsi_value=setup_ind.rsi,
            volume_ratio=trigger_candles[-1].volume / trigger_ind.volume_sma if trigger_ind.volume_sma else Decimal('1'),
        )

        logger.info(f"{symbol}: Signal generated {direction.value} score={score_components.total}")
        return signal

    def _check_volume(self, candle: Candle, ind: IndicatorValues) -> bool:
        """Volume confirmation filter"""
        if ind.volume_sma is None or ind.volume_sma == 0:
            return True  # No volume data, skip filter

        ratio = candle.volume / ind.volume_sma
        return ratio >= self.config.volume_multiplier

    def _calculate_score(
        self,
        bias: MarketBias,
        setup_ind: IndicatorValues,
        trigger_ind: IndicatorValues,
        trigger_valid: bool,
        entry_mid: Decimal,
        stop_loss: Decimal,
        tp1: Decimal,
        tp2: Decimal,
    ) -> ScoreComponents:
        """
        Calculate signal quality score 0-100.

        Breakdown:
        - HTF bias: 20
        - EMA trend: 15
        - ADX strength: 15
        - DI direction: 10
        - RSI momentum: 15
        - 5M trigger: 10
        - Volume: 10
        - Risk/Reward: 5
        """
        score = ScoreComponents()

        # HTF bias
        if bias != MarketBias.NEUTRAL:
            score.htf_bias = 20

        # EMA trend on 15M
        if setup_ind.ema_fast and setup_ind.ema_slow:
            if (bias == MarketBias.BULLISH and setup_ind.ema_fast > setup_ind.ema_slow) or \
               (bias == MarketBias.BEARISH and setup_ind.ema_fast < setup_ind.ema_slow):
                score.ema_trend = 15

        # ADX strength
        if setup_ind.adx:
            if setup_ind.adx >= self.config.adx_min + Decimal('10'):
                score.adx_strength = 15
            elif setup_ind.adx >= self.config.adx_min:
                score.adx_strength = 10

        # DI direction
        if setup_ind.plus_di and setup_ind.minus_di:
            if (bias == MarketBias.BULLISH and setup_ind.plus_di > setup_ind.minus_di) or \
               (bias == MarketBias.BEARISH and setup_ind.minus_di > setup_ind.plus_di):
                score.di_direction = 10

        # RSI momentum
        if setup_ind.rsi:
            if bias == MarketBias.BULLISH and setup_ind.rsi > self.config.rsi_midline:
                score.rsi_momentum = 15
            elif bias == MarketBias.BEARISH and setup_ind.rsi < self.config.rsi_midline:
                score.rsi_momentum = 15

        # 5M trigger
        if trigger_valid:
            score.trigger_5m = 10

        # Volume (already checked in filter)
        score.volume = 10

        # Risk/Reward
        risk = abs(entry_mid - stop_loss)
        reward_tp2 = abs(tp2 - entry_mid)
        if risk > 0:
            rr = reward_tp2 / risk
            if rr >= self.config.tp2_rr:
                score.risk_reward = 5

        return score
