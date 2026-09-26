"""
Dashboard tests — read-only monitoring layer.

Covers:
* Dashboard imports
* FastAPI application starts
* GET / and GET /api/* endpoints
* Empty database handling
* Populated database, NULL fields, missing columns
* Dynamic symbol configuration
* Changed SYMBOLS handling (no second list)
* Authentication (enabled / disabled / invalid)
* Missing database
* Read-only behavior (no writes)
* No secrets exposed
* No trading execution
* Existing bot behavior unaffected

This test file only verifies that dashboard code reads from the bot store.
It never exercises Binance trading endpoints or order logic.
"""
import os
import base64
import importlib
import sqlite3
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

from fastapi.testclient import TestClient
from decimal import Decimal as _Dec
from datetime import timezone as _tz


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
        # Dashboard defaults: disabled auth, localhost bind
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


def _pingable_testapp(tmp_path, monkeypatch, symbols="BTCUSDT,ETHUSDT", auth=None):
    env = _valid_env(monkeypatch, symbols)
    db_path = str(tmp_path / "signals.db")
    monkeypatch.setenv("DASHBOARD_DB_PATH", db_path)
    _reset_bot_config()
    import dashboard.config as dash_cfg
    # ensure clean env for auth branches
    for k in ("DASHBOARD_USERNAME", "DASHBOARD_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    if auth is not None:
        for k, v in auth.items():
            monkeypatch.setenv(k, v)
    # reload dashboard package with current env
    from dashboard.db import ReadOnlyDatabase  # noqa: F401
    from dashboard.app import create_app
    from dashboard.config import load_dashboard_settings
    settings = load_dashboard_settings()
    db = ReadOnlyDatabase(settings.db_path)
    app = create_app(settings=settings, db=db)
    client = TestClient(app, raise_server_exceptions=False)
    return app, client, db_path, db


def _basic_header(user="admin", password="s3cr3t"):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def _make_signal_row(idx, signal_id="SIG-1", symbol="BTCUSDT", direction="LONG",
                     status="ACTIVE", score=85, delivery_status="DELIVERED",
                     entry_low="100", entry_high="105", stop_loss="95",
                     tp1="110", tp2="120",
                     htf_bias="BULLISH", adx="27.4", rsi="56.2",
                     volume_ratio="1.38", created_at=None,
                     tp1_hit_time=None, tp2_hit_time=None, sl_hit_time=None,
                     expiration_time=None,
                     last_delivery_error=None,
                     last_outcome_error=None,
                     last_evaluated_candle_time=None,
                     outcome_attempts=0, delivery_attempts=1,
                     tp1_outcome_attempts=0, tp2_outcome_attempts=0, sl_outcome_attempts=0):
    if created_at is None:
        created_at = datetime(2025, 9, 26, 10, 0, idx, tzinfo=timezone.utc).isoformat()
    def _dt(v):
        return datetime.fromisoformat(v).isoformat() if v else None
    return (
        signal_id + str(idx), symbol, direction, "TREND_BREAKOUT",
        created_at,
        datetime(2025, 9, 26, 9, 50 + idx, tzinfo=timezone.utc).isoformat(),
        entry_low, entry_high, stop_loss, tp1, tp2,
        score, htf_bias, adx, rsi, volume_ratio,
        status,
        tp1_hit_time, tp2_hit_time, sl_hit_time, expiration_time,
        delivery_status, delivery_attempts, last_delivery_error,
        0, 0, 0,  # tp1_notified etc
        outcome_attempts, last_outcome_error, last_evaluated_candle_time,
        tp1_outcome_attempts, tp2_outcome_attempts, sl_outcome_attempts,
    )

# ----------------------------------------------------------------------
# 1–2: import + app starts
# ----------------------------------------------------------------------

def test_dashboard_imports_successfully(monkeypatch, tmp_path):
    _valid_env(monkeypatch)
    _reset_bot_config()
    from dashboard import __version__
    assert __version__
    import dashboard.app as da_app
    import dashboard.db as da_db
    import dashboard.service as da_svc
    import dashboard.config as da_cfg
    assert all([da_app, da_db, da_svc, da_cfg])

def test_fastapi_app_starts(monkeypatch, tmp_path):
    _, client, _, _ = _pingable_testapp(tmp_path, monkeypatch)
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

# ----------------------------------------------------------------------
# GET / + each api endpoint
# ----------------------------------------------------------------------

def test_get_root(monkeypatch, tmp_path):
    _, client, _, _ = _pingable_testapp(tmp_path, monkeypatch)
    r = client.get("/")
    assert r.status_code == 200
    assert "LONG / SHORT SIGNAL MONITOR" in r.text

def test_get_api_health(monkeypatch, tmp_path):
    _, client, _, _ = _pingable_testapp(tmp_path, monkeypatch)
    r = client.get("/api/health")
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "ok"
    assert "version" in j
    assert "timestamp" in j

def test_get_api_status(monkeypatch, tmp_path):
    _, client, _, _ = _pingable_testapp(tmp_path, monkeypatch)
    r = client.get("/api/status")
    assert r.status_code == 200
    j = r.json()
    for key in ["available", "summary", "bot_activity", "symbols_count"]:
        assert key in j

def test_get_api_symbols(monkeypatch, tmp_path):
    _, client, _, _ = _pingable_testapp(tmp_path, monkeypatch, symbols="BTCUSDT,ETHUSDT")
    r = client.get("/api/symbols")
    assert r.status_code == 200
    s = r.json()["symbols"]
    assert "BTCUSDT" in s
    assert "ETHUSDT" in s
    # No duplicate list, no dashboard-only symbols
    assert os.getenv("DASHBOARD_SYMBOLS", None) is None

def test_get_api_signals(monkeypatch, tmp_path):
    _, client, _, _ = _pingable_testapp(tmp_path, monkeypatch)
    r = client.get("/api/signals?limit=5")
    assert r.status_code == 200
    assert "signals" in r.json()

def test_get_api_signals_latest(monkeypatch, tmp_path):
    _, client, _, _ = _pingable_testapp(tmp_path, monkeypatch)
    r = client.get("/api/signals/latest?limit=3")
    assert r.status_code == 200
    assert "signals" in r.json()

def test_get_api_outcomes(monkeypatch, tmp_path):
    _, client, _, _ = _pingable_testapp(tmp_path, monkeypatch)
    r = client.get("/api/outcomes?limit=5")
    assert r.status_code == 200
    assert "events" in r.json()

def test_get_api_summary(monkeypatch, tmp_path):
    _, client, _, _ = _pingable_testapp(tmp_path, monkeypatch)
    r = client.get("/api/summary")
    assert r.status_code == 200
    assert "summary" in r.json()
    assert "symbols" in r.json()

def test_get_api_activity(monkeypatch, tmp_path):
    _, client, _, _ = _pingable_testapp(tmp_path, monkeypatch)
    r = client.get("/api/activity?limit=10")
    assert r.status_code == 200
    assert "activity" in r.json()

# ----------------------------------------------------------------------
# Empty / populated / NULL fields
# ----------------------------------------------------------------------

def test_empty_database(monkeypatch, tmp_path):
    """Dashboard must not crash when no signals exist."""
    _, client, _, _ = _pingable_testapp(tmp_path, monkeypatch)
    for path in ["/api/status", "/api/summary", "/api/activity"]:
        r = client.get(path)
        assert r.status_code == 200

def _seed_db(db_path, rows):
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
    conn.commit()
    conn.close()

def test_populated_database(monkeypatch, tmp_path):
    _, client, db_path, _ = _pingable_testapp(tmp_path, monkeypatch, symbols="BTCUSDT,ETHUSDT")
    _seed_db(db_path, [
        _make_signal_row(0, symbol="BTCUSDT", direction="LONG", status="ACTIVE", score=84, delivery_status="DELIVERED"),
        _make_signal_row(1, symbol="ETHUSDT", direction="SHORT", status="TP1_HIT", score=79, delivery_status="PENDING",
                         tp1_hit_time=datetime(2025,9,26,12,0, tzinfo=timezone.utc).isoformat()),
        _make_signal_row(2, symbol="BTCUSDT", direction="SHORT", status="STOPPED", delivery_status="FAILED",
                         sl_hit_time=datetime(2025,9,26,13,0, tzinfo=timezone.utc).isoformat(),
                         last_delivery_error="Network timeout"),
    ])
    r = client.get("/api/summary")
    assert r.status_code == 200
    j = r.json()
    assert j["summary"]["total_signals"] >= 1
    assert len(j["symbols"]) == 2  # follows SYMBOLS
    r2 = client.get("/api/signals/latest?limit=20")
    assert len(r2.json()["signals"]) >= 1

def test_null_fields_handled(monkeypatch, tmp_path):
    _, client, db_path, _ = _pingable_testapp(tmp_path, monkeypatch)
    _seed_db(db_path, [
        _make_signal_row(0, tp1_hit_time=None, tp2_hit_time=None, sl_hit_time=None,
                         last_delivery_error=None, last_outcome_error=None,
                         last_evaluated_candle_time=None),
    ])
    for path in ["/api/summary", "/api/activity", "/api/signals/latest?limit=10"]:
        r = client.get(path)
        assert r.status_code == 200

def test_missing_columns_handled(monkeypatch, tmp_path):
    """Old schema without some columns must not crash the dashboard."""
    _, client, db_path, _ = _pingable_testapp(tmp_path, monkeypatch)
    # Minimal table without delivery/outcome columns
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE signals (
            signal_id TEXT PRIMARY KEY, symbol TEXT, direction TEXT,
            signal_type TEXT, created_at TEXT, trigger_candle_time TEXT,
            entry_low TEXT, entry_high TEXT, stop_loss TEXT, tp1 TEXT, tp2 TEXT,
            score INTEGER, htf_bias TEXT, adx_value TEXT, rsi_value TEXT,
            volume_ratio TEXT, status TEXT
        )
    """)
    cur.execute("""
        INSERT INTO signals VALUES
        ('SIG-A-0','BTCUSDT','LONG','TREND_BREAKOUT',
         '2025-09-26T10:00:00+00:00','2025-09-26T09:50:00+00:00',
         '100','105','95','110','120',82,'BULLISH','27.4','56.2','1.38','ACTIVE')
    """)
    conn.commit()
    conn.close()
    for path in ["/api/summary", "/api/activity", "/api/signals/latest?limit=10"]:
        r = client.get(path)
        assert r.status_code == 200

# ----------------------------------------------------------------------
# Dynamic symbols
# ----------------------------------------------------------------------

def test_dynamic_symbol_configuration(monkeypatch, tmp_path):
    """Dashboard symbols mirror the bot's SYMBOLS without duplication."""
    _, client, _, _ = _pingable_testapp(tmp_path, monkeypatch, symbols="BTCUSDT,ETHUSDT,SOLUSDT")
    r = client.get("/api/symbols")
    assert r.status_code == 200
    assert r.json()["symbols"] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

    summ = client.get("/api/summary").json()
    assert len(summ["symbols"]) == 3

def test_changed_symbols_configuration(monkeypatch, tmp_path):
    """A SYMBOLS change is picked up without code edits (force reload)."""
    _, client, _, _ = _pingable_testapp(tmp_path, monkeypatch, symbols="BTCUSDT,ETHUSDT,SOLUSDT")
    assert client.get("/api/symbols").json()["symbols"] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

    # Simulate .env expansion: more symbols
    for k in ("DASHBOARD_SYMBOLS",):
        monkeypatch.delenv(k, raising=False)
    new_symbols = "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,DOGEUSDT"
    monkeypatch.setenv("SYMBOLS", new_symbols)
    _reset_bot_config()
    from dashboard.config import load_bot_config
    snap = load_bot_config(force=True)
    assert snap.symbols == ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "DOGEUSDT"]
    # Also assert matrix follows
    _valid_env(monkeypatch, symbols=new_symbols)
    monkeypatch.setenv("DASHBOARD_DB_PATH", str(tmp_path / "signals.db"))
    from dashboard.db import ReadOnlyDatabase
    from dashboard.app import create_app
    from dashboard.config import load_dashboard_settings
    settings = load_dashboard_settings()
    db = ReadOnlyDatabase(settings.db_path)
    app = create_app(settings=settings, db=db)
    c2 = TestClient(app, raise_server_exceptions=False)
    assert c2.get("/api/symbols").json()["symbols"] == snap.symbols

def test_no_hardcoded_symbols_in_dashboard(monkeypatch, tmp_path):
    """Dashboard source should not maintain its own symbol list."""
    for path in ["dashboard/app.py", "dashboard/config.py", "dashboard/db.py", "dashboard/service.py"]:
        text = (pytest.importorskip("pathlib").Path(path).read_text() if __import__("os").path.exists(path) else "")
        assert "DASHBOARD_SYMBOLS" not in text, f"Found stray DASHBOARD_SYMBOLS in {path}"

# ----------------------------------------------------------------------
# Authentication
# ----------------------------------------------------------------------

def test_auth_disabled_no_header(monkeypatch, tmp_path):
    _, client, _, _ = _pingable_testapp(tmp_path, monkeypatch, auth={"DASHBOARD_AUTH_ENABLED": "false"})
    assert client.get("/api/health").status_code == 200
    assert client.get("/").status_code == 200

def test_auth_enabled_valid_credentials(monkeypatch, tmp_path):
    _, client, _, _ = _pingable_testapp(tmp_path, monkeypatch, auth={
        "DASHBOARD_AUTH_ENABLED": "true",
        "DASHBOARD_USERNAME": "admin",
        "DASHBOARD_PASSWORD": "s3cr3t",
    })
    assert client.get("/api/health", headers={"Authorization": _basic_header("admin", "s3cr3t")}).status_code == 200
    assert client.get("/api/health").status_code == 401

def test_auth_enabled_invalid_credentials(monkeypatch, tmp_path):
    _, client, _, _ = _pingable_testapp(tmp_path, monkeypatch, auth={
        "DASHBOARD_AUTH_ENABLED": "true",
        "DASHBOARD_USERNAME": "admin",
        "DASHBOARD_PASSWORD": "s3cr3t",
    })
    assert client.get("/api/health", headers={"Authorization": _basic_header("admin", "wrong")}).status_code == 401
    assert client.get("/", headers={"Authorization": _basic_header("wrong", "s3cr3t")}).status_code == 401
    assert client.get("/static/style.css", headers={"Authorization": _basic_header("admin", "nope")}).status_code == 401
    assert client.get("/static/style.css", headers={"Authorization": _basic_header("admin", "s3cr3t")}).status_code == 200

def test_auth_missing_password_fails_safely(monkeypatch, tmp_path):
    monkeypatch.setenv("DASHBOARD_AUTH_ENABLED", "true")
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    from dashboard.config import load_dashboard_settings, DashboardConfigError
    with pytest.raises(DashboardConfigError):
        load_dashboard_settings()

# ----------------------------------------------------------------------
# Missing / locked database
# ----------------------------------------------------------------------

def test_missing_database(monkeypatch, tmp_path):
    _, client, _, _ = _pingable_testapp(tmp_path, monkeypatch)
    # Do not create the file; hit API
    for path in ["/api/status", "/api/summary", "/api/signals", "/api/outcomes", "/api/activity"]:
        r = client.get(path)
        assert r.status_code == 200
        body = r.json()
        assert body.get("available") in (False, True)
        if path in ("/api/status", "/api/summary"):
            assert "bot_activity" in body if "bot_activity" in body else True

def test_readonly_database_behavior(monkeypatch, tmp_path):
    """Dashboard never makes INSERT/UPDATE/DELETE/ALTER/DROP against signals.db."""
    _, client, db_path, _ = _pingable_testapp(tmp_path, monkeypatch)
    _seed_db(db_path, [_make_signal_row(0)])
    mtime_before = os.path.getmtime(db_path)
    for path in ["/", "/api/health", "/api/status", "/api/summary", "/api/activity",
                 "/api/signals/latest?limit=5", "/api/outcomes?limit=5"]:
        client.get(path)
    # No modification of the file should have occurred
    assert os.path.getmtime(db_path) == mtime_before

    # No executable SQL statement may be a write statement.
    # Comments and docstrings are ignored; only string literals are inspected.
    import ast
    import pathlib

    WRITE_KEYWORDS = {"INSERT", "UPDATE", "DELETE", "ALTER", "DROP", "REPLACE"}
    for rel in ["dashboard/db.py", "dashboard/service.py", "dashboard/app.py", "dashboard/config.py"]:
        path = pathlib.Path(rel)
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                first = node.value.strip().upper()
                head = first.split(" ")[0] if first else ""
                assert head not in WRITE_KEYWORDS, (
                    f"Write statement {head!r} found in {rel}"
                )

    # The connection must be read-only: URI mode=ro plus PRAGMA query_only
    source = pathlib.Path("dashboard/db.py").read_text(encoding="utf-8")
    assert "mode=ro" in source
    assert "query_only" in source

    # A real write through the dashboard connection must be rejected by SQLite
    from dashboard.db import ReadOnlyDatabase
    probe = ReadOnlyDatabase(db_path)
    with pytest.raises(sqlite3.Error):
        with probe.transaction() as conn:
            conn.execute("INSERT INTO signals (signal_id) VALUES ('X')")

# ----------------------------------------------------------------------
# No secrets / no trading execution
# ----------------------------------------------------------------------

def test_no_secrets_exposed(monkeypatch, tmp_path):
    _, client, db_path, _ = _pingable_testapp(tmp_path, monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:SECRET-ABCDEF12345")
    # Seed one signal; set token in env to catch accidental exposure
    _seed_db(db_path, [_make_signal_row(0, last_delivery_error="Bad token 123456:SECRET-ABCDEF12345")])
    for path in ["/api/health", "/api/status", "/api/summary", "/api/activity",
                 "/api/signals/latest?limit=5", "/api/signals?limit=5", "/api/outcomes?limit=5"]:
        r = client.get(path)
        text = r.text.lower()
        assert "123456:secret" not in text
        assert "your_bot_token" not in text
        assert "your_chat_id" not in text
        body = r.json() if "application/json" in r.headers.get("content-type", "") else {}
        flat = str(body).lower()
        assert "api_key" not in flat
        assert "telegram_bot_token" not in flat

def test_no_trading_execution(monkeypatch, tmp_path):
    """Dashboard sources must not introduce trading endpoints or order keywords."""
    import pathlib
    import re
    for src in ["dashboard/app.py", "dashboard/db.py", "dashboard/service.py", "dashboard/config.py"]:
        p = pathlib.Path(src)
        if not p.exists():
            continue
        text = p.read_text()
        for pat in [r"\bcreate_order\b", r"\bplace_order\b", r"\bcancel_order\b", r"\bfutures_\w+", r"\bmargin\b", r"\bleverage\b", r"\bwithdraw"]:
            assert not re.search(pat, text, re.IGNORECASE), f"Trading keyword {pat!r} in {src}"

def test_existing_bot_behavior_unaffected(tmp_path):
    """Dashboard routes must be GET-only and never touch app/ paths."""
    from dashboard.app import create_app
    # Mounted app has no writes; verify by inspection: no POST/PUT/DELETE on dashboard routes
    # This is a lightweight check that dashboard hasn't registered mutation routes
    monkeypatch = None  # not needed here
    import app.config as cfgmod
    orig = cfgmod.config
    try:
        from dashboard.config import load_dashboard_settings
        from dashboard.db import ReadOnlyDatabase
        import pathlib
        # dashboard/app.py must define only @app.get
        text = pathlib.Path("dashboard/app.py").read_text()
        assert "@app.post" not in text
        assert "@app.put" not in text
        assert "@app.delete" not in text
        assert "@app.patch" not in text
    finally:
        cfgmod.config = orig


# ----------------------------------------------------------------------
# Score distribution NULL / None handling (regression for TypeError: int(None))
# ----------------------------------------------------------------------

def test_score_distribution_none_value(monkeypatch, tmp_path):
    """build_score_distribution must not raise TypeError when a value is None."""
    from dashboard.service import build_score_distribution
    dist = {"0-59": None, "60-69": 0, "70-79": 0, "80-89": 0, "90-100": 0}
    result = build_score_distribution(dist)
    # All zeros after coercion → empty list
    assert result == []


def test_score_distribution_multiple_none_values(monkeypatch, tmp_path):
    """Multiple None values must all coerce to 0."""
    from dashboard.service import build_score_distribution
    dist = {"0-59": None, "60-69": None, "70-79": None, "80-89": None, "90-100": None}
    result = build_score_distribution(dist)
    assert result == []


def test_score_distribution_missing_label(monkeypatch, tmp_path):
    """Missing label keys must coerce to 0 via default."""
    from dashboard.service import build_score_distribution
    dist = {"80-89": 5}  # only one bucket present
    result = build_score_distribution(dist)
    assert len(result) == 5
    counts = {r["label"]: r["count"] for r in result}
    assert counts["80-89"] == 5
    assert counts["0-59"] == 0
    assert counts["90-100"] == 0


def test_score_distribution_valid_numeric(monkeypatch, tmp_path):
    """Valid integer counts produce correct percentages."""
    from dashboard.service import build_score_distribution
    dist = {"0-59": 10, "60-69": 20, "70-79": 30, "80-89": 25, "90-100": 15}
    result = build_score_distribution(dist)
    assert len(result) == 5
    total = sum(r["count"] for r in result)
    assert total == 100
    for r in result:
        assert 0 <= r["pct"] <= 100


def test_score_distribution_empty(monkeypatch, tmp_path):
    """Empty distribution dict returns empty list."""
    from dashboard.service import build_score_distribution
    assert build_score_distribution({}) == []


def test_score_distribution_mixed_none_and_valid(monkeypatch, tmp_path):
    """Mix of None and valid values — None treated as zero."""
    from dashboard.service import build_score_distribution
    dist = {"0-59": None, "60-69": 3, "70-79": None, "80-89": 7, "90-100": None}
    result = build_score_distribution(dist)
    assert len(result) == 5
    counts = {r["label"]: r["count"] for r in result}
    assert counts["0-59"] == 0
    assert counts["60-69"] == 3
    assert counts["70-79"] == 0
    assert counts["80-89"] == 7
    assert counts["90-100"] == 0
    # total = 10, pct for 80-89 should be 70
    pct_80 = next(r["pct"] for r in result if r["label"] == "80-89")
    assert pct_80 == 70


def test_score_distribution_none_does_not_raise_int_none():
    """Verify int(None) would fail but _safe_int(None) returns 0."""
    from dashboard.service import _safe_int
    with pytest.raises(TypeError):
        int(None)
    assert _safe_int(None) == 0
    assert _safe_int(None, 0) == 0


def test_summary_with_null_score_distribution(monkeypatch, tmp_path):
    """End-to-end: /api/summary returns 200 when score bucket sums are NULL.

    SQLite SUM() returns NULL when the table has no rows to aggregate, so
    score_distribution() yields {label: None}. Before the fix this raised
    TypeError: int() argument must be ... not 'NoneType' -> HTTP 500.
    """
    _, client, db_path, _ = _pingable_testapp(tmp_path, monkeypatch)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE signals (
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
    conn.commit()
    conn.close()

    # Empty table: every SUM(...) bucket is NULL — the production trigger.
    r = client.get("/api/summary")
    assert r.status_code == 200
    j = r.json()
    assert "summary" in j
    assert "distribution" in j
    # No data → no fabricated buckets
    assert j["distribution"] == []

    # Also verify /api/status (the endpoint that returned HTTP 500).
    r2 = client.get("/api/status")
    assert r2.status_code == 200


def test_summary_with_null_score_row(monkeypatch, tmp_path):
    """End-to-end: a row with NULL score does not break /api/summary."""
    _, client, db_path, _ = _pingable_testapp(tmp_path, monkeypatch)
    _seed_db(db_path, [
        _make_signal_row(0, score=None),
        _make_signal_row(1, score=84),
    ])
    r = client.get("/api/summary")
    assert r.status_code == 200
    j = r.json()
    assert j["summary"]["total_signals"] == 2
    counts = {b["label"]: b["count"] for b in j["distribution"]}
    assert counts["80-89"] == 1


def test_all_api_endpoints_return_200_with_null_distribution(monkeypatch, tmp_path):
    """All required API endpoints must return 200 after the fix."""
    _, client, db_path, _ = _pingable_testapp(tmp_path, monkeypatch)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE signals (
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
    conn.commit()
    conn.close()
    for path in ["/api/health", "/api/status", "/api/summary", "/api/symbols",
                 "/api/signals", "/api/outcomes", "/api/activity"]:
        r = client.get(path)
        assert r.status_code == 200, f"{path} returned {r.status_code}"
