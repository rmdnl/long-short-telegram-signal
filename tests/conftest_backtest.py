"""Shared synthetic multi-timeframe data helpers for backtest tests."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List

from app.models import Candle

UTC = timezone.utc
BASE = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)


def make_candles(tf: str, n: int, start: datetime = BASE,
                 price: Decimal = Decimal("100")) -> List[Candle]:
    """n ascending, valid, closed candles for timeframe `tf`."""
    sec = {"5m": 300, "15m": 900, "1h": 3600}[tf]
    out: List[Candle] = []
    for i in range(n):
        ts = start + timedelta(seconds=sec * i)
        o = price
        c = price + Decimal("0.1")
        out.append(Candle(
            ts, o, c + Decimal("0.05"), o - Decimal("0.05"), c, Decimal("1000"),
        ))
        price = c
    return out


def make_dataset(symbol: str,
                 n_hf: int = 700,
                 n_setup: int = 700,
                 n_trigger: int = 700,
                 start: datetime = BASE):
    """Build a backtest.data.HistoricalDataset for a symbol."""
    from backtest.data import load_rows

    rows = {
        "1h": _rows_from(make_candles("1h", n_hf, start)),
        "15m": _rows_from(make_candles("15m", n_setup, start)),
        "5m": _rows_from(make_candles("5m", n_trigger, start)),
    }
    return load_rows(rows, symbol)


def _rows_from(candles: List[Candle]) -> List[dict]:
    return [
        {
            "timestamp": c.timestamp,
            "open": c.open, "high": c.high, "low": c.low,
            "close": c.close, "volume": c.volume,
        }
        for c in candles
    ]
