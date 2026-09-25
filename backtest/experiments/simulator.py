"""
PHASE 7: Isolated experimental trade simulator.

Reuses the SAME entry-fill model, conservative same-candle ordering, and
R-multiple conventions as backtest.execution.TradeSimulator, but replaces the
post-entry exit loop with a pluggable ExitRule so the four H1 experiments
(and the V1 baseline control) run on identical signals / candles.

No-look-ahead: the exit loop only walks `future_trigger` forward and never
reads MFE/MAE or any candle beyond the current index. The baseline rule
(V1) is byte-identical to TradeSimulator's exit path, asserted in tests.
"""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from app.models import Candle, Signal, SignalDirection

from backtest.execution import (
    ExecutionModel,
    TradeResult,
    TP_MODEL_50PCT,
)
from backtest.experiments.exit_rules import Variant


# Conservative exit statuses the experimental loop can produce. These are
# reported in the exit_reasons counts but are NOT changes to the production
# execution model.
STATUS_BE_HIT = "BE_HIT"            # breakeven / trail stop reached entry
STATUS_TRAIL_HIT = "TRAIL_HIT"     # ATR trailing stop reached
STATUS_TIME_EXIT = "TIME_EXIT"     # no +0.5R within N trigger candles

# Experimental terminal statuses that resolve a position (analogous to the
# production STOPPED / TP1_HIT / TP2_HIT closed set). EXPIRED and OPEN are
# not in this set.
EXPERIMENTAL_CLOSED_STATUSES = frozenset({
    "STOPPED", "TP1_HIT", "TP2_HIT",
    STATUS_BE_HIT, STATUS_TRAIL_HIT, STATUS_TIME_EXIT,
})


@dataclass
class ExperimentTrade:
    """A TradeResult enriched with the experiment label + active stop level."""
    trade: TradeResult
    variant: str
    be_armed: bool
    final_stop: Optional[Decimal]

    @property
    def signal_id(self) -> str:
        return self.trade.signal_id

    def as_trade(self) -> TradeResult:
        return self.trade


# ---------------------------------------------------------------------------
# Exit rules
# ---------------------------------------------------------------------------

class _Rule:
    """Base: decides per post-entry candle what the effective stop is and
    whether a new (rule-specific) exit fires. All rules preserve the
    conservative SL-first / TP ordering and the expiration window."""

    name = "base"

    def on_candle(
        self,
        idx: int,
        c: Candle,
        direction: SignalDirection,
        risk: Decimal,
        stop: Decimal,
        tp1: Decimal,
        tp2: Decimal,
        entry_price: Decimal,
        half_r_level: Decimal,
        atr: Optional[Decimal],
    ) -> "tuple[Decimal, bool, str, bool]":
        """Return (effective_stop, rule_exit_now, rule_status, fired_stop_flag)."""
        raise NotImplementedError


class V1Rule(_Rule):
    """Exact copy of TradeSimulator exit path (SL first, then TP1/TP2)."""

    name = "V1"

    def on_candle(self, idx, c, direction, risk, stop, tp1, tp2, entry, half_r_level, atr):
        high, low = c.high, c.low
        if direction == SignalDirection.LONG:
            sl_touched = low <= stop
            tp1_touched = high >= tp1
            tp2_touched = high >= tp2
        else:
            sl_touched = high >= stop
            tp1_touched = low <= tp1
            tp2_touched = low <= tp2
        if sl_touched:
            return stop, True, "STOPPED", True
        if tp1_touched or tp2_touched:
            return stop, False, "TP", False
        return stop, False, "", False


class BreakevenRule(_Rule):
    """H1_BE_0_5R: once +0.5R favorable is reached, move the stop to entry.
    The original SL is retained until the arming level is reached; once armed,
    a touch of the entry price on any later candle exits at entry (0R)."""

    name = "BE"

    def __init__(self):
        self.armed = False

    def on_candle(self, idx, c, direction, risk, stop, tp1, tp2, entry, half_r_level, atr):
        high, low = c.high, c.low
        # Conservative ordering: the original SL takes precedence over
        # arming / breakeven on the same candle. If the stop is touched,
        # exit at the original stop (STOPPED), same as V1.
        sl_touched = (low <= stop) if direction == SignalDirection.LONG else (high >= stop)
        if sl_touched:
            return stop, True, "STOPPED", True
        # Arm check (favorable excursion reached). Once armed the stop
        # moves to entry; the armed stop is evaluated on this candle and
        # on every subsequent candle (the rule keeps `armed=True`).
        if not self.armed:
            if direction == SignalDirection.LONG:
                if high >= half_r_level:
                    self.armed = True
            else:
                if low <= half_r_level:
                    self.armed = True
        effective = entry if self.armed else stop
        if self.armed:
            if direction == SignalDirection.LONG:
                hit = low <= effective
            else:
                hit = high >= effective
            if hit:
                return effective, True, STATUS_BE_HIT, False
        return effective, False, "", False


class AtrTrailRule(_Rule):
    """H1_ATR_TRAIL: after +0.5R favorable, activate an ATR trailing stop
    (default 1.0 x ATR). The stop ratchets in the favorable direction and only
    ever improves; it never moves back toward the loss side."""

    name = "ATR_TRAIL"

    def __init__(self, atr_multiple: float):
        self.atr_multiple = Decimal(str(atr_multiple))
        self.active = False

    def on_candle(self, idx, c, direction, risk, stop, tp1, tp2, entry, half_r_level, atr):
        high, low = c.high, c.low
        if not self.active:
            # Original SL stays active until +0.5R favorable is reached.
            sl_touched = (low <= stop) if direction == SignalDirection.LONG else (high >= stop)
            if sl_touched:
                return stop, True, "STOPPED", True
            if direction == SignalDirection.LONG:
                if high >= half_r_level:
                    self.active = True
                    stop = self._trail_high(high, atr)
                    return stop, False, "", False
            else:
                if low <= half_r_level:
                    self.active = True
                    stop = self._trail_low(low, atr)
                    return stop, False, "", False
            return stop, False, "", False
        # Ratchet the trailing stop using this candle's favorable extreme
        # (LONG: new high; SHORT: new low). The stop only ever improves.
        if atr:
            new_trail = self._trail_high(high, atr) if direction == SignalDirection.LONG \
                else self._trail_low(low, atr)
            if direction == SignalDirection.LONG:
                stop = new_trail if new_trail > stop else stop
            else:
                stop = new_trail if new_trail < stop else stop
        # Evaluate trailing stop on this candle.
        if direction == SignalDirection.LONG:
            hit = low <= stop
        else:
            hit = high >= stop
        if hit:
            return stop, True, STATUS_TRAIL_HIT, False
        return stop, False, "", False

    @staticmethod
    def _trail_high(high: Decimal, atr: Optional[Decimal]) -> Decimal:
        """LONG trailing stop: high - atr_dist (falls back to the high
        itself when ATR is unavailable)."""
        if not atr:
            return high
        dist = atr
        return high - dist

    @staticmethod
    def _trail_low(low: Decimal, atr: Optional[Decimal]) -> Decimal:
        """SHORT trailing stop: low + atr_dist (falls back to the low
        itself when ATR is unavailable)."""
        if not atr:
            return low
        dist = atr
        return low + dist


class PartialBeRule(_Rule):
    """H1_PARTIAL_BE: at +0.5R favorable, protect 50% at breakeven and let the
    remaining 50% keep the original SL/TP model. Realized R = 0.5*0 + 0.5*R_rem."""

    name = "PARTIAL_BE"

    def __init__(self, partial_fraction: float):
        self.partial_fraction = Decimal(str(partial_fraction))
        self.took_partial = False

    def on_candle(self, idx, c, direction, risk, stop, tp1, tp2, entry, half_r_level, atr):
        high, low = c.high, c.low
        if not self.took_partial:
            if direction == SignalDirection.LONG and high >= half_r_level:
                self.took_partial = True
                return entry, False, "PARTIAL", False
            if direction == SignalDirection.SHORT and low <= half_r_level:
                self.took_partial = True
                return entry, False, "PARTIAL", False
        # Remaining fraction keeps the ORIGINAL SL / TP model, using the
        # same "TP" status convention as V1 so the simulator can apply the
        # shared 50PCT ride-to-TP2 / expiration logic.
        if direction == SignalDirection.LONG:
            sl_touched = low <= stop
            tp1_touched = high >= tp1
            tp2_touched = high >= tp2
        else:
            sl_touched = high >= stop
            tp1_touched = low <= tp1
            tp2_touched = low <= tp2
        if sl_touched:
            return stop, True, "STOPPED", True
        if tp1_touched or tp2_touched:
            return stop, False, "TP", False
        return stop, False, "", False


class TimeExitRule(_Rule):
    """H1_TIME_EXIT_12: diagnostic only. If +0.5R favorable has NOT been
    reached within N trigger candles after entry, exit at the close.
    If +0.5R IS reached first, the remaining position keeps the original
    SL/TP model. N is a fixed diagnostic value, not optimized."""

    name = "TIME_EXIT"

    def __init__(self, n_candles: int):
        self.n_candles = n_candles
        self._armed = False

    def on_candle(self, idx, c, direction, risk, stop, tp1, tp2, entry, half_r_level, atr):
        # Track whether +0.5R has been reached on ANY candle up to now.
        # Once reached, the time-exit no longer applies (the position keeps
        # the original SL/TP model from that point on).
        half_reached = (
            (direction == SignalDirection.LONG and c.high >= half_r_level)
            or (direction == SignalDirection.SHORT and c.low <= half_r_level)
        )
        if not self._armed:
            self._armed = half_reached
            half_reached = self._armed
        # Time exit fires only if +0.5R has never been reached within the
        # first N post-entry candles (idx 0..N-1 inclusive). idx+1 is the
        # number of post-entry candles evaluated so far.
        if not self._armed and idx + 1 >= self.n_candles:
            return c.close, True, STATUS_TIME_EXIT, False
        if direction == SignalDirection.LONG:
            sl_touched = c.low <= stop
            tp1_touched = c.high >= tp1
            tp2_touched = c.high >= tp2
        else:
            sl_touched = c.high >= stop
            tp1_touched = c.low <= tp1
            tp2_touched = c.low <= tp2
        if sl_touched:
            return stop, True, "STOPPED", True
        if tp1_touched:
            return stop, True, "TP1_HIT", False
        if tp2_touched:
            return stop, True, "TP2_HIT", False
        return stop, False, "", False


_RULE_FACTORY = {
    "V1": V1Rule,
    "BE": BreakevenRule,
    "ATR_TRAIL": AtrTrailRule,
    "PARTIAL_BE": PartialBeRule,
    "TIME_EXIT": TimeExitRule,
}


# ---------------------------------------------------------------------------
# Experiment simulator
# ---------------------------------------------------------------------------

class ExperimentSimulator:
    """Run one signal's lifecycle under a specific exit-management variant.

    Mirrors TradeSimulator's entry-fill block (identical fill price + candle
    ordering) but applies the variant's post-entry exit rule.
    """

    def __init__(self, model: ExecutionModel, variant: Variant,
                 atr_lookup=None):
        self.model = model
        self.variant = variant
        # atr_lookup: callable(signal, entry_price, stop) -> Decimal|None,
        # or None for non-ATR variants.
        self._atr_lookup = atr_lookup

    def _make_rule(self) -> _Rule:
        v = self.variant
        if v.kind == "ATR_TRAIL":
            return AtrTrailRule(v.atr_multiple or 1.0)
        if v.kind == "PARTIAL_BE":
            return PartialBeRule(v.partial_fraction or 0.5)
        if v.kind == "TIME_EXIT":
            return TimeExitRule(v.time_exit_candles or 12)
        if v.kind == "BE":
            return BreakevenRule()
        return V1Rule()

    def simulate(self, signal: Signal, future_trigger: List[Candle],
                 regime: str = "UNKNOWN", regime_known: bool = False,
                 warmup_min: int = 0) -> ExperimentTrade:
        D = Decimal
        direction = signal.direction
        zone_low = D(signal.entry_low)
        zone_high = D(signal.entry_high)
        stop = D(signal.stop_loss)
        tp1 = D(signal.tp1)
        tp2 = D(signal.tp2)
        rule = self._make_rule()

        entry_price: Optional[Decimal] = None
        entry_time: Optional[datetime] = None
        tp1_hit = False
        tp2_hit = False
        sl_hit = False
        exit_status = "OPEN"
        exit_time: Optional[datetime] = None
        exit_price: Optional[Decimal] = None
        holding = 0
        be_armed = False
        final_stop = stop
        realized_partial = False

        for idx, c in enumerate(future_trigger):
            high, low = c.high, c.low
            if entry_price is None:
                # Identical fill model to TradeSimulator. The production
                # simulator checks the expiration window BEFORE allowing a
                # fill, so a fill at the last window candle
                # (idx == max_hold) is treated as unfilled/EXPIRED, and the
                # post-fill exit branch never runs.
                if self.model.max_candles_after_signal > 0 and idx >= self.model.max_candles_after_signal:
                    break
                zone_intersects = (low <= zone_high and high >= zone_low)
                if zone_intersects:
                    entry_price = (zone_low if direction == SignalDirection.LONG
                                   else zone_high)
                    entry_time = c.timestamp
                    holding += 1
                continue

            holding += 1
            risk = abs(entry_price - stop)
            half_r_level = (entry_price + risk * D("0.5")) if direction == SignalDirection.LONG \
                else (entry_price - risk * D("0.5"))
            atr = self._atr_lookup(signal, entry_price, stop) if self._atr_lookup else None

            effective_stop, rule_exit_now, rule_status, fired_stop = rule.on_candle(
                idx, c, direction, risk, stop, tp1, tp2, entry_price, half_r_level, atr,
            )
            final_stop = effective_stop

            if fired_stop:
                sl_hit = True
                exit_status = "STOPPED"
                exit_price = effective_stop
                exit_time = c.timestamp
                break

            if rule_status == "PARTIAL" and not realized_partial:
                realized_partial = True
                be_armed = True
                continue

            # rule_exit_now is the rule's fired_stop flag (True only for a
            # plain SL stop). The experimental terminal statuses
            # (BE_HIT / TRAIL_HIT / TIME_EXIT) are signalled via rule_status
            # alone, so break on status regardless of rule_exit_now.
            if rule_status in (STATUS_BE_HIT, STATUS_TRAIL_HIT, STATUS_TIME_EXIT):
                exit_status = rule_status
                exit_price = effective_stop
                exit_time = c.timestamp
                break

            if rule_status == "TP":
                tp1_touched = high >= tp1 if direction == SignalDirection.LONG else low <= tp1
                tp2_touched = high >= tp2 if direction == SignalDirection.LONG else low <= tp2
                if tp2_touched:
                    tp2_hit = True
                    exit_status = "TP2_HIT"
                    exit_price = tp2
                    exit_time = c.timestamp
                    break
                if tp1_touched:
                    tp1_hit = True
                    if self.model.tp_model != TP_MODEL_50PCT:
                        exit_status = "TP1_HIT"
                        exit_price = tp1
                        exit_time = c.timestamp
                        break
                    # TP_MODEL_50PCT: TP1 was hit but we ride to TP2.
                    # Do NOT `continue` here — the expiration window must
                    # still be checked on this candle, matching production
                    # TradeSimulator where a non-terminal TP1 does not
                    # suppress the expiry check.

            # Expiration window — identical to production TradeSimulator: the
            # check is on the trigger-absolute index, NOT on a post-fill
            # holding counter. Production expires when idx reaches
            # max_candles_after_signal and closes at that candle's close.
            if self.model.max_candles_after_signal > 0 and idx >= self.model.max_candles_after_signal:
                exit_status = "EXPIRED"
                exit_time = c.timestamp
                exit_price = c.close
                break

        if entry_price is None:
            entry_status = "EXPIRED"
            exit_status = "EXPIRED"
        else:
            entry_status = "FILLED"

        r_multiple = self._compute_r(
            direction, entry_price, exit_price, tp1_hit, tp2_hit, sl_hit,
            stop, zone_low, zone_high, tp1, tp2, realized_partial,
        )

        trade = TradeResult(
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            direction=direction.value,
            signal_time=signal.trigger_candle_time,
            entry_low=zone_low,
            entry_high=zone_high,
            entry_price=entry_price,
            stop_loss=stop,
            tp1=tp1,
            tp2=tp2,
            score=signal.score,
            market_regime=regime,
            entry_status=entry_status,
            exit_status=exit_status,
            exit_time=exit_time,
            exit_price=exit_price,
            r_multiple=r_multiple,
            tp1_hit=tp1_hit,
            tp2_hit=tp2_hit,
            sl_hit=sl_hit,
            holding_candles=holding,
            fees_enabled=self.model.enable_fees,
        )
        return ExperimentTrade(trade=trade, variant=self.variant.label,
                              be_armed=be_armed, final_stop=final_stop)

    # ------------------------------------------------------------------
    def _compute_r(self, direction, entry_price, exit_price,
                   tp1_hit, tp2_hit, sl_hit, stop, zone_low, zone_high,
                   tp1, tp2, realized_partial: bool) -> Decimal:
        if entry_price is None:
            return Decimal("0")
        risk = abs(entry_price - stop)
        if risk == 0:
            return Decimal("0")

        # Realized R is driven by the exit status reached in the loop.
        # With partial protection (H1_PARTIAL_BE), half the position was
        # banked at 0R (breakeven); the remaining half keeps the original
        # SL/TP model. Realized R blends both halves:
        #   realized = 0.5*0 + 0.5*R_remaining
        # Without partial protection the whole position uses R_remaining.

        if sl_hit:
            r_remaining = Decimal("-1")
        elif tp2_hit:
            if self.model.tp_model == TP_MODEL_50PCT:
                tp1_rr = abs(tp1 - entry_price) / risk
                tp2_rr = abs(tp2 - entry_price) / risk
                r_remaining = Decimal("0.5") * tp1_rr + Decimal("0.5") * tp2_rr
            else:
                r_remaining = abs(tp2 - entry_price) / risk
        elif tp1_hit:
            r_remaining = abs(tp1 - entry_price) / risk
        elif exit_price is not None:
            # BE_HIT / TRAIL_HIT / TIME_EXIT / EXPIRED: resolve at exit price.
            r_remaining = self._r_from_exit_price(direction, entry_price, exit_price, risk)
        else:
            r_remaining = Decimal("0")

        # Blend: half protected at 0R + half at the remaining outcome.
        # (0.5*0 + 0.5*r_remaining = 0.5 * r_remaining.)
        if realized_partial:
            gross = Decimal("0.5") * r_remaining
        else:
            gross = r_remaining

        if self.model.enable_fees:
            fee = self.model.taker_fee + self.model.slippage_fraction()
            cost_r = (fee * 2) * (risk / entry_price) if entry_price else Decimal("0")
            gross = gross - cost_r
        return gross

    @staticmethod
    def _r_from_exit_price(direction, entry, exit_price, risk) -> Decimal:
        if exit_price is None:
            return Decimal("0")
        if direction == SignalDirection.LONG:
            return (exit_price - entry) / risk
        return (entry - exit_price) / risk
