"""
PHASE 4: No-look-ahead multi-timeframe alignment.

For each 5M trigger candle C (decision timestamp T = C.close_time):
- 1H / 15M / 5M are sliced to closed candles ONLY (close_time <= T).
- The currently-forming higher-timeframe candle is NEVER included.
- If the required higher-timeframe context is unavailable by T, the window
  is rejected with MISSING_HTF_DATA (no blind forward-fill).

This module reuses data_validation.cutoff_candles so the slicing rule is
identical to live mode.
"""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional

from app import data_validation as dv
from app.models import Candle

TRIGGER_TF = "5m"
SETUP_TF = "15m"
HF_TF = "1h"

# Minimum closed candles of a context timeframe required to "have" that
# timeframe at a decision point. Below this the context is treated as
# missing (MISSING_HTF_DATA) rather than forward-filled.
MIN_CONTEXT_CANDLES = 2


@dataclass
class AlignedWindow:
    """Closed-only candles for all three timeframes at one decision point."""
    symbol: str
    decision_time: datetime            # T = trigger candle close time
    trigger_candle: Candle             # the 5M candle under evaluation
    hf: List[Candle]                   # closed 1H with close_time <= T
    setup: List[Candle]                # closed 15M with close_time <= T
    trigger: List[Candle]              # closed 5M with close_time <= T
    ok: bool = True
    reason: str = ""                   # "" when ok else MISSING_HTF_DATA


@dataclass
class WindowReport:
    """Aggregate alignment results across one backtest run."""
    total_windows: int = 0
    missing_hf: int = 0
    ok_windows: int = 0

    def summary(self) -> str:
        return (f"windows={self.total_windows} ok={self.ok_windows} "
                f"missing_hf={self.missing_hf}")


def _slice(candles: List[Candle], tf: str, decision_time: datetime) -> List[Candle]:
    """Closed-only slice of a timeframe up to and including decision_time."""
    return dv.cutoff_candles(candles, tf, decision_time)


def align_window(
    symbol: str,
    hf: List[Candle],
    setup: List[Candle],
    trigger: List[Candle],
    decision_time: datetime,
) -> AlignedWindow:
    """
    Build a closed-only multi-timeframe window at a fixed decision timestamp T.

    No-look-ahead guarantee: every returned candle satisfies
    `candle_close_time(c, tf) <= T`. The forming 1H/15M candle that opens
    before T but closes after T is excluded.

    Returns an AlignedWindow with `ok=False, reason='MISSING_HTF_DATA'` when
    a context timeframe has fewer than MIN_CONTEXT_CANDLES closed candles by T.
    """
    hf_c = _slice(hf, HF_TF, decision_time)
    setup_c = _slice(setup, SETUP_TF, decision_time)
    trigger_c = _slice(trigger, TRIGGER_TF, decision_time)

    # The trigger candle under evaluation must be the most recent closed 5M
    # at T (i.e. its close time == T).
    if not trigger_c:
        return AlignedWindow(
            symbol=symbol, decision_time=decision_time, trigger_candle=None,
            hf=hf_c, setup=setup_c, trigger=trigger_c,
            ok=False, reason="MISSING_HTF_DATA",
        )

    trigger_candle = trigger_c[-1]
    reason = ""

    # Higher-timeframe context availability (no forward-fill).
    if len(hf_c) < MIN_CONTEXT_CANDLES:
        reason = "MISSING_HTF_DATA"
    elif len(setup_c) < MIN_CONTEXT_CANDLES:
        reason = "MISSING_HTF_DATA"

    ok = reason == ""
    return AlignedWindow(
        symbol=symbol,
        decision_time=decision_time,
        trigger_candle=trigger_candle,
        hf=hf_c,
        setup=setup_c,
        trigger=trigger_c,
        ok=ok,
        reason=reason,
    )


def iter_trigger_decision_points(
    trigger: List[Candle],
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> List[Candle]:
    """
    Yields each closed 5M trigger candle that is a valid decision point.

    A candle C is a decision point when its close_time is within [start, end]
    (bounds optional). Only candles that are fully closed are considered.
    """
    out: List[Candle] = []
    for c in trigger:
        close_t = dv.candle_close_time(c, TRIGGER_TF)
        if start is not None and close_t < start:
            continue
        if end is not None and close_t > end:
            continue
        out.append(c)
    return out
