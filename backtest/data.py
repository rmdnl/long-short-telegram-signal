"""
PHASE 4: Historical OHLCV dataset + data-quality reporting.

A `HistoricalDataset` holds, per symbol, three candle lists (1H / 15M / 5M).
Loaders accept a list of dict rows, a CSV file, or JSON. All timestamps are
parsed to timezone-aware UTC. Major data problems (gaps, duplicates,
unordered, invalid candles) are REPORTED, not silently repaired; minor
duplicate / order normalization is acceptable and reported as such.

No strategy logic lives here — pure data.
"""
import csv
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Data quality report
# ---------------------------------------------------------------------------

@dataclass
class DataQualityReport:
    """Per-timeframe data-quality findings for one symbol."""
    symbol: str
    tf: str
    total_candles: int
    missing_candles: int = 0        # time gaps beyond expected interval
    duplicate_candles: int = 0
    unordered_candles: int = 0
    invalid_candles: int = 0
    normalized: bool = False        # minor dup/order was applied
    major_problem: bool = False      # gap / invalid -> backtest should NOT use blindly

    def summary(self) -> str:
        if not self.missing_candles and not self.invalid_candles:
            return f"{self.symbol} {self.tf}: {self.total_candles} candles, OK"
        parts = [f"{self.symbol} {self.tf}: {self.total_candles} candles"]
        if self.missing_candles:
            parts.append(f"missing={self.missing_candles}")
        if self.duplicate_candles:
            parts.append(f"duplicate={self.duplicate_candles}")
        if self.unordered_candles:
            parts.append(f"unordered={self.unordered_candles}")
        if self.invalid_candles:
            parts.append(f"invalid={self.invalid_candles}")
        if self.normalized:
            parts.append("normalized")
        if self.major_problem:
            parts.append("MAJOR_PROBLEM")
        return "; ".join(parts)


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------

@dataclass
class HistoricalDataset:
    """Per-symbol multi-timeframe historical candles + quality reports."""
    symbol: str
    candles: Dict[str, List]          # {"1h": [...], "15m": [...], "5m": [...]}
    reports: List[DataQualityReport] = field(default_factory=list)

    def tf(self, tf: str) -> List:
        return self.candles.get(tf, [])

    def quality_report(self) -> List[str]:
        return [r.summary() for r in self.reports]


# ---------------------------------------------------------------------------
# Timeframe seconds (mirror data_validation but importable without app)
# ---------------------------------------------------------------------------

_TF_SECONDS = {"5m": 300, "15m": 900, "1h": 3600}


def _to_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_ts(value) -> datetime:
    """Parse ISO string, unix ms, or datetime to tz-aware UTC."""
    if isinstance(value, datetime):
        return _to_aware(value)
    if isinstance(value, (int, float)):
        # milliseconds (Binance klines convention)
        return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
    s = str(value).strip()
    try:
        # Accept '...Z' or offset
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return _to_aware(datetime.fromisoformat(s))
    except ValueError:
        # Fallback: unix seconds
        return datetime.fromtimestamp(float(s), tz=timezone.utc)


def _build_candle(row: dict, tf: str):
    """Build an app.models.Candle from a dict row; returns (candle, valid)."""
    from app.models import Candle

    try:
        ts = _parse_ts(row["timestamp"])
        open_ = Decimal(str(row["open"]))
        high = Decimal(str(row["high"]))
        low = Decimal(str(row["low"]))
        close = Decimal(str(row["close"]))
        volume = Decimal(str(row.get("volume", 0)))
    except (KeyError, InvalidOperation, TypeError):
        return None, False

    # validity checks
    for v in (open_, high, low, close, volume):
        if v.is_nan() or v.is_infinite():
            return None, False
    if open_ < 0 or high < 0 or low < 0 or close < 0:
        return None, False
    if volume < 0:
        return None, False
    if high < low:
        return None, False
    if close < low or close > high:
        return None, False
    if open_ < low or open_ > high:
        return None, False

    candle = Candle(ts, open_, high, low, close, volume)
    return candle, True


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _quality_report(symbol: str, tf: str, raw_ts: List[datetime],
                    candles: List, invalid_count: int) -> DataQualityReport:
    """Compute the data-quality report for one timeframe."""
    if not raw_ts:
        return DataQualityReport(symbol, tf, 0, major_problem=False)

    n = len(raw_ts)
    tf_sec = _TF_SECONDS[tf]
    expected_total = n

    # duplicates (raw, pre-normalize)
    uniq_ts = set(raw_ts)
    duplicates = n - len(uniq_ts)

    # unordered count in the raw sequence
    unordered = sum(
        1 for i in range(1, n)
        if raw_ts[i] < raw_ts[i - 1]
    )

    # time gaps: on the normalized (dedup+sorted) sequence
    normalized = sorted(uniq_ts)
    gaps = 0
    for i in range(1, len(normalized)):
        delta = (normalized[i] - normalized[i - 1]).total_seconds()
        if delta > tf_sec + 1e-6:
            gaps += int(round(delta / tf_sec)) - 1

    major = gaps > 0 or invalid_count > 0
    return DataQualityReport(
        symbol=symbol,
        tf=tf,
        total_candles=len(candles),
        missing_candles=gaps,
        duplicate_candles=duplicates,
        unordered_candles=unordered,
        invalid_candles=invalid_count,
        normalized=(duplicates > 0 or unordered > 0),
        major_problem=major,
    )


def _load_tf(symbol: str, tf: str, rows: List[dict]):
    """Build normalized candles + a DataQualityReport for one timeframe."""
    raw_ts: List[datetime] = []
    candles: List = []
    invalid = 0
    seen: set = set()

    for row in rows:
        try:
            ts = _parse_ts(row["timestamp"])
        except (KeyError, TypeError, ValueError):
            ts = None
        candle, valid = _build_candle(row, tf)
        if not valid or ts is None:
            invalid += 1
            continue
        raw_ts.append(ts)
        if ts in seen:
            continue          # keep-first dedup (minor normalization)
        seen.add(ts)
        candles.append(candle)

    candles.sort(key=lambda c: c.timestamp)
    report = _quality_report(symbol, tf, raw_ts, candles, invalid)
    return candles, report


def load_rows(dataset_rows: dict, symbol: str) -> HistoricalDataset:
    """
    Build a HistoricalDataset from nested rows.

    Args:
        dataset_rows: {"1h": [ {timestamp,open,high,low,close,volume}, ... ],
                       "15m": [...], "5m": [...]}
        symbol: symbol name

    Returns:
        HistoricalDataset with normalized candles + quality reports.
    """
    from app.models import Candle  # noqa: F401  (ensures import)

    candles: Dict[str, List] = {}
    reports: List[DataQualityReport] = []
    for tf in ("1h", "15m", "5m"):
        rows = dataset_rows.get(tf, []) or []
        c, r = _load_tf(symbol, tf, rows)
        candles[tf] = c
        reports.append(r)

    return HistoricalDataset(symbol=symbol, candles=candles, reports=reports)


def load_csv(csv_dir: str, symbol: str) -> HistoricalDataset:
    """
    Load a symbol's dataset from CSV files:
        {csv_dir}/{symbol}_1h.csv, {symbol}_15m.csv, {symbol}_5m.csv

    Each CSV must have headers: timestamp,open,high,low,close,volume
    """
    def _read(path: str) -> List[dict]:
        if not os.path.exists(path):
            return []
        with open(path, newline="") as fh:
            return list(csv.DictReader(fh))

    rows = {
        "1h": _read(os.path.join(csv_dir, f"{symbol}_1h.csv")),
        "15m": _read(os.path.join(csv_dir, f"{symbol}_15m.csv")),
        "5m": _read(os.path.join(csv_dir, f"{symbol}_5m.csv")),
    }
    return load_rows(rows, symbol)


def load_csv_dir(data_dir: str, symbol: str) -> HistoricalDataset:
    """
    Load a symbol's dataset from the downloader layout:
        {data_dir}/{symbol}/1h.csv, {data_dir}/{symbol}/15m.csv, {data_dir}/{symbol}/5m.csv

    Each CSV must have headers: timestamp,open,high,low,close,volume
    """
    def _read(path: str) -> List[dict]:
        if not os.path.exists(path):
            return []
        with open(path, newline="") as fh:
            return list(csv.DictReader(fh))

    rows = {
        "1h": _read(os.path.join(data_dir, symbol, "1h.csv")),
        "15m": _read(os.path.join(data_dir, symbol, "15m.csv")),
        "5m": _read(os.path.join(data_dir, symbol, "5m.csv")),
    }
    return load_rows(rows, symbol)


def load_json(json_path: str, symbol: str) -> HistoricalDataset:
    """
    Load a symbol's dataset from JSON:
        {json_path}: {"1h": [ {timestamp,...}, ...], "15m": [...], "5m": [...]}
    """
    with open(json_path) as fh:
        data = json.load(fh)
    if symbol in data and isinstance(data[symbol], dict):
        data = data[symbol]
    return load_rows(data, symbol)
