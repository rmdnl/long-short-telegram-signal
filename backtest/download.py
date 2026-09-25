"""
PHASE 5: Binance historical OHLCV downloader.

Downloads klines from the public Binance API (no API key, no trading endpoints).
Saves to CSV files compatible with backtest/data.py.

Capabilities:
- Paginate historical candles (Binance limit=1000 per request)
- Retry with exponential backoff (429 / 5xx / network errors)
- Rate-limit pacing
- Validate response shape
- Deduplicate + sort
- Save to CSV: data/{SYMBOL}/{1h,15m,5m}.csv
"""
import csv
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# Binance public REST endpoint (read-only, no auth required)
BINANCE_BASE_URL = "https://api.binance.com"

# Max candles per request (Binance hard limit)
_KLINE_LIMIT = 1000

# Timeframe -> seconds
_TF_SECONDS: Dict[str, int] = {
    "5m": 300,
    "15m": 900,
    "1h": 3600,
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class DownloadError(Exception):
    """Raised when a download fails irrecoverably."""
    pass


class RateLimitError(DownloadError):
    """Raised when 429 retries are exhausted."""
    pass


class TransientError(DownloadError):
    """Raised when transient (5xx / network) retries are exhausted."""
    pass


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class DownloadStats:
    """Per-symbol, per-timeframe download statistics."""
    symbol: str
    tf: str
    requests_made: int = 0
    candles_fetched: int = 0
    candles_unique: int = 0
    duplicates_removed: int = 0
    gaps_detected: int = 0
    retries: int = 0
    start_dt: Optional[datetime] = None
    end_dt: Optional[datetime] = None

    def summary(self) -> str:
        s = f"{self.symbol} {self.tf}: {self.candles_unique} candles "
        s += f"({self.requests_made} reqs, {self.duplicates_removed} dups removed)"
        if self.gaps_detected:
            s += f", {self.gaps_detected} gaps"
        s += f" [{self.start_dt} -> {self.end_dt}]"
        return s


@dataclass
class DatasetStats:
    """Aggregate stats across all symbols and timeframes."""
    symbols: List[str] = field(default_factory=list)
    timeframes: List[str] = field(default_factory=list)
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    per_tf: List[DownloadStats] = field(default_factory=list)
    total_candles: int = 0
    total_requests: int = 0

    def summary_lines(self) -> List[str]:
        lines = [
            f"Dataset: {', '.join(self.symbols)} x {', '.join(self.timeframes)}",
            f"Period: {self.start} -> {self.end}",
            f"Total candles: {self.total_candles}, total requests: {self.total_requests}",
        ]
        for s in self.per_tf:
            lines.append(f"  {s.summary()}")
        return lines


# ---------------------------------------------------------------------------
# Binance session helper
# ---------------------------------------------------------------------------

class _BinanceSession:
    """Thin wrapper around requests.Session with pacing + retry."""

    def __init__(
        self,
        base_url: str = BINANCE_BASE_URL,
        timeout: float = 15.0,
        max_retries: int = 4,
        backoff_base: float = 2.0,
        backoff_max: float = 60.0,
        min_interval: float = 0.2,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.min_interval = min_interval
        self._last_ts = 0.0
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "crypto-signal-bot/1.0"})
        self.retries_used = 0

    def _pace(self) -> None:
        elapsed = time.monotonic() - self._last_ts
        wait = self.min_interval - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_ts = time.monotonic()

    def _backoff(self, attempt: int) -> float:
        return min(self.backoff_base * (2 ** attempt), self.backoff_max)

    def get_klines(
        self,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
        limit: int = _KLINE_LIMIT,
    ) -> List[list]:
        """
        Fetch one page of klines.
        Returns raw list of lists (each: [open_time, open, high, low, close,
        volume, close_time, ...]).
        Raises RateLimitError / TransientError after exhausting retries.
        """
        url = f"{self.base_url}/api/v3/klines"
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": min(limit, _KLINE_LIMIT),
        }

        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            self._pace()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)

                if resp.status_code == 429:
                    self.retries_used += 1
                    retry_after = resp.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after else self._backoff(attempt) * 5
                    logger.warning(f"429 on {symbol} {interval}; backing off {delay:.1f}s")
                    time.sleep(delay)
                    last_exc = RateLimitError(f"429 rate limited on {symbol} {interval}")
                    continue

                if 500 <= resp.status_code < 600:
                    self.retries_used += 1
                    delay = self._backoff(attempt)
                    logger.warning(f"HTTP {resp.status_code} on {symbol} {interval}; retry in {delay:.1f}s")
                    time.sleep(delay)
                    last_exc = TransientError(f"HTTP {resp.status_code}")
                    continue

                resp.raise_for_status()
                data = resp.json()
                # Binance returns [] when range is out of bounds (no data)
                if not isinstance(data, list):
                    raise DownloadError(f"Unexpected klines response type: {type(data)}")
                return data

            except requests.exceptions.RequestException as e:
                self.retries_used += 1
                delay = self._backoff(attempt)
                logger.warning(f"Network error on {symbol} {interval}: {e}; retry in {delay:.1f}s")
                time.sleep(delay)
                last_exc = TransientError(f"Network error: {e}")
                continue

        # Exhausted
        if isinstance(last_exc, RateLimitError):
            raise last_exc
        raise last_exc or TransientError("Download failed with no error")


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def _dt_to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def fetch_range(
    session: _BinanceSession,
    symbol: str,
    tf: str,
    start: datetime,
    end: datetime,
) -> Tuple[List[dict], DownloadStats]:
    """
    Fetch all candles for symbol/tf in [start, end] via pagination.

    Returns (rows, stats) where each row is:
        {"timestamp": datetime, "open": str, "high": str, "low": str,
         "close": str, "volume": str}
    """
    tf_sec = _TF_SECONDS[tf]
    tf_api = tf  # Binance uses same tokens: 5m, 15m, 1h

    start_ms = _dt_to_ms(start)
    end_ms = _dt_to_ms(end)

    stats = DownloadStats(symbol=symbol, tf=tf, start_dt=start, end_dt=end)
    all_candles: Dict[int, dict] = {}  # open_time_ms -> row

    # Compute total number of candles expected in range, then page through
    total_ms = end_ms - start_ms + 1  # inclusive
    total_candles_expected = max(1, (total_ms + tf_sec * 1000 - 1) // (tf_sec * 1000))
    pages_needed = (total_candles_expected + _KLINE_LIMIT - 1) // _KLINE_LIMIT

    for page_idx in range(pages_needed):
        page_start_ms = start_ms + page_idx * _KLINE_LIMIT * tf_sec * 1000
        if page_start_ms > end_ms:
            break
        page_end_ms = min(page_start_ms + _KLINE_LIMIT * tf_sec * 1000 - 1, end_ms)

        raw = session.get_klines(symbol, tf_api, page_start_ms, page_end_ms)
        stats.requests_made += 1

        if not raw:
            continue

        for k in raw:
            open_ms = int(k[0])
            # Only include candles within [start_ms, end_ms]
            if open_ms < start_ms or open_ms > end_ms:
                continue
            row = {
                "timestamp": _ms_to_dt(open_ms),
                "open": str(k[1]),
                "high": str(k[2]),
                "low": str(k[3]),
                "close": str(k[4]),
                "volume": str(k[5]),
            }
            all_candles[open_ms] = row
            stats.candles_fetched += 1

    # Deduplicate (all_candles dict already dedupes by open_time)
    stats.duplicates_removed = stats.candles_fetched - len(all_candles)
    stats.candles_unique = len(all_candles)

    # Detect gaps
    if all_candles:
        sorted_ts = sorted(all_candles.keys())
        expected = int((sorted_ts[-1] - sorted_ts[0]) / (tf_sec * 1000)) + 1
        missing = expected - len(sorted_ts)
        stats.gaps_detected = max(0, missing)

    # Build sorted rows
    rows = [all_candles[ms] for ms in sorted(all_candles.keys())]
    return rows, stats


# ---------------------------------------------------------------------------
# Save / Load
# ---------------------------------------------------------------------------

_CSV_HEADER = ["timestamp", "open", "high", "low", "close", "volume"]


def save_csv(rows: List[dict], symbol: str, tf: str, data_dir: str) -> str:
    """Save rows to {data_dir}/{SYMBOL}/{tf}.csv. Returns the path."""
    sym_dir = os.path.join(data_dir, symbol)
    os.makedirs(sym_dir, exist_ok=True)
    path = os.path.join(sym_dir, f"{tf}.csv")
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_HEADER, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            # timestamp as ISO (UTC)
            ts = row["timestamp"]
            if isinstance(ts, datetime):
                row = {**row, "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ")}
            writer.writerow(row)
    return path


def load_csv_rows(path: str) -> List[dict]:
    """Load a saved CSV back into rows with datetime timestamps."""
    with open(path, newline="") as fh:
        raw = list(csv.DictReader(fh))
    for row in raw:
        ts = row["timestamp"]
        if isinstance(ts, str):
            if ts.endswith("Z"):
                ts = ts[:-1] + "+00:00"
            row["timestamp"] = datetime.fromisoformat(ts)
    return raw


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def download_symbol(
    symbol: str,
    timeframes: List[str],
    start: datetime,
    end: datetime,
    data_dir: str = "data",
    session: Optional[_BinanceSession] = None,
) -> List[DownloadStats]:
    """
    Download all timeframes for one symbol and save CSVs.

    Args:
        symbol: e.g. "BTCUSDT"
        timeframes: ["1h", "15m", "5m"]
        start: tz-aware UTC datetime
        end: tz-aware UTC datetime
        data_dir: root data directory
        session: optional pre-built session (for testing)

    Returns:
        One DownloadStats per timeframe.
    """
    sess = session or _BinanceSession()
    stats_list: List[DownloadStats] = []
    for tf in timeframes:
        rows, stats = fetch_range(sess, symbol, tf, start, end)
        path = save_csv(rows, symbol, tf, data_dir)
        logger.info(f"Saved {len(rows)} candles to {path}")
        stats_list.append(stats)
    return stats_list


def download_dataset(
    symbols: List[str],
    timeframes: List[str],
    start: datetime,
    end: datetime,
    data_dir: str = "data",
    session: Optional[_BinanceSession] = None,
) -> DatasetStats:
    """
    Download all symbols x timeframes. Saves CSVs. Returns aggregate stats.
    """
    sess = session or _BinanceSession()
    all_stats: List[DownloadStats] = []
    for sym in symbols:
        tf_stats = download_symbol(sym, timeframes, start, end, data_dir, sess)
        all_stats.extend(tf_stats)

    ds = DatasetStats(
        symbols=symbols,
        timeframes=timeframes,
        start=start,
        end=end,
        per_tf=all_stats,
        total_candles=sum(s.candles_unique for s in all_stats),
        total_requests=sum(s.requests_made for s in all_stats),
    )
    return ds


def cli_main() -> None:
    """
    CLI entry point:
        python -m backtest.download --symbols BTCUSDT,ETHUSDT \
            --timeframes 1h,15m,5m --start 2025-01-01 --end 2025-06-30 \
            --data-dir data
    """
    import argparse

    parser = argparse.ArgumentParser(description="Download Binance historical OHLCV data")
    parser.add_argument("--symbols", required=True, help="Comma-separated, e.g. BTCUSDT,ETHUSDT")
    parser.add_argument("--timeframes", default="1h,15m,5m", help="Comma-separated")
    parser.add_argument("--start", required=True, help="ISO date/datetime UTC, e.g. 2025-01-01")
    parser.add_argument("--end", required=True, help="ISO date/datetime UTC, e.g. 2025-06-30")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    if not args.quiet:
        logging.basicConfig(level=logging.INFO)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    tfs = [t.strip() for t in args.timeframes.split(",") if t.strip()]

    def _parse(s: str) -> datetime:
        s = s.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    start = _parse(args.start)
    end = _parse(args.end)

    print(f"Downloading {len(symbols)} symbols x {len(tfs)} timeframes")
    print(f"Period: {start} -> {end}")
    print(f"Output: {args.data_dir}")

    ds = download_dataset(symbols, tfs, start, end, args.data_dir)

    print("\n=== DOWNLOAD SUMMARY ===")
    for line in ds.summary_lines():
        print(line)


if __name__ == "__main__":
    cli_main()
