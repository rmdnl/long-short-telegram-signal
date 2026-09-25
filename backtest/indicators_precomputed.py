"""
PHASE 5: Precomputed indicator arrays for O(1)-per-window indicator access.

EMA, RSI, ATR, ADX(+DI/-DI), and volume-SMA are all causal: the value at
index i depends only on data up to and including index i. This means the
indicator value computed over the full series is identical to the value
computed over any prefix ending at i — so precomputing once over the full
series is exactly equivalent to computing at each decision point.

This eliminates the O(n²) bottleneck in BacktestEngine.run_symbol where
compute_indicators was being re-run 30k+ times per symbol, each over the
entire growing history.
"""
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List, Optional

from app import indicators
from app.models import Candle, IndicatorValues


@dataclass
class _TimeframeSeries:
    """Indicator arrays for one timeframe, indexed by candle position."""
    ema_fast:  List[Optional[Decimal]]
    ema_slow:  List[Optional[Decimal]]
    rsi:       List[Optional[Decimal]]
    atr:       List[Optional[Decimal]]
    adx:       List[Optional[Decimal]]
    plus_di:   List[Optional[Decimal]]
    minus_di:  List[Optional[Decimal]]
    volume_sma: List[Optional[Decimal]]


class PrecomputedIndicators:
    """
    Precompute indicator arrays for all three timeframes of one symbol.

    After precompute(), build_ind(hf_i, setup_i, trigger_i) returns the
    IndicatorValues that would have been produced by compute_indicators()
    on the corresponding candle prefix — but in O(1) time.
    """

    def __init__(self, config=None):
        if config is None:
            from app.config import get_config
            config = get_config()
        self._cfg = config

    def precompute(
        self,
        hf_candles:  List[Candle],
        setup_candles: List[Candle],
        trigger_candles: List[Candle],
    ) -> None:
        """Compute all indicator arrays once. Call before build_ind()."""
        c = self._cfg

        def _prep(candles: List[Candle]):
            closes = [x.close for x in candles]
            return (
                closes,
                indicators.ema(closes, c.ema_fast),
                indicators.ema(closes, c.ema_slow),
                indicators.rsi(closes, c.rsi_length),
                indicators.atr(candles, c.atr_length),
                indicators.adx(candles, c.adx_length),
                indicators.volume_sma(candles, c.volume_sma_length),
            )

        def _bundle(arr):
            closes, ef, es, rsi_v, atr_v, (adx_v, p_di, m_di), vol_sma = arr
            n = len(closes)
            return _TimeframeSeries(
                ema_fast=_pad(ef, n),
                ema_slow=_pad(es, n),
                rsi=_pad(rsi_v, n),
                atr=_pad(atr_v, n),
                adx=_pad(adx_v, n),
                plus_di=_pad(p_di, n),
                minus_di=_pad(m_di, n),
                volume_sma=_pad(vol_sma, n),
            )

        self._hf    = _bundle(_prep(hf_candles))
        self._setup = _bundle(_prep(setup_candles))
        self._trig  = _bundle(_prep(trigger_candles))

    def build_ind(
        self,
        hf_index:    int,
        setup_index: int,
        trigger_index: int,
    ) -> tuple[IndicatorValues, IndicatorValues, IndicatorValues]:
        """
        Return (hf_ind, setup_ind, trigger_ind) as IndicatorValues built
        from the precomputed arrays at the given candle indices.
        """
        return (
            _pick(self._hf,    hf_index),
            _pick(self._setup, setup_index),
            _pick(self._trig,  trigger_index),
        )

    def ready_at(self, setup_index: int, trigger_index: int) -> bool:
        """True when both setup and trigger indicators are fully warm at these indices."""
        s = self._setup
        t = self._trig
        return (
            s.ema_fast[setup_index]    is not None
            and s.ema_slow[setup_index]   is not None
            and s.rsi[setup_index]        is not None
            and s.atr[setup_index]        is not None
            and s.adx[setup_index]        is not None
            and s.plus_di[setup_index]    is not None
            and s.minus_di[setup_index]   is not None
            and t.ema_fast[trigger_index]  is not None
            and t.ema_slow[trigger_index]  is not None
            and t.rsi[trigger_index]       is not None
            and t.atr[trigger_index]       is not None
            and t.adx[trigger_index]       is not None
            and t.plus_di[trigger_index]   is not None
            and t.minus_di[trigger_index]  is not None
        )


def _pad(arr: list, n: int) -> List[Optional[Decimal]]:
    """Pad array to length n with None at the front (indicator warmup)."""
    if len(arr) >= n:
        return arr
    return [None] * (n - len(arr)) + list(arr)


def _pick(series: _TimeframeSeries, idx: int) -> IndicatorValues:
    """Build an IndicatorValues from a series at the given index."""
    if idx < 0 or idx >= len(series.ema_fast):
        return IndicatorValues(
            ema_fast=None, ema_slow=None, rsi=None, atr=None,
            adx=None, plus_di=None, minus_di=None, volume_sma=None,
        )
    return IndicatorValues(
        ema_fast=series.ema_fast[idx],
        ema_slow=series.ema_slow[idx],
        rsi=series.rsi[idx],
        atr=series.atr[idx],
        adx=series.adx[idx],
        plus_di=series.plus_di[idx],
        minus_di=series.minus_di[idx],
        volume_sma=series.volume_sma[idx],
    )
