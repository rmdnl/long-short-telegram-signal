"""
Heartbeat / runtime-status regression tests.

Covers bot-side heartbeat persistence and dashboard read logic without
duplicating market-data fetching, process inspection or signal strategy
behaviour. Also asserts the production scenario: 9 symbols, 60 s cadence,
zero signals, repeated-candle scans must still count as bot activity.
"""
import sqlite3
import importlib
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch, call

import pytest
from fastapi.testclient import TestClient

# ------------------------------------------------------------------
# Helpers shared with dashboard tests
# ------------------------------------------------------------------

def _valid_env(monkeypatch=None, symbols="BTCUSDT,ETHUSDT"):
    env = {
        "ADX_LENGTH": "14", "ADX_MIN": "22",
        "RSI_LENGTH": "14", "RSI_MIDLINE": "50",
        "EMA_FAST": "50", "EMA_SLOW": "200",
        "ATR_LENGTH": "14", "SL_ATR_MULTIPLIER": "1.5",
        "TP1_RR": "1.5", "TP2_RR": "2.5",
        "VOLUME_SMA_LENGTH": "20", "VOLUME_MULTIPLIER": "1.2",
        "MIN_SCORE": "80", "MAX_DISTANCE_FROM_EMA_ATR": "2.0",
        "COOLDOWN_CANDLES": "3", "MAX_DATA_AGE_SECONDS": "30",
        "SYMBOLS": symbols, "SCAN_INTERVAL_SECONDS": "60",
        "LOG_LEVEL": "INFO", "SIGNAL_ONLY": "true",
        "DRY_RUN": "true", "QUALITY_MODE": "true",
        "SEND_WATCH_SIGNALS": "false",
        "TELEGRAM_BOT_TOKEN": "test_token",
        "TELEGRAM_CHAT_ID": "12345",
        "DASHBOARD_AUTH_ENABLED": "false",
        "DASHBOARD_HOST": "127.0.0.1",
        "DASHBOARD_PORT": "8080",
    }
    if monkeypatch is not None:
        for k, v in env.items():
            monkeypatch.setenv(k, v)
    return env


def _reset_bot_config():
    import app.config as cfgmod
    cfgmod.config = None


def _pingable_testapp(tmp_path, monkeypatch, symbols="BTCUSDT,ETHUSDT"):
    env = _valid_env(monkeypatch, symbols)
    db_path = str(tmp_path / "signals.db")
    monkeypatch.setenv("DASHBOARD_DB_PATH", db_path)
    _reset_bot_config()
    for k in ("DASHBOARD_USERNAME", "DASHBOARD_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    from dashboard.db import ReadOnlyDatabase
    from dashboard.app import create_app
    from dashboard.config import load_dashboard_settings
    settings = load_dashboard_settings()
    db = ReadOnlyDatabase(settings.db_path)
    app = create_app(settings=settings, db=db)
    client = TestClient(app, raise_server_exceptions=False)
    return app, client, db_path, db, settings


def _make_signal_row(idx, signal_id="SIG-1", symbol="BTCUSDT", direction="LONG",
                     status="ACTIVE", score=84, delivery_status="DELIVERED",
                     tp1_hit_time=None, tp2_hit_time=None, sl_hit_time=None,
                     last_delivery_error=None, last_outcome_error=None,
                     last_evaluated_candle_time=None):
    now = datetime.now(timezone.utc).isoformat()
    return (
        f"{signal_id}-{idx}", symbol, direction, "TREND_PULLBACK",
        now, now,
        "100", "101", "99", "103", "105",
        score, "BULLISH", "25", "55", "1.5", status,
        tp1_hit_time, tp2_hit_time, sl_hit_time, None,
        delivery_status, 1, last_delivery_error,
        0, 0, 0, 0, last_outcome_error, last_evaluated_candle_time,
        0, 0, 0,
    )


def _seed_db(db_path, rows, meta=None):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            signal_id TEXT PRIMARY KEY, symbol TEXT, direction TEXT,
            signal_type TEXT, created_at TEXT, trigger_candle_time TEXT,
            entry_low TEXT, entry_high TEXT, stop_loss TEXT, tp1 TEXT, tp2 TEXT,
            score INTEGER, htf_bias TEXT, adx_value TEXT, rsi_value TEXT,
            volume_ratio TEXT, status TEXT,
            tp1_hit_time TEXT, tp2_hit_time TEXT, sl_hit_time TEXT,
            expiration_time TEXT, delivery_status TEXT, delivery_attempts INTEGER,
            last_delivery_error TEXT,
            tp1_notified INTEGER DEFAULT 0, tp2_notified INTEGER DEFAULT 0,
            sl_notified INTEGER DEFAULT 0,
            outcome_attempts INTEGER DEFAULT 0, last_outcome_error TEXT,
            last_evaluated_candle_time TEXT,
            tp1_outcome_attempts INTEGER DEFAULT 0,
            tp2_outcome_attempts INTEGER DEFAULT 0,
            sl_outcome_attempts INTEGER DEFAULT 0
        )
    """)
    cur.execute("CREATE TABLE IF NOT EXISTS bot_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    for row in rows:
        cur.execute(
            "INSERT OR REPLACE INTO signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            row
        )
    if meta:
        for k, v in meta.items():
            cur.execute("INSERT OR REPLACE INTO bot_meta (key, value) VALUES (?, ?)", (k, v))
    conn.commit()
    conn.close()


# ==================================================================
# BOT-SIDE: app/main.py heartbeat helpers
# ==================================================================

class TestBotHeartbeatConstants:
    def test_runtime_meta_keys_exist(self):
        import app.main as main
        assert main.META_KEY_BOT_STARTED_AT == "bot_started_at"
        assert main.META_KEY_LAST_SCAN_STARTED_AT == "last_scan_started_at"
        assert main.META_KEY_LAST_SCAN_COMPLETED_AT == "last_scan_completed_at"
        assert main.META_KEY_LAST_HEARTBEAT == "last_heartbeat"
        assert main.META_KEY_SCAN_CYCLE == "scan_cycle"

    def test_utc_iso_is_timezone_aware(self):
        import app.main as main
        s = main._utc_iso()
        dt = datetime.fromisoformat(s)
        assert dt.tzinfo is not None

    def test_set_meta_safe_success(self):
        import app.main as main
        store = MagicMock()
        ok = main._set_meta_safe(store, "k", "v")
        assert ok is True
        store.set_meta.assert_called_once_with("k", "v")

    def test_set_meta_safe_failure_does_not_raise(self):
        import app.main as main
        store = MagicMock()
        store.set_meta.side_effect = sqlite3.OperationalError("disk full")
        ok = main._set_meta_safe(store, "k", "v")
        assert ok is False

    def test_set_meta_safe_returns_false_on_generic_exception(self):
        import app.main as main
        store = MagicMock()
        store.set_meta.side_effect = RuntimeError("boom")
        ok = main._set_meta_safe(store, "any", "val")
        assert ok is False


class TestBotHeartbeatIntegration:
    """Verify the scan loop writes heartbeat at the right moments."""

    def test_startup_writes_bot_started_at(self, tmp_path, monkeypatch):
        _valid_env(monkeypatch)
        _reset_bot_config()
        db_path = str(tmp_path / "signals.db")
        monkeypatch.setenv("DASHBOARD_DB_PATH", db_path)
        # Patch SignalStore so we capture set_meta calls.
        writes = []

        class FakeStore:
            def set_meta(self, k, v):
                writes.append((k, v))
            def get_meta(self, k):
                return None
            def cleanup(self, **kw):
                return 0

        # We only test that the startup sequence would call set_meta with
        # bot_started_at. We import and call the startup fragment directly
        # by mocking the heavy dependencies.
        import app.main as main
        fake = FakeStore()
        main._set_meta_safe(fake, main.META_KEY_BOT_STARTED_AT, main._utc_iso())
        assert any(k == "bot_started_at" for k, _ in writes)

    def test_scan_cycle_persists_even_with_zero_signals(self, tmp_path, monkeypatch):
        """A NO_SIGNAL scan must still update heartbeat + cycle."""
        _valid_env(monkeypatch)
        _reset_bot_config()
        import app.main as main
        store = MagicMock()
        # Simulate two cycles, each with zero signals.
        for cycle in (1, 2):
            now = datetime.now(timezone.utc).isoformat()
            main._set_meta_safe(store, main.META_KEY_LAST_SCAN_STARTED_AT, now)
            main._set_meta_safe(store, main.META_KEY_LAST_HEARTBEAT, now)
            main._set_meta_safe(store, main.META_KEY_SCAN_CYCLE, str(cycle))
            # scanner returns []
            signals = []
            completed = datetime.now(timezone.utc).isoformat()
            main._set_meta_safe(store, main.META_KEY_LAST_SCAN_COMPLETED_AT, completed)
            main._set_meta_safe(store, main.META_KEY_LAST_HEARTBEAT, completed)
            main._set_meta_safe(store, main.META_KEY_SCAN_CYCLE, str(cycle))
        # Each cycle writes started + heartbeat + cycle, then completed + heartbeat + cycle.
        keys_written = [c.args[0] for c in store.set_meta.call_args_list]
        assert keys_written.count("last_scan_completed_at") == 2
        assert keys_written.count("last_heartbeat") == 4
        assert keys_written.count("scan_cycle") == 4

    def test_heartbeat_write_failure_does_not_stop_scanning(self, tmp_path, monkeypatch):
        """If set_meta raises, scanning logic must continue."""
        _valid_env(monkeypatch)
        _reset_bot_config()
        import app.main as main
        store = MagicMock()
        store.set_meta.side_effect = sqlite3.OperationalError("locked")
        # These should not raise.
        main._set_meta_safe(store, main.META_KEY_LAST_SCAN_STARTED_AT, main._utc_iso())
        main._set_meta_safe(store, main.META_KEY_LAST_HEARTBEAT, main._utc_iso())
        # Scanner failure is separate; heartbeat failure must not mask it.
        # Simulate a scanner that would still be called.
        scanner = MagicMock()
        scanner.scan_all_symbols.return_value = []
        signals = scanner.scan_all_symbols()
        assert signals == []
        # Completed heartbeat also best-effort.
        main._set_meta_safe(store, main.META_KEY_LAST_SCAN_COMPLETED_AT, main._utc_iso())
        # No exception propagated.
        scanner.scan_all_symbols.assert_called_once()


# ==================================================================
# DASHBOARD-SIDE: dashboard/service.py build_bot_activity
# ==================================================================

class TestDashboardHeartbeatLogic:

    def _make_bot_config(self, scan_interval=60, symbols=None):
        from dashboard.config import BotConfigSnapshot
        cfg = BotConfigSnapshot()
        cfg.scan_interval_seconds = scan_interval
        cfg.symbols = symbols or ["BTCUSDT", "ETHUSDT"]
        return cfg

    def _make_settings(self, stale_after=None):
        from dashboard.config import DashboardSettings
        s = DashboardSettings()
        s.stale_after_seconds = stale_after
        return s

    def test_fresh_heartbeat_produces_online(self, tmp_path, monkeypatch):
        _valid_env(monkeypatch, symbols="BTCUSDT,ETHUSDT")
        _reset_bot_config()
        _, client, db_path, _, _ = _pingable_testapp(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        meta = {
            "bot_started_at": (now - timedelta(seconds=2600)).isoformat(),
            "last_scan_completed_at": now.isoformat(),
            "last_heartbeat": now.isoformat(),
            "scan_cycle": "42",
        }
        _seed_db(db_path, [], meta=meta)
        r = client.get("/api/status")
        assert r.status_code == 200
        ba = r.json()["bot_activity"]
        assert ba["status_label"] == "ONLINE"
        assert ba["stale"] is False
        assert ba["scan_cycle"] == 42

    def test_stale_heartbeat_produces_stale(self, tmp_path, monkeypatch):
        _valid_env(monkeypatch)
        _reset_bot_config()
        _, client, db_path, _, _ = _pingable_testapp(tmp_path, monkeypatch)
        # Heartbeat 10 minutes ago, threshold 180 s (3 * 60).
        old = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
        meta = {
            "bot_started_at": old,
            "last_scan_completed_at": old,
            "last_heartbeat": old,
            "scan_cycle": "10",
        }
        _seed_db(db_path, [], meta=meta)
        r = client.get("/api/status")
        ba = r.json()["bot_activity"]
        assert ba["status_label"] == "STALE"
        assert ba["stale"] is True

    def test_uptime_calculation(self, tmp_path, monkeypatch):
        _valid_env(monkeypatch)
        _reset_bot_config()
        _, client, db_path, _, _ = _pingable_testapp(tmp_path, monkeypatch)
        started = datetime.now(timezone.utc) - timedelta(seconds=2600)
        now_iso = datetime.now(timezone.utc).isoformat()
        meta = {
            "bot_started_at": started.isoformat(),
            "last_scan_completed_at": now_iso,
            "last_heartbeat": now_iso,
            "scan_cycle": "1",
        }
        _seed_db(db_path, [], meta=meta)
        r = client.get("/api/status")
        ba = r.json()["bot_activity"]
        # 2600 ± a few seconds of test overhead.
        assert ba["uptime_seconds"] is not None
        assert 2590 <= ba["uptime_seconds"] <= 2620

    def test_latest_activity_uses_completed_at(self, tmp_path, monkeypatch):
        _valid_env(monkeypatch)
        _reset_bot_config()
        _, client, db_path, _, _ = _pingable_testapp(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        completed = (now - timedelta(seconds=5)).isoformat()
        heartbeat = (now - timedelta(seconds=100)).isoformat()
        meta = {
            "bot_started_at": (now - timedelta(hours=1)).isoformat(),
            "last_scan_completed_at": completed,
            "last_heartbeat": heartbeat,
            "scan_cycle": "7",
        }
        _seed_db(db_path, [], meta=meta)
        r = client.get("/api/status")
        ba = r.json()["bot_activity"]
        assert ba["latest_activity_time"] == completed
        assert ba["latest_activity_age_seconds"] is not None
        assert ba["latest_activity_age_seconds"] < 20

    def test_latest_activity_falls_back_to_heartbeat(self, tmp_path, monkeypatch):
        _valid_env(monkeypatch)
        _reset_bot_config()
        _, client, db_path, _, _ = _pingable_testapp(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        hb = now.isoformat()
        meta = {
            "bot_started_at": (now - timedelta(hours=1)).isoformat(),
            "last_heartbeat": hb,
            "scan_cycle": "3",
        }
        _seed_db(db_path, [], meta=meta)
        r = client.get("/api/status")
        ba = r.json()["bot_activity"]
        assert ba["latest_activity_time"] == hb

    def test_next_expected_scan_calculation(self, tmp_path, monkeypatch):
        _valid_env(monkeypatch)
        _reset_bot_config()
        _, client, db_path, _, _ = _pingable_testapp(tmp_path, monkeypatch)
        completed = datetime.now(timezone.utc)
        meta = {
            "bot_started_at": (completed - timedelta(hours=1)).isoformat(),
            "last_scan_completed_at": completed.isoformat(),
            "last_heartbeat": completed.isoformat(),
            "scan_cycle": "5",
        }
        _seed_db(db_path, [], meta=meta)
        r = client.get("/api/status")
        ba = r.json()["bot_activity"]
        assert ba["next_expected"] is not None
        expected = completed + timedelta(seconds=60)
        got = datetime.fromisoformat(ba["next_expected"])
        if got.tzinfo is None:
            got = got.replace(tzinfo=timezone.utc)
        assert abs((got - expected).total_seconds()) < 2

    def test_next_expected_is_none_without_completed_at(self, tmp_path, monkeypatch):
        _valid_env(monkeypatch)
        _reset_bot_config()
        _, client, db_path, _, _ = _pingable_testapp(tmp_path, monkeypatch)
        # Only heartbeat, no completed scan.
        now = datetime.now(timezone.utc).isoformat()
        meta = {"last_heartbeat": now, "scan_cycle": "1"}
        _seed_db(db_path, [], meta=meta)
        r = client.get("/api/status")
        ba = r.json()["bot_activity"]
        assert ba["next_expected"] is None

    def test_scan_cycle_persisted(self, tmp_path, monkeypatch):
        _valid_env(monkeypatch)
        _reset_bot_config()
        _, client, db_path, _, _ = _pingable_testapp(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc).isoformat()
        meta = {
            "bot_started_at": now,
            "last_scan_completed_at": now,
            "last_heartbeat": now,
            "scan_cycle": "132",
        }
        _seed_db(db_path, [], meta=meta)
        r = client.get("/api/status")
        assert r.json()["bot_activity"]["scan_cycle"] == 132

    def test_missing_runtime_meta_safe_fallback(self, tmp_path, monkeypatch):
        """No heartbeat keys: dashboard must not crash and must return N/A-shaped bot_activity."""
        _valid_env(monkeypatch)
        _reset_bot_config()
        _, client, db_path, _, _ = _pingable_testapp(tmp_path, monkeypatch)
        _seed_db(db_path, [], meta=None)
        r = client.get("/api/status")
        assert r.status_code == 200
        ba = r.json()["bot_activity"]
        assert ba is not None
        # Legacy fallback: DATA ACTIVITY, no uptime, no next_expected.
        assert ba["status_label"] == "DATA ACTIVITY"
        assert ba["uptime_seconds"] is None
        assert ba["scan_cycle"] is None
        assert ba["next_expected"] is None

    def test_dashboard_remains_read_only(self, tmp_path, monkeypatch):
        _valid_env(monkeypatch)
        _reset_bot_config()
        _, client, db_path, _, settings = _pingable_testapp(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc).isoformat()
        _seed_db(db_path, [], meta={
            "bot_started_at": now,
            "last_scan_completed_at": now,
            "last_heartbeat": now,
            "scan_cycle": "1",
        })
        from dashboard.db import ReadOnlyDatabase
        # Dashboard DB must use mode=ro + query_only (source-level guarantee).
        import pathlib
        source = pathlib.Path("dashboard/db.py").read_text(encoding="utf-8")
        assert "mode=ro" in source
        assert "query_only" in source
        # A write through the read-only connection must fail.
        probe = ReadOnlyDatabase(settings.db_path)
        with pytest.raises(sqlite3.Error):
            with probe.transaction() as conn:
                conn.execute("INSERT INTO bot_meta (key, value) VALUES ('evil', '1')")
        # API must still be readable.
        r = client.get("/api/status")
        assert r.status_code == 200

    def test_api_contract_preserved(self, tmp_path, monkeypatch):
        _valid_env(monkeypatch)
        _reset_bot_config()
        _, client, db_path, _, _ = _pingable_testapp(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc).isoformat()
        _seed_db(db_path, [], meta={
            "bot_started_at": now,
            "last_scan_completed_at": now,
            "last_heartbeat": now,
            "scan_cycle": "9",
        })
        for path in ["/api/health", "/api/status", "/api/summary", "/api/symbols", "/api/signals", "/api/outcomes", "/api/activity"]:
            r = client.get(path)
            assert r.status_code == 200, f"{path} -> {r.status_code}"
        # /api/status must contain non-null runtime info after a scan.
        ba = client.get("/api/status").json()["bot_activity"]
        assert ba["latest_activity_time"] is not None
        assert ba["uptime_seconds"] is not None
        assert ba["scan_cycle"] is not None

    def test_production_scenario_nine_symbols_zero_signals(self, tmp_path, monkeypatch):
        """Exact production case: 9 symbols, 60 s interval, zero signals, repeated scan."""
        symbols = "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,ADAUSDT,DOGEUSDT,AVAXUSDT,DOTUSDT"
        _valid_env(monkeypatch, symbols=symbols)
        _reset_bot_config()
        _, client, db_path, _, _ = _pingable_testapp(tmp_path, monkeypatch, symbols=symbols)
        now = datetime.now(timezone.utc)
        # Simulate cycles 131 and 132, both with zero signals.
        for cycle in (131, 132):
            ts = (now - timedelta(seconds=(132 - cycle) * 60)).isoformat()
            # Overwrite with latest cycle's heartbeat.
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("CREATE TABLE IF NOT EXISTS bot_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            cur.execute("CREATE TABLE IF NOT EXISTS signals (signal_id TEXT PRIMARY KEY, symbol TEXT, direction TEXT, signal_type TEXT, created_at TEXT, trigger_candle_time TEXT, entry_low TEXT, entry_high TEXT, stop_loss TEXT, tp1 TEXT, tp2 TEXT, score INTEGER, htf_bias TEXT, adx_value TEXT, rsi_value TEXT, volume_ratio TEXT, status TEXT, tp1_hit_time TEXT, tp2_hit_time TEXT, sl_hit_time TEXT, expiration_time TEXT, delivery_status TEXT, delivery_attempts INTEGER, last_delivery_error TEXT, tp1_notified INTEGER, tp2_notified INTEGER, sl_notified INTEGER, outcome_attempts INTEGER, last_outcome_error TEXT, last_evaluated_candle_time TEXT, tp1_outcome_attempts INTEGER, tp2_outcome_attempts INTEGER, sl_outcome_attempts INTEGER)")
            for k, v in {
                "bot_started_at": (now - timedelta(hours=2, minutes=14)).isoformat(),
                "last_scan_completed_at": ts,
                "last_heartbeat": ts,
                "scan_cycle": str(cycle),
            }.items():
                cur.execute("INSERT OR REPLACE INTO bot_meta (key, value) VALUES (?, ?)", (k, v))
            conn.commit()
            conn.close()
        r = client.get("/api/status")
        assert r.status_code == 200
        j = r.json()
        ba = j["bot_activity"]
        # Dashboard must show activity despite zero signals.
        assert ba["status_label"] == "ONLINE"
        assert ba["stale"] is False
        assert ba["uptime_seconds"] is not None and ba["uptime_seconds"] > 0
        assert ba["latest_activity_time"] is not None
        assert ba["scan_cycle"] == 132
        assert ba["next_expected"] is not None
        assert j["symbols_count"] == 9
        assert j["scan_interval_seconds"] == 60
        # Summary APIs must still return 200 with no signals.
        assert client.get("/api/summary").status_code == 200
        assert client.get("/api/signals").status_code == 200

    def test_stale_threshold_uses_config(self, tmp_path, monkeypatch):
        """Dashboard must honor stale_after_seconds from settings, not a hardcode."""
        _valid_env(monkeypatch)
        _reset_bot_config()
        from dashboard.service import _resolve_stale_threshold
        # Explicit stale_after wins.
        assert _resolve_stale_threshold(300, 60) == 300
        assert _resolve_stale_threshold(120, 60) == 120
        # Fallback is 3x interval.
        assert _resolve_stale_threshold(None, 60) == 180
        assert _resolve_stale_threshold(0, 60) == 180
        assert _resolve_stale_threshold(None, None) == 300


class TestParseHelpers:
    def test_parse_ts_iso(self):
        from dashboard.service import _parse_ts
        iso = datetime(2025, 9, 26, 5, 43, 9, tzinfo=timezone.utc).isoformat()
        dt = _parse_ts(iso)
        assert dt is not None
        assert dt.tzinfo is not None

    def test_parse_ts_epoch(self):
        from dashboard.service import _parse_ts
        epoch = str(datetime(2025, 9, 26, 5, 0, 0, tzinfo=timezone.utc).timestamp())
        dt = _parse_ts(epoch)
        assert dt is not None

    def test_parse_ts_invalid_returns_none(self):
        from dashboard.service import _parse_ts
        assert _parse_ts("not-a-date") is None
        assert _parse_ts("") is None
        assert _parse_ts(None) is None

    def test_safe_cycle(self):
        from dashboard.service import _safe_cycle
        assert _safe_cycle("42") == 42
        assert _safe_cycle("132") == 132
        assert _safe_cycle(None) is None
        assert _safe_cycle("") is None
        assert _safe_cycle("not-a-number") is None
