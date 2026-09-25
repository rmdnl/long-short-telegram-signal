"""
Signal storage: SQLite-based signal history.

Tracks all signals sent, with status updates.
"""
import sqlite3
from pathlib import Path
from typing import Optional, List
from datetime import datetime

from app.models import Signal, SignalStatus, SignalDirection
from app.logger import get_logger

logger = get_logger(__name__)


class SignalStore:
    """SQLite signal storage"""

    def __init__(self, db_path: str = "signals.db"):
        self.db_path = Path(db_path)
        self._init_db()

    def _init_db(self):
        """Create signals table if not exists"""
        conn = sqlite3.connect(self.db_path)
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
                expiration_time TEXT
            )
        """)

        conn.commit()
        conn.close()

    def save_signal(self, signal: Signal) -> None:
        """Save signal to database"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            INSERT OR REPLACE INTO signals (
                signal_id, symbol, direction, signal_type,
                created_at, trigger_candle_time,
                entry_low, entry_high, stop_loss, tp1, tp2,
                score, htf_bias, adx_value, rsi_value, volume_ratio,
                status, tp1_hit_time, tp2_hit_time, sl_hit_time, expiration_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        ))

        conn.commit()
        conn.close()
        logger.debug(f"Saved signal {signal.signal_id}")

    def get_signal(self, signal_id: str) -> Optional[Signal]:
        """Retrieve signal by ID"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM signals WHERE signal_id = ?", (signal_id,))
        row = cursor.fetchone()
        conn.close()

        if not row:
            return None

        # TODO: Parse row back to Signal object (not critical for V1)
        return None

    def signal_exists(self, signal_id: str) -> bool:
        """Check if signal ID exists"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("SELECT 1 FROM signals WHERE signal_id = ?", (signal_id,))
        exists = cursor.fetchone() is not None

        conn.close()
        return exists

    def get_all_signal_ids(self) -> List[str]:
        """Get all signal IDs"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("SELECT signal_id FROM signals")
        rows = cursor.fetchall()

        conn.close()
        return [row[0] for row in rows]
