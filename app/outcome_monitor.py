"""
Outcome Monitor: evaluates active signals against closed-market data to detect
TP1, TP2, and SL hits.

Pure-deterministic evaluation on CLOSED candles only (no lookahead, no forming
candle). State transitions are monotonic and idempotent via SQLite persistence.

State machine:
  ACTIVE → TP1_HIT → TP2_HIT (terminal)
            → STOPPED  (terminal, SL before or after TP1)
  ACTIVE → STOPPED  (terminal, SL before TP1)
  ANY   → EXPIRED   (terminal, time-based, silent)

Same-candle ambiguity policy (conservative, deterministic):
  If a single candle touches both a TP level and SL, SL wins — we never know
  the intra-candle order, so we assume the worst case for the signal.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional, Tuple

from app.models import Candle, Signal, SignalDirection, SignalStatus
from app.data_validation import cutoff_candles
from app.market_data import BinanceMarketData
from app.logger import get_logger

logger = get_logger(__name__)

# Configurable via environment (fallback defaults here).
SIGNAL_MAX_AGE_HOURS = 48
OUTCOME_MAX_ATTEMPTS = 3

#: Outcome levels that produce a Telegram notification. "EXPIRY" is
#: persisted but deliberately silent — the spec only asks for TP1/TP2/SL.
_NOTIFIED_LEVELS = ("TP1", "TP2", "SL")

#: Maximum candles per Binance API request (hard limit is 1000).
_BINANCE_MAX_LIMIT = 1000

#: Maximum 5M candles that can cover SIGNAL_MAX_AGE_HOURS (48h * 12 = 576).
#: We use this as the absolute upper bound for catch-up.
_MAX_CATCHUP_CANDLES = SIGNAL_MAX_AGE_HOURS * 12  # 576

#: Seconds per outcome candle (monitor is hard-wired to the 5M trigger TF).
_OUTCOME_TF_SECONDS = 300

#: Hard stop on catch-up paging. With a 1000-candle page the full 48h TTL
#: (576 candles) resolves in a single request; the bound only exists so a
#: misbehaving feed can never spin the monitor.
_MAX_FETCH_PAGES = 8


def _catchup_window_start(signal: Signal, now: datetime) -> datetime:
    """Oldest candle instant the monitor may need for this signal.

    Bounded by the signal TTL so we never request history Binance no longer
    serves, and floored at ``created_at`` so we never evaluate candles from
    before the signal existed.
    """
    ttl_floor = now - timedelta(hours=SIGNAL_MAX_AGE_HOURS)
    return max(ttl_floor, signal.created_at)


@dataclass(frozen=True)
class OutcomeTransition:
    """A single deterministic state transition to notify/persist."""
    level: str                # "TP1" | "TP2" | "SL"
    new_status: str           # SignalStatus enum value (e.g. "TP1_HIT")
    hit_time: datetime        # candle close time when the level was hit


def evaluate_signal_outcome(
    signal: Signal,
    candles: List[Candle],
    last_evaluated_candle_time: Optional[datetime],
    now: datetime,
    max_age_hours: int = SIGNAL_MAX_AGE_HOURS,
) -> Tuple[List[OutcomeTransition], Optional[datetime]]:
    """
    Deterministically evaluate a signal against a list of closed 5M candles.

    Args:
        signal: The active signal to monitor.
        candles: All fetched 5M candles (including potentially forming).
        last_evaluated_candle_time: The timestamp of the most recently
            evaluated candle (from SignalStore replay cursor). None = first run.
        now: Current wall-clock time (decision time). Used for closed-candle
            cutoff and age-based expiration.
        max_age_hours: Signal TTL. Exceeding this marks the signal EXPIRED.

    Returns:
        (transitions, new_replay_cursor)

        - transitions: ordered list of OutcomeTransition to apply. May be
          empty. Multiple transitions can fire in one call (e.g., TP1 then
          TP2 on the same candle) — always in deterministic order TP1→TP2→SL.
        - new_replay_cursor: timestamp of the last candle processed. Caller
          must persist this via SignalStore.set_last_evaluated_candle_time so a
          restart never re-evaluates the same closed candle.
    """
    if not candles:
        return [], None

    # 1) Closed-candle cutoff: never evaluate the forming candle.
    closed = cutoff_candles(candles, "5m", now)
    if not closed:
        return [], None

    # 1b) Signal-creation floor: never evaluate a candle that opened before
    #     the signal existed. Guards against replaying pre-signal history
    #     after a long outage.
    closed = [c for c in closed if c.timestamp >= signal.created_at]
    if not closed:
        return [], None

    # 2) Replay cursor: only evaluate candles strictly after the last one
    #    we processed. Candle timestamp = OPEN time. The close time is
    #    open + 5m. If last_evaluated_candle_time is the open of the last
    #    processed candle, we skip any candle with open <= that value.
    if last_evaluated_candle_time is not None:
        start_idx = next(
            (i for i, c in enumerate(closed) if c.timestamp > last_evaluated_candle_time),
            len(closed)
        )
        to_evaluate = closed[start_idx:]
    else:
        to_evaluate = closed

    if not to_evaluate:
        # Nothing new to process. Cursor stays at the last evaluated candle.
        cursor = closed[-1].timestamp if closed else None
        return [], cursor

    # 3) Expiration check (silent terminal transition).
    age_hours = (now - signal.created_at).total_seconds() / 3600
    if age_hours >= max_age_hours:
        if signal.status not in (
            SignalStatus.TP2_HIT,
            SignalStatus.STOPPED,
            SignalStatus.INVALIDATED,
            SignalStatus.EXPIRED,
        ):
            logger.info(f"SIGNAL_EXPIRED {signal.signal_id} age={age_hours:.1f}h")
            return [OutcomeTransition(
                level="EXPIRY",
                new_status=SignalStatus.EXPIRED.value,
                hit_time=now,
            )], to_evaluate[-1].timestamp

    # 4) Iterate candles oldest→newest. Evaluate levels in each candle.
    transitions: List[OutcomeTransition] = []
    current_status = signal.status

    for candle in to_evaluate:
        close_time = candle.timestamp + timedelta(seconds=300)

        if signal.direction == SignalDirection.LONG:
            # LONG: high touches TP, low touches SL
            tp1_touch = candle.high >= signal.tp1
            tp2_touch = candle.high >= signal.tp2
            sl_touch = candle.low <= signal.stop_loss
        else:
            # SHORT: low touches TP, high touches SL
            tp1_touch = candle.low <= signal.tp1
            tp2_touch = candle.low <= signal.tp2
            sl_touch = candle.high >= signal.stop_loss

        # Conservative same-candle policy: SL beats TP in the same candle.
        # So we check SL first. If SL touched, it's a STOP regardless of TP.
        if sl_touch:
            if current_status not in (
                SignalStatus.TP2_HIT,
                SignalStatus.STOPPED,
                SignalStatus.INVALIDATED,
                SignalStatus.EXPIRED,
            ):
                transitions.append(OutcomeTransition(
                    level="SL",
                    new_status=SignalStatus.STOPPED.value,
                    hit_time=close_time,
                ))
                current_status = SignalStatus.STOPPED
                # Terminal — no further candles processed for this signal.
                break

        # TP2 implies TP1 (TP2 > TP1 for LONG, TP2 < TP1 for SHORT).
        # We fire TP1 first if not yet notified, then TP2, to keep
        # notifications deterministic and human-readable.
        if tp2_touch:
            if current_status in (
                SignalStatus.ACTIVE,
                SignalStatus.TP1_HIT,
            ):
                if current_status == SignalStatus.ACTIVE:
                    # TP1 not yet recorded — fire it first.
                    transitions.append(OutcomeTransition(
                        level="TP1",
                        new_status=SignalStatus.TP1_HIT.value,
                        hit_time=close_time,
                    ))
                    current_status = SignalStatus.TP1_HIT
                # Now TP2 (terminal).
                transitions.append(OutcomeTransition(
                    level="TP2",
                    new_status=SignalStatus.TP2_HIT.value,
                    hit_time=close_time,
                ))
                current_status = SignalStatus.TP2_HIT
                break  # TP2_HIT is terminal

        elif tp1_touch:
            if current_status == SignalStatus.ACTIVE:
                transitions.append(OutcomeTransition(
                    level="TP1",
                    new_status=SignalStatus.TP1_HIT.value,
                    hit_time=close_time,
                ))
                current_status = SignalStatus.TP1_HIT
                # Not terminal — continue to next candle for TP2/SL.

    # 5) Cursor advances to the last evaluated candle's open time.
    new_cursor = to_evaluate[-1].timestamp
    return transitions, new_cursor


# ------------------------------------------------------------------
# Notification formatting
# ------------------------------------------------------------------
def format_outcome(signal: Signal, level: str) -> str:
    """Format an outcome notification message for Telegram."""
    direction_text = "LONG" if signal.direction == SignalDirection.LONG else "SHORT"
    emoji = {"TP1": "✅", "TP2": "🎯", "SL": "🛑"}.get(level, "ℹ️")

    msg = f"""{emoji} **{level} HIT** — {direction_text}

`{signal.symbol}` 5M
Entry: `{signal.entry_low:.2f} - {signal.entry_high:.2f}`
SL: `{signal.stop_loss:.2f}`
TP1: `{signal.tp1:.2f}`
TP2: `{signal.tp2:.2f}`

Signal: `{signal.signal_id}`"""
    return msg


def format_startup_notification(
    symbols: List[str],
    interval_seconds: int,
    min_score: int,
    telegram_enabled: bool,
    components: dict,
) -> str:
    """Format the single 🚀 BOT ONLINE startup message."""
    status_lines = []
    for name, ok in components.items():
        emoji = "✅" if ok else "❌"
        status_lines.append(f"{emoji} {name}")

    comp_text = "\n".join(status_lines) if status_lines else "—"

    msg = f"""🚀 **BOT ONLINE**

Symbols: `{", ".join(symbols)}`
Interval: `{interval_seconds}s`
Min Score: `{min_score}/100`
Telegram: `{"ENABLED" if telegram_enabled else "DISABLED"}`

Components:
{comp_text}

Signal only • No auto trading"""
    return msg


# ------------------------------------------------------------------
# Runner: per-cycle orchestration of outcome evaluation + notification
# ------------------------------------------------------------------
class OutcomeMonitorRunner:
    """One pass per main loop cycle.

    - Pages through 5M candle history from the replay cursor to now,
      bounded by the signal's TTL, so a long VPS outage never loses
      outcome-relevant candles.
    - Evaluates every active signal against the closed candles only.
    - Persists state transitions idempotently in SQLite.
    - Sends exactly-once Telegram notifications on state changes.
    - Records a replay cursor so a restart never re-evaluates
      a closed candle.
    - Caps notification attempts per outcome level (TP1 / TP2 / SL)
      independently, so a permanently broken Telegram delivery for
      one level never blocks the others.

    Fully isolated from the scanner: a monitor failure never aborts
    the scan and a scanner failure never aborts the monitor.
    """

    def __init__(self, store, telegram_bot, market_data=None, now_fn=None,
                 chunk_limit: int = _BINANCE_MAX_LIMIT):
        self.store = store
        self.telegram_bot = telegram_bot
        self.market_data = market_data if market_data is not None else BinanceMarketData()
        # Injectable clock keeps closed-candle cutoff deterministic in tests.
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        # Page size for history catch-up. Bounded by Binance's 1000-candle cap.
        self._chunk_limit = max(1, min(int(chunk_limit), _BINANCE_MAX_LIMIT))

    def run_once(self) -> None:
        """Evaluate all active signals once."""
        active = self.store.get_active_signals(limit=50)
        if not active:
            return

        now = self._now_fn()
        for signal in active:
            self._monitor_one(signal, now)

    def _monitor_one(self, signal: Signal, now: datetime) -> None:
        """Evaluate a single signal and persist/notify any transitions."""
        # Snapshot outcome levels that are already persisted but whose
        # notification never landed (Telegram outage, or a crash between
        # persisting the outcome and marking it delivered). The replay
        # cursor has already moved past the candle that produced them, so
        # these levels can never be re-derived by evaluation — they must be
        # recovered from persisted state. Snapshotted BEFORE this cycle's
        # transitions so a level is attempted at most once per cycle.
        pending = self._pending_outcome_levels(signal.signal_id)

        # Fetch every closed 5M candle needed to bridge the replay gap,
        # bounded by the signal TTL and paged in Binance-sized chunks.
        # Returns None when history could not be fetched — the cursor is
        # then left untouched so no candle is silently skipped.
        candles = self._fetch_catchup_candles(signal, now)
        if candles is None:
            # History unavailable: still retry notifications we already know
            # are owed, but never advance the cursor.
            self._flush_pending_notifications(signal, pending)
            return

        if not candles:
            self._flush_pending_notifications(signal, pending)
            return

        last_cursor = self.store.get_last_evaluated_candle_time(
            signal.signal_id)
        try:
            transitions, new_cursor = evaluate_signal_outcome(
                signal, candles, last_cursor, now)
        except Exception as e:
            # Evaluation failure must NOT advance the cursor, otherwise the
            # unprocessed candles would be lost forever.
            logger.error(
                f"Outcome evaluation failed for {signal.signal_id}: {e}",
                exc_info=True)
            self._flush_pending_notifications(signal, pending)
            return

        if not transitions and new_cursor:
            # Advance the replay cursor even when nothing changed,
            # so a restart never re-evaluates the same closed candle.
            self._advance_cursor(signal.signal_id, new_cursor)
            # Also retry any previously-persisted but undelivered level
            # (the cursor has moved past its candle, so it can never be
            # re-derived by evaluation).
            self._flush_pending_notifications(signal, pending)
            return

        for transition in transitions:
            self._apply_transition(signal, transition, new_cursor)

        # Retry any level left undelivered by an earlier cycle/restart.
        self._flush_pending_notifications(signal, pending)

    # ------------------------------------------------------------------
    # Bounded history catch-up
    # ------------------------------------------------------------------
    def _fetch_catchup_candles(self, signal: Signal,
                               now: datetime) -> Optional[List[Candle]]:
        """Fetch the closed-candle history the replay cursor still needs.

        The window starts at the later of the signal's creation time and the
        TTL boundary, so we never request candles older than
        SIGNAL_MAX_AGE_HOURS nor candles from before the signal existed. The
        window is then walked forward in ``chunk_limit``-sized pages, which
        keeps us inside Binance's 1000-candle cap while still covering the
        full 48h TTL after a long outage.

        Returns:
            Chronologically ordered candles, or None if any page failed.
            None is a hard stop: the caller must not advance the cursor.
        """
        window_start = _catchup_window_start(signal, now)
        if window_start >= now:
            return []

        collected: List[Candle] = []
        seen: set = set()
        cursor = window_start

        for _ in range(_MAX_FETCH_PAGES):
            try:
                batch = self.market_data.fetch_klines(
                    signal.symbol, "5m",
                    limit=self._chunk_limit,
                    start_time=cursor,
                    end_time=now,
                )
            except Exception as e:
                logger.error(
                    f"Outcome history fetch failed for {signal.symbol} "
                    f"at {cursor.isoformat()}: {e}. Replay cursor left "
                    f"unchanged so no candle is skipped.")
                return None

            if not batch:
                break

            added = 0
            for candle in batch:
                if candle.timestamp in seen:
                    continue
                seen.add(candle.timestamp)
                collected.append(candle)
                added += 1

            if added == 0:
                # No forward progress (duplicate page) — stop rather than loop.
                break

            if len(batch) < self._chunk_limit:
                # Short page: Binance has no more history at/after cursor.
                break

            if len(collected) >= _MAX_CATCHUP_CANDLES:
                # Hard cap on recovered history (48h of 5M candles).
                break

            next_cursor = batch[-1].timestamp + timedelta(seconds=_OUTCOME_TF_SECONDS)
            if next_cursor <= cursor:
                # Defensive: no progress possible, stop paging.
                break
            cursor = next_cursor
        else:
            logger.error(
                f"Outcome history for {signal.symbol} exceeded the "
                f"{_MAX_FETCH_PAGES}-page catch-up bound; stopping at "
                f"{cursor.isoformat()}")
            return None

        collected.sort(key=lambda c: c.timestamp)
        return collected

    def _pending_outcome_levels(self, signal_id: str) -> Tuple[str, ...]:
        """Outcome levels whose hit is persisted but not yet notified.

        Derived purely from SQLite state, so it is identical before and
        after a restart. Returned in deterministic TP1 -> TP2 -> SL order.
        Levels that have exhausted their OWN retry budget are excluded: their
        budget is independent, so one dead level never blocks the others.
        """
        state = self.store.get_outcome_state(signal_id)
        pairs = (
            ("TP1", "tp1_hit_time", "tp1_notified"),
            ("TP2", "tp2_hit_time", "tp2_notified"),
            ("SL", "sl_hit_time", "sl_notified"),
        )
        return tuple(
            level for level, hit_col, flag_col in pairs
            if state.get(hit_col) and not state.get(flag_col)
            and not self._level_retry_exhausted(signal_id, level)
        )

    def _level_retry_exhausted(self, signal_id: str, level: str) -> bool:
        """True when this level alone has used up its retry budget."""
        return (self.store.get_outcome_attempts(signal_id, level)
                >= OUTCOME_MAX_ATTEMPTS)

    def _flush_pending_notifications(self, signal: Signal,
                                     pending: Tuple[str, ...]) -> None:
        """Re-attempt Telegram delivery for persisted-but-unnotified levels.

        Stops at the first failure so TP1 always precedes TP2 and a broken
        chat cannot spin through every level in a single pass. Each level is
        bounded by its own OUTCOME_MAX_ATTEMPTS budget, so an exhausted TP1
        never prevents a later TP2 or SL from being delivered.
        """
        if not pending:
            return

        if not self.store.is_delivered(signal.signal_id):
            return

        for level in pending:
            if self._level_retry_exhausted(signal.signal_id, level):
                # Budget for this level is spent; the others may still run.
                continue
            if not self._send_and_mark(signal, level):
                return

    def _send_and_mark(self, signal: Signal, level: str) -> bool:
        """Send one outcome notification; mark delivered only on success.

        Returns True when Telegram accepted the message. On failure the
        notification flag is left untouched so the level stays pending and
        is retried on a later cycle or after a restart, up to this level's
        own attempt budget.
        """
        try:
            ok = bool(self.telegram_bot.send_outcome(signal, level))
        except Exception as e:
            logger.error(
                f"Outcome notification raised for {signal.signal_id} "
                f"({level}): {e}",
                exc_info=True)
            ok = False

        if ok:
            # Telegram confirmed delivery — only now is the level marked.
            self.store.mark_outcome_notified(signal.signal_id, level)
            logger.info(f"Outcome {level} notified for {signal.signal_id}")
            return True

        attempts = self.store.record_outcome_failure(
            signal.signal_id, level, f"outcome_{level}_failed")
        if attempts >= OUTCOME_MAX_ATTEMPTS:
            logger.warning(
                f"Outcome {level} for {signal.signal_id} exhausted its own "
                f"retry budget ({attempts}/{OUTCOME_MAX_ATTEMPTS}); other "
                f"outcome levels remain eligible")
        return False

    def _advance_cursor(self, signal_id: str, cursor) -> bool:
        """Persist the replay cursor. Returns False on failure so the caller
        skips notification (the state transition itself is already saved)."""
        if cursor is None:
            return True
        try:
            self.store.set_last_evaluated_candle_time(signal_id, cursor)
            return True
        except Exception as e:
            logger.error(
                f"Failed to persist replay cursor for {signal_id}: {e}")
            return False

    def _apply_transition(self, signal: Signal, transition,
                          new_cursor) -> None:
        """Persist state, send Telegram, then mark notified on success."""
        level = transition.level

        # EXPIRY is silent — persisted for bookkeeping, not notified.
        if level == "EXPIRY":
            self.store.record_outcome(
                signal.signal_id, transition.new_status,
            )
            if new_cursor is not None:
                self._advance_cursor(signal.signal_id, new_cursor)
            return

        if level not in _NOTIFIED_LEVELS:
            return

        # Persist the state transition FIRST, writing only the hit-time
        # column that belongs to this level. Every other column stays NULL
        # so a TP1 hit can never accidentally stamp sl_hit_time.
        self.store.record_outcome(
            signal.signal_id, transition.new_status,
            tp1_hit_time=transition.hit_time if level == "TP1" else None,
            tp2_hit_time=transition.hit_time if level == "TP2" else None,
            sl_hit_time=transition.hit_time if level == "SL" else None,
        )

        # Advance the replay cursor (idempotent).
        if not self._advance_cursor(signal.signal_id, new_cursor):
            return

        # Only notify the user if the signal was already delivered
        # (they saw the original signal). If delivery never succeeded,
        # there is no TP/SL message to send.
        if not self.store.is_delivered(signal.signal_id):
            logger.debug(
                f"Skipping outcome {level} for {signal.signal_id}: "
                f"signal not delivered")
            return

        # Notify Telegram. Failures are recorded but never crash the loop.
        # IMPORTANT: mark_outcome_notified() is called ONLY on Telegram
        # success. This ensures the notification flag is never set before
        # the message is actually delivered. If the process crashes between
        # a successful send and marking, a duplicate may occur (bounded by
        # the per-level flag idempotency check on retry). This is the
        # unavoidable delivery window when atomicity between Telegram and
        # SQLite is impossible.
        try:
            ok = self.telegram_bot.send_outcome(signal, level)
        except Exception as e:
            logger.error(
                f"Outcome notification raised for {signal.signal_id}: {e}",
                exc_info=True)
            ok = False

        if ok:
            # Telegram succeeded — now persist the notification flag.
            self.store.mark_outcome_notified(signal.signal_id, level)
            logger.info(
                f"Outcome {level} notified for {signal.signal_id}")
        else:
            attempts = self.store.record_outcome_failure(
                signal.signal_id, level, f"outcome_{level}_failed")
            if attempts >= OUTCOME_MAX_ATTEMPTS:
                logger.warning(
                    f"Outcome {level} for {signal.signal_id} exhausted its own "
                    f"retry budget ({attempts}/{OUTCOME_MAX_ATTEMPTS}); other "
                    f"outcome levels remain eligible")