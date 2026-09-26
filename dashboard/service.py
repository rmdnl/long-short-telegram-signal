"""
Dashboard service layer.

Pure data transformation over the read-only database and bot
configuration. Never mutates trading state, signals, or Telegram.
"""
import math
from datetime import datetime, timedelta, timezone
from typing import Optional

from dashboard.db import ReadOnlyDatabase
from dashboard.config import DashboardSettings, BotConfigSnapshot

NA = None

STALE_THRESHOLD_DEFAULT = 300

# Runtime heartbeat keys written ONLY by the bot (app/main.py) into the
# existing bot_meta table. The dashboard only reads them.
RUNTIME_KEY_BOT_STARTED_AT = "bot_started_at"
RUNTIME_KEY_LAST_SCAN_STARTED_AT = "last_scan_started_at"
RUNTIME_KEY_LAST_SCAN_COMPLETED_AT = "last_scan_completed_at"
RUNTIME_KEY_LAST_HEARTBEAT = "last_heartbeat"
RUNTIME_KEY_SCAN_CYCLE = "scan_cycle"


def _safe_int(value, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_decimal(value, default: Optional[float] = None) -> Optional[float]:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _fmt(value, digits: int = 1) -> Optional[str]:
    v = _safe_decimal(value)
    if v is None:
        return None
    return f"{v:.{digits}f}"


def _iso(dt) -> Optional[str]:
    if dt is None or dt == "":
        return None
    if isinstance(dt, str):
        try:
            return datetime.fromisoformat(dt).isoformat()
        except ValueError:
            return dt
    return dt.isoformat()


def _age_seconds(ts_iso: Optional[str]) -> Optional[float]:
    dt = _parse_ts(ts_iso)
    if dt is None:
        return None
    try:
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return None


def _parse_ts(value) -> Optional[datetime]:
    """Parse an ISO string or epoch-seconds string into aware UTC datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (TypeError, ValueError, OSError, OverflowError):
            return None
    text = str(value).strip()
    if not text:
        return None
    # Legacy epoch-seconds format (e.g. bot_booted_at_epoch).
    try:
        epoch = float(text)
        # Epoch format is bare digits (optionally with a dot). Guard against
        # ISO years like "2025-..." which also float()-fail, so a successful
        # float parse here means epoch — but only if no date separators.
        if all(c.isdigit() or c in "+-." for c in text):
            return datetime.fromtimestamp(epoch, tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        pass
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _safe_cycle(value) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _resolve_stale_threshold(
    settings_stale_after: Optional[int],
    scan_interval: Optional[int],
) -> int:
    """Use the configured dashboard threshold; fall back to 3x scan interval."""
    if settings_stale_after and settings_stale_after > 0:
        return int(settings_stale_after)
    if scan_interval and scan_interval > 0:
        return int(scan_interval) * 3
    return STALE_THRESHOLD_DEFAULT


def _truncate_error(msg: str, limit: int = 200) -> str:
    if not msg:
        return ""
    msg = str(msg)
    if len(msg) > limit:
        return msg[:limit] + "..."
    return msg


def _scrub_secrets(text: str) -> str:
    """Redact bot tokens and sensitive patterns."""
    import re
    text = str(text)
    text = re.sub(r"\b\d{6,}:[A-Za-z0-9_-]{10,}\b", "***REDACTED***", text)
    return text


def build_symbol_matrix(
    latest_per_symbol: dict[str, dict],
    symbols: list[str],
) -> list[dict]:
    """Build the dynamic symbol matrix from configured symbols."""
    rows = []
    for symbol in symbols:
        s = latest_per_symbol.get(symbol)
        if not s:
            rows.append({
                "symbol": symbol,
                "latest_direction": None, "score": None,
                "htf_bias": None, "adx": None, "rsi": None,
                "volume_ratio": None, "status": None,
                "signal_time": None,
            })
            continue
        rows.append({
            "symbol": symbol,
            "latest_direction": s.get("direction"),
            "score": _safe_int(s.get("score")),
            "htf_bias": s.get("htf_bias"),
            "adx": _fmt(s.get("adx_value")),
            "rsi": _fmt(s.get("rsi_value")),
            "volume_ratio": _fmt(s.get("volume_ratio")),
            "status": s.get("status"),
            "signal_time": _iso(s.get("created_at")),
        })
    return rows


def build_signal_feed(
    signals: list[dict],
    max_items: int = 20,
) -> list[dict]:
    """Transform signals for the feed with display values."""
    out = []
    for s in signals[:max_items]:
        out.append({
            "direction": s.get("direction"),
            "symbol": s.get("symbol"),
            "score": _safe_int(s.get("score")),
            "entry_low": _fmt(s.get("entry_low")),
            "entry_high": _fmt(s.get("entry_high")),
            "stop_loss": _fmt(s.get("stop_loss")),
            "tp1": _fmt(s.get("tp1")),
            "tp2": _fmt(s.get("tp2")),
            "status": s.get("status"),
            "created_at": _iso(s.get("created_at")),
            "delivery_status": s.get("delivery_status"),
            "delivery_attempts": _safe_int(s.get("delivery_attempts")),
            "last_delivery_error": _scrub_secrets(
                _truncate_error(s.get("last_delivery_error"))
            ) if s.get("last_delivery_error") else None,
        })
    return out


def build_outcome_events(
    events: list[dict],
    max_items: int = 20,
) -> list[dict]:
    """Transform outcome rows for display."""
    out = []
    for e in events[:max_items]:
        outcome = e.get("status")
        ts = None
        if outcome == "TP1_HIT":
            ts = _iso(e.get("tp1_hit_time"))
        elif outcome == "TP2_HIT":
            ts = _iso(e.get("tp2_hit_time"))
        elif outcome == "STOPPED":
            ts = _iso(e.get("sl_hit_time"))
        elif outcome == "EXPIRED":
            ts = _iso(e.get("expiration_time"))
        out.append({
            "symbol": e.get("symbol"),
            "direction": e.get("direction"),
            "outcome": outcome,
            "time": ts,
        })
    return out


def build_outcome_reliability(
    attempts: dict,
    last_error: Optional[str],
    last_evaluated: Optional[str],
) -> dict:
    """Build outcome health panel."""
    return {
        "outcome_attempts": _safe_int(attempts.get("outcome_attempts"), 0),
        "tp1_attempts": _safe_int(attempts.get("tp1_outcome_attempts"), 0),
        "tp2_attempts": _safe_int(attempts.get("tp2_outcome_attempts"), 0),
        "sl_attempts": _safe_int(attempts.get("sl_outcome_attempts"), 0),
        "last_outcome_error": _scrub_secrets(
            _truncate_error(last_error)
        ) if last_error else None,
        "last_evaluated_candle_time": _iso(last_evaluated),
    }


def build_delivery_health(
    counts: dict,
    errors: list[dict],
) -> dict:
    """Build Telegram delivery section."""
    return {
        "delivered": _safe_int(counts.get("delivered"), 0),
        "pending": _safe_int(counts.get("pending"), 0),
        "failed": _safe_int(counts.get("failed"), 0),
        "retry_attempts": _safe_int(counts.get("total_attempts"), 0),
        "recent_errors": [
            {
                "signal_id": e.get("signal_id"),
                "error": _scrub_secrets(
                    _truncate_error(e.get("last_delivery_error"), 120)
                ) if e.get("last_delivery_error") else None,
                "delivery_status": e.get("delivery_status"),
                "created_at": _iso(e.get("created_at")),
                "delivery_attempts": _safe_int(e.get("delivery_attempts")),
            }
            for e in errors[:8]
        ],
    }


def build_statistics(
    total: int,
    by_direction: dict[str, int],
    by_status: dict[str, int],
    delivery_counts: dict,
) -> dict:
    """Build performance statistics from persisted data."""
    active = by_status.get("ACTIVE", 0) + by_status.get("TP1_HIT", 0)
    return {
        "total_signals": total,
        "long_signals": by_direction.get("LONG", 0),
        "short_signals": by_direction.get("SHORT", 0),
        "active": active,
        "tp1_hit": by_status.get("TP1_HIT", 0),
        "tp2_hit": by_status.get("TP2_HIT", 0),
        "stopped": by_status.get("STOPPED", 0),
        "expired": by_status.get("EXPIRED", 0),
        "telegram_delivered": delivery_counts.get("delivered", 0),
        "telegram_failed": delivery_counts.get("failed", 0),
    }


def build_activity(
    signals: list[dict],
    max_items: int = 30,
) -> list[dict]:
    """Build the recent activity timeline."""
    feed = build_signal_feed(signals[:max_items], max_items)
    events = []
    for s in feed:
        created = s["created_at"]
        status = s["status"]
        direction = s["direction"]
        symbol = s["symbol"]

        if direction and symbol:
            events.append({
                "type": "SIGNAL_GENERATED",
                "label": f"{direction} signal generated",
                "detail": symbol,
                "time": created,
                "time_source": "persisted",
                "severity": "info",
            })

        if status == "TP1_HIT":
            events.append({
                "type": "TP1_HIT",
                "label": "TP1 HIT",
                "detail": symbol,
                "time": _iso(s.get("tp1_hit_time")) or created,
                "time_source": "persisted" if s.get("tp1_hit_time") else "created_at_fallback",
                "severity": "success",
            })
        elif status == "TP2_HIT":
            events.append({
                "type": "TP2_HIT",
                "label": "TP2 HIT",
                "detail": symbol,
                "time": _iso(s.get("tp2_hit_time")) or created,
                "time_source": "persisted" if s.get("tp2_hit_time") else "created_at_fallback",
                "severity": "success",
            })
        elif status == "STOPPED":
            events.append({
                "type": "STOPPED",
                "label": "SL HIT",
                "detail": symbol,
                "time": _iso(s.get("sl_hit_time")) or created,
                "time_source": "persisted" if s.get("sl_hit_time") else "created_at_fallback",
                "severity": "danger",
            })
        elif status == "EXPIRED":
            events.append({
                "type": "EXPIRED",
                "label": "EXPIRED",
                "detail": symbol,
                "time": _iso(s.get("expiration_time")) or created,
                "time_source": "persisted" if s.get("expiration_time") else "created_at_fallback",
                "severity": "neutral",
            })

        if s["delivery_status"] == "FAILED" and s["delivery_attempts"] > 0:
            events.append({
                "type": "DELIVERY_FAILED",
                "label": "Telegram delivery failed",
                "detail": symbol,
                "time": created,
                "time_source": "created_at_fallback",
                "severity": "danger",
            })

        if status in ("ACTIVE", "TP1_HIT"):
            events.append({
                "type": "OUTCOME_PENDING",
                "label": "Outcome pending",
                "detail": symbol,
                "time": created,
                "time_source": "created_at_fallback",
                "severity": "warning",
            })

    events.sort(key=lambda x: x["time"] or "", reverse=True)
    return events[:max_items]


def build_score_distribution(
    distribution: dict[str, int],
) -> list[dict]:
    """Format score buckets for the frontend."""
    labels = ["0-59", "60-69", "70-79", "80-89", "90-100"]
    total = sum(_safe_int(distribution.get(l, 0)) for l in labels)
    if total == 0:
        return []
    return [
        {
            "label": label,
            "count": _safe_int(distribution.get(label, 0)),
            "pct": round(_safe_int(distribution.get(label, 0)) / total * 100),
        }
        for label in labels
    ]


def _bot_activity_from_meta(
    meta: dict,
    now: datetime,
    settings: DashboardSettings,
    bot_config: BotConfigSnapshot,
) -> dict:
    """Derive ONLINE/STALE, uptime, last scan, cycle, next expected."""
    bot_started_at = meta.get(RUNTIME_KEY_BOT_STARTED_AT)
    last_scan_completed_at = meta.get(RUNTIME_KEY_LAST_SCAN_COMPLETED_AT)
    last_heartbeat = meta.get(RUNTIME_KEY_LAST_HEARTBEAT)
    scan_cycle = _safe_cycle(meta.get(RUNTIME_KEY_SCAN_CYCLE))

    # Uptime from bot_started_at.
    uptime_seconds = None
    if bot_started_at:
        dt = _parse_ts(bot_started_at)
        if dt:
            uptime_seconds = int((now - dt).total_seconds())

    # Latest activity: prefer completed scan, then heartbeat.
    latest_activity_time = None
    latest_activity_age = None
    if last_scan_completed_at:
        dt = _parse_ts(last_scan_completed_at)
        if dt:
            latest_activity_time = dt.isoformat()
            latest_activity_age = (now - dt).total_seconds()
    if latest_activity_age is None and last_heartbeat:
        dt = _parse_ts(last_heartbeat)
        if dt:
            latest_activity_time = dt.isoformat()
            latest_activity_age = (now - dt).total_seconds()

    # Next expected scan = last_scan_completed_at + interval.
    next_expected = None
    if last_scan_completed_at:
        dt = _parse_ts(last_scan_completed_at)
        if dt:
            interval = bot_config.scan_interval_seconds
            if interval and interval > 0:
                next_dt = dt + timedelta(seconds=interval)
                next_expected = next_dt.isoformat()

    stale_threshold = _resolve_stale_threshold(
        settings.stale_after_seconds,
        bot_config.scan_interval_seconds,
    )
    stale = latest_activity_age is not None and latest_activity_age > stale_threshold

    return {
        "status_label": "STALE" if stale else "ONLINE",
        "latest_activity_time": latest_activity_time,
        "latest_activity_age_seconds": round(latest_activity_age, 1) if latest_activity_age is not None else None,
        "stale": stale,
        "stale_threshold_seconds": stale_threshold,
        "uptime_seconds": uptime_seconds,
        "scan_cycle": scan_cycle,
        "next_expected": next_expected,
    }


def _legacy_bot_activity(
    latest_time: Optional[str],
    evaluated_time: Optional[str],
    boot_epoch: Optional[str],
    scan_interval: Optional[int],
) -> dict:
    """Preserve existing fallback when runtime meta is absent."""
    timestamps = []
    latest_age = None
    latest_time_val = None
    if latest_time:
        age = _age_seconds(latest_time)
        if age is not None:
            timestamps.append(("latest_signal_created_at", latest_time, age))
    if evaluated_time:
        age = _age_seconds(evaluated_time)
        if age is not None:
            timestamps.append(("last_evaluated_candle", evaluated_time, age))
    if timestamps:
        timestamps.sort(key=lambda t: t[2])
        latest_age = timestamps[0][2]
        latest_time_val = timestamps[0][1]

    stale_threshold = scan_interval and scan_interval * 3 or STALE_THRESHOLD_DEFAULT
    stale = latest_age is not None and latest_age > stale_threshold

    uptime_seconds = None
    if boot_epoch:
        dt = _parse_ts(boot_epoch)
        if dt:
            uptime_seconds = int((datetime.now(timezone.utc) - dt).total_seconds())

    return {
        "status_label": "DATA ACTIVITY",
        "latest_activity_time": latest_time_val,
        "latest_activity_age_seconds": round(latest_age, 1) if latest_age is not None else None,
        "stale": stale,
        "stale_threshold_seconds": stale_threshold,
        "uptime_seconds": uptime_seconds,
        "scan_cycle": None,
        "next_expected": None,
    }


def build_bot_activity(
    db: ReadOnlyDatabase,
    settings: DashboardSettings,
    bot_config: BotConfigSnapshot,
) -> dict:
    """Determine bot activity status from runtime heartbeat meta.

    Uses bot_meta keys written by the running bot when present;
    otherwise falls back to the legacy signal-based behaviour so
    the dashboard degrades gracefully before the bot has written
    any heartbeat.
    """
    now = datetime.now(timezone.utc)
    meta = db.get_meta_all()

    # Runtime meta present when any heartbeat key exists or a cycle
    # number has been persisted.
    has_runtime = any(
        meta.get(k) not in (None, "")
        for k in (
            RUNTIME_KEY_BOT_STARTED_AT,
            RUNTIME_KEY_LAST_SCAN_COMPLETED_AT,
            RUNTIME_KEY_LAST_HEARTBEAT,
            RUNTIME_KEY_SCAN_CYCLE,
        )
    )
    if has_runtime:
        return _bot_activity_from_meta(meta, now, settings, bot_config)
    return _legacy_bot_activity(
        db.latest_signal_time(),
        db.last_evaluated_time(),
        db.bot_uptime_epoch(),
        bot_config.scan_interval_seconds,
    )


def _age_human(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def build_dashboard_context(
    settings: DashboardSettings,
    bot_config: BotConfigSnapshot,
    db: ReadOnlyDatabase,
) -> dict:
    """Assemble all dashboard data for the API responses."""
    db_available = db.is_available
    ctx = {
        "available": db_available,
        "db_reason": db.unavailable_reason if not db_available else None,
        "bot_config": bot_config,
        "symbols": bot_config.symbols,
        "scan_interval_seconds": bot_config.scan_interval_seconds,
        "min_score": bot_config.min_score,
        "telegram_enabled": bot_config.telegram_enabled,
        "signal_only": bot_config.signal_only,
        "config_error": bot_config.error,
    }

    if not db_available:
        ctx.update({
            "summary": build_statistics(0, {}, {}, {}),
            "matrix": build_symbol_matrix({}, bot_config.symbols),
            "feed": [],
            "outcomes": [],
            "delivery": build_delivery_health({}, []),
            "reliability": {},
            "distribution": [],
            "activity": [],
            "bot_activity": {
                "status_label": "DATA ACTIVITY",
                "evidence": ["database unavailable"],
                "latest_activity_time": None,
                "latest_activity_age_seconds": None,
                "stale": False,
                "stale_threshold_seconds": _resolve_stale_threshold(
                    settings.stale_after_seconds,
                    bot_config.scan_interval_seconds,
                ),
                "uptime_seconds": None,
                "scan_cycle": None,
                "next_expected": None,
            },
        })
        return ctx

    total = db.count_total()
    by_direction = db.count_by_direction()
    by_status = db.count_by_status()
    delivery_counts = db.delivery_counts()
    latest = db.fetch_latest_per_symbol()
    matrix = build_symbol_matrix(latest, bot_config.symbols)
    feed = build_signal_feed(
        db.fetch_signals(limit=settings.max_feed_items),
        max_items=settings.max_feed_items,
    )
    outcome_events = build_outcome_events(
        db.fetch_outcome_events(limit=settings.max_feed_items),
    )
    attempts = db.outcome_attempts()
    reliability = build_outcome_reliability(
        attempts,
        db.last_outcome_error_value(),
        db.last_evaluated_time(),
    )
    delivery_errors = db.last_delivery_error()
    delivery = build_delivery_health(delivery_counts, delivery_errors)
    distribution = build_score_distribution(db.score_distribution())
    activity = build_activity(
        db.fetch_signals(limit=settings.activity_limit),
        max_items=settings.activity_limit,
    )
    bot_activity = build_bot_activity(db, settings, bot_config)

    ctx.update({
        "summary": build_statistics(total, by_direction, by_status, delivery_counts),
        "matrix": matrix,
        "feed": feed,
        "outcomes": outcome_events,
        "delivery": delivery,
        "reliability": reliability,
        "distribution": distribution,
        "activity": activity,
        "bot_activity": bot_activity,
    })
    return ctx
