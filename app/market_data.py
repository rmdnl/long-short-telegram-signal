from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional, Dict, Any
import time

import requests

from app.models import Candle
from app.logger import get_logger

logger = get_logger(__name__)


class MarketDataError(Exception):
    """Raised when market data operations fail"""
    pass


class RateLimitError(MarketDataError):
    """Raised when a 429 response is received (rate limited)."""
    pass


class TransientError(MarketDataError):
    """Raised on transient network / 5xx errors (safe to retry)."""
    pass


class BinanceMarketData:
    """Binance public market data fetcher (read-only, no trading endpoints)."""

    BASE_URL = "https://api.binance.com"

    def __init__(
        self,
        base_url: str = "https://api.binance.com",
        request_timeout_seconds: float = 10.0,
        max_retries: int = 3,
        backoff_base_seconds: float = 1.0,
        max_backoff_seconds: float = 60.0,
        min_request_interval_seconds: float = 0.15,
        pacing_enabled: bool = True,
    ):
        self.base_url = base_url
        self.request_timeout_seconds = request_timeout_seconds
        self.max_retries = max_retries
        self.backoff_base_seconds = backoff_base_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.min_request_interval_seconds = min_request_interval_seconds
        self.pacing_enabled = pacing_enabled

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "crypto-signal-bot/1.0"})
        self._last_request_ts = 0.0

    # ------------------------------------------------------------------
    # Fetching
    # ------------------------------------------------------------------
    def fetch_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 500,
    ) -> List[Candle]:
        """
        Fetch OHLCV klines from Binance (public, read-only).

        Retry policy:
        - transient 5xx / network errors: exponential backoff up to max_retries
        - 429: honor Retry-After when present, else backoff, then retry

        Raises:
            MarketDataError on non-transient HTTP errors / bad payload
            RateLimitError after exhausting 429 retries
            TransientError after exhausting transient retries
        """
        if limit <= 0 or limit > 1000:
            raise MarketDataError("limit must be between 1 and 1000")

        endpoint = f"{self.base_url}/api/v3/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}

        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            self._pace()
            try:
                response = self.session.get(
                    endpoint,
                    params=params,
                    timeout=self.request_timeout_seconds,
                )

                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    delay = self._retry_after_delay(retry_after)
                    last_error = RateLimitError(f"429 rate limited on {symbol} {interval}")
                    logger.warning(f"429 from Binance; backing off {delay:.1f}s")
                    self._sleep(delay)
                    continue

                if 500 <= response.status_code < 600:
                    delay = self._backoff_delay(attempt)
                    last_error = TransientError(
                        f"HTTP {response.status_code} on {symbol} {interval}"
                    )
                    logger.warning(f"5xx from Binance; backing off {delay:.1f}s")
                    self._sleep(delay)
                    continue

                response.raise_for_status()
                data = response.json()
                if not data:
                    raise MarketDataError(f"No klines data for {symbol} {interval}")
                candles = self._parse_klines(data)
                logger.debug(f"Fetched {len(candles)} candles for {symbol} {interval}")
                return candles

            except requests.exceptions.RequestException as e:
                # Network-level failure: transient, retry with backoff
                delay = self._backoff_delay(attempt)
                last_error = TransientError(f"Network error on {symbol} {interval}: {e}")
                logger.warning(f"Transient network error; backing off {delay:.1f}s")
                self._sleep(delay)
                continue

        # Exhausted retries
        if isinstance(last_error, RateLimitError):
            raise last_error
        raise last_error or TransientError(f"Failed to fetch {symbol} {interval}")

    def fetch_ticker(self, symbol: str) -> Dict[str, Any]:
        """Fetch 24h ticker data (public)."""
        endpoint = f"{self.base_url}/api/v3/ticker/24hr"
        params = {"symbol": symbol}

        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            self._pace()
            try:
                response = self.session.get(
                    endpoint,
                    params=params,
                    timeout=self.request_timeout_seconds,
                )
                if 400 <= response.status_code < 500:
                    raise MarketDataError(
                        f"HTTP {response.status_code} ticker {symbol}: {response.text[:200]}"
                    )
                if 500 <= response.status_code < 600 or response.status_code == 429:
                    delay = self._backoff_delay(attempt)
                    last_error = TransientError(f"HTTP {response.status_code} ticker {symbol}")
                    self._sleep(delay)
                    continue
                response.raise_for_status()
                return response.json()
            except requests.exceptions.RequestException as e:
                delay = self._backoff_delay(attempt)
                last_error = TransientError(f"Network error ticker {symbol}: {e}")
                self._sleep(delay)
                continue

        raise last_error or TransientError(f"Failed to fetch ticker {symbol}")

    # ------------------------------------------------------------------
    # Parsing & validation
    # ------------------------------------------------------------------
    def _parse_klines(self, raw_data: List) -> List[Candle]:
        """Parse Binance klines response into Candle objects."""
        candles = []
        for item in raw_data:
            # [open_time, open, high, low, close, volume, close_time, ...]
            timestamp_ms = int(item[0])
            timestamp = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
            candles.append(Candle(
                timestamp=timestamp,
                open=Decimal(str(item[1])),
                high=Decimal(str(item[2])),
                low=Decimal(str(item[3])),
                close=Decimal(str(item[4])),
                volume=Decimal(str(item[5])),
            ))
        return candles

    def validate_candles(self, candles: List[Candle], tf: str = "5m",
                         min_history: int = 2) -> None:
        """
        Structural validation (ordering / duplicates / OHLC / negative values).
        Warmup requirements are enforced separately by the scanner via
        app.data_validation.min_history_for_indicators.
        """
        from app.data_validation import DataValidationError
        if not candles:
            raise MarketDataError("Empty candles list")
        seen = set()
        for c in candles:
            if c.timestamp in seen:
                raise MarketDataError("Duplicate candle timestamps detected")
            seen.add(c.timestamp)
        for i in range(1, len(candles)):
            if candles[i].timestamp <= candles[i - 1].timestamp:
                raise MarketDataError("Candles not sorted by timestamp")
        for i, c in enumerate(candles):
            if c.high < c.low:
                raise MarketDataError(f"Candle {i}: high < low")
            if c.high < c.open or c.high < c.close:
                raise MarketDataError(f"Candle {i}: high is not highest")
            if c.low > c.open or c.low > c.close:
                raise MarketDataError(f"Candle {i}: low is not lowest")
            if c.volume < 0:
                raise MarketDataError(f"Candle {i}: negative volume")

    def check_data_freshness(self, candles: List[Candle], tf: str = "5m",
                             max_age_seconds: int = 30) -> None:
        """
        Check candle data freshness using the LATEST CANDLE'S CLOSE TIME.

        Distinguishes API response time from candle close time.
        """
        from app.data_validation import candle_close_time, DataValidationError, validate_series
        if not candles:
            raise MarketDataError("No candles to check freshness")
        now = datetime.now(timezone.utc)
        try:
            validate_series(candles, tf, min_history=1, now=now, max_age_seconds=max_age_seconds)
        except DataValidationError as e:
            reason = str(e)
            if reason.startswith("STALE"):
                latest_close = candle_close_time(candles[-1], tf)
                age = (now - latest_close).total_seconds()
                raise MarketDataError(
                    f"Data stale: latest closed candle is {age:.0f}s old (max {max_age_seconds}s)"
                )
            raise MarketDataError(f"Freshness check failed: {reason}")

    # ------------------------------------------------------------------
    # Pacing & backoff internals
    # ------------------------------------------------------------------
    def _pace(self) -> None:
        """Ensure at least min_request_interval_seconds between requests."""
        if not self.pacing_enabled or self.min_request_interval_seconds <= 0:
            return
        elapsed = time.monotonic() - self._last_request_ts
        wait = self.min_request_interval_seconds - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request_ts = time.monotonic()

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff: base * 2**attempt, capped."""
        delay = self.backoff_base_seconds * (2 ** attempt)
        return min(delay, self.max_backoff_seconds)

    def _retry_after_delay(self, retry_after: Optional[str]) -> float:
        """Honor Retry-After header if present; else use a modest backoff."""
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
        return self.backoff_base_seconds * 5

    def _sleep(self, seconds: float) -> None:
        """Centralized sleep for pacing/testability."""
        if seconds > 0:
            time.sleep(seconds)


def normalize_candles(candles: List[Candle]) -> List[Candle]:
    """
    Normalize candle list (dedupe + sort ascending).
    Kept as module-level alias for backward compatibility.
    """
    from app.data_validation import normalize_candles as _norm
    return _norm(candles)
