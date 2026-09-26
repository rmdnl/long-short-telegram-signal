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
from datetime import datetime
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
                last_delivery_error TEXT
            )
        """)

        # Schema migration for databases created before delivery tracking.
        self._ensure_column(
            cursor, "delivery_status", "TEXT NOT NULL DEFAULT 'PENDING'")
        self._ensure_column(
            cursor, "delivery_attempts", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(cursor, "last_delivery_error", "TEXT")

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
            "SELECT signal_id, symbol, direction, signal_type, created_at,"
            " trigger_candle_time, entry_low, entry_high, stop_loss, tp1, tp2,"
            " score, htf_bias, adx_value, rsi_value, volume_ratio, status,"
            " tp1_hit_time, tp2_hit_time, sl_hit_time, expiration_time"
            " FROM signals WHERE signal_id = ?",
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
            "SELECT signal_id, symbol, direction, signal_type, created_at,"
            " trigger_candle_time, entry_low, entry_high, stop_loss, tp1, tp2,"
            " score, htf_bias, adx_value, rsi_value, volume_ratio, status,"
            " tp1_hit_time, tp2_hit_time, sl_hit_time, expiration_time"
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
