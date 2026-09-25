from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional


class SignalDirection(Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class SignalStatus(Enum):
    ACTIVE = "ACTIVE"
    INVALIDATED = "INVALIDATED"
    TP1_HIT = "TP1_HIT"
    TP2_HIT = "TP2_HIT"
    STOPPED = "STOPPED"
    EXPIRED = "EXPIRED"


class SignalType(Enum):
    TREND_PULLBACK = "TREND_PULLBACK"
    TREND_BREAKOUT = "TREND_BREAKOUT"
    MOMENTUM_CONTINUATION = "MOMENTUM_CONTINUATION"


class MarketBias(Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class MarketRegime(Enum):
    RANGING = "RANGING"
    WEAK_TREND = "WEAK_TREND"
    TRENDING = "TRENDING"


@dataclass
class Candle:
    """OHLCV candle with timezone-aware timestamp"""
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    
    def __post_init__(self):
        if self.timestamp.tzinfo is None:
            raise ValueError("Candle timestamp must be timezone-aware")


@dataclass
class IndicatorValues:
    """All indicator values for a timeframe"""
    ema_fast: Optional[Decimal] = None
    ema_slow: Optional[Decimal] = None
    rsi: Optional[Decimal] = None
    atr: Optional[Decimal] = None
    adx: Optional[Decimal] = None
    plus_di: Optional[Decimal] = None
    minus_di: Optional[Decimal] = None
    volume_sma: Optional[Decimal] = None


@dataclass
class Signal:
    """Trading signal with all metadata"""
    signal_id: str
    symbol: str
    direction: SignalDirection
    signal_type: SignalType
    
    created_at: datetime
    trigger_candle_time: datetime
    
    entry_low: Decimal
    entry_high: Decimal
    stop_loss: Decimal
    tp1: Decimal
    tp2: Decimal
    
    score: int
    
    htf_bias: MarketBias
    adx_value: Decimal
    rsi_value: Decimal
    volume_ratio: Decimal
    
    status: SignalStatus = SignalStatus.ACTIVE
    
    tp1_hit_time: Optional[datetime] = None
    tp2_hit_time: Optional[datetime] = None
    sl_hit_time: Optional[datetime] = None
    expiration_time: Optional[datetime] = None
    
    def __post_init__(self):
        if self.created_at.tzinfo is None:
            raise ValueError("Signal created_at must be timezone-aware")
        if self.trigger_candle_time.tzinfo is None:
            raise ValueError("Signal trigger_candle_time must be timezone-aware")
    
    @property
    def risk(self) -> Decimal:
        """Calculate risk (distance from entry to stop loss)"""
        entry_mid = (self.entry_low + self.entry_high) / 2
        return abs(entry_mid - self.stop_loss)
    
    @property
    def reward_tp1(self) -> Decimal:
        """Calculate reward to TP1"""
        entry_mid = (self.entry_low + self.entry_high) / 2
        return abs(self.tp1 - entry_mid)
    
    @property
    def reward_tp2(self) -> Decimal:
        """Calculate reward to TP2"""
        entry_mid = (self.entry_low + self.entry_high) / 2
        return abs(self.tp2 - entry_mid)
    
    @property
    def rr_tp1(self) -> Decimal:
        """Risk/Reward ratio for TP1"""
        risk = self.risk
        if risk == 0:
            return Decimal('0')
        return self.reward_tp1 / risk
    
    @property
    def rr_tp2(self) -> Decimal:
        """Risk/Reward ratio for TP2"""
        risk = self.risk
        if risk == 0:
            return Decimal('0')
        return self.reward_tp2 / risk


@dataclass
class ScoreComponents:
    """Breakdown of signal score"""
    htf_bias: int = 0
    ema_trend: int = 0
    adx_strength: int = 0
    di_direction: int = 0
    rsi_momentum: int = 0
    trigger_5m: int = 0
    volume: int = 0
    risk_reward: int = 0
    
    @property
    def total(self) -> int:
        return (
            self.htf_bias +
            self.ema_trend +
            self.adx_strength +
            self.di_direction +
            self.rsi_momentum +
            self.trigger_5m +
            self.volume +
            self.risk_reward
        )
