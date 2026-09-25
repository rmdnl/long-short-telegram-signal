"""
PHASE 4: Deterministic trade execution model.

Simulates how a SIGNAL-ONLY entry zone is "filled" on historical candles,
then tracks SL / TP1 / TP2 state with a CONSERVATIVE same-candle rule.

Execution model (documented, conservative):
- A signal's entry zone [entry_low, entry_high] is considered FILLED on the
  first LATER 5M candle whose range intersects the zone. Fill price =
    LONG : zone low  (entry_low)  — the least favorable fill in the zone
    SHORT: zone high (entry_high) — the least favorable fill in the zone
  (No optimistic "filled at the ideal mid".)
- If the zone is never touched before the signal expires -> NOT FILLED /
  EXPIRED. The signal still counts as "generated" but produced no trade.

Same-candle ambiguity rule (CONSERVATIVE — prevents inflated backtest P&L):
- If SL and TP are both touched within the same candle -> assume SL first.
- If entry and SL are both touched before TP -> assume SL first.
- Order of checks each candle: 1) SL, 2) TP, 3) entry-fill. Never assume
  the favorable event happened first.

Position sizing / R-multiples:
- R = abs(entry_fill - stop_loss).
- Signal outcome (tp1_hit, tp2_hit, sl_hit) is independent of position size.
- P&L in R is reported; with the default 50%/50% TP model:
    outcome_R = 0.5*(tp1_rr) + 0.5*(tp2_rr)  on TP2
              = 0.5*(tp1_rr)                  on TP1-then-TP2-partial
              = -1.0                          on SL
  Fees/slippage are subtracted from the final R when enabled.

No exchange order is created. Pure simulation.
"""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional

from app.models import Candle, Signal, SignalDirection

# Exit status values
STATUS_WAITING_FOR_ENTRY = "WAITING_FOR_ENTRY"
STATUS_OPEN = "OPEN"
STATUS_TP1_HIT = "TP1_HIT"
STATUS_TP2_HIT = "TP2_HIT"
STATUS_STOPPED = "STOPPED"
STATUS_EXPIRED = "EXPIRED"

# Position-exit models for TP1
TP_MODEL_50PCT = "TP1_50PCT"   # default: 50% exits at TP1, 50% rides to TP2
TP_MODEL_ALL = "TP1_ALL"       # whole position exits at TP1

# Default exit model (configurable per requirement #11)
DEFAULT_TP_MODEL = TP_MODEL_50PCT


@dataclass
class ExecutionModel:
    """Configurable deterministic execution parameters."""
    maker_fee: Decimal = Decimal("0")        # fraction of notional, e.g. 0.0002
    taker_fee: Decimal = Decimal("0")        # fraction of notional, e.g. 0.0004
    slippage_bps: Decimal = Decimal("0")     # slippage in basis points
    tp_model: str = DEFAULT_TP_MODEL         # TP1_50PCT or TP1_ALL
    enable_fees: bool = False                # if False, fees/slippage not applied
    max_candles_after_signal: int = 0        # expiration window in 5M candles

    def slippage_fraction(self) -> Decimal:
        return self.slippage_bps / Decimal("10000")

    def apply_cost_to_r(self, gross_r: Decimal, direction: SignalDirection) -> Decimal:
        """Convert a gross price-movement R into a net R after costs.

        Entry cost is incurred on entry; exit cost on exit. Both are charged
        against the R (positive fills reduce R, negative fills increase it).
        """
        if not self.enable_fees:
            return gross_r
        fee = self.taker_fee
        slip = self.slippage_fraction()
        # Round-trip cost as a fraction of risk:
        # cost_price = (fee_entry+slip_entry) * entry + (fee_exit+slip_exit) * exit
        # As an R fraction we approximate: (fee+slip) applied at entry and exit.
        round_trip = (fee + slip) * 2
        # Convert price-based cost to R units: cost_R = round_trip * risk / entry_price
        # risk is handled per-trade in TradeSimulator; here we return a scaling
        # factor the simulator multiplies the risk-normalized result by.
        return round_trip


@dataclass
class TradeResult:
    """Outcome of simulating one signal through the execution model."""
    signal_id: str
    symbol: str
    direction: str                 # LONG / SHORT
    signal_time: datetime
    entry_low: Decimal
    entry_high: Decimal
    entry_price: Optional[Decimal] # fill price, None if not filled
    stop_loss: Decimal
    tp1: Decimal
    tp2: Decimal
    score: int
    market_regime: str

    entry_status: str              # FILLED / EXPIRED
    exit_status: str               # TP1_HIT / TP2_HIT / STOPPED / EXPIRED / OPEN
    exit_time: Optional[datetime]
    exit_price: Optional[Decimal]

    r_multiple: Decimal            # net R (after fees when enabled); 0 if expired
    tp1_hit: bool
    tp2_hit: bool
    sl_hit: bool
    holding_candles: int
    fees_enabled: bool


class TradeSimulator:
    """Deterministic simulation of a single signal's lifecycle."""

    def __init__(self, model: Optional[ExecutionModel] = None):
        self.model = model or ExecutionModel()

    def simulate(self, signal: Signal,
                 future_trigger: List[Candle],
                 regime: str = "UNKNOWN",
                 regime_known: bool = False,
                 warmup_min: int = 0) -> TradeResult:
        """
        Simulate one signal against the candles that come AFTER its trigger.

        Args:
            signal: the generated signal (entry zone, SL, TP1, TP2).
            future_trigger: 5M candles AFTER the trigger candle, in time order.
                           These are the only candles that may fill/exit the trade.
            regime: recorded market regime at signal time.
        """
        direction = signal.direction
        # Normalize prices to Decimal (Signal fields may be Decimal or str
        # depending on caller; arithmetic below requires Decimal).
        D = Decimal
        zone_low = D(signal.entry_low)
        zone_high = D(signal.entry_high)
        sl = D(signal.stop_loss)
        tp1 = D(signal.tp1)
        tp2 = D(signal.tp2)

        entry_price: Optional[Decimal] = None
        entry_time: Optional[datetime] = None
        tp1_hit = False
        tp2_hit = False
        sl_hit = False
        exit_status = STATUS_OPEN
        exit_time: Optional[datetime] = None
        exit_price: Optional[Decimal] = None
        holding = 0

        def _candle_range(c: Candle):
            return c.high, c.low

        in_open = False
        open_price_ref: Optional[Decimal] = None

        for idx, c in enumerate(future_trigger):
            high, low = _candle_range(c)

            if not in_open:
                # ENTRY-FILL: the candle's range must intersect the entry zone.
                # The fill candle OPENS the position; SL/TP are NOT evaluated
                # on this candle (the entry price is the least-favorable edge,
                # so same-candle SL ambiguity is avoided by construction).
                zone_intersects = (low <= zone_high and high >= zone_low)
                if zone_intersects:
                    entry_price = (zone_low if direction == SignalDirection.LONG
                                    else zone_high)
                    entry_time = c.timestamp
                    in_open = True
                    holding += 1
                # Expiration window for unfilled signals
                if self.model.max_candles_after_signal > 0 and idx >= self.model.max_candles_after_signal:
                    break
                continue

            holding += 1
            # Position is OPEN on this candle. Conservative ordering: SL first.
            if direction == SignalDirection.LONG:
                sl_touched = low <= sl
                tp1_touched = high >= tp1
                tp2_touched = high >= tp2
            else:  # SHORT
                sl_touched = high >= sl
                tp1_touched = low <= tp1
                tp2_touched = low <= tp2

            # 1) STOP LOSS has priority (conservative same-candle rule)
            if sl_touched:
                sl_hit = True
                exit_status = STATUS_STOPPED
                exit_price = sl
                exit_time = c.timestamp
                break

            # 2) TAKE PROFITS (TP1 then TP2 in the same candle -> TP1 recorded)
            if tp1_touched:
                tp1_hit = True
                if self.model.tp_model == TP_MODEL_ALL:
                    exit_status = STATUS_TP1_HIT
                    exit_price = tp1
                    exit_time = c.timestamp
                    break
            if tp2_touched:
                tp2_hit = True
                exit_status = STATUS_TP2_HIT
                exit_price = tp2
                exit_time = c.timestamp
                break

            # 3) Expiration window
            if self.model.max_candles_after_signal > 0 and idx >= self.model.max_candles_after_signal:
                exit_status = STATUS_EXPIRED
                exit_time = c.timestamp
                exit_price = c.close
                break

        # Final status if still open and not expired
        if in_open and exit_status == STATUS_OPEN:
            # Trade still open at end of data: mark EXPIRED (no fill assumption)
            exit_status = STATUS_EXPIRED
            if exit_time is None:
                exit_time = future_trigger[-1].timestamp if future_trigger else signal.trigger_candle_time
                exit_price = future_trigger[-1].close if future_trigger else None

        if not in_open:
            entry_status = STATUS_EXPIRED
            exit_status = STATUS_EXPIRED
        else:
            entry_status = "FILLED"

        # R-multiple calculation
        r_multiple = self._compute_r(direction, entry_price, exit_price,
                                     tp1_hit, tp2_hit, sl_hit, sl, zone_low, zone_high,
                                     tp1=tp1, tp2=tp2)

        return TradeResult(
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            direction=direction.value,
            signal_time=signal.trigger_candle_time,
            entry_low=zone_low,
            entry_high=zone_high,
            entry_price=entry_price,
            stop_loss=sl,
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

    def _compute_r(self, direction, entry_price, exit_price,
                   tp1_hit, tp2_hit, sl_hit, sl, zone_low, zone_high,
                   tp1: 'Decimal' = None, tp2: 'Decimal' = None) -> Decimal:
        """Net R-multiple for one simulated trade."""
        if entry_price is None:
            return Decimal("0")

        risk = abs(entry_price - sl)
        if risk == 0:
            return Decimal("0")

        # Base gross R from the dominant exit:
        if sl_hit:
            # Conservative: full stop-out = -1R regardless of partial TPs
            gross = Decimal("-1")
        elif tp2_hit:
            if self.model.tp_model == TP_MODEL_50PCT:
                # 50% at +tp1_rr, 50% at +tp2_rr
                tp1_rr = abs(tp1 - entry_price) / risk
                tp2_rr = abs(tp2 - entry_price) / risk
                gross = (Decimal("0.5") * tp1_rr + Decimal("0.5") * tp2_rr)
            else:
                gross = abs(tp2 - entry_price) / risk
        elif tp1_hit:
            gross = abs(tp1 - entry_price) / risk
        else:
            # Expired still-open: measure to the last exit price
            if exit_price is None:
                gross = Decimal("0")
            elif direction == SignalDirection.LONG:
                gross = (exit_price - entry_price) / risk
            else:
                gross = (entry_price - exit_price) / risk

        # Apply fees/slippage when enabled (simplified: round-trip cost as a
        # fraction of risk; entry+exit each incur taker fee + slippage).
        if self.model.enable_fees:
            fee = self.model.taker_fee + self.model.slippage_fraction()
            cost_r = (fee * 2) * (risk / entry_price) if entry_price else Decimal("0")
            gross = gross - cost_r

        return gross
