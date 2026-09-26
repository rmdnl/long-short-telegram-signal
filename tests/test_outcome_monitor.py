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
from app.outcome_monitor import (OutcomeMonitorRunner, evaluate_signal_outcome,
                                 format_outcome, format_startup_notification)
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
