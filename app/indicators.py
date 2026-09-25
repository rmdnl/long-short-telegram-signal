from decimal import Decimal
from typing import List, Optional

import pandas as pd
import numpy as np

from app.models import Candle


class IndicatorError(Exception):
    """Raised when indicator calculation fails"""
    pass


def ema(data: List[Decimal], period: int) -> List[Optional[Decimal]]:
    """
    Calculate Exponential Moving Average
    
    Args:
        data: Price data (typically close prices)
        period: EMA period
    
    Returns:
        List of EMA values (None for insufficient data)
    """
    if period <= 0:
        raise IndicatorError("EMA period must be > 0")
    
    if len(data) < period:
        return [None] * len(data)
    
    # Convert to float for pandas
    df = pd.Series([float(d) for d in data])
    ema_series = df.ewm(span=period, adjust=False).mean()
    
    # Convert back to Decimal
    result = []
    for i, val in enumerate(ema_series):
        if i < period - 1:
            result.append(None)
        else:
            result.append(Decimal(str(val)))
    
    return result


def rsi(data: List[Decimal], period: int = 14) -> List[Optional[Decimal]]:
    """
    Calculate Relative Strength Index
    
    Args:
        data: Price data (typically close prices)
        period: RSI period
    
    Returns:
        List of RSI values (None for insufficient data)
    """
    if period <= 0:
        raise IndicatorError("RSI period must be > 0")
    
    if len(data) < period + 1:
        return [None] * len(data)
    
    # Calculate price changes
    deltas = []
    for i in range(1, len(data)):
        deltas.append(float(data[i] - data[i-1]))
    
    # Separate gains and losses
    gains = [max(d, 0) for d in deltas]
    losses = [abs(min(d, 0)) for d in deltas]
    
    # Calculate average gains and losses
    avg_gains = pd.Series(gains).ewm(span=period, adjust=False).mean()
    avg_losses = pd.Series(losses).ewm(span=period, adjust=False).mean()
    
    # Calculate RS and RSI
    result = [None]  # First value has no delta
    
    for i in range(len(avg_gains)):
        if i < period - 1:
            result.append(None)
        else:
            avg_loss = avg_losses.iloc[i]
            if avg_loss == 0:
                rsi_val = Decimal('100')
            else:
                rs = avg_gains.iloc[i] / avg_loss
                rsi_val = Decimal('100') - (Decimal('100') / (Decimal('1') + Decimal(str(rs))))
            result.append(rsi_val)
    
    return result


def atr(candles: List[Candle], period: int = 14) -> List[Optional[Decimal]]:
    """
    Calculate Average True Range
    
    Args:
        candles: List of Candle objects
        period: ATR period
    
    Returns:
        List of ATR values (None for insufficient data)
    """
    if period <= 0:
        raise IndicatorError("ATR period must be > 0")
    
    if len(candles) < period + 1:
        return [None] * len(candles)
    
    # Calculate True Range
    true_ranges = []
    
    for i in range(len(candles)):
        if i == 0:
            tr = candles[i].high - candles[i].low
        else:
            high_low = candles[i].high - candles[i].low
            high_close = abs(candles[i].high - candles[i-1].close)
            low_close = abs(candles[i].low - candles[i-1].close)
            tr = max(high_low, high_close, low_close)
        
        true_ranges.append(float(tr))
    
    # Calculate ATR using EMA
    tr_series = pd.Series(true_ranges)
    atr_series = tr_series.ewm(span=period, adjust=False).mean()
    
    # Convert to Decimal
    result = []
    for i, val in enumerate(atr_series):
        if i < period:
            result.append(None)
        else:
            result.append(Decimal(str(val)))
    
    return result


def adx(candles: List[Candle], period: int = 14) -> tuple[List[Optional[Decimal]], List[Optional[Decimal]], List[Optional[Decimal]]]:
    """
    Calculate Average Directional Index (ADX) with +DI and -DI
    
    Args:
        candles: List of Candle objects
        period: ADX period
    
    Returns:
        Tuple of (ADX values, +DI values, -DI values)
    """
    if period <= 0:
        raise IndicatorError("ADX period must be > 0")
    
    if len(candles) < period * 2:
        return ([None] * len(candles), [None] * len(candles), [None] * len(candles))
    
    # Calculate directional movement
    plus_dm = []
    minus_dm = []
    true_ranges = []
    
    for i in range(len(candles)):
        if i == 0:
            plus_dm.append(0)
            minus_dm.append(0)
            tr = float(candles[i].high - candles[i].low)
        else:
            high_diff = float(candles[i].high - candles[i-1].high)
            low_diff = float(candles[i-1].low - candles[i].low)
            
            plus_dm_val = high_diff if high_diff > low_diff and high_diff > 0 else 0
            minus_dm_val = low_diff if low_diff > high_diff and low_diff > 0 else 0
            
            plus_dm.append(plus_dm_val)
            minus_dm.append(minus_dm_val)
            
            high_low = float(candles[i].high - candles[i].low)
            high_close = abs(float(candles[i].high - candles[i-1].close))
            low_close = abs(float(candles[i].low - candles[i-1].close))
            tr = max(high_low, high_close, low_close)
        
        true_ranges.append(tr)
    
    # Smooth using EMA
    smoothed_plus_dm = pd.Series(plus_dm).ewm(span=period, adjust=False).mean()
    smoothed_minus_dm = pd.Series(minus_dm).ewm(span=period, adjust=False).mean()
    smoothed_tr = pd.Series(true_ranges).ewm(span=period, adjust=False).mean()
    
    # Calculate +DI and -DI
    plus_di_vals = []
    minus_di_vals = []
    dx_vals = []
    
    for i in range(len(candles)):
        if i < period:
            plus_di_vals.append(None)
            minus_di_vals.append(None)
            dx_vals.append(None)
        else:
            tr_val = smoothed_tr.iloc[i]
            if tr_val == 0:
                plus_di_vals.append(Decimal('0'))
                minus_di_vals.append(Decimal('0'))
                dx_vals.append(None)
            else:
                plus_di = Decimal('100') * Decimal(str(smoothed_plus_dm.iloc[i])) / Decimal(str(tr_val))
                minus_di = Decimal('100') * Decimal(str(smoothed_minus_dm.iloc[i])) / Decimal(str(tr_val))
                
                plus_di_vals.append(plus_di)
                minus_di_vals.append(minus_di)
                
                # Calculate DX
                di_sum = plus_di + minus_di
                if di_sum == 0:
                    dx_vals.append(None)
                else:
                    dx = Decimal('100') * abs(plus_di - minus_di) / di_sum
                    dx_vals.append(float(dx))
    
    # Calculate ADX (smoothed DX)
    dx_series = pd.Series([float(d) if d is not None else np.nan for d in dx_vals])
    adx_series = dx_series.ewm(span=period, adjust=False).mean()
    
    adx_vals = []
    for i in range(len(candles)):
        if i < period * 2 - 1:
            adx_vals.append(None)
        else:
            if np.isnan(adx_series.iloc[i]):
                adx_vals.append(None)
            else:
                adx_vals.append(Decimal(str(adx_series.iloc[i])))
    
    return adx_vals, plus_di_vals, minus_di_vals


def volume_sma(candles: List[Candle], period: int = 20) -> List[Optional[Decimal]]:
    """
    Calculate Simple Moving Average of volume
    
    Args:
        candles: List of Candle objects
        period: SMA period
    
    Returns:
        List of Volume SMA values (None for insufficient data)
    """
    if period <= 0:
        raise IndicatorError("Volume SMA period must be > 0")
    
    if len(candles) < period:
        return [None] * len(candles)
    
    volumes = [float(c.volume) for c in candles]
    vol_series = pd.Series(volumes)
    sma_series = vol_series.rolling(window=period).mean()
    
    result = []
    for i, val in enumerate(sma_series):
        if i < period - 1:
            result.append(None)
        else:
            result.append(Decimal(str(val)))
    
    return result
