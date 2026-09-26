/**
 * Signal Monitor – vanilla JS frontend.
 * Read-only. Never calls trading endpoints. Never sends secrets.
 */
"use strict";

const REFRESH_MS = 10000;
const API = { status: "/api/status", symbols: "/api/symbols", summary: "/api/summary", activity: "/api/activity" };

let lastUpdated = null;

// ── Helpers ──
function $(id) { return document.getElementById(id); }

function escapeHtml(str) {
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function fmt(value, suffix) {
  if (value === null || value === undefined || value === "") return '<span class="na-value">N/A</span>';
  return escapeHtml(String(value)) + (suffix ? suffix : "");
}

function fmtTime(iso) {
  if (!iso) return '<span class="na-value">N/A</span>';
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return escapeHtml(iso);
    return d.toLocaleString();
  } catch (e) { return escapeHtml(iso); }
}

function ageHuman(seconds) {
  if (seconds === null || seconds === undefined) return null;
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return s + "s ago";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
}

function durationHuman(seconds) {
  if (seconds === null || seconds === undefined) return null;
  const s = Math.max(0, Math.floor(seconds));
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return d + "d " + h + "h";
  if (h > 0) return h + "h " + m + "m";
  return m + "m " + (s % 60) + "s";
}

function statusClass(status) {
  const s = String(status || "").toUpperCase();
  if (s === "ACTIVE") return "active";
  if (s === "TP1_HIT") return "tp1";
  if (s === "TP2_HIT") return "tp2";
  if (s === "STOPPED" || s === "INVALIDATED") return "stopped";
  if (s === "EXPIRED") return "expired";
  return "expired";
}

function deliveryClass(status) {
  const s = String(status || "").toUpperCase();
  if (s === "DELIVERED") return "delivered";
  if (s === "PENDING") return "pending";
  if (s === "FAILED") return "failed";
  return "pending";
}

function directionClass(direction) {
  return String(direction || "").toUpperCase() === "SHORT" ? "short" : "long";
}

function biasClass(bias) {
  const b = String(bias || "").toUpperCase();
  if (b === "BULLISH") return "bias-bullish";
  if (b === "BEARISH") return "bias-bearish";
  return "bias-neutral";
}

function setHtml(id, html) {
  const el = $(id);
  if (el) el.innerHTML = html;
}

function setText(id, text) {
  const el = $(id);
  if (el) el.textContent = text;
}

// ── Data fetching ──
async function fetchJson(url) {
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(url + " -> " + res.status);
  return res.json();
}

function showError(show) {
  const b = $("error-banner");
  if (b) b.classList.toggle("hidden", !show);
}

function updateTimestamp() {
  lastUpdated = Date.now();
  const el = $("last-updated");
  if (el) el.textContent = new Date().toLocaleTimeString();
}

// ── Rendering ──
function renderStatus(data) {
  const ba = data.bot_activity || {};

  if (ba.status_label) {
    const stale = ba.stale ? " 🟡 STALE" : "";
    setText("badge-bot-status", "🟢 " + ba.status_label + stale);
  }

  setText("card-bot-status", ba.status_label || "N/A");

  const uptime = durationHuman(ba.uptime_seconds);
  setText("card-uptime", uptime || "N/A");

  const age = ageHuman(ba.latest_activity_age_seconds);
  setText("card-last-scan", age || "N/A");

  const interval = data.scan_interval_seconds;
  if (age && interval) {
    setText("card-next-scan", "~" + interval + "s cadence");
  } else {
    setText("card-next-scan", "N/A");
  }

  const tg = data.telegram_enabled;
  setText("card-telegram", tg ? "ENABLED" : "DISABLED");
  setText("badge-telegram-status", tg ? "ENABLED" : "DISABLED");

  setText("card-database", data.available ? "CONNECTED" : "UNAVAILABLE");
  setText("card-total-signals", data.summary ? String(data.summary.total_signals) : "N/A");
  setText("card-active-signals", data.summary ? String(data.summary.active) : "N/A");
}

function renderMatrix(rows) {
  const body = $("symbol-matrix-body");
  if (!body) return;
  if (!rows || rows.length === 0) {
    body.innerHTML = '<tr><td colspan="9" class="na-value" style="text-align:center;padding:14px">No symbols configured or no data</td></tr>';
    return;
  }
  body.innerHTML = rows.map(function (r) {
    const dir = r.latest_direction
      ? '<span class="direction-' + directionClass(r.latest_direction) + '">' + escapeHtml(r.latest_direction) + "</span>"
      : '<span class="na-value">—</span>';
    const status = r.status
      ? '<span class="status-badge ' + statusClass(r.status) + '">' + escapeHtml(r.status) + "</span>"
      : '<span class="na-value">—</span>';
    const bias = r.htf_bias
      ? '<span class="' + biasClass(r.htf_bias) + '">' + escapeHtml(r.htf_bias) + "</span>"
      : '<span class="na-value">—</span>';
    return "<tr>" +
      "<td>" + escapeHtml(r.symbol) + "</td>" +
      "<td>" + dir + "</td>" +
      "<td>" + fmt(r.score) + "</td>" +
      "<td>" + bias + "</td>" +
      "<td>" + fmt(r.adx) + "</td>" +
      "<td>" + fmt(r.rsi) + "</td>" +
      "<td>" + fmt(r.volume_ratio, "x") + "</td>" +
      "<td>" + status + "</td>" +
      "<td>" + fmtTime(r.signal_time) + "</td>" +
      "</tr>";
  }).join("");
}

function renderFeed(items) {
  const el = $("signal-feed");
  if (!el) return;
  if (!items || items.length === 0) {
    el.innerHTML = '<div class="na-value" style="padding:8px 0">No signals yet</div>';
    return;
  }
  el.innerHTML = items.map(function (s) {
    const dir = directionClass(s.direction);
    const err = s.last_delivery_error
      ? '<div class="error-text">⚠ ' + escapeHtml(s.last_delivery_error) + "</div>"
      : "";
    return '<div class="signal-card">' +
      '<div class="signal-card-header">' +
        '<span class="dir-badge ' + dir + '">' + escapeHtml(s.direction) + "</span>" +
        "<strong>" + escapeHtml(s.symbol) + "</strong>" +
        '<span class="signal-score">Score ' + fmt(s.score) + "</span>" +
        '<span class="status-badge ' + statusClass(s.status) + '">' + escapeHtml(s.status) + "</span>" +
        '<span class="status-badge ' + deliveryClass(s.delivery_status) + '">' + escapeHtml(s.delivery_status) + "</span>" +
      "</div>" +
      '<div><span class="signal-label">Entry</span><br><span class="signal-val">' + fmt(s.entry_low) + " - " + fmt(s.entry_high) + "</span></div>" +
      '<div><span class="signal-label">SL</span><br><span class="signal-val">' + fmt(s.stop_loss) + "</span></div>" +
      '<div><span class="signal-label">TP1 / TP2</span><br><span class="signal-val">' + fmt(s.tp1) + " / " + fmt(s.tp2) + "</span></div>" +
      '<div><span class="signal-label">Created</span><br><span class="signal-val">' + fmtTime(s.created_at) + "</span></div>" +
      '<div><span class="signal-label">Delivery Attempts</span><br><span class="signal-val">' + fmt(s.delivery_attempts) + "</span></div>" +
      err +
      "</div>";
  }).join("");
}

function renderOutcomeCards(summary) {
  if (!summary) return;
  setText("oc-active", String(summary.active || 0));
  setText("oc-tp1", String(summary.tp1_hit || 0));
  setText("oc-tp2", String(summary.tp2_hit || 0));
  setText("oc-stopped", String(summary.stopped || 0));
  setText("oc-expired", String(summary.expired || 0));
}

function renderOutcomes(events) {
  const el = $("outcome-events");
  if (!el) return;
  if (!events || events.length === 0) {
    el.innerHTML = '<div class="na-value" style="padding:8px 0">No outcome events yet</div>';
    return;
  }
  const iconFor = { TP1_HIT: "🎯", TP2_HIT: "🎯", STOPPED: "🛑", EXPIRED: "⏰", INVALIDATED: "⚠" };
  el.innerHTML = events.map(function (e) {
    const sev = e.outcome === "TP2_HIT" ? "success" : (e.outcome === "STOPPED" ? "danger" : (e.outcome === "TP1_HIT" ? "warning" : "neutral"));
    return '<div class="activity-item severity-' + sev + '">' +
      '<span class="activity-icon">' + (iconFor[e.outcome] || "•") + "</span>" +
      '<span class="activity-text"><strong>' + escapeHtml(e.symbol) + "</strong> " + escapeHtml(e.direction) + " → " + escapeHtml(e.outcome) + "</span>" +
      '<span class="activity-time">' + fmtTime(e.time) + "</span>" +
      "</div>";
  }).join("");
}

function renderDelivery(delivery) {
  if (!delivery) return;
  setText("dh-delivered", String(delivery.delivered || 0));
  setText("dh-pending", String(delivery.pending || 0));
  setText("dh-failed", String(delivery.failed || 0));
  setText("dh-retries", String(delivery.retry_attempts || 0));

  const el = $("delivery-errors");
  if (!el) return;
  const errs = delivery.recent_errors || [];
  if (errs.length === 0) {
    el.innerHTML = '<div class="na-value" style="font-size:.72rem">No recent delivery errors</div>';
    return;
  }
  el.innerHTML = errs.map(function (e) {
    return '<div class="error-entry"><span class="error-id">' + escapeHtml(e.signal_id) + "</span>" +
      escapeHtml(e.error || "") + "</div>";
  }).join("");
}

function renderReliability(rel) {
  if (!rel) return;
  setText("or-attempts", rel.outcome_attempts !== undefined ? String(rel.outcome_attempts) : "N/A");
  setText("or-tp1", rel.tp1_attempts !== undefined ? String(rel.tp1_attempts) : "N/A");
  setText("or-tp2", rel.tp2_attempts !== undefined ? String(rel.tp2_attempts) : "N/A");
  setText("or-sl", rel.sl_attempts !== undefined ? String(rel.sl_attempts) : "N/A");
  setText("or-error", rel.last_outcome_error || "N/A");
  setText("or-evaluated", rel.last_evaluated_candle_time || "N/A");
}

function renderStats(stats) {
  if (!stats) return;
  setText("st-total", String(stats.total_signals || 0));
  setText("st-long", String(stats.long_signals || 0));
  setText("st-short", String(stats.short_signals || 0));
  setText("st-active", String(stats.active || 0));
  setText("st-tp1", String(stats.tp1_hit || 0));
  setText("st-tp2", String(stats.tp2_hit || 0));
  setText("st-stopped", String(stats.stopped || 0));
  setText("st-expired", String(stats.expired || 0));
  setText("st-delivered", String(stats.telegram_delivered || 0));
  setText("st-failed", String(stats.telegram_failed || 0));
}

function renderDistribution(dist) {
  const el = $("score-distribution");
  if (!el) return;
  if (!dist || dist.length === 0) {
    el.innerHTML = '<div class="na-value">Not enough signal data yet</div>';
    return;
  }
  const maxCount = Math.max.apply(null, dist.map(function (d) { return d.count; })) || 1;
  el.innerHTML = dist.map(function (d, i) {
    const pct = Math.round((d.count / maxCount) * 100);
    return '<div class="dist-row">' +
      '<span class="dist-label">' + escapeHtml(d.label) + "</span>" +
      '<span class="dist-bar-bg"><span class="dist-bar color-' + i + '" style="width:' + pct + '%"></span></span>' +
      '<span class="dist-count">' + d.count + "</span>" +
      '<span class="dist-pct">' + (d.pct || 0) + "%</span>" +
      "</div>";
  }).join("");
}

function renderActivity(events) {
  const el = $("activity-feed");
  if (!el) return;
  if (!events || events.length === 0) {
    el.innerHTML = '<div class="na-value" style="padding:8px 0">No activity yet</div>';
    return;
  }
  const iconFor = {
    SIGNAL_GENERATED: "🟢",
    TP1_HIT: "🎯",
    TP2_HIT: "🎯",
    STOPPED: "🛑",
    EXPIRED: "⏰",
    DELIVERY_FAILED: "⚠",
    OUTCOME_PENDING: "⏳"
  };
  const iconForShort = function (label) {
    const l = String(label || "").toUpperCase();
    if (l.indexOf("SHORT") >= 0) return "🔴";
    if (l.indexOf("LONG") >= 0) return "🟢";
    return null;
  };
  el.innerHTML = events.map(function (ev) {
    const icon = iconFor[ev.type] || iconForShort(ev.detail) || "•";
    const fallback = ev.time_source === "created_at_fallback" ? " ~" : "";
    return '<div class="activity-item severity-' + escapeHtml(ev.severity || "info") + '">' +
      '<span class="activity-icon">' + icon + "</span>" +
      '<span class="activity-text">' + escapeHtml(ev.label) +
        (ev.detail ? " — <strong>" + escapeHtml(ev.detail) + "</strong>" : "") +
      "</span>" +
      '<span class="activity-time">' + fmtTime(ev.time) + fallback + "</span>" +
      "</div>";
  }).join("");
}

// ── Orchestration ──
async function refresh() {
  try {
    const [statusData, symbolsData, summaryData, activityData] = await Promise.all([
      fetchJson(API.status),
      fetchJson(API.symbols),
      fetchJson(API.summary),
      fetchJson(API.activity)
    ]);

    showError(false);

    renderStatus(statusData);
    renderMatrix(summaryData.symbols || []);
    renderOutcomeCards(summaryData.summary);
    renderDelivery(summaryData.delivery);
    renderReliability(summaryData.reliability);
    renderStats(summaryData.summary);
    renderDistribution(summaryData.distribution);
    renderActivity(activityData.activity);

    // Signal feed + outcomes via dedicated endpoints
    const feed = await fetchJson("/api/signals/latest?limit=20").catch(function () { return { signals: [] }; });
    renderFeed(feed.signals || []);

    const outcomes = await fetchJson("/api/outcomes?limit=20").catch(function () { return { events: [] }; });
    renderOutcomes(outcomes.events || []);

    updateTimestamp();
  } catch (err) {
    showError(true);
    console.error("Refresh failed:", err);
  }
}

function startTicker() {
  setInterval(function () {
    if (lastUpdated === null) return;
    const elapsed = Math.floor((Date.now() - lastUpdated) / 1000);
    const el = $("last-updated");
    if (el) el.textContent = "Updated " + elapsed + "s ago";
  }, 1000);
}

document.addEventListener("DOMContentLoaded", function () {
  refresh();
  setInterval(refresh, REFRESH_MS);
  startTicker();
});
