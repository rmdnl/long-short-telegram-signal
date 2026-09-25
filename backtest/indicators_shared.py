"""
PHASE 4: Shared indicator bundle for backtest.

Mirrors Scanner._calc_indicators EXACTLY (same indicators.* calls, same
config periods, same "last value" selection) so the backtest computes the
identical indicator values the live scanner would at the same candle.

This is not a second strategy — it is the same indicator math the scanner
already uses, factored out so both live and backtest share it.
"""
from decimal import Decimal
from typing import List, Optional

from app import indicators
from app.models import Candle, IndicatorValues
from app.config import get_config


def compute_indicators(candles: List[Candle], config=None) -> IndicatorValues:
    """Compute the indicator bundle for a closed candle series.

    Identical logic to app.scanner.Scanner._calc_indicators:
    - EMA fast/slow, RSI, ATR, ADX(+DI/-DI), Volume-SMA
    - Each returned as its LAST value (None when warmup incomplete).
    """
    if config is None:
        config = get_config()
    c = config

    closes = [x.close for x in candles]

    ema_fast = indicators.ema(closes, c.ema_fast)
    ema_slow = indicators.ema(closes, c.ema_slow)
    rsi_val = indicators.rsi(closes, c.rsi_length)
    atr_val = indicators.atr(candles, c.atr_length)
    adx_val, plus_di, minus_di = indicators.adx(candles, c.adx_length)
    vol_sma = indicators.volume_sma(candles, c.volume_sma_length)

    def _last(arr):
        try:
            if not arr:
                return None
            return arr[-1]
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


def indicators_ready(ind: IndicatorValues) -> bool:
    """True when every indicator required by the strategy is available.

    The engine gates on these; if any is None the window is treated as
    still warming up and no signal is emitted (no warmup-period signals).
    """
    return all(v is not None for v in (
        ind.ema_fast, ind.ema_slow, ind.rsi, ind.atr, ind.adx,
        ind.plus_di, ind.minus_di,
    ))
