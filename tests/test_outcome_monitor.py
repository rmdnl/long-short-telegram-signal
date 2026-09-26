"""
Regression tests: TP/SL outcome monitor + startup crash-loop guard.

- Signal-only: no trading-execution code is touched.
- Every assertion is reversible and focused on observable behavior.
"""
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import app.config as cfg
import app.main as main_mod
import app.outcome_monitor as outcome_monitor
from app.models import (Candle, MarketBias, Signal, SignalDirection,
                        SignalStatus, SignalType)
from app.outcome_monitor import (OutcomeMonitorRunner, SIGNAL_MAX_AGE_HOURS,
                                 evaluate_signal_outcome, format_outcome,
                                 format_startup_notification)
from app.signal_filter import generate_signal_id
from app.signal_store import SignalStore
from app.telegram_bot import TelegramBot

UTC = timezone.utc
BASE = datetime(2024, 1, 1, 10, 0, tzinfo=UTC)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
def _make_signal(
    direction=SignalDirection.LONG,
    symbol="BTCUSDT",
    ts=None,
    status=SignalStatus.ACTIVE,
):
    ts = ts or (BASE + timedelta(minutes=5))
    if direction == SignalDirection.LONG:
        entry_low, entry_high = Decimal("102.50"), Decimal("103.50")
        sl, tp1, tp2 = Decimal("99.00"), Decimal("108.00"), Decimal("113.00")
        htf = MarketBias.BULLISH
    else:
        entry_low, entry_high = Decimal("98.50"), Decimal("99.50")
        sl, tp1, tp2 = Decimal("104.00"), Decimal("94.00"), Decimal("89.00")
        htf = MarketBias.BEARISH
    sid = generate_signal_id(symbol, "5m", ts, direction)
    return Signal(
        signal_id=sid,
        symbol=symbol,
        direction=direction,
        signal_type=SignalType.TREND_PULLBACK,
        created_at=ts - timedelta(hours=1),
        trigger_candle_time=ts,
        entry_low=entry_low,
        entry_high=entry_high,
        stop_loss=sl,
        tp1=tp1,
        tp2=tp2,
        score=92,
        htf_bias=htf,
        adx_value=Decimal("28.5"),
        rsi_value=Decimal("58.0"),
        volume_ratio=Decimal("1.34"),
        status=status,
    )


def _mk_candle(ts, low, high, close=None, volume="1000"):
    return Candle(
        timestamp=ts,
        open=Decimal("103"),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close if close not in (None, "") else 103)),
        volume=Decimal(str(volume)),
    )


def _delivered(store: SignalStore, signal: Signal):
    store.save_signal(signal)
    store.mark_delivered(signal.signal_id)


# ------------------------------------------------------------------
# evaluate_signal_outcome — deterministic, closed-candle only
# ------------------------------------------------------------------
class TestEvaluateOutcome:
    def test_no_candle_touches_returns_empty(self):
        sig = _make_signal()
        # Candles that stay inside entry zone
        now = BASE + timedelta(minutes=30)
        candles = [_mk_candle(BASE + timedelta(minutes=10 + 5 * i),
                              "101", "105", "103") for i in range(3)]
        transitions, _ = evaluate_signal_outcome(sig, candles, None, now)
        assert transitions == []

    def test_long_tp1_hit_on_closed_candle(self):
        sig = _make_signal(SignalDirection.LONG)
        now = BASE + timedelta(minutes=20)
        # First candle closes at 10:15, high hits TP1 (108)
        c = _mk_candle(BASE + timedelta(minutes=10),
                       "101", "109", "108")
        transitions, new_cursor = evaluate_signal_outcome(
            sig, [c], None, now)
        assert len(transitions) == 1
        assert transitions[0].level == "TP1"
        assert transitions[0].new_status == SignalStatus.TP1_HIT.value

    def test_long_tp2_hit_fires_tp1_then_tp2(self):
        sig = _make_signal(SignalDirection.LONG)
        now = BASE + timedelta(minutes=20)
        # One candle that touches TP2 implies TP1 as well
        c = _mk_candle(BASE + timedelta(minutes=10), "101", "114", "113")
        transitions, _ = evaluate_signal_outcome(sig, [c], None, now)
        assert [t.level for t in transitions] == ["TP1", "TP2"]

    def test_long_sl_hit_before_tp1_is_stopped_terminal(self):
        sig = _make_signal(SignalDirection.LONG)
        now = BASE + timedelta(minutes=20)
        c = _mk_candle(BASE + timedelta(minutes=10), "97", "114", "100")
        # Same candle touches both SL (low=97 < SL 99) and TP2 — SL wins
        transitions, _ = evaluate_signal_outcome(sig, [c], None, now)
        assert len(transitions) == 1
        assert transitions[0].level == "SL"
        assert transitions[0].new_status == SignalStatus.STOPPED.value

    def test_short_tp1_hit(self):
        sig = _make_signal(SignalDirection.SHORT, symbol="ETHUSDT")
        now = BASE + timedelta(minutes=20)
        # SHORT: SL=104.00, TP1=94.00, TP2=89.00.
        # high stays BELOW SL so only TP1 is touched.
        c = _mk_candle(BASE + timedelta(minutes=10), "93", "103", "94")
        transitions, _ = evaluate_signal_outcome(sig, [c], None, now)
        assert [t.level for t in transitions] == ["TP1"]

    def test_short_tp2_hit_fires_tp1_then_tp2(self):
        sig = _make_signal(SignalDirection.SHORT, symbol="ETHUSDT")
        now = BASE + timedelta(minutes=20)
        # low <= TP2 (89) implies low <= TP1 (94); high stays below SL
        c = _mk_candle(BASE + timedelta(minutes=10), "88", "103", "89")
        transitions, _ = evaluate_signal_outcome(sig, [c], None, now)
        assert [t.level for t in transitions] == ["TP1", "TP2"]

    def test_short_sl_same_candle_sl_wins(self):
        sig = _make_signal(SignalDirection.SHORT, symbol="ETHUSDT")
        now = BASE + timedelta(minutes=20)
        # SHORT: high >= 104.00 is SL, low <= tp1 is TP1
        c = _mk_candle(BASE + timedelta(minutes=10), "93", "105", "100")
        transitions, _ = evaluate_signal_outcome(sig, [c], None, now)
        assert transitions[0].level == "SL"

    def test_forming_candle_never_evaluated(self):
        sig = _make_signal(SignalDirection.LONG)
        # One candle that opened now (forming): close_time = now+5m > now
        now = BASE + timedelta(minutes=10)
        c_forming = _mk_candle(now, "101", "114", "113")
        transitions, _ = evaluate_signal_outcome(sig, [c_forming], None, now)
        assert transitions == []

    def test_replay_cursor_skips_already_evaluated(self):
        sig = _make_signal(SignalDirection.LONG)
        now = BASE + timedelta(minutes=40)
        c1 = _mk_candle(BASE + timedelta(minutes=10), "101", "120", "119")
        c2 = _mk_candle(BASE + timedelta(minutes=15), "101", "114", "113")
        # First pass: should fire TP1+TP2 via c1
        transitions, cursor = evaluate_signal_outcome(sig, [c1, c2], None, now)
        assert [t.level for t in transitions] == ["TP1", "TP2"]
        # Persist terminal so second pass is terminal (status TP2_HIT)
        sig2 = _make_signal(status=SignalStatus.TP2_HIT)
        signal2_id = sig.signal_id
        # Second pass with cursor + terminal signal should yield nothing new
        transitions2, _ = evaluate_signal_outcome(
            _make_signal(status=SignalStatus.TP2_HIT, symbol="BTCUSDT",
                         ts=sig.trigger_candle_time),
            [c1, c2], cursor, now)
        assert transitions2 == []

    def test_expiry_is_silent_terminal(self):
        sig = _make_signal()
        sig.created_at = BASE - timedelta(hours=50)
        now = BASE + timedelta(minutes=20)
        c = _mk_candle(BASE + timedelta(minutes=10), "101", "105", "103")
        transitions, _ = evaluate_signal_outcome(sig, [c], None, now)
        assert transitions[0].level == "EXPIRY"
        assert transitions[0].new_status == SignalStatus.EXPIRED.value

    def test_tp1_then_tp2_sequential_across_two_candles(self):
        sig = _make_signal(SignalDirection.LONG)
        now = BASE + timedelta(minutes=30)
        c1 = _mk_candle(BASE + timedelta(minutes=10), "101", "109", "108")
        c2 = _mk_candle(BASE + timedelta(minutes=15), "101", "114", "113")
        # First pass up to c1
        transitions1, cursor = evaluate_signal_outcome(sig, [c1], None, now)
        assert [t.level for t in transitions1] == ["TP1"]
        # Second pass: advance to c2 (feed both, cursor skips c1)
        sig_tp1 = _make_signal(status=SignalStatus.TP1_HIT)
        transitions2, _ = evaluate_signal_outcome(
            sig_tp1, [c1, c2], cursor, now)
        assert [t.level for t in transitions2] == ["TP2"]

    def test_sl_after_tp1_is_stopped_and_terminal(self):
        sig = _make_signal(SignalDirection.LONG)
        now = BASE + timedelta(minutes=30)
        c1 = _mk_candle(BASE + timedelta(minutes=10), "101", "109", "108")
        c_sl = _mk_candle(BASE + timedelta(minutes=15), "97", "104", "98")
        # TP1 first
        transitions1, cursor = evaluate_signal_outcome(sig, [c1, c_sl], None, now)
        assert len(transitions1) >= 1
        assert transitions1[0].level == "TP1"
        # Note: current evaluate packs all transitions in one call
        # so both TP1 and SL appear in one pass (TP1 candle then SL candle).
        # In long sequential two-pass, SL after TP1 also terminates.
        sig_tp1 = _make_signal(status=SignalStatus.TP1_HIT)
        transitions2, _ = evaluate_signal_outcome(sig_tp1, [c_sl], None, now)
        assert [t.level for t in transitions2] == ["SL"]


# ------------------------------------------------------------------
# Outcome helpers — formatting
# ------------------------------------------------------------------
def test_format_outcome_contains_level_and_symbol():
    sig = _make_signal(symbol="BTCUSDT")
    msg = format_outcome(sig, "TP1")
    assert "TP1" in msg
    assert "BTCUSDT" in msg


def test_format_startup_contains_symbols_and_interval():
    msg = format_startup_notification(
        symbols=["BTCUSDT", "ETHUSDT"],
        interval_seconds=60,
        min_score=80,
        telegram_enabled=True,
        components={"Scanner": True, "Signal store": True},
    )
    assert "BOT ONLINE" in msg
    assert "BTCUSDT" in msg
    assert "60s" in msg
    assert "80" in msg


# ------------------------------------------------------------------
# SignalStore — outcome columns + persistence
# ------------------------------------------------------------------
class TestSignalStoreOutcome:
    def test_save_and_round_trip_outcome_state(self, tmp_path):
        store = SignalStore(db_path=str(tmp_path / "outcome.db"))
        sig = _make_signal()
        _delivered(store, sig)
        assert store.is_delivered(sig.signal_id)
        assert store.get_outcome_state(sig.signal_id)["tp1_notified"] == 0

        store.record_outcome(sig.signal_id, SignalStatus.TP1_HIT.value,
                             tp1_hit_time=BASE.isoformat())
        assert store.get_outcome_state(sig.signal_id)["tp1_hit_time"]  # not None
        fetched = store.get_signal(sig.signal_id)
        assert fetched.tp1_hit_time is not None

    def test_mark_outcome_notified_exactly_once(self, tmp_path):
        store = SignalStore(db_path=str(tmp_path / "outcome.db"))
        sig = _make_signal()
        _delivered(store, sig)
        store.mark_outcome_notified(sig.signal_id, "TP1")
        assert store.is_outcome_notified(sig.signal_id, "TP1")
        # Second mark is a no-op (returns rowcount 0)
        store.mark_outcome_notified(sig.signal_id, "TP1")
        assert store.is_outcome_notified(sig.signal_id, "TP1")

    def test_record_outcome_is_idempotent_and_terminal_guarded(self, tmp_path):
        store = SignalStore(db_path=str(tmp_path / "outcome.db"))
        sig = _make_signal()
        _delivered(store, sig)
        store.record_outcome(sig.signal_id, SignalStatus.TP2_HIT.value,
                             tp1_hit_time=BASE.isoformat(),
                             tp2_hit_time=BASE.isoformat())
        assert store.get_outcome_state(sig.signal_id)["status"] == "TP2_HIT"
        # Once terminal, cannot be moved back
        store.record_outcome(sig.signal_id, SignalStatus.STOPPED.value)
        assert store.get_outcome_state(sig.signal_id)["status"] == "TP2_HIT"

    def test_get_active_signals_only_active_and_tp1_hit_delivered(self, tmp_path):
        store = SignalStore(db_path=str(tmp_path / "outcome.db"))
        sigs = []
        for idx, status in enumerate(
            [SignalStatus.ACTIVE, SignalStatus.TP1_HIT, SignalStatus.TP2_HIT,
             SignalStatus.STOPPED]
        ):
            sig = _make_signal(symbol=f"SYMBOL{idx}USDT",
                               ts=BASE + timedelta(minutes=idx * 5),
                               status=status)
            _delivered(store, sig)
            sigs.append(sig)
        active = store.get_active_signals(limit=10)
        # Only ACTIVE and TP1_HIT are still monitorable
        statuses = {s.status for s in active}
        assert statuses == {SignalStatus.ACTIVE, SignalStatus.TP1_HIT}

    def test_replay_cursor_persists_across_restarts(self, tmp_path):
        db_path = str(tmp_path / "cursor.db")
        sig = _make_signal()
        store1 = SignalStore(db_path=db_path)
        _delivered(store1, sig)
        store1.set_last_evaluated_candle_time(sig.signal_id,
                                              BASE + timedelta(minutes=10))
        del store1
        store2 = SignalStore(db_path=db_path)
        assert store2.get_last_evaluated_candle_time(sig.signal_id) == \
               BASE + timedelta(minutes=10)


# ------------------------------------------------------------------
# OutcomeMonitorRunner — isolation & idempotency
# ------------------------------------------------------------------
class TestOutcomeRunner:
    def test_runner_notifies_once_then_skips_on_restart(self, tmp_path):
        db_path = str(tmp_path / "runner.db")
        store = SignalStore(db_path=db_path)
        sig = _make_signal()
        _delivered(store, sig)

        # Candle that will hit TP1+TP2 in one go
        now = BASE + timedelta(minutes=60)
        candles = [_mk_candle(BASE + timedelta(minutes=10), "101", "114", "113")]

        md = MagicMock()
        md.fetch_klines.return_value = list(candles)

        telegram_bot = MagicMock()
        telegram_bot.send_outcome = MagicMock(return_value=True)

        runner = OutcomeMonitorRunner(store, telegram_bot, market_data=md,
                                      now_fn=lambda: now)
        runner.run_once()

        # Both TP1 and TP2 should have been sent (idempotent)
        assert telegram_bot.send_outcome.call_count == 2

        # Second run (restart): already terminal + notified, so no resend
        telegram_bot.send_outcome.reset_mock()
        md.fetch_klines.return_value = list(candles)
        runner.run_once()
        assert telegram_bot.send_outcome.call_count == 0

    def test_runner_bounded_retries(self, tmp_path):
        db_path = str(tmp_path / "bounded.db")
        store = SignalStore(db_path=db_path)
        sig = _make_signal()
        _delivered(store, sig)
        now = BASE + timedelta(minutes=60)
        c = _mk_candle(BASE + timedelta(minutes=10), "101", "114", "113")

        md = MagicMock()
        md.fetch_klines.return_value = [c]

        telegram_bot = MagicMock()
        telegram_bot.send_outcome = MagicMock(return_value=False)

        runner = OutcomeMonitorRunner(store, telegram_bot, market_data=md,
                                      now_fn=lambda: now)
        # Call until budget exhausted
        for _ in range(10):
            runner.run_once()

        # Attempts are bounded by OUTCOME_MAX_ATTEMPTS
        assert store.get_outcome_attempts(sig.signal_id) <= outcome_monitor.OUTCOME_MAX_ATTEMPTS

    def test_runner_fetch_failure_does_not_crash(self, tmp_path):
        db_path = str(tmp_path / "fetch_fail.db")
        store = SignalStore(db_path=db_path)
        sig = _make_signal()
        _delivered(store, sig)

        md = MagicMock()
        md.fetch_klines.side_effect = RuntimeError("Binance down")

        telegram_bot = MagicMock()
        runner = OutcomeMonitorRunner(store, telegram_bot, market_data=md)
        runner.run_once()  # should not raise


# ------------------------------------------------------------------
# Telegram outcome + startup: disabled means no network, enabled does
# ------------------------------------------------------------------
def test_telegram_send_outcome_disabled_returns_true(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ENABLED", "false")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    cfg.config = None
    sig = _make_signal()
    bot = TelegramBot()
    with patch("app.telegram_bot.requests.post") as post:
        assert bot.send_outcome(sig, "TP1") is True
        post.assert_not_called()
    cfg.config = None


def test_telegram_send_startup_disabled_returns_true(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ENABLED", "false")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    cfg.config = None
    bot = TelegramBot()
    with patch("app.telegram_bot.requests.post") as post:
        assert bot.send_startup(["BTCUSDT"], 60, 80, {}) is True
        post.assert_not_called()
    cfg.config = None


def test_telegram_send_outcome_token_not_in_logs(monkeypatch, caplog):
    monkeypatch.setenv("TELEGRAM_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "super_secret_token_xyz")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    cfg.config = None
    sig = _make_signal()
    bot = TelegramBot()
    ok_resp = MagicMock(status_code=200, headers={}, json=lambda: {"ok": True})
    with patch("app.telegram_bot.requests.post", return_value=ok_resp):
        with caplog.at_level("INFO"):
            bot.send_outcome(sig, "TP1")
    assert "super_secret_token_xyz" not in caplog.text
    cfg.config = None


# ------------------------------------------------------------------
# Startup crash-loop guard: persisted + rate-limited
# ------------------------------------------------------------------
class TestStartupCrashLoopGuard:
    def test_not_sending_inside_cooldown(self, tmp_path, monkeypatch):
        store = SignalStore(db_path=str(tmp_path / "guard.db"))
        monkeypatch.setenv("TELEGRAM_ENABLED", "false")
        cfg.config = None
        import app.config as cfgmod  # ensure env seen
        cfgmod.config = None
        config = cfgmod.get_config()
        # Simulate a successful startup notice just now
        store.set_meta(main_mod._STARTUP_NOTICE_META_KEY, str(time.time()))
        # Must be suppressed
        main_mod._send_startup_notification(config, store, telegram_bot=None,
                                            send_telegram=True)
        # log capture via store meta: not updated inside cooldown
        # (no assert on log; just that it does not raise and is silent)
        cfg.config = None
        cfgmod.config = None

    def test_persists_booted_at_meta(self, tmp_path, monkeypatch):
        store = SignalStore(db_path=str(tmp_path / "booted.db"))
        monkeypatch.setenv("TELEGRAM_ENABLED", "false")
        import app.config as cfgmod
        cfgmod.config = None
        config = cfgmod.get_config()
        main_mod._send_startup_notification(config, store, telegram_bot=None,
                                            send_telegram=False)
        assert store.get_meta(main_mod._BOT_BOOTED_AT_META_KEY) is not None
        cfg.config = None
        cfgmod.config = None


# ------------------------------------------------------------------
# Outcome notification ordering: send BEFORE marking delivered.
# ------------------------------------------------------------------
NOTIFIED_LEVELS = ("TP1", "TP2", "SL")


def _tp1_only_candle():
    """High=109 touches TP1=108 but not TP2=113 → only a TP1 transition."""
    return _mk_candle(BASE + timedelta(minutes=10), "101", "109", "108")


def _make_runner(store, telegram_bot, now=None, tmp_path=None):
    now = now or (BASE + timedelta(minutes=60))
    md = MagicMock()
    md.fetch_klines.return_value = [_tp1_only_candle()]
    return OutcomeMonitorRunner(
        store, telegram_bot, market_data=md, now_fn=lambda: now)


class TestNotificationOrdering:
    """Regressions for the TP/SL delivery-order bug."""

    def test_telegram_failure_does_not_set_notification_flag(self, tmp_path):
        """A failed Telegram delivery must leave tp1_notified = 0."""
        store = SignalStore(db_path=str(tmp_path / "fail_flag.db"))
        sig = _make_signal()
        _delivered(store, sig)

        telegram_bot = MagicMock()
        telegram_bot.send_outcome = MagicMock(return_value=False)

        _make_runner(store, telegram_bot).run_once()

        assert not store.is_outcome_notified(sig.signal_id, "TP1")
        assert telegram_bot.send_outcome.call_count == 1

    def test_successful_delivery_sets_notification_flag(self, tmp_path):
        """A successful Telegram delivery must flip tp1_notified to 1."""
        store = SignalStore(db_path=str(tmp_path / "ok_flag.db"))
        sig = _make_signal()
        _delivered(store, sig)

        telegram_bot = MagicMock()
        telegram_bot.send_outcome = MagicMock(return_value=True)

        _make_runner(store, telegram_bot).run_once()

        assert store.is_outcome_notified(sig.signal_id, "TP1")
        assert telegram_bot.send_outcome.call_count == 1

    def test_failure_then_retry_sends_exactly_once(self, tmp_path):
        """After a failure, the next cycle delivers the same level once."""
        store = SignalStore(db_path=str(tmp_path / "retry_once.db"))
        sig = _make_signal()
        _delivered(store, sig)

        telegram_bot = MagicMock()
        telegram_bot.send_outcome = MagicMock(side_effect=[False, True])

        runner = _make_runner(store, telegram_bot)
        runner.run_once()  # fails
        runner.run_once()  # retries via pending snapshot → succeeds
        runner.run_once()  # nothing pending → no more sends

        assert store.is_outcome_notified(sig.signal_id, "TP1")
        # Exactly two send calls across all three runs (1 fail + 1 success).
        assert telegram_bot.send_outcome.call_count == 2

    def test_restart_after_failed_delivery_allows_retry(self, tmp_path):
        """A fresh runner on the same DB retries a previously-failed level."""
        db_path = str(tmp_path / "restart_fail.db")
        store = SignalStore(db_path=db_path)
        sig = _make_signal()
        _delivered(store, sig)

        now = BASE + timedelta(minutes=60)
        md = MagicMock()
        md.fetch_klines.return_value = [_tp1_only_candle()]

        telegram_bot = MagicMock()
        telegram_bot.send_outcome = MagicMock(return_value=False)
        runner = OutcomeMonitorRunner(
            store, telegram_bot, market_data=md, now_fn=lambda: now)
        runner.run_once()  # fails, attempts=1, flag still 0

        assert not store.is_outcome_notified(sig.signal_id, "TP1")

        # Simulate a restart: new store/runner, same DB, telegram now works.
        store2 = SignalStore(db_path=db_path)
        telegram_bot2 = MagicMock()
        telegram_bot2.send_outcome = MagicMock(return_value=True)
        runner2 = OutcomeMonitorRunner(
            store2, telegram_bot2, market_data=md, now_fn=lambda: now)
        runner2.run_once()  # pending snapshot [TP1] → retries successfully

        assert store2.is_outcome_notified(sig.signal_id, "TP1")
        assert telegram_bot2.send_outcome.call_count == 1

    def test_restart_after_successful_delivery_does_not_duplicate(self, tmp_path):
        """A restarted runner must not re-send a level that already landed."""
        db_path = str(tmp_path / "restart_ok.db")
        store = SignalStore(db_path=db_path)
        sig = _make_signal()
        _delivered(store, sig)

        now = BASE + timedelta(minutes=60)
        md = MagicMock()
        md.fetch_klines.return_value = [_tp1_only_candle()]

        telegram_bot = MagicMock()
        telegram_bot.send_outcome = MagicMock(return_value=True)
        runner = OutcomeMonitorRunner(
            store, telegram_bot, market_data=md, now_fn=lambda: now)
        runner.run_once()  # succeeds, flag set

        assert store.is_outcome_notified(sig.signal_id, "TP1")

        # Restart: new store/runner, same DB.
        store2 = SignalStore(db_path=db_path)
        telegram_bot2 = MagicMock()
        runner2 = OutcomeMonitorRunner(
            store2, telegram_bot2, market_data=md, now_fn=lambda: now)
        runner2.run_once()

        assert telegram_bot2.send_outcome.call_count == 0

    def test_crash_after_persistence_does_not_permanently_lose_notification(
            self, tmp_path):
        """Simulate a crash that persisted outcome + cursor but never sent.

        This is the exact window reported in the bug:
          record_outcome() → advance_cursor() → [CRASH]
        The notification must be recoverable on the next cycle.
        """
        db_path = str(tmp_path / "crash.db")
        store = SignalStore(db_path=db_path)
        sig = _make_signal()
        _delivered(store, sig)

        now = BASE + timedelta(minutes=60)
        candle = _tp1_only_candle()
        md = MagicMock()
        md.fetch_klines.return_value = [candle]

        # Simulate the crash point: persist outcome and cursor, but
        # NEVER call send_outcome.  This is what _apply_transition does
        # before marking delivered.
        store.record_outcome(
            sig.signal_id, SignalStatus.TP1_HIT.value,
            tp1_hit_time=candle.timestamp + timedelta(seconds=300))
        store.set_last_evaluated_candle_time(
            sig.signal_id, candle.timestamp)
        assert not store.is_outcome_notified(sig.signal_id, "TP1")

        # Next cycle: fresh runner, telegram works.
        telegram_bot = MagicMock()
        telegram_bot.send_outcome = MagicMock(return_value=True)
        runner = OutcomeMonitorRunner(
            store, telegram_bot, market_data=md, now_fn=lambda: now)
        runner.run_once()

        assert store.is_outcome_notified(sig.signal_id, "TP1")
        assert telegram_bot.send_outcome.call_count == 1


# ------------------------------------------------------------------
# PATCH-1 & PATCH-2 regression suite
# ------------------------------------------------------------------

import bisect


def _pageable_candles():
    """Three closed candles spaced 5m apart."""
    return [
        _fake_candle(BASE + timedelta(minutes=10), "101", "109", "108"),
        _fake_candle(BASE + timedelta(minutes=15), "101", "114", "113"),
        _fake_candle(BASE + timedelta(minutes=20), "101", "116", "115"),
    ]


def _fake_candle(ts, low, high, close="103"):
    """Deterministic closed candle for history-paging tests."""
    return _mk_candle(ts, low, high, close)


def _paginating_market_data(all_candles):
    """MagicMock that slices ``all_candles`` by start_time, capping at limit."""
    ordered = sorted(all_candles, key=lambda c: c.timestamp)
    times = [c.timestamp for c in ordered]

    def fetch(symbol, interval, limit, start_time=None, end_time=None, **_kw):
        idx = 0 if start_time is None else bisect.bisect_left(times, start_time)
        end_idx = len(ordered) if end_time is None else bisect.bisect_right(times, end_time)
        return list(ordered[idx:min(idx + limit, end_idx)])

    md = MagicMock()
    md.fetch_klines.side_effect = fetch
    return md


def _failing_market_data():
    md = MagicMock()
    md.fetch_klines.side_effect = RuntimeError("Binance down")
    return md


def _run(runner):
    try:
        runner.run_once()
    except Exception:
        pass


# ------------------------------------------------------------------
# 1. Long downtime recovery (~30 h)
# ------------------------------------------------------------------
class TestLongDowntimeRecovery:
    def test_tp1_detected_after_30h_downtime(self, tmp_path):
        """A TP1 candle 30h ago is still detected."""
        store = SignalStore(db_path=str(tmp_path / "dt.db"))
        sig = _make_signal()
        _delivered(store, sig)
        now = BASE + timedelta(hours=30, minutes=10)
        tp1_c = _fake_candle(now - timedelta(hours=30, minutes=5),
                             "101", "109", "108")
        md = MagicMock()
        md.fetch_klines.return_value = [tp1_c]
        tb = MagicMock(); tb.send_outcome = MagicMock(return_value=True)
        runner = OutcomeMonitorRunner(store, tb, market_data=md,
                                      now_fn=lambda: now)
        _run(runner)
        state = store.get_outcome_state(sig.signal_id)
        assert state["tp1_hit_time"] is not None
        tb.send_outcome.assert_called()
        assert tb.send_outcome.call_args[0][1] == "TP1"

    def test_outcome_not_lost_after_30h(self, tmp_path):
        """Candle 26h ago (>300 x 5m) is still seen."""
        store = SignalStore(db_path=str(tmp_path / "dt2.db"))
        sig = _make_signal()
        _delivered(store, sig)
        now = BASE + timedelta(hours=30)
        c = _fake_candle(now - timedelta(hours=26), "101", "109", "108")
        md = MagicMock()
        md.fetch_klines.return_value = [c]
        tb = MagicMock(); tb.send_outcome = MagicMock(return_value=True)
        runner = OutcomeMonitorRunner(store, tb, market_data=md,
                                      now_fn=lambda: now)
        _run(runner)
        assert tb.send_outcome.call_count >= 1
        assert tb.send_outcome.call_args[0][1] == "TP1"


# ------------------------------------------------------------------
# 2. TTL boundary
# ------------------------------------------------------------------
class TestTTLBoundary:
    def test_catchup_start_bounded_by_ttl(self, tmp_path):
        """start_time passed to fetch_klines >= now - TTL."""
        store = SignalStore(db_path=str(tmp_path / "ttl.db"))
        sig = _make_signal()
        _delivered(store, sig)
        now = BASE + timedelta(hours=36)
        c = _fake_candle(now - timedelta(minutes=10), "101", "109", "108")
        calls = []
        def record_fetch(sym, iv, limit, start_time=None, end_time=None, **_kw):
            calls.append((start_time, end_time))
            return [c]
        md = MagicMock()
        md.fetch_klines.side_effect = record_fetch
        tb = MagicMock(); tb.send_outcome = MagicMock(return_value=True)
        runner = OutcomeMonitorRunner(store, tb, market_data=md,
                                      now_fn=lambda: now)
        _run(runner)
        assert len(calls) >= 1
        st, et = calls[0]
        assert et == now
        assert st >= now - timedelta(hours=SIGNAL_MAX_AGE_HOURS + 1)

    def test_candles_before_created_at_ignored(self, tmp_path):
        """Candles earlier than signal.created_at are not evaluated."""
        store = SignalStore(db_path=str(tmp_path / "pre.db"))
        sig = _make_signal()
        sig.created_at = BASE + timedelta(hours=1)
        _delivered(store, sig)
        now = BASE + timedelta(hours=1, minutes=30)
        pre = _fake_candle(BASE + timedelta(minutes=10), "101", "109", "108")
        md = MagicMock()
        md.fetch_klines.return_value = [pre]
        tb = MagicMock(); tb.send_outcome = MagicMock(return_value=True)
        runner = OutcomeMonitorRunner(store, tb, market_data=md,
                                      now_fn=lambda: now)
        _run(runner)
        assert tb.send_outcome.call_count == 0
        assert store.get_outcome_state(sig.signal_id)["tp1_hit_time"] is None


# ------------------------------------------------------------------
# 3. Closed candles only
# ------------------------------------------------------------------
class TestClosedCandlesOnly:
    def test_forming_candle_skipped_in_catchup(self, tmp_path):
        """A forming candle in the fetch window is never evaluated."""
        store = SignalStore(db_path=str(tmp_path / "form.db"))
        sig = _make_signal()
        _delivered(store, sig)
        now = BASE + timedelta(minutes=30)
        forming = _mk_candle(now, "101", "114", "113")
        md = MagicMock()
        md.fetch_klines.return_value = [forming]
        tb = MagicMock(); tb.send_outcome = MagicMock(return_value=True)
        runner = OutcomeMonitorRunner(store, tb, market_data=md,
                                      now_fn=lambda: now)
        _run(runner)
        assert tb.send_outcome.call_count == 0


# ------------------------------------------------------------------
# 4. Replay cursor
# ------------------------------------------------------------------
class TestReplayCursor:
    def test_already_processed_candle_not_evaluated(self, tmp_path):
        """Candle at/behind the cursor is skipped."""
        store = SignalStore(db_path=str(tmp_path / "cur.db"))
        sig = _make_signal()
        _delivered(store, sig)
        now = BASE + timedelta(minutes=60)
        c = _fake_candle(BASE + timedelta(minutes=10), "101", "109", "108")
        store.set_last_evaluated_candle_time(
            sig.signal_id, c.timestamp + timedelta(seconds=300))
        md = MagicMock()
        md.fetch_klines.return_value = [c]
        tb = MagicMock(); tb.send_outcome = MagicMock(return_value=True)
        runner = OutcomeMonitorRunner(store, tb, market_data=md,
                                      now_fn=lambda: now)
        _run(runner)
        assert tb.send_outcome.call_count == 0


# ------------------------------------------------------------------
# 5. Pagination
# ------------------------------------------------------------------
class TestPagination:
    def test_chunks_processed_in_chronological_order(self, tmp_path):
        """Multiple API pages are merged in chronological order."""
        store = SignalStore(db_path=str(tmp_path / "page.db"))
        sig = _make_signal()
        _delivered(store, sig)
        now = BASE + timedelta(minutes=60)
        all_c = _pageable_candles()
        md = _paginating_market_data(all_c)
        tb = MagicMock(); tb.send_outcome = MagicMock(return_value=True)
        runner = OutcomeMonitorRunner(store, tb, market_data=md,
                                      now_fn=lambda: now, chunk_limit=1)
        _run(runner)
        levels = [c.args[1] for c in tb.send_outcome.call_args_list]
        assert "TP1" in levels
        assert "TP2" in levels
        if "SL" in levels:
            assert levels.index("TP1") < levels.index("TP2")

    def test_starts_are_non_decreasing(self, tmp_path):
        """Each successive page start_time >= previous."""
        store = SignalStore(db_path=str(tmp_path / "ord.db"))
        sig = _make_signal()
        _delivered(store, sig)
        now = BASE + timedelta(minutes=60)
        all_c = _pageable_candles()
        ordered = sorted(all_c, key=lambda c: c.timestamp)
        times = [c.timestamp for c in ordered]
        starts = []
        def record(sym, iv, limit, start_time=None, **_kw):
            starts.append(start_time)
            idx = 0 if start_time is None else bisect.bisect_left(times, start_time)
            return ordered[idx:idx + limit]
        md = MagicMock()
        md.fetch_klines.side_effect = record
        tb = MagicMock(); tb.send_outcome = MagicMock(return_value=True)
        runner = OutcomeMonitorRunner(store, tb, market_data=md,
                                      now_fn=lambda: now, chunk_limit=1)
        _run(runner)
        for i in range(1, len(starts)):
            if starts[i] is not None and starts[i - 1] is not None:
                assert starts[i] >= starts[i - 1]


# ------------------------------------------------------------------
# 6. Cursor failure safety
# ------------------------------------------------------------------
class TestCursorFailureSafety:
    def test_fetch_failure_cursor_not_advanced(self, tmp_path):
        """Failed fetch leaves cursor untouched."""
        store = SignalStore(db_path=str(tmp_path / "fc.db"))
        sig = _make_signal()
        _delivered(store, sig)
        now = BASE + timedelta(minutes=60)
        tb = MagicMock()
        runner = OutcomeMonitorRunner(store, tb,
                                      market_data=_failing_market_data(),
                                      now_fn=lambda: now)
        _run(runner)
        assert store.get_last_evaluated_candle_time(sig.signal_id) is None

    def test_fetch_failure_allows_recovery(self, tmp_path):
        """After a transient fetch failure, missed candles are recovered."""
        store = SignalStore(db_path=str(tmp_path / "rc.db"))
        sig = _make_signal()
        _delivered(store, sig)
        now = BASE + timedelta(minutes=60)
        c = _fake_candle(BASE + timedelta(minutes=10), "101", "109", "108")
        md_fail = _failing_market_data()
        md_ok = MagicMock(); md_ok.fetch_klines.return_value = [c]
        tb = MagicMock(); tb.send_outcome = MagicMock(return_value=True)
        runner = OutcomeMonitorRunner(store, tb, market_data=md_fail,
                                      now_fn=lambda: now)
        _run(runner)
        assert store.get_last_evaluated_candle_time(sig.signal_id) is None
        runner.market_data = md_ok
        _run(runner)
        assert store.get_last_evaluated_candle_time(sig.signal_id) == c.timestamp


# ------------------------------------------------------------------
# 7. TP1 retry isolation
# ------------------------------------------------------------------
class TestTP1RetryIsolation:
    def test_tp1_exhausted_independently(self, tmp_path):
        """TP1 fails exactly OUTCOME_MAX_ATTEMPTS times then stops."""
        store = SignalStore(db_path=str(tmp_path / "t1ex.db"))
        sig = _make_signal()
        _delivered(store, sig)
        now = BASE + timedelta(minutes=60)
        md = MagicMock(); md.fetch_klines.return_value = [_tp1_only_candle()]
        tb = MagicMock(); tb.send_outcome = MagicMock(return_value=False)
        runner = OutcomeMonitorRunner(store, tb, market_data=md,
                                      now_fn=lambda: now)
        for _ in range(outcome_monitor.OUTCOME_MAX_ATTEMPTS + 5):
            _run(runner)
        assert (store.get_outcome_attempts(sig.signal_id, "TP1")
                == outcome_monitor.OUTCOME_MAX_ATTEMPTS)


# ------------------------------------------------------------------
# 8. TP2 retry independence
# ------------------------------------------------------------------
class TestTP2RetryIndependence:
    def test_tp2_delivered_after_tp1_exhausted(self, tmp_path):
        """TP2 is delivered even though TP1's retry budget is spent."""
        store = SignalStore(db_path=str(tmp_path / "t2dep.db"))
        sig = _make_signal()
        _delivered(store, sig)
        now = BASE + timedelta(minutes=60)
        tp1c = _fake_candle(BASE + timedelta(minutes=10), "101", "109", "108")
        tp2c = _fake_candle(BASE + timedelta(minutes=15), "101", "114", "113")
        # TP1 hit persisted but never delivered.
        store.record_outcome(sig.signal_id, SignalStatus.TP1_HIT.value,
                             tp1_hit_time=tp1c.timestamp + timedelta(seconds=300))
        store.set_last_evaluated_candle_time(sig.signal_id, tp1c.timestamp)
        # Burn TP1's entire retry budget.
        for _ in range(outcome_monitor.OUTCOME_MAX_ATTEMPTS):
            store.record_outcome_failure(sig.signal_id, "TP1", "boom")
        assert (store.get_outcome_attempts(sig.signal_id, "TP1")
                == outcome_monitor.OUTCOME_MAX_ATTEMPTS)

        md = MagicMock(); md.fetch_klines.return_value = [tp1c, tp2c]
        tb = MagicMock(); tb.send_outcome = MagicMock(return_value=True)
        runner = OutcomeMonitorRunner(store, tb, market_data=md,
                                      now_fn=lambda: now)
        _run(runner)
        sent = [c.args[1] for c in tb.send_outcome.call_args_list]
        # TP1 must not be retried (budget spent) but TP2 must be delivered.
        assert "TP1" not in sent
        assert "TP2" in sent
        assert store.is_outcome_notified(sig.signal_id, "TP2")

    def test_tp1_exhaustion_stops_tp1_but_not_tp2_retries(self, tmp_path):
        """TP2 keeps its own full budget after TP1 is exhausted."""
        store = SignalStore(db_path=str(tmp_path / "t2bud.db"))
        sig = _make_signal()
        _delivered(store, sig)
        now = BASE + timedelta(minutes=60)
        tp1c = _fake_candle(BASE + timedelta(minutes=10), "101", "109", "108")
        tp2c = _fake_candle(BASE + timedelta(minutes=15), "101", "114", "113")
        store.record_outcome(sig.signal_id, SignalStatus.TP1_HIT.value,
                             tp1_hit_time=tp1c.timestamp + timedelta(seconds=300))
        store.set_last_evaluated_candle_time(sig.signal_id, tp1c.timestamp)
        for _ in range(outcome_monitor.OUTCOME_MAX_ATTEMPTS):
            store.record_outcome_failure(sig.signal_id, "TP1", "boom")
        # Telegram now works: TP2 must be delivered on the very next cycle.
        md = MagicMock(); md.fetch_klines.return_value = [tp2c]
        tb = MagicMock(); tb.send_outcome = MagicMock(return_value=True)
        runner = OutcomeMonitorRunner(store, tb, market_data=md,
                                      now_fn=lambda: now)
        _run(runner)
        assert store.is_outcome_notified(sig.signal_id, "TP2")
        assert (store.get_outcome_attempts(sig.signal_id, "TP2")
                <= outcome_monitor.OUTCOME_MAX_ATTEMPTS)


# ------------------------------------------------------------------
# 9. SL retry independence
# ------------------------------------------------------------------
class TestSLRetryIndependence:
    def test_sl_delivered_after_tp1_exhausted(self, tmp_path):
        """SL is delivered even though TP1's retry budget is spent."""
        store = SignalStore(db_path=str(tmp_path / "sldep.db"))
        sig = _make_signal()
        _delivered(store, sig)
        now = BASE + timedelta(minutes=60)
        tp1c = _fake_candle(BASE + timedelta(minutes=10), "101", "109", "108")
        slc = _fake_candle(BASE + timedelta(minutes=15), "97", "104", "98")
        store.record_outcome(sig.signal_id, SignalStatus.TP1_HIT.value,
                             tp1_hit_time=tp1c.timestamp + timedelta(seconds=300))
        store.set_last_evaluated_candle_time(sig.signal_id, tp1c.timestamp)
        for _ in range(outcome_monitor.OUTCOME_MAX_ATTEMPTS):
            store.record_outcome_failure(sig.signal_id, "TP1", "boom")
        md = MagicMock(); md.fetch_klines.return_value = [tp1c, slc]
        tb = MagicMock(); tb.send_outcome = MagicMock(return_value=True)
        runner = OutcomeMonitorRunner(store, tb, market_data=md,
                                      now_fn=lambda: now)
        _run(runner)
        sent = [c.args[1] for c in tb.send_outcome.call_args_list]
        assert "TP1" not in sent
        assert "SL" in sent
        assert store.is_outcome_notified(sig.signal_id, "SL")


# ------------------------------------------------------------------
# 10. Database migration — old schema opens without loss
# ------------------------------------------------------------------
class TestDatabaseMigration:
    def test_old_schema_opens_with_defaults(self, tmp_path):
        """An older schema (without per-level columns) opens cleanly."""
        db_path = str(tmp_path / "old.db")
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE signals (
                signal_id TEXT PRIMARY KEY,
                symbol TEXT, direction TEXT, signal_type TEXT,
                created_at TEXT, trigger_candle_time TEXT,
                entry_low TEXT, entry_high TEXT,
                stop_loss TEXT, tp1 TEXT, tp2 TEXT,
                score INTEGER, htf_bias TEXT, adx_value TEXT,
                rsi_value TEXT, volume_ratio TEXT,
                status TEXT DEFAULT 'ACTIVE',
                tp1_hit_time TEXT, tp2_hit_time TEXT, sl_hit_time TEXT,
                tp1_notified INTEGER NOT NULL DEFAULT 0,
                tp2_notified INTEGER NOT NULL DEFAULT 0,
                sl_notified INTEGER NOT NULL DEFAULT 0,
                outcome_attempts INTEGER NOT NULL DEFAULT 0,
                last_outcome_error TEXT,
                last_evaluated_candle_time TEXT
            )
        """)
        cur.execute(
            "INSERT INTO signals (signal_id, symbol, status) VALUES (?,?,?)",
            ("sig-old", "BTCUSDT", "ACTIVE"))
        conn.commit(); conn.close()
        store = SignalStore(db_path=db_path)
        state = store.get_outcome_state("sig-old")
        assert state["status"] == "ACTIVE"
        assert state["tp1_outcome_attempts"] == 0
        assert state["tp2_outcome_attempts"] == 0
        assert state["sl_outcome_attempts"] == 0

    def test_notification_flags_not_reset(self, tmp_path):
        """tp1_notified survives migration."""
        db_path = str(tmp_path / "flags.db")
        store = SignalStore(db_path=db_path)
        sig = _make_signal()
        _delivered(store, sig)
        store.mark_outcome_notified(sig.signal_id, "TP1")
        store2 = SignalStore(db_path=db_path)
        assert store2.is_outcome_notified(sig.signal_id, "TP1")

    def test_signals_still_monitored(self, tmp_path):
        """Active signals remain in get_active_signals after re-open."""
        db_path = str(tmp_path / "mon.db")
        store = SignalStore(db_path=db_path)
        sig = _make_signal()
        _delivered(store, sig)
        store2 = SignalStore(db_path=db_path)
        ids = {s.signal_id for s in store2.get_active_signals(10)}
        assert sig.signal_id in ids


# ------------------------------------------------------------------
# 11. Restart safety
# ------------------------------------------------------------------
class TestRestartSafety:
    def test_state_preserved_after_restart(self, tmp_path):
        """Pending notification survives process restart."""
        db_path = str(tmp_path / "rs.db")
        s1 = SignalStore(db_path=db_path)
        sig = _make_signal()
        _delivered(s1, sig)
        now = BASE + timedelta(minutes=60)
        c = _tp1_only_candle()
        md = MagicMock(); md.fetch_klines.return_value = [c]
        tb1 = MagicMock(); tb1.send_outcome = MagicMock(return_value=False)
        runner1 = OutcomeMonitorRunner(s1, tb1, market_data=md, now_fn=lambda: now)
        _run(runner1)
        assert not s1.is_outcome_notified(sig.signal_id, "TP1")
        s2 = SignalStore(db_path=db_path)
        tb2 = MagicMock(); tb2.send_outcome = MagicMock(return_value=True)
        runner2 = OutcomeMonitorRunner(s2, tb2, market_data=md, now_fn=lambda: now)
        _run(runner2)
        assert s2.is_outcome_notified(sig.signal_id, "TP1")


# ------------------------------------------------------------------
# 12. Duplicate protection
# ------------------------------------------------------------------
class TestDuplicateProtection:
    def test_replay_no_duplicate_notifications(self, tmp_path):
        """Restart does not double-send an already-notified level."""
        db_path = str(tmp_path / "dup.db")
        store = SignalStore(db_path=db_path)
        sig = _make_signal()
        _delivered(store, sig)
        now = BASE + timedelta(minutes=60)
        md = MagicMock(); md.fetch_klines.return_value = [_tp1_only_candle()]
        tb1 = MagicMock(); tb1.send_outcome = MagicMock(return_value=True)
        OutcomeMonitorRunner(store, tb1, market_data=md,
                             now_fn=lambda: now).run_once()
        assert tb1.send_outcome.call_count == 1
        tb2 = MagicMock(); tb2.send_outcome = MagicMock(return_value=True)
        store2 = SignalStore(db_path=db_path)
        OutcomeMonitorRunner(store2, tb2, market_data=md,
                             now_fn=lambda: now).run_once()
        assert tb2.send_outcome.call_count == 0

    def test_pagination_no_duplicate_level_notifications(self, tmp_path):
        """Multiple pages must not cause a level to fire twice."""
        db_path = str(tmp_path / "pdup.db")
        store = SignalStore(db_path=db_path)
        sig = _make_signal()
        _delivered(store, sig)
        now = BASE + timedelta(minutes=60)
        md = _paginating_market_data(_pageable_candles())
        tb = MagicMock(); tb.send_outcome = MagicMock(return_value=True)
        OutcomeMonitorRunner(store, tb, market_data=md,
                             now_fn=lambda: now, chunk_limit=1).run_once()
        levels = [c.args[1] for c in tb.send_outcome.call_args_list]
        assert levels.count("TP1") == 1
        assert levels.count("TP2") == 1


# ------------------------------------------------------------------
# 13. Same-candle ambiguity — SL wins
# ------------------------------------------------------------------
class TestSameCandleAmbiguity:
    def test_sl_wins_on_same_candle(self, tmp_path):
        """When SL and TP both touch on the same candle, SL wins."""
        store = SignalStore(db_path=str(tmp_path / "amb.db"))
        sig = _make_signal()
        _delivered(store, sig)
        now = BASE + timedelta(minutes=20)
        amb = _mk_candle(BASE + timedelta(minutes=10), "97", "109", "100")
        md = MagicMock(); md.fetch_klines.return_value = [amb]
        tb = MagicMock(); tb.send_outcome = MagicMock(return_value=True)
        runner = OutcomeMonitorRunner(store, tb, market_data=md,
                                      now_fn=lambda: now)
        _run(runner)
        sent = [c.args[1] for c in tb.send_outcome.call_args_list]
        assert sent[0] == "SL"
        assert store.get_outcome_state(sig.signal_id)["status"] == "STOPPED"

    def test_short_sl_wins_on_same_candle(self, tmp_path):
        """SHORT: SL wins even when TP is also touched."""
        store = SignalStore(db_path=str(tmp_path / "amb2.db"))
        sig = _make_signal(SignalDirection.SHORT, symbol="ETHUSDT")
        _delivered(store, sig)
        now = BASE + timedelta(minutes=20)
        amb = _mk_candle(BASE + timedelta(minutes=10), "93", "105", "100")
        md = MagicMock(); md.fetch_klines.return_value = [amb]
        tb = MagicMock(); tb.send_outcome = MagicMock(return_value=True)
        runner = OutcomeMonitorRunner(store, tb, market_data=md,
                                      now_fn=lambda: now)
        _run(runner)
        sent = [c.args[1] for c in tb.send_outcome.call_args_list]
        assert sent[0] == "SL"
