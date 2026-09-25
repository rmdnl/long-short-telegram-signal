"""
PHASE 6: Forensics calculation tests.

Deterministic synthetic data only. No network, no live data.
Tests every forensic calculation: percentile, R distribution, exit reasons,
holding time, MAE/MFE, bucket grouping, OOS separation, entry quality,
RR validation, and the CSV/markdown builders.
"""
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from backtest import forensics as F


# ---------------------------------------------------------------------------
# Fixture: synthetic ForensicsTrade list
# ---------------------------------------------------------------------------

def _mk_trade(
    signal_id="S1",
    symbol="BTCUSDT",
    direction="LONG",
    signal_time=None,
    entry_low=None,
    entry_high=None,
    entry_price=None,
    stop_loss=None,
    tp1=None,
    tp2=None,
    score=85,
    market_regime="TRENDING",
    entry_status="FILLED",
    exit_status="STOPPED",
    exit_time=None,
    exit_price=None,
    r_multiple=None,
    tp1_hit=False,
    tp2_hit=False,
    sl_hit=False,
    holding_candles=10,
    adx=None,
    rsi=None,
    atr=None,
    ema_fast=None,
    ema_slow=None,
    volume_ratio=None,
    ema_distance_atr=None,
    mae_r=None,
    mfe_r=None,
    failure_sequence="SL_DIRECT",
):
    t = F.ForensicsTrade(
        signal_id=signal_id,
        symbol=symbol,
        direction=direction,
        signal_time=signal_time or datetime(2025, 1, 15, tzinfo=timezone.utc),
        entry_low=entry_low if entry_low is not None else Decimal("100"),
        entry_high=entry_high if entry_high is not None else Decimal("105"),
        entry_price=entry_price if entry_price is not None else Decimal("100"),
        stop_loss=stop_loss if stop_loss is not None else Decimal("95"),
        tp1=tp1 if tp1 is not None else Decimal("107.5"),
        tp2=tp2 if tp2 is not None else Decimal("112.5"),
        score=score,
        market_regime=market_regime,
        entry_status=entry_status,
        exit_status=exit_status,
        exit_time=exit_time,
        exit_price=exit_price,
        r_multiple=r_multiple if r_multiple is not None else Decimal("-1"),
        tp1_hit=tp1_hit,
        tp2_hit=tp2_hit,
        sl_hit=sl_hit,
        holding_candles=holding_candles,
        fees_enabled=False,
        adx=adx,
        rsi=rsi,
        atr=atr,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        volume_ratio=volume_ratio,
        ema_distance_atr=ema_distance_atr,
        mae_r=mae_r,
        mfe_r=mfe_r,
        failure_sequence=failure_sequence,
    )
    return t


# ---------------------------------------------------------------------------
# percentile
# ---------------------------------------------------------------------------

class TestPercentile:
    def test_empty(self):
        assert F.percentile([]) is None

    def test_single(self):
        assert F.percentile([42.0]) == 42.0

    def test_min_max(self):
        vals = [1, 2, 3, 4, 5]
        assert F.percentile(vals, 0) == 1
        assert F.percentile(vals, 100) == 5

    def test_median_odd(self):
        vals = [1, 2, 3, 4, 5]
        assert F.percentile(vals, 50) == 3

    def test_median_even(self):
        vals = [1, 2, 3, 4]
        assert F.percentile(vals, 50) == 2.5

    def test_quartiles(self):
        vals = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        # linear interpolation: p25 -> index 2.25 -> 3.25
        assert abs(F.percentile(vals, 25) - 3.25) < 1e-9
        assert abs(F.percentile(vals, 75) - 7.75) < 1e-9

    def test_deterministic_unsorted_input(self):
        a = [10, 2, 8, 4, 6]
        b = [6, 4, 10, 2, 8]
        assert F.percentile(a, 50) == F.percentile(b, 50)


# ---------------------------------------------------------------------------
# R distribution
# ---------------------------------------------------------------------------

class TestRDistribution:
    def test_all_closed(self):
        trades = [
            _mk_trade(signal_id="A", r_multiple=Decimal("1.5"),
                      exit_status="TP2_HIT", tp2_hit=True),
            _mk_trade(signal_id="B", r_multiple=Decimal("-1"),
                      exit_status="STOPPED", sl_hit=True),
            _mk_trade(signal_id="C", r_multiple=Decimal("0.5"),
                      exit_status="TP1_HIT", tp1_hit=True),
        ]
        d = F.r_distribution(trades)
        assert d.count == 3
        assert abs(d.min_r - (-1.0)) < 1e-9
        assert abs(d.max_r - 1.5) < 1e-9
        assert abs(d.total_r - 1.0) < 1e-9
        # median of [1.5, -1, 0.5] -> sorted [-1, 0.5, 1.5] -> 0.5
        assert abs(d.median_r - 0.5) < 1e-9

    def test_direction_filter(self):
        trades = [
            _mk_trade(signal_id="A", direction="LONG", r_multiple=Decimal("2")),
            _mk_trade(signal_id="B", direction="SHORT", r_multiple=Decimal("-1")),
        ]
        dl = F.r_distribution(trades, direction="LONG")
        assert dl.count == 1
        assert abs(dl.min_r - 2.0) < 1e-9
        ds = F.r_distribution(trades, direction="SHORT")
        assert ds.count == 1
        assert abs(ds.min_r - (-1.0)) < 1e-9

    def test_excludes_open(self):
        trades = [
            _mk_trade(signal_id="A", exit_status="EXPIRED",
                      r_multiple=Decimal("0.3")),
            _mk_trade(signal_id="B", exit_status="STOPPED",
                      r_multiple=Decimal("-1"), sl_hit=True),
        ]
        # EXPIRED is not "closed" in forensics (closed = TP1/TP2/STOPPED)
        d = F.r_distribution(trades)
        assert d.count == 1
        assert abs(d.min_r - (-1.0)) < 1e-9

    def test_empty(self):
        d = F.r_distribution([])
        assert d.count == 0
        assert d.min_r is None


# ---------------------------------------------------------------------------
# Exit reasons
# ---------------------------------------------------------------------------

class TestExitReasons:
    def test_counts(self):
        trades = [
            _mk_trade(signal_id="A", exit_status="STOPPED", sl_hit=True),
            _mk_trade(signal_id="B", exit_status="TP1_HIT", tp1_hit=True),
            _mk_trade(signal_id="C", exit_status="TP2_HIT", tp2_hit=True),
            _mk_trade(signal_id="D", exit_status="EXPIRED"),
            _mk_trade(signal_id="E", entry_status="NOT_FILLED"),
        ]
        e = F.exit_reasons(trades)
        assert e.sl == 1
        assert e.tp1 == 1
        assert e.tp2 == 1
        assert e.expired == 1
        assert e.not_filled == 1
        assert e.total == 5
        assert abs(e.pct(e.sl) - 0.2) < 1e-9

    def test_direction_split(self):
        trades = [
            _mk_trade(signal_id="A", direction="LONG",
                      exit_status="STOPPED", sl_hit=True),
            _mk_trade(signal_id="B", direction="SHORT",
                      exit_status="STOPPED", sl_hit=True),
        ]
        el = F.exit_reasons(trades, direction="LONG")
        assert el.sl == 1
        es = F.exit_reasons(trades, direction="SHORT")
        assert es.sl == 1

    def test_empty(self):
        e = F.exit_reasons([])
        assert e.total == 0
        assert e.pct(0) is None


# ---------------------------------------------------------------------------
# Holding time
# ---------------------------------------------------------------------------

class TestHoldingTime:
    def test_all(self):
        trades = [
            _mk_trade(signal_id="A", holding_candles=10),
            _mk_trade(signal_id="B", holding_candles=20),
            _mk_trade(signal_id="C", holding_candles=30),
            _mk_trade(signal_id="D", holding_candles=40),
        ]
        h = F.holding_time(trades)
        assert h.median == 25.0   # linear interp of [10,20,30,40] at 50%
        assert h.maximum == 40

    def test_winners_only(self):
        trades = [
            _mk_trade(signal_id="A", r_multiple=Decimal("1"),
                      exit_status="TP2_HIT", tp2_hit=True, holding_candles=10),
            _mk_trade(signal_id="B", r_multiple=Decimal("-1"),
                      exit_status="STOPPED", sl_hit=True, holding_candles=50),
        ]
        h = F.holding_time(trades, winners_only=True)
        assert h.median == 10.0

    def test_losers_only(self):
        trades = [
            _mk_trade(signal_id="A", r_multiple=Decimal("1"),
                      exit_status="TP2_HIT", tp2_hit=True, holding_candles=10),
            _mk_trade(signal_id="B", r_multiple=Decimal("-1"),
                      exit_status="STOPPED", sl_hit=True, holding_candles=50),
        ]
        h = F.holding_time(trades, losers_only=True)
        assert h.median == 50.0

    def test_empty(self):
        h = F.holding_time([])
        assert h.median is None


# ---------------------------------------------------------------------------
# MAE / MFE
# ---------------------------------------------------------------------------

class TestMaeMfe:
    def test_average(self):
        trades = [
            _mk_trade(signal_id="A", r_multiple=Decimal("1"),
                      exit_status="TP2_HIT", tp2_hit=True,
                      mae_r=0.2, mfe_r=2.0),
            _mk_trade(signal_id="B", r_multiple=Decimal("-1"),
                      exit_status="STOPPED", sl_hit=True,
                      mae_r=0.8, mfe_r=0.3),
        ]
        m = F.mae_mfe(trades)
        # winners: avg mae 0.2, avg mfe 2.0
        assert abs(m.winners_mae - 0.2) < 1e-9
        assert abs(m.winners_mfe - 2.0) < 1e-9
        # losers: avg mae 0.8, avg mfe 0.3
        assert abs(m.losers_mae - 0.8) < 1e-9
        assert abs(m.losers_mfe - 0.3) < 1e-9
        # loser mfe 0.3 < 0.5 -> not counted in losers_hit_half_r
        assert abs(m.losers_hit_half_r - 0.0) < 1e-9

    def test_losers_hit_half_r(self):
        trades = [
            _mk_trade(signal_id="A", r_multiple=Decimal("-1"),
                     exit_status="STOPPED", sl_hit=True,
                     mae_r=0.5, mfe_r=0.7),
            _mk_trade(signal_id="B", r_multiple=Decimal("-1"),
                     exit_status="STOPPED", sl_hit=True,
                     mae_r=0.5, mfe_r=0.2),
        ]
        m = F.mae_mfe(trades)
        assert abs(m.losers_hit_half_r - 0.5) < 1e-9

    def test_empty(self):
        m = F.mae_mfe([])
        assert m.winners_mae is None


# ---------------------------------------------------------------------------
# Bucket grouping
# ---------------------------------------------------------------------------

class TestBuckets:
    def _trades(self):
        return [
            _mk_trade(signal_id="A", score=82, adx=Decimal("23"),
                     rsi=Decimal("52"), volume_ratio=Decimal("1.1"),
                     ema_distance_atr=0.3),
            _mk_trade(signal_id="B", score=87, adx=Decimal("27"),
                     rsi=Decimal("57"), volume_ratio=Decimal("1.3"),
                     ema_distance_atr=0.7),
            _mk_trade(signal_id="C", score=92, adx=Decimal("35"),
                     rsi=Decimal("62"), volume_ratio=Decimal("1.7"),
                     ema_distance_atr=1.2),
            _mk_trade(signal_id="D", score=97, adx=Decimal("45"),
                     rsi=Decimal("68"), volume_ratio=Decimal("2.5"),
                     ema_distance_atr=1.8),
        ]

    def test_score_buckets(self):
        stats = F.score_buckets(self._trades())
        assert "80-84" in stats
        assert "85-89" in stats
        assert "90-94" in stats
        assert "95-100" in stats
        assert stats["80-84"].signals == 1
        assert stats["85-89"].signals == 1
        assert stats["90-94"].signals == 1
        assert stats["95-100"].signals == 1

    def test_adx_buckets(self):
        stats = F.adx_buckets(self._trades())
        assert stats["22-25"].signals == 1
        assert stats["25-30"].signals == 1
        assert stats["30-40"].signals == 1
        assert stats["40+"].signals == 1

    def test_rsi_buckets_direction_aware(self):
        trades = [
            _mk_trade(signal_id="A", direction="LONG", rsi=Decimal("52")),
            _mk_trade(signal_id="B", direction="SHORT", rsi=Decimal("47")),
        ]
        stats = F.rsi_buckets(trades)
        # LONG: 52 in 50-55 -> "LONG:50-55"
        assert "LONG:50-55" in stats
        # SHORT: 47 in 45-50 -> "SHORT:45-50"
        assert "SHORT:45-50" in stats

    def test_rsi_buckets_short_ranges(self):
        trades = [
            _mk_trade(signal_id="A", direction="SHORT", rsi=Decimal("42")),
            _mk_trade(signal_id="B", direction="SHORT", rsi=Decimal("37")),
            _mk_trade(signal_id="C", direction="SHORT", rsi=Decimal("30")),
        ]
        stats = F.rsi_buckets(trades)
        assert "SHORT:40-45" in stats
        assert "SHORT:35-40" in stats
        assert "SHORT:<35" in stats   # below 35 -> "<35"

    def test_volume_buckets(self):
        stats = F.volume_buckets(self._trades())
        assert stats["1.0-1.2"].signals == 1
        assert stats["1.2-1.5"].signals == 1
        assert stats["1.5-2.0"].signals == 1
        assert stats["2.0+"].signals == 1

    def test_ema_distance_buckets(self):
        stats = F.ema_distance_buckets(self._trades())
        assert stats["0.0-0.5"].signals == 1
        assert stats["0.5-1.0"].signals == 1
        assert stats["1.0-1.5"].signals == 1
        assert stats["1.5-2.0"].signals == 1

    def test_group_by_regime(self):
        trades = [
            _mk_trade(signal_id="A", market_regime="TRENDING"),
            _mk_trade(signal_id="B", market_regime="WEAK_TREND"),
            _mk_trade(signal_id="C", market_regime="WEAK_TREND"),
        ]
        stats = F.group_by(trades, lambda t: t.market_regime)
        assert stats["TRENDING"].signals == 1
        assert stats["WEAK_TREND"].signals == 2

    def test_group_by_symbol(self):
        trades = [
            _mk_trade(signal_id="A", symbol="BTCUSDT"),
            _mk_trade(signal_id="B", symbol="ETHUSDT"),
        ]
        stats = F.group_by(trades, lambda t: t.symbol)
        assert stats["BTCUSDT"].signals == 1
        assert stats["ETHUSDT"].signals == 1

    def test_group_by_month(self):
        from datetime import timezone
        t1 = _mk_trade(signal_id="A",
                      signal_time=datetime(2025, 3, 10, tzinfo=timezone.utc))
        t2 = _mk_trade(signal_id="B",
                      signal_time=datetime(2025, 4, 10, tzinfo=timezone.utc))
        stats = F.group_by([t1, t2], lambda t: t.signal_time.strftime("%Y-%m"))
        assert stats["2025-03"].signals == 1
        assert stats["2025-04"].signals == 1


# ---------------------------------------------------------------------------
# GroupStats aggregate
# ---------------------------------------------------------------------------

class TestGroupStats:
    def test_pf_none_when_no_losses(self):
        trades = [
            _mk_trade(signal_id="A", r_multiple=Decimal("1"),
                     exit_status="TP2_HIT", tp2_hit=True),
        ]
        g = F._group_stats(trades)
        assert g.wins == 1
        assert g.losses == 0
        assert g.profit_factor is None   # N/A when no losses

    def test_pf_with_losses(self):
        trades = [
            _mk_trade(signal_id="A", r_multiple=Decimal("2"),
                     exit_status="TP2_HIT", tp2_hit=True),
            _mk_trade(signal_id="B", r_multiple=Decimal("-1"),
                     exit_status="STOPPED", sl_hit=True),
        ]
        g = F._group_stats(trades)
        assert abs(g.profit_factor - 2.0) < 1e-9

    def test_expectancy(self):
        trades = [
            _mk_trade(signal_id="A", r_multiple=Decimal("3"),
                     exit_status="TP2_HIT", tp2_hit=True),
            _mk_trade(signal_id="B", r_multiple=Decimal("-1"),
                     exit_status="STOPPED", sl_hit=True),
            _mk_trade(signal_id="C", r_multiple=Decimal("-1"),
                     exit_status="STOPPED", sl_hit=True),
        ]
        g = F._group_stats(trades)
        assert abs(g.expectancy - (3 - 1 - 1) / 3) < 1e-9

    def test_max_dd(self):
        trades = [
            _mk_trade(signal_id="A", r_multiple=Decimal("5"),
                     exit_status="TP2_HIT", tp2_hit=True),
            _mk_trade(signal_id="B", r_multiple=Decimal("-1"),
                     exit_status="STOPPED", sl_hit=True),
            _mk_trade(signal_id="C", r_multiple=Decimal("-1"),
                     exit_status="STOPPED", sl_hit=True),
            _mk_trade(signal_id="D", r_multiple=Decimal("-1"),
                     exit_status="STOPPED", sl_hit=True),
        ]
        g = F._group_stats(trades)
        # equity: 5 -> 4 -> 3 -> 2, peak 5, dd = 3
        assert abs(g.max_drawdown_r - 3.0) < 1e-9


# ---------------------------------------------------------------------------
# OOS split
# ---------------------------------------------------------------------------

class TestOosSplit:
    def test_three_segments(self):
        from datetime import timezone
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        end = datetime(2025, 4, 1, tzinfo=timezone.utc)  # 90 days
        trades = [
            _mk_trade(signal_id="A", signal_time=datetime(2025, 1, 10, tzinfo=timezone.utc)),
            _mk_trade(signal_id="B", signal_time=datetime(2025, 2, 10, tzinfo=timezone.utc)),
            _mk_trade(signal_id="C", signal_time=datetime(2025, 3, 20, tzinfo=timezone.utc)),
        ]
        out = F.oos_split_labels(trades, start, end, 0.3, 0.2)
        assert len(out["in_sample"]) + len(out["validation"]) + len(out["out_of_sample"]) == 3
        # 90 days: in_sample=0-63 days (Jan1..Mar4), validation=63-76.5 (Mar4..Mar17),
        # oos=76.5-90 (Mar17..Apr1)
        assert any(t.signal_id == "A" for t in out["in_sample"])
        assert any(t.signal_id == "B" for t in out["in_sample"])
        assert any(t.signal_id == "C" for t in out["out_of_sample"])


# ---------------------------------------------------------------------------
# Entry quality
# ---------------------------------------------------------------------------

class TestEntryQuality:
    def test_conservative_edge(self):
        # LONG filled at zone_low -> edge count
        trades = [
            _mk_trade(signal_id="A", direction="LONG",
                     entry_low=Decimal("100"), entry_high=Decimal("105"),
                     entry_price=Decimal("100")),
            _mk_trade(signal_id="B", direction="LONG",
                     entry_low=Decimal("100"), entry_high=Decimal("105"),
                     entry_price=Decimal("105")),
        ]
        eq = F.entry_quality(trades)
        assert eq.filled == 2
        # A: offset 0 (at low edge), B: offset 1.0 (at high edge)
        assert abs(eq.avg_fill_offset_pct - 0.5) < 1e-9
        assert abs(eq.conservative_edge_fill_pct - 0.5) < 1e-9

    def test_short_edge(self):
        trades = [
            _mk_trade(signal_id="A", direction="SHORT",
                     entry_low=Decimal("100"), entry_high=Decimal("105"),
                     entry_price=Decimal("105")),
        ]
        eq = F.entry_quality(trades)
        assert abs(eq.conservative_edge_fill_pct - 1.0) < 1e-9  # short at high edge

    def test_unfilled(self):
        trades = [
            _mk_trade(signal_id="A", entry_status="NOT_FILLED",
                     entry_price=None),
            _mk_trade(signal_id="B"),
        ]
        eq = F.entry_quality(trades)
        assert eq.filled == 1
        assert eq.unfilled_expired == 1


# ---------------------------------------------------------------------------
# RR validation
# ---------------------------------------------------------------------------

class TestRRValidation:
    def test_matches_config(self):
        # config tp1_rr=1.5, tp2_rr=2.5
        # LONG: entry 100, SL 95 (risk 5), TP1 107.5 (1.5R), TP2 112.5 (2.5R)
        trades = [
            _mk_trade(signal_id="A", entry_price=Decimal("100"),
                     stop_loss=Decimal("95"),
                     tp1=Decimal("107.5"), tp2=Decimal("112.5")),
        ]
        rr = F.rr_validation(trades, Decimal("1.5"), Decimal("2.5"))
        assert abs(rr.avg_sl_distance_r - 1.0) < 1e-9
        assert abs(rr.avg_tp1_r - 1.5) < 1e-9
        assert abs(rr.avg_tp2_r - 2.5) < 1e-9
        assert rr.config_tp1_rr == 1.5
        assert rr.config_tp2_rr == 2.5

    def test_empty(self):
        rr = F.rr_validation([], Decimal("1.5"), Decimal("2.5"))
        assert rr.count == 0
        assert rr.avg_tp1_r is None


# ---------------------------------------------------------------------------
# Failure sequence classification
# ---------------------------------------------------------------------------

class TestFailureSequence:
    """Test _classify_sequence directly (the unit that assigns the label)."""

    def _label(self, **kwargs) -> str:
        t = _mk_trade(**kwargs)
        t.failure_sequence = F._classify_sequence(t, [], 0, 0, {})
        return t.failure_sequence

    def test_unfilled(self):
        assert self._label(entry_status="NOT_FILLED",
                           entry_price=None) == "N/A"

    def test_tp2_winner(self):
        assert self._label(r_multiple=Decimal("2.46"),
                           exit_status="TP2_HIT", tp2_hit=True) == "TP2_EXITS"

    def test_tp1_winner(self):
        assert self._label(r_multiple=Decimal("0.75"),
                           exit_status="TP1_HIT", tp1_hit=True) == "TP1_EXITS"

    def test_sl_direct(self):
        assert self._label(r_multiple=Decimal("-1"),
                           exit_status="STOPPED", sl_hit=True,
                           mfe_r=0.3) == "SL_DIRECT"

    def test_favorable_then_sl(self):
        assert self._label(r_multiple=Decimal("-1"),
                           exit_status="STOPPED", sl_hit=True,
                           mfe_r=0.8) == "FAVORABLE_THEN_SL"

    def test_tp1_then_sl(self):
        assert self._label(r_multiple=Decimal("-1"),
                           exit_status="STOPPED", sl_hit=True,
                           tp1_hit=True, mfe_r=0.9) == "TP1_THEN_SL"

    def test_expired_open(self):
        assert self._label(exit_status="EXPIRED",
                           r_multiple=Decimal("0.2")) == "EXPIRED_OPEN"



# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

class TestCsvExport:
    def test_columns(self, tmp_path):
        t = _mk_trade(
            signal_id="S1", adx=Decimal("30"), rsi=Decimal("55"),
            atr=Decimal("455"), ema_fast=Decimal("93000"),
            ema_slow=Decimal("95000"), volume_ratio=Decimal("1.5"),
            ema_distance_atr=0.8, mae_r=0.5, mfe_r=1.2,
        )
        out = F.export_forensics_csv([t], str(tmp_path / "f.csv"))
        with open(out) as fh:
            lines = fh.readlines()
        header = lines[0].strip().split(",")
        expected = ["signal_id", "symbol", "direction", "signal_time",
                    "entry_price", "stop_loss", "tp1", "tp2", "score",
                    "adx", "rsi", "volume_ratio", "atr", "ema50", "ema200",
                    "ema_distance_atr", "market_regime",
                    "entry_status", "exit_status", "exit_time",
                    "r_multiple", "holding_time", "MAE_R", "MFE_R",
                    "failure_sequence"]
        assert header == expected
        assert len(lines) == 2   # header + 1 row

    def test_does_not_touch_baseline(self, tmp_path):
        # write a sentinel baseline file, ensure export_forensics_csv
        # writes only to the requested path
        sentinel = tmp_path / "baseline_trades.csv"
        sentinel.write_text("original\n")
        t = _mk_trade()
        out = F.export_forensics_csv([t], str(tmp_path / "forensics_trades.csv"))
        assert out == str(tmp_path / "forensics_trades.csv")
        assert sentinel.read_text() == "original\n"


# ---------------------------------------------------------------------------
# Markdown builder
# ---------------------------------------------------------------------------

class TestMarkdownBuilder:
    def test_contains_all_sections(self):
        trades = [
            _mk_trade(signal_id="A", score=82, adx=Decimal("23"),
                     rsi=Decimal("52"), volume_ratio=Decimal("1.1"),
                     ema_distance_atr=0.3, r_multiple=Decimal("-1"),
                     exit_status="STOPPED", sl_hit=True, mae_r=0.5,
                     mfe_r=0.3, failure_sequence="SL_DIRECT"),
            _mk_trade(signal_id="B", score=95, adx=Decimal("45"),
                      rsi=Decimal("68"), volume_ratio=Decimal("2.5"),
                      ema_distance_atr=1.8, direction="SHORT",
                      r_multiple=Decimal("2.46"), exit_status="TP2_HIT",
                      tp2_hit=True, mfe_r=2.0, failure_sequence="TP2_EXITS"),
        ]
        md = F.build_forensics_markdown(trades,
                                         _cfg_stub(),
                                         {"in_sample": trades,
                                          "validation": [],
                                          "out_of_sample": trades})
        for section in ("## 1. Baseline Recap", "## 2. R-Multiple Distribution",
                        "## 3. Exit Reason Counts", "## 4. Holding Time",
                        "## 5. MAE / MFE", "## 6. Score Buckets",
                        "## 7. ADX Buckets", "## 8. RSI Buckets",
                        "## 9. Volume-Ratio Buckets",
                        "## 10. Distance from EMA50", "## 11. Market Regime",
                        "## 12. LONG vs SHORT", "## 13. Per-Symbol",
                        "## 14. Monthly", "## 15. OOS Separation",
                        "## 16. Failure Sequences", "## 17. Entry Quality",
                        "## 18. Risk/Reward Validation",
                        "## 19. Implementation Audit",
                        "## 20. Potential Hypotheses"):
            assert section in md, f"missing {section}"

    def test_disclaimer(self):
        md = F.build_forensics_markdown([], _cfg_stub(),
                                         {"in_sample": [], "validation": [],
                                          "out_of_sample": []})
        assert "HISTORICAL SIMULATION" in md
        assert "DISCLAIMER" in md


def _cfg_stub():
    from decimal import Decimal
    class Cfg:
        tp1_rr = Decimal("1.5")
        tp2_rr = Decimal("2.5")
        adx_min = Decimal("22")
    return Cfg()
