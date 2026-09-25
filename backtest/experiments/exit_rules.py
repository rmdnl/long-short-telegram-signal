"""
PHASE 7: Experimental EXIT-MANAGEMENT rules.

Each rule captures how the stop / trailing / partial / time logic behaves
AFTER the position has filled. Rules are pure functions of the candles seen so
far — they NEVER look ahead. A decision at candle index `i` may only use
candles [0..i] of the post-entry series.

The entry fill model is intentionally identical to backtest.execution.TradeSimulator
(so every variant is on the same footing), but the exit loop is replaced by the
selected rule. The original V1 baseline rule (V1_BASELINE) reproduces
TradeSimulator exactly, and is asserted equal in the test suite.
"""
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Post-entry state
# ---------------------------------------------------------------------------

@dataclass
class ExitDecision:
    """Outcome of evaluating one post-entry candle."""
    exit_now: bool
    status: str = "OPEN"            # OPEN / STOPPED / TP1_HIT / TP2_HIT / BE_HIT / TRAIL_HIT / TIME_EXIT
    price: object = None
    take_partial: bool = False
    partial_fraction: object = None
    note: str = ""


# ---------------------------------------------------------------------------
# Variant registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Variant:
    label: str
    kind: str                      # "V1" | "BE" | "ATR_TRAIL" | "PARTIAL_BE" | "TIME_EXIT"
    atr_multiple: Optional[float] = None   # ATR-trailing distance (ATR units)
    time_exit_candles: int = 0     # trigger candles before +0.5R, else time exit
    partial_fraction: Optional[float] = None  # fraction protected at breakeven


# V1_BASELINE is the untouched production exit model (SL / TP1 / TP2 / expire).
V1_BASELINE = Variant(label="V1_BASELINE", kind="V1")
H1_BE_0_5R = Variant(label="H1_BE_0_5R", kind="BE")
H1_ATR_TRAIL = Variant(label="H1_ATR_TRAIL", kind="ATR_TRAIL", atr_multiple=1.0)
H1_PARTIAL_BE = Variant(label="H1_PARTIAL_BE", kind="PARTIAL_BE", partial_fraction=0.5)
H1_TIME_EXIT_12 = Variant(label="H1_TIME_EXIT_12", kind="TIME_EXIT", time_exit_candles=12)

VARIANTS = [V1_BASELINE, H1_BE_0_5R, H1_ATR_TRAIL, H1_PARTIAL_BE, H1_TIME_EXIT_12]


def make_variant(label: str) -> Variant:
    for v in VARIANTS:
        if v.label == label:
            return v
    raise KeyError(f"unknown experiment variant: {label}")
