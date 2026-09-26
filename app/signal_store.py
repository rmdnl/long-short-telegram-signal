"""
Signal storage: SQLite-based signal history.

Tracks all signals sent, with status updates.

Two independent state axes are persisted per signal:

* ``status``            - TRADE lifecycle (ACTIVE / TP1_HIT / STOPPED / ...).
* ``delivery_status``   - MESSAGE delivery lifecycle
                          (PENDING / DELIVERED / FAILED).

They are deliberately separate: a signal can be a perfectly valid ACTIVE
trade setup whose Telegram message has not been delivered yet. Collapsing
them would let a failed send masquerade as a delivered signal (or vice
versa), which is exactly the failure mode we must avoid.

SQLite is the source of truth for both axes, so delivery state and
duplicate suppression survive a process restart.
"""
import sqlite3
from pathlib import Path
from typing import Optional, List
from datetime import datetime, timezone
from decimal import Decimal

from app.models import (
    Signal, SignalStatus, SignalDirection, SignalType, MarketBias,
)
from app.logger import get_logger

logger = get_logger(__name__)


# Delivery state machine (message delivery, NOT trade lifecycle).
DELIVERY_PENDING = "PENDING"
DELIVERY_DELIVERED = "DELIVERED"
DELIVERY_FAILED = "FAILED"

# Outcome levels that can each be notified exactly once.
OUTCOME_TP1 = "TP1"
OUTCOME_TP2 = "TP2"
OUTCOME_SL = "SL"

#: Maps an outcome level to its persisted "already notified" column.
_OUTCOME_NOTIFY_COLUMNS = {
    OUTCOME_TP1: "tp1_notified",
    OUTCOME_TP2: "tp2_notified",
    OUTCOME_SL: "sl_notified",
}

#: Shared SELECT column list for full Signal reconstruction.
_SIGNAL_COLUMNS = (
    "SELECT signal_id, symbol, direction, signal_type, created_at,"
    " trigger_candle_time, entry_low, entry_high, stop_loss, tp1, tp2,"
    " score, htf_bias, adx_value, rsi_value, volume_ratio, status,"
    " tp1_hit_time, tp2_hit_time, sl_hit_time, expiration_time"
)


def _to_aware_datetime(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 string back into a timezone-aware datetime.

    ``datetime.fromisoformat`` keeps the UTC offset that ``isoformat()``
    wrote, so round-tripping preserves the original tzinfo exactly.
    """
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        # Defensive: never hand a naive datetime to Signal.__post_init__.
        from datetime import timezone
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _iso(dt: datetime) -> str:
    """Serialize a datetime as an ISO string sqlite3 can bind without
    the deprecated datetime adapter (avoids the 3.12 warning)."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None).isoformat()


def _to_optional_decimal(value) -> Optional[Decimal]:
    """Parse a stored TEXT decimal back into Decimal, preserving precision."""
    if value is None or value == "":
        return None
    return Decimal(value)


class SignalStore:
    """SQLite signal storage"""

    def __init__(self, db_path: str = "signals.db"):
        self.db_path = Path(db_path)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        """Create signals table if not exists, then migrate older schemas."""
        conn = self._connect()
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                signal_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                created_at TEXT NOT NULL,
                trigger_candle_time TEXT NOT NULL,
                entry_low TEXT NOT NULL,
                entry_high TEXT NOT NULL,
                stop_loss TEXT NOT NULL,
                tp1 TEXT NOT NULL,
                tp2 TEXT NOT NULL,
                score INTEGER NOT NULL,
                htf_bias TEXT NOT NULL,
                adx_value TEXT NOT NULL,
                rsi_value TEXT NOT NULL,
                volume_ratio TEXT NOT NULL,
                status TEXT NOT NULL,
                tp1_hit_time TEXT,
                tp2_hit_time TEXT,
                sl_hit_time TEXT,
                expiration_time TEXT,
                delivery_status TEXT NOT NULL DEFAULT 'PENDING',
                delivery_attempts INTEGER NOT NULL DEFAULT 0,
                last_delivery_error TEXT,
                tp1_notified INTEGER NOT NULL DEFAULT 0,
                tp2_notified INTEGER NOT NULL DEFAULT 0,
                sl_notified INTEGER NOT NULL DEFAULT 0,
                outcome_attempts INTEGER NOT NULL DEFAULT 0,
                last_outcome_error TEXT,
                last_evaluated_candle_time TEXT
            )
        """)

        # Schema migration for databases created before delivery tracking.
        self._ensure_column(
            cursor, "delivery_status", "TEXT NOT NULL DEFAULT 'PENDING'")
        self._ensure_column(
            cursor, "delivery_attempts", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(cursor, "last_delivery_error", "TEXT")
        # Outcome-monitor migration (TP/SL notification state).
        self._ensure_column(
            cursor, "tp1_notified", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(
            cursor, "tp2_notified", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(
            cursor, "sl_notified", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(
            cursor, "outcome_attempts", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(cursor, "last_outcome_error", "TEXT")
        self._ensure_column(cursor, "last_evaluated_candle_time", "TEXT")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bot_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        conn.commit()
        conn.close()

    @staticmethod
    def _ensure_column(cursor, column: str, ddl: str) -> None:
        """Add a column only when the existing table lacks it (idempotent)."""
        existing = {row[1] for row in cursor.execute("PRAGMA table_info(signals)")}
        if column not in existing:
            cursor.execute(f"ALTER TABLE signals ADD COLUMN {column} {ddl}")
            logger.info(f"Migrated signals table: added column '{column}'")

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------
    def save_signal(self, signal: Signal) -> None:
        """Save signal to database (INSERT OR REPLACE on the deterministic id).

        Re-saving the same signal_id resets delivery bookkeeping, so callers
        must not re-save an already-delivered signal. Use the dedicated
        mark_delivered() / record_delivery_failure() helpers instead.
        """
        conn = self._connect()
        cursor = conn.cursor()

        cursor.execute("""
            INSERT OR REPLACE INTO signals (
                signal_id, symbol, direction, signal_type,
                created_at, trigger_candle_time,
                entry_low, entry_high, stop_loss, tp1, tp2,
                score, htf_bias, adx_value, rsi_value, volume_ratio,
                status, tp1_hit_time, tp2_hit_time, sl_hit_time, expiration_time,
                delivery_status, delivery_attempts, last_delivery_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?)
        """, (
            signal.signal_id,
            signal.symbol,
            signal.direction.value,
            signal.signal_type.value,
            signal.created_at.isoformat(),
            signal.trigger_candle_time.isoformat(),
            str(signal.entry_low),
            str(signal.entry_high),
            str(signal.stop_loss),
            str(signal.tp1),
            str(signal.tp2),
            signal.score,
            signal.htf_bias.value,
            str(signal.adx_value),
            str(signal.rsi_value),
            str(signal.volume_ratio),
            signal.status.value,
            signal.tp1_hit_time.isoformat() if signal.tp1_hit_time else None,
            signal.tp2_hit_time.isoformat() if signal.tp2_hit_time else None,
            signal.sl_hit_time.isoformat() if signal.sl_hit_time else None,
            signal.expiration_time.isoformat() if signal.expiration_time else None,
            DELIVERY_PENDING,
            0,
            None,
        ))

        conn.commit()
        conn.close()
        logger.debug(f"Saved signal {signal.signal_id}")

    def mark_delivered(self, signal_id: str) -> None:
        """Mark a signal's Telegram message as successfully delivered."""
        conn = self._connect()
        conn.execute(
            "UPDATE signals SET delivery_status = ?, last_delivery_error = NULL"
            " WHERE signal_id = ?",
            (DELIVERY_DELIVERED, signal_id),
        )
        conn.commit()
        conn.close()
        logger.info(f"SIGNAL_DELIVERED {signal_id}")

    def record_delivery_failure(self, signal_id: str, error: str = "") -> int:
        """Record a failed delivery attempt and return the new attempt count.

        The signal is left non-DELIVERED so it is never treated as fully
        processed, and the attempt counter bounds retries across restarts.
        """
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE signals"
            " SET delivery_status = ?, delivery_attempts = delivery_attempts + 1,"
            "     last_delivery_error = ?"
            " WHERE signal_id = ?",
            (DELIVERY_FAILED, error[:500], signal_id),
        )
        cursor.execute(
            "SELECT delivery_attempts FROM signals WHERE signal_id = ?",
            (signal_id,),
        )
        row = cursor.fetchone()
        conn.commit()
        conn.close()
        attempts = row[0] if row else 0
        logger.warning(
            f"SIGNAL_DELIVERY_FAILED {signal_id} attempts={attempts} error={error}")
        return attempts

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------
    def get_signal(self, signal_id: str) -> Optional[Signal]:
        """Retrieve signal by ID and reconstruct the full Signal object.

        Round-trips every persisted field:
            Signal -> SQLite -> get_signal() -> Signal
        Decimals stay exact (TEXT storage), timestamps stay timezone-aware,
        and the deterministic signal_id is returned unchanged.
        """
        conn = self._connect()
        cursor = conn.cursor()

        cursor.execute(
            _SIGNAL_COLUMNS + " FROM signals WHERE signal_id = ?",
            (signal_id,),
        )
        row = cursor.fetchone()
        conn.close()

        if not row:
            return None

        return self._row_to_signal(row)

    @staticmethod
    def _row_to_signal(row) -> Signal:
        """Map a `signals` table row tuple to a Signal instance."""
        return Signal(
            signal_id=row[0],
            symbol=row[1],
            direction=SignalDirection(row[2]),
            signal_type=SignalType(row[3]),
            created_at=_to_aware_datetime(row[4]),
            trigger_candle_time=_to_aware_datetime(row[5]),
            entry_low=Decimal(row[6]),
            entry_high=Decimal(row[7]),
            stop_loss=Decimal(row[8]),
            tp1=Decimal(row[9]),
            tp2=Decimal(row[10]),
            score=int(row[11]),
            htf_bias=MarketBias(row[12]),
            adx_value=Decimal(row[13]),
            rsi_value=Decimal(row[14]),
            volume_ratio=Decimal(row[15]),
            status=SignalStatus(row[16]),
            tp1_hit_time=_to_aware_datetime(row[17]),
            tp2_hit_time=_to_aware_datetime(row[18]),
            sl_hit_time=_to_aware_datetime(row[19]),
            expiration_time=_to_aware_datetime(row[20]),
        )

    def is_delivered(self, signal_id: str) -> bool:
        """True only if the signal's message was confirmed delivered.

        This is the authoritative duplicate-suppression check: a signal that
        was generated but never delivered is NOT considered processed.
        """
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT delivery_status FROM signals WHERE signal_id = ?",
            (signal_id,),
        )
        row = cursor.fetchone()
        conn.close()
        return bool(row) and row[0] == DELIVERY_DELIVERED

    def get_undelivered_signals(self, limit: int = 50) -> List[Signal]:
        """Return pending/failed signals, oldest first, for bounded retry.

        Survives restart because delivery state lives in SQLite.
        """
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            _SIGNAL_COLUMNS +
            " FROM signals WHERE delivery_status != ?"
            " ORDER BY created_at ASC LIMIT ?",
            (DELIVERY_DELIVERED, limit),
        )
        rows = cursor.fetchall()
        conn.close()
        return [self._row_to_signal(r) for r in rows]

    def get_delivery_attempts(self, signal_id: str) -> int:
        """Number of recorded delivery attempts for a signal."""
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT delivery_attempts FROM signals WHERE signal_id = ?",
            (signal_id,),
        )
        row = cursor.fetchone()
        conn.close()
        return int(row[0]) if row else 0

    def get_last_delivered_by_symbol(self) -> List[tuple]:
        """Return (symbol, trigger_candle_time, direction) of the most recent
        DELIVERED signal per symbol.

        Used to rehydrate the scanner's cooldown / opposite-direction gate
        after a restart, so a restart cannot be used to bypass the cooldown.
        Only DELIVERED signals count: an undelivered signal was never
        announced, so it must not start a cooldown window.
        """
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT s.symbol, s.trigger_candle_time, s.direction
            FROM signals s
            JOIN (
                SELECT symbol, MAX(trigger_candle_time) AS max_t
                FROM signals
                WHERE delivery_status = ?
                GROUP BY symbol
            ) m
            ON s.symbol = m.symbol AND s.trigger_candle_time = m.max_t
            WHERE s.delivery_status = ?
            GROUP BY s.symbol
        """, (DELIVERY_DELIVERED, DELIVERY_DELIVERED))
        rows = cursor.fetchall()
        conn.close()
        return rows

    def signal_exists(self, signal_id: str) -> bool:
        """Check if signal ID exists (row present, regardless of delivery)."""
        conn = self._connect()
        cursor = conn.cursor()

        cursor.execute("SELECT 1 FROM signals WHERE signal_id = ?", (signal_id,))
        exists = cursor.fetchone() is not None

        conn.close()
        return exists

    def get_all_signal_ids(self) -> List[str]:
        """Get all signal IDs"""
        conn = self._connect()
        cursor = conn.cursor()

        cursor.execute("SELECT signal_id FROM signals")
        rows = cursor.fetchall()

        conn.close()
        return [row[0] for row in rows]

    # ------------------------------------------------------------------
    # Outcome state (TP1 / TP2 / SL monitor)
    # ------------------------------------------------------------------
    def get_active_signals(self, limit: int = 50) -> List[Signal]:
        """Return signals still being monitored, oldest trigger first.

        A signal stays monitorable while its lifecycle status is ACTIVE or
        TP1_HIT (TP1_HIT still needs a TP2/SL verdict). Terminal states
        (TP2_HIT / STOPPED / INVALIDATED / EXPIRED) are excluded.

        Only DELIVERED signals are monitorable: the user must have actually
        seen the signal before being told it hit TP or SL.
        """
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            _SIGNAL_COLUMNS +
            " FROM signals WHERE status IN (?, ?)"
            " AND delivery_status = ?"
            " ORDER BY trigger_candle_time ASC LIMIT ?",
            (SignalStatus.ACTIVE.value, SignalStatus.TP1_HIT.value,
             DELIVERY_DELIVERED, limit),
        )
        rows = cursor.fetchall()
        conn.close()
        return [self._row_to_signal(r) for r in rows]

    def get_outcome_state(self, signal_id: str) -> dict:
        """Return the outcome bookkeeping row for a signal.

        Keys: status, tp1_hit_time, tp2_hit_time, sl_hit_time,
        tp1_notified, tp2_notified, sl_notified, outcome_attempts.
        Missing rows return a zeroed default so callers never crash.
        """
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT status, tp1_hit_time, tp2_hit_time, sl_hit_time,"
            " tp1_notified, tp2_notified, sl_notified, outcome_attempts"
            " FROM signals WHERE signal_id = ?",
            (signal_id,),
        )
        row = cursor.fetchone()
        conn.close()
        if not row:
            return {
                "status": SignalStatus.ACTIVE.value,
                "tp1_hit_time": None, "tp2_hit_time": None, "sl_hit_time": None,
                "tp1_notified": 0, "tp2_notified": 0, "sl_notified": 0,
                "outcome_attempts": 0,
            }
        return {
            "status": row[0],
            "tp1_hit_time": row[1],
            "tp2_hit_time": row[2],
            "sl_hit_time": row[3],
            "tp1_notified": int(row[4] or 0),
            "tp2_notified": int(row[5] or 0),
            "sl_notified": int(row[6] or 0),
            "outcome_attempts": int(row[7] or 0),
        }

    def record_outcome(
        self,
        signal_id: str,
        status: str,
        tp1_hit_time: Optional[object] = None,
        tp2_hit_time: Optional[object] = None,
        sl_hit_time: Optional[object] = None,
    ) -> None:
        """Persist an outcome state transition (monotonic, idempotent).

        Only NULL columns are filled: a previously recorded hit time is never
        overwritten, so replaying the same evaluation is harmless. The
        terminal-state guard mirrors the state machine — once the signal is
        TP2_HIT / STOPPED / INVALIDATED / EXPIRED it can never move back to a
        non-terminal status.

        ``*_hit_time`` may be a string or a datetime — datetimes are
        serialized via :func:`_iso` so the sqlite3 datetime-adapter warning
        (Python 3.12) never fires.
        """
        def _maybe_iso(value):
            if value is None:
                return None
            if isinstance(value, datetime):
                return _iso(value)
            return str(value)

        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE signals SET"
            " status = CASE WHEN status IN"
            "   ('TP2_HIT', 'STOPPED', 'INVALIDATED', 'EXPIRED')"
            "   THEN status ELSE ? END,"
            " tp1_hit_time = COALESCE(tp1_hit_time, ?),"
            " tp2_hit_time = COALESCE(tp2_hit_time, ?),"
            " sl_hit_time = COALESCE(sl_hit_time, ?)"
            " WHERE signal_id = ?",
            (status, _maybe_iso(tp1_hit_time), _maybe_iso(tp2_hit_time),
             _maybe_iso(sl_hit_time), signal_id),
        )
        conn.commit()
        conn.close()
        logger.info(f"OUTCOME_UPDATED {signal_id} status={status}")

    def mark_outcome_notified(self, signal_id: str, level: str) -> None:
        """Flip the per-level notification flag so a level is sent exactly once.

        The UPDATE is guarded by `= 0` so a concurrent/restarted process
        cannot send the same level twice.
        """
        if level not in _OUTCOME_NOTIFY_COLUMNS:
            raise ValueError(f"Unknown outcome level: {level}")
        column = _OUTCOME_NOTIFY_COLUMNS[level]
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE signals SET {column} = 1 WHERE signal_id = ? AND {column} = 0",
            (signal_id,),
        )
        changed = cursor.rowcount
        conn.commit()
        conn.close()
        if changed == 0:
            logger.debug(f"Outcome notification {level} already sent for {signal_id}")

    def is_outcome_notified(self, signal_id: str, level: str) -> bool:
        """True when the given level's notification already went out."""
        if level not in _OUTCOME_NOTIFY_COLUMNS:
            raise ValueError(f"Unknown outcome level: {level}")
        column = _OUTCOME_NOTIFY_COLUMNS[level]
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT {column} FROM signals WHERE signal_id = ?", (signal_id,))
        row = cursor.fetchone()
        conn.close()
        return bool(row) and int(row[0] or 0) == 1

    def record_outcome_failure(self, signal_id: str, error: str = "") -> int:
        """Record a failed outcome notification and return the attempt count.

        Bounded by OUTCOME_MAX_ATTEMPTS in the monitor so a permanently
        broken chat can never produce an infinite notification loop.
        """
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE signals SET outcome_attempts = outcome_attempts + 1,"
            " last_outcome_error = ? WHERE signal_id = ?",
            (error[:500], signal_id),
        )
        cursor.execute(
            "SELECT outcome_attempts FROM signals WHERE signal_id = ?",
            (signal_id,))
        row = cursor.fetchone()
        conn.commit()
        conn.close()
        attempts = row[0] if row else 0
        logger.warning(
            f"OUTCOME_NOTIFICATION_FAILED {signal_id} "
            f"attempts={attempts} error={error}")
        return attempts

    def get_outcome_attempts(self, signal_id: str) -> int:
        """Number of recorded outcome-notification attempts for a signal."""
        return int(self.get_outcome_state(signal_id).get("outcome_attempts") or 0)

    def get_last_evaluated_candle_time(self, signal_id: str) -> Optional[datetime]:
        """Replay cursor: the most recent candle the outcome monitor
        has evaluated for this signal. None when never evaluated."""
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT last_evaluated_candle_time FROM signals WHERE signal_id = ?",
            (signal_id,),
        )
        row = cursor.fetchone()
        conn.close()
        if not row or row[0] is None:
            return None
        return _to_aware_datetime(row[0])

    def set_last_evaluated_candle_time(self, signal_id: str, ts: datetime) -> None:
        """Advance the replay cursor so a restarted monitor never
        re-evaluates a closed candle."""
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE signals SET last_evaluated_candle_time = ? WHERE signal_id = ?",
            (_iso(ts), signal_id),
        )
        conn.commit()
        conn.close()

    # ------------------------------------------------------------------
    # Key/value metadata (crash-loop protection, uptime bookkeeping)
    # ------------------------------------------------------------------
    def get_meta(self, key: str) -> Optional[str]:
        """Read a persisted metadata value, or None when absent."""
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM bot_meta WHERE key = ?", (key,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        """Write a persisted metadata value (upsert)."""
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO bot_meta (key, value) VALUES (?, ?)",
            (key, value))
        conn.commit()
        conn.close()
