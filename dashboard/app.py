"""
Read-only FastAPI application for monitoring the Crypto Long/Short Signal Bot.

This module serves the dashboard web interface and read-only JSON APIs.
It is deliberately isolated from the trading engine. It never touches:
* Binance trading endpoints, credentials, or order execution
* Signal generation, indicator, or risk logic
* Telegram bot tokens, credentials, or messaging
* Database mutations, table creation, or schema changes
* Any bot state, configuration files, or deployment scripts

The only writes performed are logging and reading Python environment
variables via ``os.environ``.
"""
from contextlib import asynccontextmanager
import secrets
import time
from base64 import b64decode
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from dashboard import __version__
from dashboard.config import (
    DashboardSettings,
    load_dashboard_settings,
    load_bot_config,
    BotConfigSnapshot,
)
from dashboard.db import ReadOnlyDatabase
from dashboard.service import (
    build_dashboard_context,
    build_signal_feed,
    build_outcome_events,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
INDEX_PATH = Path(__file__).resolve().parent / "templates" / "index.html"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup banner: read-only declarations only, no mutations."""
    settings: DashboardSettings = app.state.settings
    print("-----------------------------------------------------------")
    print("  LONG / SHORT SIGNAL MONITOR")
    print("  Read-only monitoring dashboard")
    print(f"  Version: {__version__}")
    print(f"  Database: {settings.db_path}")
    print(f"  Bind: {settings.host}:{settings.port}")
    print("-----------------------------------------------------------")
    print("  SECURITY NOTICE: READ-ONLY dashboard.")
    print("  No trading execution. No order placement. No credentials.")
    print("-----------------------------------------------------------")
    yield


class AuthMiddleware(BaseHTTPMiddleware):
    """Optional HTTP basic authentication for every route and static file."""

    def __init__(self, app, username: str, password: str, enabled: bool):
        super().__init__(app)
        self.username = username
        self.password = password
        self.enabled = enabled

    async def dispatch(self, request: Request, call_next):
        if not self.enabled:
            return await call_next(request)
        header = request.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                decoded = b64decode(header[6:], validate=True).decode("utf-8")
                user, password = decoded.split(":", 1)
                user_ok = secrets.compare_digest(user, self.username)
                pass_ok = secrets.compare_digest(password, self.password)
                if user_ok and pass_ok:
                    return await call_next(request)
            except Exception:
                pass
        return Response(
            content=b"Unauthorized",
            status_code=401,
            media_type="text/plain",
            headers={"WWW-Authenticate": 'Basic realm="Signal Monitor"'},
        )


def create_app(
    settings: Optional[DashboardSettings] = None,
    db: Optional[ReadOnlyDatabase] = None,
) -> FastAPI:
    """Create the FastAPI application with injected dependencies.

    This function never mutates the bot, the signals database, or Telegram
    state. All database operations are read-only, and configuration reads go
    through the existing bot configuration system (no second symbol list).
    """
    if settings is None:
        settings = load_dashboard_settings()

    if db is None:
        db = ReadOnlyDatabase(settings.db_path, settings.db_timeout_seconds)

    app = FastAPI(
        title="Long/Short Signal Monitor",
        description="Read-only monitoring dashboard for the Crypto Long/Short Signal Bot",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.db = db
    app.state.bot_config = BotConfigSnapshot()
    app.state._bot_config_refresh = 0.0

    app.add_middleware(
        AuthMiddleware,
        username=settings.username,
        password=settings.password,
        enabled=settings.auth_enabled,
    )

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    _register_routes(app)
    return app


def _get_context(app: FastAPI) -> dict:
    """Fetch the dashboard context, reusing recently loaded bot config."""
    settings: DashboardSettings = app.state.settings
    db: ReadOnlyDatabase = app.state.db
    now = time.time()
    if now - app.state._bot_config_refresh > settings.refresh_seconds:
        try:
            app.state.bot_config = load_bot_config(force=False)
        except Exception:
            pass
        app.state._bot_config_refresh = now
    return build_dashboard_context(settings, app.state.bot_config, db)


def _register_routes(app: FastAPI) -> None:
    """Register GET-only read-only endpoints."""

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def root():
        if not INDEX_PATH.exists():
            return HTMLResponse(content="<h2>index.html not found</h2>", status_code=500)
        return HTMLResponse(content=INDEX_PATH.read_text(encoding="utf-8"))

    @app.get("/api/health")
    async def health():
        db: ReadOnlyDatabase = app.state.db
        return {
            "status": "ok",
            "version": __version__,
            "db_available": db.is_available,
            "db_reason": db.unavailable_reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    @app.get("/api/status")
    async def status():
        ctx = _get_context(app)
        return {
            "available": ctx["available"],
            "bot_activity": ctx.get("bot_activity"),
            "telegram_enabled": ctx.get("telegram_enabled", False),
            "signal_only": ctx.get("signal_only", True),
            "config_error": ctx.get("config_error"),
            "scan_interval_seconds": ctx.get("scan_interval_seconds"),
            "min_score": ctx.get("min_score"),
            "symbols_count": len(ctx.get("symbols", [])),
            "summary": ctx.get("summary"),
        }

    @app.get("/api/symbols")
    async def symbols():
        return {"symbols": _get_context(app).get("symbols", [])}

    @app.get("/api/signals")
    async def signals(limit: int = 20, offset: int = 0, symbol: str = "", status: str = ""):
        db: ReadOnlyDatabase = app.state.db
        if not db.is_available:
            return {"available": False, "signals": []}
        rows = db.fetch_signals(
            limit=max(1, min(limit, 100)),
            offset=max(0, offset),
            symbol=symbol if symbol else None,
            status=status if status else None,
        )
        return {"available": True, "signals": build_signal_feed(rows, max_items=100)}

    @app.get("/api/signals/latest")
    async def signals_latest(limit: int = 20):
        db: ReadOnlyDatabase = app.state.db
        if not db.is_available:
            return {"available": False, "signals": []}
        rows = db.fetch_signals(limit=max(1, min(limit, 100)))
        return {"available": True, "signals": build_signal_feed(rows, max_items=100)}

    @app.get("/api/outcomes")
    async def outcomes(limit: int = 20):
        db: ReadOnlyDatabase = app.state.db
        if not db.is_available:
            return {"available": False, "events": []}
        rows = db.fetch_outcome_events(limit=max(1, min(limit, 100)))
        return {"available": True, "events": build_outcome_events(rows, max_items=100)}

    @app.get("/api/summary")
    async def summary():
        ctx = _get_context(app)
        return {
            "available": ctx["available"],
            "summary": ctx.get("summary"),
            "bot_activity": ctx.get("bot_activity"),
            "delivery": ctx.get("delivery"),
            "reliability": ctx.get("reliability"),
            "distribution": ctx.get("distribution"),
            "symbols": ctx.get("matrix"),
            "config_error": ctx.get("config_error"),
            "config_source": ctx.get("bot_config").source if ctx.get("bot_config") else None,
        }

    @app.get("/api/activity")
    async def activity(limit: int = 30):
        ctx = _get_context(app)
        items = ctx.get("activity", [])[:max(1, min(limit, 200))]
        return {"available": ctx.get("available", True), "activity": items}


def main() -> None:
    """Run the dashboard. Read-only. Binds to 127.0.0.1 by default."""
    import uvicorn

    settings = load_dashboard_settings()
    app = create_app(settings=settings)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
