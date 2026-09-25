"""
PHASE 4: Metrics formula-correctness tests.

Validates documented metric formulas against deterministic TradeResult
inputs. No live data.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from backtest.execution import TradeResult
from backtest.metrics import metrics_from_trades, BacktestMetrics

UTC = timezone.utc
BASE = datetime(2024, 1, 1, tzinfo=UTC)


def _trade(r, status, direction="LONG", regime="TRENDING",
           signal_time=None, symbol="AAA", tp1=False, tp2=False, sl=False,
           filled=True, hold=1):
    return TradeResult(
        signal_id=f"T-{r}", symbol=symbol, direction=direction,
        signal_time=signal_time or (BASE + timedelta(minutes=5)),
        entry_low=Decimal("100"), entry_high=Decimal("102"),
        entry_price=Decimal("100") if filled else None,
        stop_loss=Decimal("95"), tp1=Decimal("105"), tp2=Decimal("110"),
        score=90, market_regime=regime,
        entry_status="FILLED" if filled else "EXPIRED",
        exit_status=status,
        exit_time=BASE + timedelta(hours=1), exit_price=Decimal("110"),
        r_multiple=Decimal(str(r)),
        tp1_hit=tp1, tp2_hit=tp2, sl_hit=sl,
        holding_candles=hold, fees_enabled=False,
    )


# ---------------------------------------------------------------------------
# Profit factor (N/A when zero losses)
# ---------------------------------------------------------------------------

def test_profit_factor_na_when_no_losses():
    trades = [_trade("1", "TP2_HIT"), _trade("1.5", "TP2_HIT")]
    m = metrics_from_trades(trades)
    assert m.losses == 0
    assert m.profit_factor is None  # N/A, not infinity


def test_profit_factor_gross_win_over_abs_gross_loss():
    trades = [
        _trade("1.5", "TP2_HIT"), _trade("1.0", "TP2_HIT"),
        _trade("-1.0", "STOPPED"), _trade("-0.5", "STOPPED"),
    ]
    m = metrics_from_trades(trades)
    # gross win = 2.5, gross loss = 1.5
    assert m.profit_factor == pytest.approx(2.5 / 1.5)


# ---------------------------------------------------------------------------
# Expectancy = avg R per closed trade
# ---------------------------------------------------------------------------

def test_expectancy_equals_average_closed_r():
    trades = [_trade("1.0", "TP2_HIT"), _trade("-1.0", "STOPPED"),
              _trade("2.0", "TP2_HIT")]
    m = metrics_from_trades(trades)
    assert m.expectancy == pytest.approx((1.0 - 1.0 + 2.0) / 3)
    assert m.avg_r == m.expectancy


# ---------------------------------------------------------------------------
# Win rate = wins / closed trades
# ---------------------------------------------------------------------------

def test_win_rate_wins_over_closed():
    trades = [
        _trade("1.0", "TP2_HIT"), _trade("-1.0", "STOPPED"),
        _trade("1.5", "TP2_HIT"), _trade("-0.5", "STOPPED"),
        _trade("0.5", "TP1_HIT"),
    ]
    m = metrics_from_trades(trades)
    assert m.wins == 3
    assert m.losses == 2
    assert m.win_rate == pytest.approx(3 / 5)


# ---------------------------------------------------------------------------
# Total R
# ---------------------------------------------------------------------------

def test_total_r_sums_closed_only():
    trades = [
        _trade("1.0", "TP2_HIT"), _trade("-1.0", "STOPPED"),
        _trade("0", "EXPIRED"),  # expired -> excluded from total_r
    ]
    m = metrics_from_trades(trades)
    assert m.total_r == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Max drawdown (R)
# ---------------------------------------------------------------------------

def test_max_drawdown_r_peak_to_trough():
    # Sequence: +1, +1, -3, +2  -> equity 1,2,-1,1 -> dd = 3
    trades = [
        _trade("1.0", "TP2_HIT"), _trade("1.0", "TP2_HIT"),
        _trade("-3.0", "STOPPED"), _trade("2.0", "TP2_HIT"),
    ]
    m = metrics_from_trades(trades)
    assert m.max_drawdown_r == pytest.approx(3.0)


def test_max_consecutive_losses():
    trades = [
        _trade("-1.0", "STOPPED"), _trade("-1.0", "STOPPED"),
        _trade("1.0", "TP2_HIT"), _trade("-1.0", "STOPPED"),
        _trade("-1.0", "STOPPED"), _trade("-1.0", "STOPPED"),
    ]
    m = metrics_from_trades(trades)
    assert m.max_consecutive_losses == 3


# ---------------------------------------------------------------------------
# TP hit rates + SL rate
# ---------------------------------------------------------------------------

def test_tp_sl_rates():
    trades = [
        _trade("1.5", "TP2_HIT", tp1=True, tp2=True, sl=False),
        _trade("-1.0", "STOPPED", tp1=True, tp2=False, sl=True),
    ]
    m = metrics_from_trades(trades)
    assert m.tp1_hit_rate == pytest.approx(1.0)      # both touched TP1
    assert m.tp2_hit_rate == pytest.approx(0.5)
    assert m.sl_rate == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Grouped metrics (by symbol / direction / regime)
# ---------------------------------------------------------------------------

def test_grouped_by_symbol_direction_regime():
    t1 = _trade("1.0", "TP2_HIT", symbol="AAA", direction="LONG", regime="TRENDING")
    t2 = _trade("-1.0", "STOPPED", symbol="BBB", direction="SHORT", regime="RANGING")
    m = metrics_from_trades([t1, t2])
    assert set(m.by_symbol.keys()) == {"AAA", "BBB"}
    assert set(m.by_direction.keys()) == {"LONG", "SHORT"}
    assert set(m.by_regime.keys()) == {"TRENDING", "RANGING"}
    assert m.by_symbol["AAA"].total_signals == 1
    assert m.by_regime["RANGING"].total_r == pytest.approx(-1.0)


# ---------------------------------------------------------------------------
# Empty metrics: no crashes, correct zero-state
# ---------------------------------------------------------------------------

def test_empty_trades_metrics():
    m = metrics_from_trades([])
    assert m.total_signals == 0
    assert m.profit_factor is None
    assert m.expectancy is None
    assert m.win_rate is None
    assert m.max_drawdown_r == 0.0
    assert m.max_consecutive_losses == 0
