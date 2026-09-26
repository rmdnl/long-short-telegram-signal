"""
Dashboard configuration.

This module is separated from the trading configuration on purpose.
It never handles:
* Trading API keys or secrets
* Signal logic
* Order or outcome state

It does read the bot's ``SYMBOLS`` list through the existing bot
configuration system (``app.config``), so the dashboard always
displays exactly the symbols the bot processes without duplicating
any symbol list.
"""
import inspect
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import find_dotenv, load_dotenv

# Dashboard settings that live in the environment, NOT in .env.
# These must never be overridden by a .env file.
_DASHBOARD_ENV_KEYS = (
    "DASHBOARD_HOST",
    "DASHBOARD_PORT",
    "DASHBOARD_DB_PATH",
    "DASHBOARD_AUTH_ENABLED",
    "DASHBOARD_USERNAME",
    "DASHBOARD_PASSWORD",
    "DASHBOARD_REFRESH_SECONDS",
    "DASHBOARD_MAX_FEED",
    "DASHBOARD_STALE_AFTER_SECONDS",
    "DASHBOARD_DB_TIMEOUT_SECONDS",
    "DASHBOARD_ACTIVITY_LIMIT",
)

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8080
_DEFAULT_REFRESH_SECONDS = 10
_DEFAULT_MAX_FEED = 20
_DEFAULT_STALE_AFTER_SECONDS = 0
_DEFAULT_DB_TIMEOUT_SECONDS = 2.0
_DEFAULT_ACTIVITY_LIMIT = 30

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class DashboardConfigError(RuntimeError):
    """Raised when dashboard configuration cannot be satisfied."""


@dataclass(frozen=True)
class DashboardSettings:
    """Operational settings for the dashboard web server."""

    host: str = _DEFAULT_HOST
    port: int = _DEFAULT_PORT
    db_path: str = "signals.db"
    auth_enabled: bool = False
    username: str = "admin"
    password: str = ""
    refresh_seconds: int = _DEFAULT_REFRESH_SECONDS
    max_feed_items: int = _DEFAULT_MAX_FEED
    stale_after_seconds: int = _DEFAULT_STALE_AFTER_SECONDS
    db_timeout_seconds: float = _DEFAULT_DB_TIMEOUT_SECONDS
    activity_limit: int = _DEFAULT_ACTIVITY_LIMIT


@dataclass(frozen=True)
class BotConfigSnapshot:
    """A point-in-time read of the bot configuration."""

    symbols: list[str] = field(default_factory=list)
    scan_interval_seconds: Optional[int] = None
    min_score: Optional[int] = None
    telegram_enabled: bool = False
    signal_only: bool = True
    error: Optional[str] = None
    source: Optional[str] = None
    loaded_at: str = ""
    fingerprint_changed: bool = False


def _resolve_db_path(raw: Optional[str]) -> str:
    """Resolve the database path. Prefer the bot's own default path."""
    try:
        from app.signal_store import SignalStore
        param = inspect.signature(SignalStore.__init__).parameters.get("db_path")
        default_db = param.default if param and isinstance(param.default, str) else "signals.db"
    except Exception:
        default_db = "signals.db"

    path = raw or default_db
    if not os.path.isabs(path):
        path = str(_PROJECT_ROOT / path)
    return os.path.normpath(path)


def _load_env_vars() -> tuple[dict[str, str], dict[str, str]]:
    """Load bot .env values while preserving dashboard env keys.

    Returns (saved_dashboard_keys, dotenv_path_or_empty).
    """
    saved = {}
    for key in _DASHBOARD_ENV_KEYS:
        if key in os.environ:
            saved[key] = os.environ[key]
    return saved, find_dotenv()


def _restore_env_keys(saved: dict[str, str]) -> None:
    """Restore dashboard-specific environment variables after reload."""
    for key, value in saved.items():
        os.environ[key] = value
    for key in _DASHBOARD_ENV_KEYS:
        if key not in os.environ and key in saved:
            os.environ[key] = saved[key]


def load_dashboard_settings(env=None) -> DashboardSettings:
    """Read dashboard settings from environment variables only.

    Never reads trading credentials. Default binds to 127.0.0.1.
    """
    env = env or os.environ

    raw_host = env.get("DASHBOARD_HOST", _DEFAULT_HOST)
    raw_port = env.get("DASHBOARD_PORT", str(_DEFAULT_PORT))
    raw_db = env.get("DASHBOARD_DB_PATH", None)
    raw_auth = env.get("DASHBOARD_AUTH_ENABLED", "false").lower() == "true"
    raw_user = env.get("DASHBOARD_USERNAME", "admin")
    raw_pass = env.get("DASHBOARD_PASSWORD", "")
    raw_refresh = env.get("DASHBOARD_REFRESH_SECONDS", str(_DEFAULT_REFRESH_SECONDS))
    raw_max_feed = env.get("DASHBOARD_MAX_FEED", str(_DEFAULT_MAX_FEED))
    raw_stale = env.get("DASHBOARD_STALE_AFTER_SECONDS", str(_DEFAULT_STALE_AFTER_SECONDS))
    raw_timeout = env.get("DASHBOARD_DB_TIMEOUT_SECONDS", str(_DEFAULT_DB_TIMEOUT_SECONDS))
    raw_activity = env.get("DASHBOARD_ACTIVITY_LIMIT", str(_DEFAULT_ACTIVITY_LIMIT))

    if raw_host == "0.0.0.0":
        pass  # explicit user choice; no default override needed

    if raw_auth and not raw_pass:
        raise DashboardConfigError(
            "DASHBOARD_AUTH_ENABLED=true but DASHBOARD_PASSWORD is missing. "
            "Failing safely instead of running an unlocked dashboard."
        )

    try:
        port = int(raw_port)
    except ValueError:
        raise DashboardConfigError(f"Invalid DASHBOARD_PORT: {raw_port!r}")
    if not (1 <= port <= 65535):
        raise DashboardConfigError(f"Invalid DASHBOARD_PORT: {raw_port}")

    try:
        refresh = int(raw_refresh)
    except ValueError:
        raise DashboardConfigError(f"Invalid DASHBOARD_REFRESH_SECONDS: {raw_refresh!r}")

    try:
        max_feed = int(raw_max_feed)
    except ValueError:
        raise DashboardConfigError(f"Invalid DASHBOARD_MAX_FEED: {raw_max_feed!r}")

    try:
        stale = int(raw_stale)
    except ValueError:
        raise DashboardConfigError(f"Invalid DASHBOARD_STALE_AFTER_SECONDS: {raw_stale!r}")

    try:
        timeout = float(raw_timeout)
    except ValueError:
        raise DashboardConfigError(f"Invalid DASHBOARD_DB_TIMEOUT_SECONDS: {raw_timeout!r}")

    try:
        activity = int(raw_activity)
    except ValueError:
        raise DashboardConfigError(f"Invalid DASHBOARD_ACTIVITY_LIMIT: {raw_activity!r}")

    return DashboardSettings(
        host=raw_host,
        port=port,
        db_path=_resolve_db_path(raw_db),
        auth_enabled=raw_auth,
        username=raw_user,
        password=raw_pass,
        refresh_seconds=refresh,
        max_feed_items=max_feed,
        stale_after_seconds=stale,
        db_timeout_seconds=timeout,
        activity_limit=activity,
    )


def load_bot_config(force: bool = False) -> BotConfigSnapshot:
    """Load the bot configuration used by the running signal bot.

    The dashboard never duplicates the symbol list. It re-reads
    the bot's existing configuration source (``.env`` + environment)
    and returns ``symbols`` exactly as the bot sees them.

    Setting ``force=True`` reloads ``.env`` and refreshes the
    singleton so a changed ``SYMBOLS=`` value is picked up.
    """
    snapshot = BotConfigSnapshot()
    try:
        saved, dotenv_path = _load_env_vars()
        try:
            # Same default as app/config.py: environment vars win over .env.
            load_dotenv(dotenv_path)
        except Exception:
            pass
        _restore_env_keys(saved)

        from app.config import get_config, ConfigValidationError
        from app.config import config as _bot_config_singleton

        if force:
            _bot_config_singleton = None
            import app.config as _cfgmod
            _cfgmod.config = None

        cfg = get_config()
        snapshot = BotConfigSnapshot(
            symbols=list(cfg.symbols),
            scan_interval_seconds=cfg.scan_interval_seconds,
            min_score=cfg.min_score,
            telegram_enabled=cfg.telegram_enabled,
            signal_only=cfg.signal_only,
            source=dotenv_path if dotenv_path else None,
            loaded_at=datetime.now(timezone.utc).isoformat(),
            fingerprint_changed=False,
        )
    except Exception as exc:
        snapshot = BotConfigSnapshot(
            symbols=[],
            error=f"{type(exc).__name__}: {exc}",
            loaded_at=datetime.now(timezone.utc).isoformat(),
            fingerprint_changed=False,
        )
    return snapshot
