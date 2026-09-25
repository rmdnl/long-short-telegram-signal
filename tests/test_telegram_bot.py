"""
PHASE 9: Telegram delivery tests.

Tests for Telegram message delivery functionality.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch, MagicMock
import pytest

import app.config as config_module
from app.models import Signal, SignalDirection, SignalType, MarketBias
from app.telegram_bot import TelegramBot
from app.signal_filter import generate_signal_id
from app.signal_store import SignalStore

UTC = timezone.utc
BASE = datetime(2024, 1, 1, 10, 0, tzinfo=UTC)


def make_signal(direction=SignalDirection.LONG, symbol="BTCUSDT", score=92,
                signal_type=SignalType.TREND_PULLBACK,
                htf_bias=MarketBias.BULLISH, ts=None):
    """Build a minimal valid Signal for Telegram tests."""
    if ts is None:
        ts = BASE + timedelta(minutes=5)
    if direction == SignalDirection.LONG:
        entry_low = Decimal("102.50")
        entry_high = Decimal("103.50")
        stop_loss = Decimal("99.00")
        tp1 = Decimal("108.00")
        tp2 = Decimal("113.00")
        adx = Decimal("28.5")
        rsi = Decimal("58.0")
        vol_ratio = Decimal("1.34")
        inv = "below"
        htf_bias_val = MarketBias.BULLISH
    else:
        entry_low = Decimal("98.50")
        entry_high = Decimal("99.50")
        stop_loss = Decimal("104.00")
        tp1 = Decimal("94.00")
        tp2 = Decimal("89.00")
        adx = Decimal("31.2")
        rsi = Decimal("42.0")
        vol_ratio = Decimal("1.80")
        inv = "above"
        htf_bias_val = MarketBias.BEARISH

    sid = generate_signal_id(symbol, "5m", ts, direction)
    return Signal(
        signal_id=sid,
        symbol=symbol,
        direction=direction,
        signal_type=signal_type,
        created_at=BASE,
        trigger_candle_time=ts,
        entry_low=entry_low,
        entry_high=entry_high,
        stop_loss=stop_loss,
        tp1=tp1,
        tp2=tp2,
        score=score,
        htf_bias=htf_bias_val,
        adx_value=adx,
        rsi_value=rsi,
        volume_ratio=vol_ratio,
    )


def make_mock_response(status_code=200, json_data=None, headers=None):
    """Create a mock requests.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.json.return_value = json_data or {"ok": True, "result": {}}
    return resp


@pytest.fixture
def reset_config(monkeypatch):
    monkeypatch.setattr(config_module, "config", None)
    yield
    monkeypatch.setattr(config_module, "config", None)


@pytest.fixture
def telegram_disabled(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ENABLED", "false")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test_token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(config_module, "config", None)


@pytest.fixture
def telegram_enabled(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "real_test_token_12345")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "67890")
    monkeypatch.setattr(config_module, "config", None)


@pytest.fixture
def signal_long():
    return make_signal(SignalDirection.LONG, "BTCUSDT", 92)


@pytest.fixture
def signal_short():
    return make_signal(SignalDirection.SHORT, "ETHUSDT", 87)


@pytest.fixture
def mock_ok_response():
    return make_mock_response(200, {"ok": True, "result": {"message_id": 1}})


# 1. Telegram disabled -- no network call
def test_telegram_disabled_no_network(telegram_disabled, signal_long):
    """When TELEGRAM_ENABLED=false: no network call, returns True."""
    bot = TelegramBot()
    assert bot.config.telegram_enabled is False

    with patch("app.telegram_bot.requests.post") as mock_post:
        result = bot.send_signal(signal_long)

    mock_post.assert_not_called()
    assert result is True


def test_telegram_disabled_logs_message(telegram_disabled, signal_long, caplog):
    """Dry-run logs the message that would be sent."""
    bot = TelegramBot()
    with caplog.at_level("INFO"):
        result = bot.send_signal(signal_long)

    assert result is True
    assert "TELEGRAM DISABLED" in caplog.text
    assert signal_long.symbol in caplog.text


# 2. Telegram enabled -- mocked HTTP
def test_telegram_enabled_sends_message(telegram_enabled, signal_long, mock_ok_response):
    """When enabled + valid creds: a POST is made and returns True."""
    bot = TelegramBot()
    assert bot.config.telegram_enabled is True

    with patch("app.telegram_bot.requests.post", return_value=mock_ok_response) as mock_post:
        result = bot.send_signal(signal_long)

    assert result is True
    mock_post.assert_called_once()
    url = mock_post.call_args[0][0]
    assert "api.telegram.org/bot" in url


def test_telegram_send_uses_correct_endpoint(telegram_enabled, signal_long, mock_ok_response):
    """Verify the URL hits the Bot API sendMessage endpoint."""
    bot = TelegramBot()
    with patch("app.telegram_bot.requests.post", return_value=mock_ok_response) as mock_post:
        bot.send_signal(signal_long)

    url = mock_post.call_args[0][0]
    token = bot.config.telegram_bot_token
    assert token in url
    assert "/sendMessage" in url


# 3. Message formatting
def test_message_format_contains_required_sections(telegram_disabled, signal_long):
    """Verify the formatted message contains all required sections per spec."""
    bot = TelegramBot()
    message = bot._format_signal(signal_long)

    assert signal_long.symbol in message
    assert "TIMEFRAME: 5M" in message
    assert "Entry:" in message
    assert "SL:" in message
    assert "TP1:" in message
    assert "TP2:" in message
    assert "RR:" in message
    assert "Score:" in message
    assert "Setup:" in message
    assert "Confirmation:" in message
    assert "Invalidation:" in message
    assert "Signal only" in message


def test_message_format_uses_markdown_backticks(telegram_disabled, signal_long):
    """Verify backtick formatting for key fields."""
    bot = TelegramBot()
    message = bot._format_signal(signal_long)

    assert "`102.50 - 103.50`" in message
    assert "`99.00`" in message
    assert "`108.00`" in message
    assert "`92/100`" in message


# 4. LONG message
def test_long_message_direction(telegram_disabled, signal_long):
    """LONG signal message contains LONG header."""
    bot = TelegramBot()
    message = bot._format_signal(signal_long)
    assert "**LONG**" in message


def test_long_message_entry_tps_above_sl(telegram_disabled, signal_long):
    """LONG: TP1 and TP2 above entry, SL below."""
    bot = TelegramBot()
    message = bot._format_signal(signal_long)
    entry_mid = (signal_long.entry_low + signal_long.entry_high) / 2
    assert signal_long.stop_loss < entry_mid
    assert signal_long.tp1 > entry_mid
    assert signal_long.tp2 > signal_long.tp1
    assert f"{signal_long.tp1:.2f}" in message


def test_long_message_score_not_probability(telegram_disabled, signal_long):
    """Score must NOT be labeled as probability."""
    bot = TelegramBot()
    message = bot._format_signal(signal_long)
    assert "probability" not in message.lower()
    assert "Score:" in message


# 5. SHORT message
def test_short_message_direction(telegram_disabled, signal_short):
    """SHORT signal message contains SHORT header."""
    bot = TelegramBot()
    message = bot._format_signal(signal_short)
    assert "**SHORT**" in message


def test_short_message_entry_tps_below_sl(telegram_disabled, signal_short):
    """SHORT: TP1 and TP2 below entry, SL above."""
    bot = TelegramBot()
    message = bot._format_signal(signal_short)
    entry_mid = (signal_short.entry_low + signal_short.entry_high) / 2
    assert signal_short.stop_loss > entry_mid
    assert signal_short.tp1 < entry_mid
    assert signal_short.tp2 < signal_short.tp1


# 6. Deterministic signal ID
def test_signal_id_is_deterministic():
    """Same inputs always produce the same signal ID."""
    ts = BASE + timedelta(minutes=5)
    id1 = generate_signal_id("BTCUSDT", "5m", ts, SignalDirection.LONG)
    id2 = generate_signal_id("BTCUSDT", "5m", ts, SignalDirection.LONG)
    assert id1 == id2


def test_signal_id_differs_for_symbol():
    """Different symbols produce different IDs."""
    ts = BASE + timedelta(minutes=5)
    id_btc = generate_signal_id("BTCUSDT", "5m", ts, SignalDirection.LONG)
    id_eth = generate_signal_id("ETHUSDT", "5m", ts, SignalDirection.LONG)
    assert id_btc != id_eth


# 7. Duplicate prevention
def test_duplicate_prevention_in_signal_store(tmp_path):
    """Duplicate protection using SQLite signal store."""
    db_path = str(tmp_path / "test_signals.db")
    store = SignalStore(db_path=db_path)

    ts = BASE + timedelta(minutes=5)
    signal = make_signal(SignalDirection.LONG, "BTCUSDT", 85, ts=ts)

    store.save_signal(signal)
    assert store.signal_exists(signal.signal_id) is True

    other_sid = generate_signal_id("ETHUSDT", "5m", ts, SignalDirection.LONG)
    assert store.signal_exists(other_sid) is False


# 8. Restart duplicate prevention
def test_restart_duplicate_prevention(tmp_path):
    """After app restart, previously-sent signals are not re-sent."""
    db_path = str(tmp_path / "test_restart.db")
    ts = BASE + timedelta(minutes=5)

    store1 = SignalStore(db_path=db_path)
    signal = make_signal(SignalDirection.LONG, "BTCUSDT", 85, ts=ts)
    store1.save_signal(signal)
    del store1

    store2 = SignalStore(db_path=db_path)
    assert store2.signal_exists(signal.signal_id) is True


# 9. Telegram failure handling
def test_telegram_400_error_returns_false(telegram_enabled, signal_long):
    """HTTP 400 (non-retryable) -> returns False."""
    bot = TelegramBot()
    error_resp = make_mock_response(400, {"ok": False, "description": "Bad Request"})

    with patch("app.telegram_bot.requests.post", return_value=error_resp):
        result = bot.send_signal(signal_long)

    assert result is False


def test_telegram_500_failure_returns_false(telegram_enabled, signal_long):
    """HTTP 500 with retries exhausted returns False."""
    bot = TelegramBot()
    error_resp = make_mock_response(503, {"ok": False})

    with patch("app.telegram_bot.requests.post", return_value=error_resp), \
         patch("app.telegram_bot.time.sleep"):
        result = bot.send_signal(signal_long)

    assert result is False


# 10. Bounded retry
def test_bounded_retry_max_attempts(telegram_enabled, signal_long):
    """Retry is bounded: after max_retries attempts, returns False."""
    bot = TelegramBot()
    transient_resp = make_mock_response(503, {"ok": False})

    with patch("app.telegram_bot.requests.post", return_value=transient_resp), \
         patch("app.telegram_bot.time.sleep"):
        result = bot.send_signal(signal_long)

    assert result is False


# 11. Secret not in log
def test_token_not_in_logs(telegram_enabled, signal_long, caplog):
    """Token value never appears in logged output."""
    bot = TelegramBot()
    mock_ok = make_mock_response(200, {"ok": True})

    with patch("app.telegram_bot.requests.post", return_value=mock_ok):
        with caplog.at_level("INFO"):
            bot.send_signal(signal_long)

    token = bot.config.telegram_bot_token
    assert token not in caplog.text


# 12. Missing Telegram configuration
def test_missing_token_when_enabled_raises(monkeypatch):
    """Missing TELEGRAM_BOT_TOKEN when enabled raises config error."""
    monkeypatch.setenv("TELEGRAM_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "your_bot_token_here")  # placeholder
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(config_module, "config", None)

    bot = TelegramBot()
    with pytest.raises(Exception):  # ConfigValidationError or TelegramConfigError
        bot._resolve_credentials()


def test_missing_chat_id_when_enabled_raises(monkeypatch):
    """Missing TELEGRAM_CHAT_ID when enabled raises config error."""
    monkeypatch.setenv("TELEGRAM_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "valid_token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "your_chat_id_here")  # placeholder
    monkeypatch.setattr(config_module, "config", None)

    bot = TelegramBot()
    with pytest.raises(Exception):
        bot._resolve_credentials()