"""
Read-only SQLite access for the dashboard.

This module NEVER mutates the bot database. It:
* Opens the database in read-only URI mode
* Uses parameterized SQL only
* Handles missing tables and missing columns gracefully
* Never creates, alters, drops, or writes to the database

The dashboard never touches INSERT, UPDATE, DELETE, ALTER, or DROP.
"""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional


class DatabaseUnavailable(Exception):
    """The bot database cannot be opened for reading."""

    def __init__(self, reason: str, db_path: str):
        super().__init__(reason)
        self.reason = reason
        self.db_path = db_path


def _available_columns(conn: sqlite3.Connection, table: str) -> dict[str, str]:
    """Return {column: type} for an existing table."""
    try:
        rows = conn.execute(
            f"PRAGMA table_info({table})"
        ).fetchall()
        return {r[1]: r[2].upper() for r in rows}
    except sqlite3.Error:
        return {}


def _safe_select(columns: list[str], available: dict[str, str]) -> str:
    """Build a SELECT column list only from columns that exist."""
    out = []
    for col in columns:
        if col in available:
            out.append(col)
    if not out:
        out = ["NULL AS _empty"]
    return ", ".join(out)


def _connect(db_path: str, timeout: float) -> sqlite3.Connection:
    """Open a read-only SQLite connection."""
    if not os.path.exists(db_path):
        raise DatabaseUnavailable("Database file not found", db_path)
    if not os.path.isfile(db_path):
        raise DatabaseUnavailable("Database path is not a file", db_path)

    uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 2000")
    return conn


class ReadOnlyDatabase:
    """Read-only accessor for the bot's SQLite database."""

    TABLE = "signals"

    # Displayed columns in order across dashboard panels.
    SIGNAL_COLUMNS = [
        "signal_id", "symbol", "direction", "signal_type",
        "created_at", "trigger_candle_time",
        "entry_low", "entry_high", "stop_loss", "tp1", "tp2",
        "score", "htf_bias", "adx_value", "rsi_value", "volume_ratio",
        "status", "tp1_hit_time", "tp2_hit_time", "sl_hit_time",
        "expiration_time", "delivery_status", "delivery_attempts",
        "last_delivery_error", "tp1_notified", "tp2_notified",
        "sl_notified", "outcome_attempts", "last_outcome_error",
        "last_evaluated_candle_time", "tp1_outcome_attempts",
        "tp2_outcome_attempts", "sl_outcome_attempts",
    ]

    def __init__(self, db_path: str, timeout: float = 2.0):
        self.db_path = db_path
        self.timeout = timeout
        self._columns: dict[str, str] = {}
        self._available: bool = False
        self._unavailable_reason: Optional[str] = None

    def connect(self) -> sqlite3.Connection:
        """Open and validate a read-only connection."""
        try:
            conn = _connect(self.db_path, self.timeout)
        except sqlite3.OperationalError as exc:
            self._available = False
            self._unavailable_reason = str(exc)
            raise DatabaseUnavailable(str(exc), self.db_path) from exc

        cols = _available_columns(conn, self.TABLE)
        self._columns = cols
        self._available = bool(cols)
        self._unavailable_reason = None
        return conn

    @contextmanager
    def transaction(self):
        """Yield a read-only connection. Closes on exit."""
        conn = self.connect()
        try:
            yield conn
        finally:
            try:
                conn.close()
            except Exception:
                pass

    @property
    def is_available(self) -> bool:
        """True when the database can be opened and the signals table exists."""
        if self._available:
            return True
        try:
            self.connect()
            return True
        except Exception:
            return False

    @property
    def unavailable_reason(self) -> Optional[str]:
        return self._unavailable_reason

    def _rows_to_dicts(self, rows) -> list[dict]:
        return [dict(r) for r in rows]

    def fetch_signals(self, limit: int = 20, offset: int = 0,
                      symbol: Optional[str] = None,
                      status: Optional[str] = None) -> list[dict]:
        """Fetch signals sorted newest first."""
        limit = max(0, int(limit))
        offset = max(0, int(offset))
        cols = _safe_select(self.SIGNAL_COLUMNS, self._columns)
        conditions = []
        params: list = []
        if symbol and "symbol" in self._columns:
            conditions.append("symbol = ?")
            params.append(symbol)
        if status and "status" in self._columns:
            conditions.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        sql = f"SELECT {cols} FROM {self.TABLE}{where} ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self.transaction() as conn:
            rows = conn.execute(sql, params).fetchall()
        return self._rows_to_dicts(rows)

    def fetch_latest_per_symbol(self) -> dict[str, dict]:
        """Return the newest signal for each configured symbol."""
        cols = _safe_select(self.SIGNAL_COLUMNS, self._columns)
        if "symbol" not in self._columns or "created_at" not in self._columns:
            return {}
        sql = (
            f"SELECT {cols} FROM {self.TABLE} s "
            "WHERE s.created_at = ("
            "  SELECT MAX(created_at) FROM signals WHERE symbol = s.symbol"
            ") "
            "ORDER BY s.created_at DESC"
        )
        with self.transaction() as conn:
            rows = conn.execute(sql).fetchall()
        return {r["symbol"]: dict(r) for r in rows}

    def fetch_outcome_events(self, limit: int = 30) -> list[dict]:
        """Fetch signals that reached a terminal outcome, newest first."""
        limit = max(0, int(limit))
        cols = _safe_select(self.SIGNAL_COLUMNS, self._columns)
        where = ""
        params: list = []
        if "status" in self._columns:
            where = "WHERE status IN (?, ?, ?, ?)"
            params = ["TP1_HIT", "TP2_HIT", "STOPPED", "EXPIRED"]
        sql = f"SELECT {cols} FROM {self.TABLE} {where} ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.transaction() as conn:
            rows = conn.execute(sql, params).fetchall()
        return self._rows_to_dicts(rows)

    def fetch_activity(self, limit: int = 30) -> list[dict]:
        """Return all signals newest first for the activity feed."""
        return self.fetch_signals(limit=limit, offset=0)

    def count_by_status(self) -> dict[str, int]:
        """Counts per signal status."""
        if "status" not in self._columns:
            return {}
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM signals GROUP BY status"
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    def count_by_direction(self) -> dict[str, int]:
        """Counts per direction."""
        if "direction" not in self._columns:
            return {}
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT direction, COUNT(*) AS cnt FROM signals GROUP BY direction"
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    def count_total(self) -> int:
        """Total signals persisted."""
        with self.transaction() as conn:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM signals").fetchone()
        return row[0] if row else 0

    def delivery_counts(self) -> dict:
        """Delivery health metrics."""
        result = {"delivered": 0, "pending": 0, "failed": 0, "total_attempts": 0}
        if "delivery_status" not in self._columns or "delivery_attempts" not in self._columns:
            return result
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT delivery_status, COUNT(*), COALESCE(SUM(delivery_attempts), 0) "
                "FROM signals GROUP BY delivery_status"
            ).fetchall()
        for status, cnt, attempts in rows:
            key = status.lower() if status else "unknown"
            result[key] = cnt
            result["total_attempts"] += int(attempts)
        result["failed"] = result.get("failed", 0)
        return result

    def last_outcome_error_value(self) -> Optional[str]:
        """Return the most recent last_outcome_error value."""
        if "last_outcome_error" not in self._columns:
            return None
        try:
            with self.transaction() as conn:
                row = conn.execute(
                    "SELECT last_outcome_error FROM signals "
                    "WHERE last_outcome_error IS NOT NULL AND last_outcome_error != '' "
                    "ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
            return row[0] if row else None
        except sqlite3.Error:
            return None

    def last_delivery_error(self, limit: int = 10) -> list[dict]:
        """Recent non-null delivery errors."""
        if "last_delivery_error" not in self._columns:
            return []
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT signal_id, last_delivery_error, delivery_status, "
                "created_at, delivery_attempts "
                "FROM signals WHERE last_delivery_error IS NOT NULL AND last_delivery_error != '' "
                "ORDER BY created_at DESC LIMIT ?", [limit]
            ).fetchall()
        return self._rows_to_dicts(rows)

    def outcome_attempts(self) -> dict:
        """Outcome monitoring attempt counters."""
        keys = ["outcome_attempts", "tp1_outcome_attempts",
                "tp2_outcome_attempts", "sl_outcome_attempts"]
        available = set(self._columns.keys())
        if not any(k in available for k in keys):
            return {}
        select = ", ".join(k for k in keys if k in available) or "0"
        with self.transaction() as conn:
            row = conn.execute(f"SELECT MAX({select}) FROM signals").fetchone()
        if not row:
            return {}
        return dict(zip([k for k in keys if k in available], row))

    def last_evaluated_candle(self) -> list[dict]:
        """Most recent evaluated candle timestamps."""
        if "last_evaluated_candle_time" not in self._columns:
            return []
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT symbol, last_evaluated_candle_time "
                "FROM signals WHERE last_evaluated_candle_time IS NOT NULL "
                "ORDER BY last_evaluated_candle_time DESC LIMIT 20"
            ).fetchall()
        return self._rows_to_dicts(rows)

    def score_distribution(self) -> dict[str, int]:
        """Bucket signals by score range."""
        if "score" not in self._columns:
            return {}
        bucket_specs = [
            ("bucket_0_59", "score BETWEEN 0 AND 59"),
            ("bucket_60_69", "score BETWEEN 60 AND 69"),
            ("bucket_70_79", "score BETWEEN 70 AND 79"),
            ("bucket_80_89", "score BETWEEN 80 AND 89"),
            ("bucket_90_100", "score BETWEEN 90 AND 100"),
        ]
        exprs = [
            f"SUM(CASE WHEN {expr} THEN 1 ELSE 0 END) AS {alias}"
            for alias, expr in bucket_specs
        ]
        sql = f"SELECT {', '.join(exprs)} FROM {self.TABLE}"
        with self.transaction() as conn:
            row = conn.execute(sql).fetchone()
        if not row:
            return {}
        labels = [alias.replace("bucket_", "").replace("_", "-") for alias, _ in bucket_specs]
        return dict(zip(labels, row))

    def get_meta(self, key: str) -> Optional[str]:
        """Read a persisted metadata value."""
        try:
            with self.transaction() as conn:
                row = conn.execute(
                    "SELECT value FROM bot_meta WHERE key = ?", (key,)
                ).fetchone()
            return row[0] if row else None
        except sqlite3.Error:
            return None

    def get_meta_all(self) -> dict:
        """Return all metadata as a dict."""
        try:
            with self.transaction() as conn:
                rows = conn.execute("SELECT key, value FROM bot_meta").fetchall()
            return {r[0]: r[1] for r in rows}
        except sqlite3.Error:
            return {}

    def latest_signal_time(self) -> Optional[str]:
        """Newest signal timestamp."""
        try:
            with self.transaction() as conn:
                row = conn.execute("SELECT MAX(created_at) FROM signals").fetchone()
            return row[0] if row else None
        except sqlite3.Error:
            return None

    def last_evaluated_time(self) -> Optional[str]:
        """Newest evaluated candle time."""
        if "last_evaluated_candle_time" not in self._columns:
            return None
        try:
            with self.transaction() as conn:
                row = conn.execute("SELECT MAX(last_evaluated_candle_time) FROM signals").fetchone()
            return row[0] if row else None
        except sqlite3.Error:
            return None

    def bot_uptime_epoch(self) -> Optional[str]:
        """Persisted bot boot epoch."""
        return self.get_meta("bot_booted_at_epoch")
