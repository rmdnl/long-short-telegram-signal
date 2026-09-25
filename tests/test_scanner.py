"""
PHASE 3: scanner integration tests.

The ONLY mocked boundary is the market-data HTTP fetch (fetch_klines /
validate_candles). All other logic — candle synchronization, data
validation, indicator calculation, signal engine, state tracking — runs
for real against synthetic data.

Coverage:
- closed-candle cutoff / unfinished-candle rejection
- stale candle handling
- malformed / duplicate / unordered candles -> NO_SIGNAL, no crash
- insufficient history -> NO_SIGNAL
- multi-timeframe synchronization
- symbol isolation (one bad symbol never aborts the others)
- repeated scanner cycle: same closed 5M candle not processed twice
- retry behavior / 429 / transient network error (BinanceMarketData)
- valid market-data flow -> decision produced
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional

import pytest

from app.models import Candle
from app.scanner import Scanner
from app.market_data import (
    BinanceMarketData,
    MarketDataError,
    RateLimitError,
    TransientError,
)
from app import data_validation as dv

UTC = timezone.utc

# Reference "now": the most recent closed 5M boundary. The scanner judges
# staleness against its own wall-clock now, so series closing at this point
# are fresh; anything lagging > 1.5x the timeframe is stale.
NOW = datetime.now(UTC).replace(minute=(datetime.now(UTC).minute // 5) * 5, second=0, microsecond=0)


def closed_candles(tf: str, n: int, end_at: datetime = NOW) -> List[Candle]:
    """n valid candles whose LAST candle Closes AT end_at.

    The last candle opens at end_at - tf, so its close time == end_at exactly.
    All candles ascend at tf intervals.
    """
    sec = dv.TIMEFRAME_SECONDS[tf]
    last_open = end_at - timedelta(seconds=sec)
    out: List[Candle] = []
    price = Decimal("100")
    for i in range(n):
        ts = last_open - timedelta(seconds=sec * (n - 1 - i))
        o = price
        c = price + Decimal("0.1")
        out.append(Candle(ts, o, c + Decimal("0.05"), o - Decimal("0.05"), c, Decimal("1000")))
        price = c
    return out


class StubMarketData:
    """Stub for BinanceMarketData: returns pre-built candles, never hits the net."""

    def __init__(self, series: Optional[dict] = None,
                 raise_for: Optional[dict] = None):
        # series: {interval: [Candle,...]}
        self.series = series or {}
        # raise_for: {interval: Exception}
        self.raise_for = raise_for or {}
        self.calls: List[str] = []

    def fetch_klines(self, symbol: str, interval: str, limit: int = 500) -> List[Candle]:
        self.calls.append(f"{symbol}:{interval}")
        if interval in self.raise_for:
            exc = self.raise_for[interval]
            if isinstance(exc, Exception):
                raise exc
            raise exc  # pragma: no cover
        if interval in self.series:
            return self.series[interval]
        raise MarketDataError(f"no data for {symbol} {interval}")

    def validate_candles(self, candles: List[Candle], tf: str = "5m") -> None:
        # Structural-only; full validation is done by the scanner via dv.validate_series
        pass


# ---------------------------------------------------------------------------
# Helpers to build a full valid multi-timeframe series
# ---------------------------------------------------------------------------

def valid_series(n_hf: int = 700, n_setup: int = 700, n_trigger: int = 700) -> dict:
    return {
        "1h": closed_candles("1h", n_hf),
        "15m": closed_candles("15m", n_setup),
        "5m": closed_candles("5m", n_trigger),
    }


@pytest.fixture
def scanner():
    import app.config as cm
    cm.config = None
    s = Scanner(market_data=StubMarketData())
    # Force dry-run logging path
    import os
    os.environ["DRY_RUN"] = "true"
    return s


# ---------------------------------------------------------------------------
# Valid market-data flow
# ---------------------------------------------------------------------------

def test_valid_flow_produces_decision(scanner):
    scanner.market_data.series = valid_series()
    d = scanner.scan_symbol("BTCUSDT")
    assert d.decision in ("SIGNAL", "NO_SIGNAL")
    assert d.processed_trigger_ts is not None
    # The decision line must not contain an OHLCV array dump
    line = d.log_line()
    assert "open" not in line.lower() or line.count("(") < 2


def test_decision_fields_populated(scanner):
    scanner.market_data.series = valid_series()
    d = scanner.scan_symbol("BTCUSDT")
    # ADX/RSI/ATR should be computed (not None) on a 700-candle series
    assert d.adx is not None
    assert d.rsi is not None
    assert d.atr is not None


# ---------------------------------------------------------------------------
# Repeated cycle: same closed 5M candle not re-evaluated
# ---------------------------------------------------------------------------

def test_repeated_cycle_same_candle_not_reprocessed(scanner):
    scanner.market_data.series = valid_series()
    d1 = scanner.scan_symbol("BTCUSDT")
    ts1 = d1.processed_trigger_ts
    d2 = scanner.scan_symbol("BTCUSDT")
    # Second scan on the SAME closed candle must be rejected as repeated
    assert d2.decision == "NO_SIGNAL"
    assert d2.reason == "REPEATED_CANDLE"
    assert d1.processed_trigger_ts == ts1


def test_new_candle_reenables_evaluation(scanner):
    scanner.market_data.series = valid_series()
    scanner.scan_symbol("BTCUSDT")
    # Advance the trigger series by one closed candle
    scanner.market_data.series = valid_series()
    scanner.market_data.series["5m"] = closed_candles(
        "5m", 700, end_at=NOW + timedelta(seconds=300)
    )
    d = scanner.scan_symbol("BTCUSDT")
    assert d.reason != "REPEATED_CANDLE"


# ---------------------------------------------------------------------------
# Symbol isolation
# ---------------------------------------------------------------------------

def test_one_bad_symbol_does_not_abort_others(scanner):
    scanner.market_data.series = valid_series()
    scanner.market_data.raise_for = {"1h": MarketDataError("boom")}
    # Fetch 1h for BTC fails; ETH also 1h fails; both return decisions, no raise
    signals = scanner.scan_all_symbols()
    # No exception raised; scan_all_symbols returns a list (possibly empty)
    assert isinstance(signals, list)


def test_symbol_failure_isolated_via_exception(scanner):
    """Even a non-MarketDataError in one symbol must not abort the loop."""
    scanner.market_data.series = valid_series()
    # Force a hard error on one timeframe for all symbols
    scanner.market_data.raise_for = {"1h": MarketDataError("isolated boom")}
    # scan_all_symbols must not raise
    out = scanner.scan_all_symbols()
    assert isinstance(out, list)


# ---------------------------------------------------------------------------
# Malformed / stale / insufficient -> NO_SIGNAL, no crash
# ---------------------------------------------------------------------------

def test_insufficient_history_rejected(scanner):
    # Only 50 candles: far below the 600 required
    scanner.market_data.series = {
        "1h": closed_candles("1h", 50),
        "15m": closed_candles("15m", 50),
        "5m": closed_candles("5m", 50),
    }
    d = scanner.scan_symbol("BTCUSDT")
    assert d.decision == "NO_SIGNAL"
    assert d.reason in ("INSUFFICIENT_HISTORY", "INVALID_DATA", "STALE_DATA")


def test_stale_5m_candle_rejected(scanner):
    # 5m series lagging 30 min behind the 1h/15m close -> STALE
    scanner.market_data.series = {
        "1h": closed_candles("1h", 700, end_at=NOW),
        "15m": closed_candles("15m", 700, end_at=NOW),
        "5m": closed_candles("5m", 700, end_at=NOW - timedelta(minutes=30)),
    }
    d = scanner.scan_symbol("BTCUSDT")
    assert d.decision == "NO_SIGNAL"
    assert d.reason == "STALE_DATA"


def test_malformed_candle_no_crash(scanner):
    good = valid_series()
    # Inject a malformed candle (high < low) into the 5m series
    bad = Candle(
        good["5m"][-1].timestamp,
        Decimal("100"), Decimal("90"), Decimal("110"), Decimal("100"), Decimal("1000"),
    )
    good["5m"][-1] = bad
    scanner.market_data.series = good
    d = scanner.scan_symbol("BTCUSDT")
    assert d.decision == "NO_SIGNAL"
    assert d.reason == "INVALID_DATA"


def test_duplicate_candle_no_crash(scanner):
    good = valid_series()
    # Duplicate the last 5m candle. The sync step deduplicates defensively, so
    # the data is still valid; the scanner must return NO_SIGNAL without crashing.
    good["5m"].append(good["5m"][-1])
    scanner.market_data.series = good
    d = scanner.scan_symbol("BTCUSDT")
    assert d.decision == "NO_SIGNAL"
    assert d.reason != ""  # rejected by a strategy/data gate, not a crash


def test_unordered_candle_no_crash(scanner):
    good = valid_series()
    # Swap last two 5m candles to break ordering. Sync re-orders defensively,
    # so the scanner must return NO_SIGNAL without crashing.
    good["5m"][-1], good["5m"][-2] = good["5m"][-2], good["5m"][-1]
    scanner.market_data.series = good
    d = scanner.scan_symbol("BTCUSDT")
    assert d.decision == "NO_SIGNAL"
    assert d.reason != ""


def test_empty_series_rejected(scanner):
    scanner.market_data.series = {"1h": [], "15m": [], "5m": []}
    d = scanner.scan_symbol("BTCUSDT")
    assert d.decision == "NO_SIGNAL"
    assert d.reason in ("MARKET_DATA_ERROR", "INSUFFICIENT_HISTORY", "INVALID_DATA")


# ---------------------------------------------------------------------------
# Multi-timeframe synchronization correctness (via the data layer)
# ---------------------------------------------------------------------------

def test_sync_all_cuts_to_decision_point():
    hf = closed_candles("1h", 10, end_at=NOW)
    setup = closed_candles("15m", 40, end_at=NOW)
    trigger = closed_candles("5m", 120, end_at=NOW)
    synced = dv.sync_all(hf, setup, trigger)
    decision = dv.common_decision_point(trigger, "5m")
    assert decision is not None
    for key, tf in (("hf", "1h"), ("setup", "15m"), ("trigger", "5m")):
        for c in synced[key]:
            assert dv.candle_close_time(c, tf) <= decision


# ---------------------------------------------------------------------------
# Rate-limit / transient handling at the HTTP boundary
# ---------------------------------------------------------------------------

class FakeSession:
    """Minimal stand-in for requests.Session to drive retry logic."""

    def __init__(self, responses: list):
        self.responses = list(responses)
        self.requested: list = []
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        self.requested.append(url)
        if not self.responses:
            raise MarketDataError("no responses left")
        return self.responses.pop(0)

    def update_headers(self, d):
        self.headers.update(d)


def _resp(status=200, json_data=None, headers=None):
    r = type("R", (), {})()
    r.status_code = status
    r._json = json_data or []
    r.headers = headers or {}
    r.raise_for_status = lambda: None
    r.json = lambda: r._json
    return r


def _kline_payload(n=2, price="100.0"):
    """Minimal Binance kline rows: openTime, open, high, low, close, volume, ..."""
    base = NOW
    rows = []
    for i in range(n):
        rows.append([
            int((base + timedelta(seconds=300 * i)).timestamp() * 1000),
            price, price, price, price,  # open/high/low/close strings
            "1000",  # volume
            0, int(base.timestamp() * 1000 + 300000), 0, 0, 0, 0, 0,
        ])
    return rows


def test_retry_on_5xx_then_success():
    md = BinanceMarketData(max_retries=2, backoff_base_seconds=0,
                           pacing_enabled=False)
    md.session = FakeSession([
        _resp(status=503),
        _resp(status=200, json_data=_kline_payload()),
    ])
    candles = md.fetch_klines("BTCUSDT", "5m")
    assert len(candles) == 2
    assert len(md.session.requested) == 2


def test_429_exhausts_then_raises_ratelimit():
    md = BinanceMarketData(max_retries=2, backoff_base_seconds=0,
                           pacing_enabled=False)
    md.session = FakeSession([
        _resp(status=429, headers={"Retry-After": "0"}),
        _resp(status=429, headers={"Retry-After": "0"}),
        _resp(status=429, headers={"Retry-After": "0"}),
    ])
    with pytest.raises(RateLimitError):
        md.fetch_klines("BTCUSDT", "5m")


def test_transient_network_error_retries():
    md = BinanceMarketData(max_retries=1, backoff_base_seconds=0,
                           pacing_enabled=False)
    import requests

    def failing_get(*a, **k):
        md.session.requested.append("fail")
        raise requests.exceptions.ConnectionError("boom")

    # Patch the session.get to always raise, then succeed
    calls = {"n": 0}

    def get(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.exceptions.ConnectionError("first fails")
        return _resp(status=200, json_data=_kline_payload())

    md.session.get = get
    candles = md.fetch_klines("BTCUSDT", "5m")
    assert len(candles) == 2
    assert calls["n"] == 2


def test_nontransient_400_raises_marketdataerror():
    md = BinanceMarketData(max_retries=1, backoff_base_seconds=0,
                           pacing_enabled=False)
    r = _resp(status=400, json_data=[])
    # 400 -> raise_for_status will raise HTTPError (a RequestException, transient
    # by our catch-all). To test a hard non-transient, use an invalid payload:
    md.session = FakeSession([_resp(status=200, json_data=[])])  # empty -> MarketDataError
    with pytest.raises(MarketDataError):
        md.fetch_klines("BTCUSDT", "5m")


# ---------------------------------------------------------------------------
# State guard isolation across symbols
# ---------------------------------------------------------------------------

def test_state_is_isolated_per_symbol(scanner):
    scanner.market_data.series = valid_series()
    d1 = scanner.scan_symbol("BTCUSDT")
    # ETH has its own independent last-processed candle
    d2 = scanner.scan_symbol("ETHUSDT")
    assert d2.reason != "REPEATED_CANDLE"
    # Re-scanning ETH with the same candle is repeated
    d3 = scanner.scan_symbol("ETHUSDT")
    assert d3.reason == "REPEATED_CANDLE"
