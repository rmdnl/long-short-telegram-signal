/**
 * SIGNAL//ROOM — Long / Short Market Monitor
 *
 * Read-only client. Renders only what the existing read-only APIs provide:
 *   /api/status   — bot activity, telegram/signal-only flags, config summary
 *   /api/summary  — statistics + per-symbol matrix (bias, adx, rsi, score)
 *   /api/symbols  — the configured symbol list (source of truth)
 *   /api/signals  — recent signal feed (entry, sl, tp1, tp2, score, status)
 *
 * No fabricated market data. Missing values render as "--".
 */
const $ = (id) => document.getElementById(id);

const state = {
  refreshMs: 10000,
  symbols: [],
  status: null,
  summary: null,
  signals: [],
  connection: "unknown", // online | offline | unknown
};

/* ------------------------------------------------------------------ */
/* formatting helpers                                                  */
/* ------------------------------------------------------------------ */

function safeNumber(v, fallback = 0) {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

function isNum(v) {
  if (v === null || v === undefined || v === "") return false;
  return Number.isFinite(Number(v));
}

function escapeHtml(value) {
  if (value === null || value === undefined) return "--";
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function duration(sec) {
  if (!isNum(sec)) return "--";
  sec = Math.max(0, Math.floor(Number(sec)));
  const d = Math.floor(sec / 86400); sec %= 86400;
  const h = Math.floor(sec / 3600); sec %= 3600;
  const m = Math.floor(sec / 60); sec %= 60;
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${sec}s`;
  return `${sec}s`;
}

function ago(iso) {
  if (!iso) return "--";
  const t = new Date(iso).getTime();
  if (!Number.isFinite(t)) return "--";
  const diff = Math.max(0, (Date.now() - t) / 1000);
  if (diff < 60) return `${Math.floor(diff)}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function clock(iso) {
  if (!iso) return "--";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "--";
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}

function stamp(iso) {
  if (!iso) return "--";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "--";
  return d.toLocaleString([], { month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}

/** "$109,842.30" — only when a real numeric value exists. */
function priceText(v) {
  if (!isNum(v)) return "--";
  const n = Number(v);
  const digits = n >= 1000 ? 2 : n >= 1 ? 4 : 8;
  return `$${n.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}

/** "+0.42%" — only when a real numeric value exists. */
function pctText(v) {
  if (!isNum(v)) return "--";
  const n = Number(v);
  return `${n > 0 ? "+" : ""}${n.toFixed(2)}%`;
}

function numText(v, digits = 1) {
  if (!isNum(v)) return "--";
  return Number(v).toFixed(digits);
}

function intText(v) {
  if (!isNum(v)) return "--";
  return String(Math.round(Number(v)));
}

function setText(id, value) {
  const el = $(id);
  if (el) el.textContent = value == null ? "--" : value;
}

function setHtml(id, html) {
  const el = $(id);
  if (el) el.innerHTML = html;
}

function setState(el, cls, on) {
  if (!el) return;
  el.classList.remove("ok", "warn", "bad", "info", "neutral", "on", "off", "stale");
  if (on && cls) el.classList.add(cls);
}

function setStatusRow(id, label, cls) {
  const el = $(id);
  if (!el) return;
  el.innerHTML = `<i class="dot"></i><span>${escapeHtml(label)}</span>`;
  setState(el, cls, true);
}

/* ------------------------------------------------------------------ */
/* bot activity                                                        */
/* ------------------------------------------------------------------ */

function activity() {
  return (state.status && state.status.bot_activity) || {};
}

function scannerOnline() {
  // Backend contract: "DATA ACTIVITY" = fresh heartbeat, "ONLINE" = legacy
  // healthy label, "NO RECENT ACTIVITY" = known but quiet, "STALE" = delayed.
  const label = activity().status_label;
  return label === "ONLINE" || label === "DATA ACTIVITY" || (!!activity().latest_activity_time && !activity().stale);
}

function scannerStale() {
  return activity().stale === true;
}

function secondsUntil(iso) {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (!Number.isFinite(t)) return null;
  return Math.max(0, Math.round((t - Date.now()) / 1000));
}

function nextScanHint() {
  const secs = secondsUntil(activity().next_expected);
  if (secs !== null) return `Next scan in ${secs}s`;
  const interval = state.status && state.status.scan_interval_seconds;
  if (isNum(interval) && Number(interval) > 0) return `Next scan in ~${Math.round(Number(interval))}s`;
  return "Next scan time unavailable";
}

/* ------------------------------------------------------------------ */
/* render: status strip + header                                       */
/* ------------------------------------------------------------------ */

function renderStatus(data) {
  state.status = data || {};
  const a = activity();
  // API available + no stale flag + no config error = the monitor layer is up.
  // A fresh bot may have no heartbeat yet; that is "awaiting first scan",
  // not "offline".
  const apiUp = state.status.available === true;
  const online = scannerOnline() || apiUp;
  const stale = scannerStale();
  const healthy = online && !stale;
  const awaiting = online && !a.latest_activity_time;

  // header — system
  const sysPill = $("header-system");
  setState(sysPill, healthy ? "on" : stale ? "stale" : "off", true);
  setText("header-system-label", healthy ? "SYSTEM ONLINE" : stale ? "SYSTEM STALE" : "SYSTEM OFFLINE");

  // header — telegram
  const tgOn = state.status.telegram_enabled === true;
  setState($("header-telegram"), tgOn ? "on" : "off", true);
  setText("header-telegram-label", tgOn ? "TELEGRAM ON" : "TELEGRAM OFF");

  // strip
  setText("strip-system", healthy ? "ONLINE" : stale ? "STALE" : "OFFLINE");
  setState($("strip-system"), healthy ? "ok" : stale ? "warn" : "bad", true);
  setText("strip-system-sub", healthy ? (awaiting ? "awaiting first scan" : "scanner responsive") : stale ? "heartbeat delayed" : "api unavailable");

  setText("strip-scan", isNum(a.scan_cycle) ? `#${intText(a.scan_cycle)}` : "#--");
  setText("strip-scan-sub", isNum(state.status.scan_interval_seconds) ? `${intText(state.status.scan_interval_seconds)}s cadence` : "cycle");

  setText("strip-last", a.latest_activity_time ? ago(a.latest_activity_time) : "--");
  setText("strip-last-sub", a.latest_activity_time ? clock(a.latest_activity_time) : "awaiting first scan");

  setText("strip-next", a.next_expected ? clock(a.next_expected) : "--");
  setText("strip-next-sub", a.next_expected ? nextScanHint() : "unavailable");

  const markets = state.symbols.length || safeNumber(state.status.symbols_count, 0);
  setText("strip-markets", String(markets));
  setText("strip-markets-sub", markets === 1 ? "market monitored" : "markets monitored");

  const total = safeNumber(state.status.summary && state.status.summary.total_signals, 0);
  setText("strip-signals", String(total));
  setText("strip-signals-sub", total === 1 ? "signal recorded" : "signals recorded");
}

function renderSummary(data) {
  state.summary = data || {};
}

/* ------------------------------------------------------------------ */
/* render: market overview                                             */
/* ------------------------------------------------------------------ */

/** /api/symbols may be a list of strings or a list of objects. */
function normalizeSymbols(payload) {
  if (Array.isArray(payload)) return payload;
  if (payload && Array.isArray(payload.symbols)) return payload.symbols;
  if (payload && Array.isArray(payload.items)) return payload.items;
  return [];
}

function normalizeSymbolEntry(item) {
  if (typeof item === "string") {
    return {
      symbol: item.trim(),
      direction: "",
      side: "",
      score: null,
      htfBias: "",
      bias: "",
      status: "WAIT",
      setup: "",
      price: null,
      change: null,
      adx: null,
      rsi: null,
    };
  }
  if (!item || typeof item !== "object") {
    return { symbol: "--", direction: "", side: "", score: null, htfBias: "", bias: "", status: "WAIT", setup: "", price: null, change: null, adx: null, rsi: null };
  }
  const pick = (...keys) => {
    for (const k of keys) {
      if (item[k] !== null && item[k] !== undefined && item[k] !== "") return item[k];
    }
    return null;
  };
  return {
    symbol: String(pick("symbol", "name", "ticker") || "--").trim(),
    direction: String(pick("direction", "side") || "").toUpperCase(),
    side: String(pick("side", "direction") || "").toUpperCase(),
    score: isNum(item.score) ? intText(item.score) : null,
    htfBias: String(pick("htf_bias", "bias") || "WAIT").toUpperCase(),
    bias: String(pick("bias", "htf_bias") || "WAIT").toUpperCase(),
    status: String(pick("status", "setup") || "WAIT").toUpperCase(),
    setup: String(pick("setup", "status") || "").toUpperCase(),
    // Only used when a real price/change is exposed by the API. Never derived.
    price: pick("last_price", "price", "current_price", "close", "mark_price"),
    change: pick("price_change_percent", "change_24h", "changePercent", "percent_change", "change24h"),
    adx: isNum(pick("adx", "adx_value")) ? numText(pick("adx", "adx_value"), 1) : null,
    rsi: isNum(pick("rsi", "rsi_value")) ? numText(pick("rsi", "rsi_value"), 1) : null,
  };
}

function renderSymbols(payload) {
  const entries = normalizeSymbols(payload).map(normalizeSymbolEntry);
  state.symbols = entries.filter((e) => e.symbol && e.symbol !== "--");

  // dynamic subtitle: "{N} markets monitored"
  setText("markets-subtitle", `${state.symbols.length} market${state.symbols.length === 1 ? "" : "s"} monitored`);

  const online = scannerOnline();
  const stale = scannerStale();
  setState($("scanner-chip"), online && !stale ? "on" : stale ? "stale" : "off", true);
  setText("scanner-chip-label", online && !stale ? "Scanner active" : stale ? "Scanner stale" : "Scanner offline");

  const grid = $("markets-grid");
  if (!grid) return;

  if (!state.symbols.length) {
    setHtml("markets-meta", "unavailable");
    grid.innerHTML =
      '<div class="markets-empty"><strong>No markets configured</strong>' +
      "<span>The bot has no symbols in its current configuration. Set <code>SYMBOLS</code> in .env and reload the bot.</span></div>";
    return;
  }

  // enrich from the /api/summary per-symbol matrix when present
  const matrix = state.summary && (state.summary.symbols || state.summary.matrix);
  const bySymbol = {};
  (Array.isArray(matrix) ? matrix : []).forEach((m) => {
    if (m && m.symbol) bySymbol[m.symbol] = m;
  });

  grid.innerHTML = state.symbols
    .map((entry) => {
      const m = bySymbol[entry.symbol] || {};
      const direction = entry.direction || String(m.latest_direction || "").toUpperCase() || "";
      const status = entry.status !== "WAIT" ? entry.status : String(m.status || "WAIT").toUpperCase() || "WAIT";
      const htf = (m.htf_bias || entry.htfBias || "WAIT").toString().toUpperCase();
      const score = m.score != null ? intText(m.score) : entry.score;
      const adx = m.adx != null ? numText(m.adx, 1) : entry.adx;
      const rsi = m.rsi != null ? numText(m.rsi, 1) : entry.rsi;
      const signalTime = m.signal_time ? clock(m.signal_time) : null;

      const badgeText = direction || (status === "SCANNING" ? "SCANNING" : "WAIT");
      const badgeClass = direction === "LONG" ? "long" : direction === "SHORT" ? "short" : status === "SCANNING" ? "scanning" : "wait";

      const price = priceText(entry.price);
      const change = pctText(entry.change);
      const changeClass = isNum(entry.change) ? (Number(entry.change) < 0 ? "down" : "up") : "";

      const biasClass = htf === "BULLISH" ? "bull" : htf === "BEARISH" ? "bear" : "";

      return `<article class="market-card" role="listitem">
        <div class="market-top">
          <span class="market-symbol">${escapeHtml(entry.symbol)}</span>
          <span class="badge ${badgeClass}">${escapeHtml(badgeText)}</span>
        </div>
        <div class="market-price-row">
          <span class="market-price">${escapeHtml(price)}</span>
          <span class="market-change ${changeClass}">${escapeHtml(change)}</span>
        </div>
        <div class="market-meta-row">
          <span class="meta-key">Status</span>
          <span class="meta-val">${escapeHtml(status)}</span>
          ${signalTime ? `<span class="meta-key">Last signal</span><span class="meta-val">${escapeHtml(signalTime)}</span>` : ""}
        </div>
        <div class="market-divider"></div>
        <div class="market-ind">
          <div><span>1H Bias</span><strong class="${biasClass}">${escapeHtml(htf)}</strong></div>
          <div><span>ADX</span><strong>${escapeHtml(adx || "--")}</strong></div>
          <div><span>RSI</span><strong>${escapeHtml(rsi || "--")}</strong></div>
          <div><span>Score</span><strong>${escapeHtml(score || "--")}</strong></div>
        </div>
      </article>`;
    })
    .join("");

  setHtml("markets-meta", `${state.symbols.length} market${state.symbols.length === 1 ? "" : "s"} · live monitor`);
}

/* ------------------------------------------------------------------ */
/* render: live signal activity                                        */
/* ------------------------------------------------------------------ */

function renderActivity() {
  const body = $("activity-body");
  if (!body) return;

  const online = scannerOnline();
  const stale = scannerStale();
  const apiUp = state.status.available === true;
  const awaiting = online && !activity().latest_activity_time;
  const feed = state.signals || [];
  const markets = state.symbols.length;

  setText("activity-sub", feed.length ? `${feed.length} most recent signal${feed.length === 1 ? "" : "s"}` : "Latest qualified setups");

  if (!feed.length) {
    let tone, statusCls, statusText;
    if (apiUp && awaiting) {
      tone = "Scanner is running and monitoring the market.";
      statusCls = "on";
      statusText = "SCANNER ACTIVE (awaiting first scan)";
    } else if (online && !stale) {
      tone = "Scanner is running and monitoring the market.";
      statusCls = "on";
      statusText = "● SCANNER ACTIVE";
    } else if (stale) {
      tone = "Scanner heartbeat is stale — check bot process.";
      statusCls = "warn";
      statusText = "SCANNER STALE";
    } else if (apiUp) {
      tone = "API is up but no scanner heartbeat yet.";
      statusCls = "warn";
      statusText = "SCANNER STARTING";
    } else {
      tone = "Cannot reach dashboard API.";
      statusCls = "off";
      statusText = "SCANNER OFFLINE";
    }
    body.innerHTML = `<div class="activity-empty">
      <span class="activity-status ${online && !stale ? "on" : stale ? "warn" : "off"}">● SCANNER ${online && !stale ? "ACTIVE" : stale ? "STALE" : "OFFLINE"}</span>
      <h3>No qualified setup</h3>
      <p>${escapeHtml(tone)} ${markets} market${markets === 1 ? "" : "s"} under watch.</p>
      <div class="activity-next">${escapeHtml(nextScanHint())}</div>
    </div>`;
    return;
  }

  const cards = feed.slice(0, 4).map((s) => {
    const dir = String(s.direction || s.side || "").toUpperCase();
    const cls = dir === "LONG" ? "long" : dir === "SHORT" ? "short" : "";
    const entry = s.entry_low && s.entry_high ? `${s.entry_low} – ${s.entry_high}` : s.entry_low || "--";
    return `<article class="activity-card ${cls}">
      <div class="activity-card-top">
        <span class="activity-title"><b>${escapeHtml(s.symbol || "--")}</b><i class="badge ${cls || "wait"}">${escapeHtml(dir || "SIGNAL")}</i></span>
        <time>${escapeHtml(clock(s.created_at))}</time>
      </div>
      <div class="activity-grid">
        <div><span>Entry</span><strong>${escapeHtml(entry)}</strong></div>
        <div><span>SL</span><strong>${escapeHtml(s.stop_loss || "--")}</strong></div>
        <div><span>TP1</span><strong>${escapeHtml(s.tp1 || "--")}</strong></div>
        <div><span>TP2</span><strong>${escapeHtml(s.tp2 || "--")}</strong></div>
      </div>
      <div class="activity-foot">
        <span>Score <b>${escapeHtml(intText(s.score))}</b></span>
        <span>Status <b>${escapeHtml(s.status || "--")}</b></span>
        <span>Telegram <b>${escapeHtml(s.delivery_status || "--")}</b></span>
      </div>
    </article>`;
  });

  body.innerHTML = cards.join("") + `<div class="activity-next">${escapeHtml(nextScanHint())}</div>`;
}

/* ------------------------------------------------------------------ */
/* render: system status panel                                         */
/* ------------------------------------------------------------------ */

function renderSystemStatus() {
  const s = state.status || {};
  const a = activity();
  const apiUp = s.available === true;
  const online = scannerOnline() || apiUp;
  const stale = scannerStale();
  const awaiting = online && !a.latest_activity_time;
  const dbOk = apiUp;

  setStatusRow("sys-scanner", online ? (stale ? "STALE" : awaiting ? "STARTING" : "ONLINE") : "OFFLINE", online && !stale ? "ok" : stale ? "warn" : "bad");
  setStatusRow("sys-database", dbOk ? "CONNECTED" : "UNAVAILABLE", dbOk ? "ok" : "bad");
  setStatusRow("sys-telegram", s.telegram_enabled === true ? "ENABLED" : "DISABLED", s.telegram_enabled === true ? "info" : "neutral");

  const mode = $("sys-mode");
  if (mode) {
    const signalOnly = s.signal_only !== false;
    mode.innerHTML = `<i class="dot"></i><span>${signalOnly ? "SIGNAL ONLY" : "AUTOMATED"}</span>`;
    setState(mode, signalOnly ? "info" : "warn", true);
  }

  setText("sys-minscore", isNum(s.min_score) ? String(Math.round(Number(s.min_score))) : "--");

  const stats = (s.summary && s.summary.total_signals != null)
    ? s.summary
    : (state.summary && state.summary.summary) || {};
  setText("sys-signals", intText(stats.total_signals));
  setText("sys-signals-sub", `${intText(stats.active)} active · ${intText(stats.expired)} expired`);

  setText("sys-uptime", duration(a.uptime_seconds));

  const parts = [];
  if (apiUp && awaiting) parts.push("Scanner is up; awaiting first scan cycle.");
  else if (online && !stale) parts.push("Scanner heartbeat received.");
  else if (stale) parts.push("Scanner heartbeat is stale — check bot process.");
  else parts.push("No scanner heartbeat recorded.");
  if (isNum(a.scan_cycle)) parts.push(`Scan cycle #${intText(a.scan_cycle)}.`);
  if (a.next_expected) parts.push(nextScanHint() + ".");
  if (!dbOk) parts.push("Database unavailable — showing configuration only.");
  if (s.config_error) parts.push(`Configuration error: ${s.config_error}`);
  setText("sys-note", parts.join(" "));
}

/* ------------------------------------------------------------------ */
/* render: recent signals                                              */
/* ------------------------------------------------------------------ */

function normalizeSignals(payload) {
  if (Array.isArray(payload)) return payload;
  if (payload && Array.isArray(payload.signals)) return payload.signals;
  if (payload && Array.isArray(payload.items)) return payload.items;
  return [];
}

function renderSignals(payload) {
  const raw = normalizeSignals(payload);
  const feed = raw.filter((s) => s && typeof s === "object");

  const tbody = $("signals-body");
  const empty = $("signals-empty");
  const count = $("recent-count");
  if (count) count.textContent = `${feed.length} record${feed.length === 1 ? "" : "s"}`;

  if (!tbody) return;

  if (!feed.length) {
    tbody.innerHTML = "";
    if (empty) empty.hidden = false;
    return;
  }

  if (empty) empty.hidden = true;

  tbody.innerHTML = feed
    .slice(0, 25)
    .map((s) => {
      const dir = String(s.direction || s.side || "").toUpperCase();
      const cls = dir === "LONG" ? "long" : dir === "SHORT" ? "short" : "";
      const entry = s.entry_low && s.entry_high ? `${s.entry_low}–${s.entry_high}` : s.entry_low || "--";
      return `<tr>
        <td data-label="Time">${escapeHtml(stamp(s.created_at))}</td>
        <td data-label="Symbol" class="cell-symbol">${escapeHtml(s.symbol || "--")}</td>
        <td data-label="Side"><span class="side-tag ${cls}">${escapeHtml(dir || "--")}</span></td>
        <td data-label="Entry">${escapeHtml(entry)}</td>
        <td data-label="SL">${escapeHtml(s.stop_loss || "--")}</td>
        <td data-label="TP1">${escapeHtml(s.tp1 || "--")}</td>
        <td data-label="TP2">${escapeHtml(s.tp2 || "--")}</td>
        <td data-label="Score">${escapeHtml(intText(s.score))}</td>
        <td data-label="Status">${escapeHtml(s.status || "--")}</td>
      </tr>`;
    })
    .join("");
}

/* ------------------------------------------------------------------ */
/* data loop                                                           */
/* ------------------------------------------------------------------ */

async function getJSON(path) {
  const res = await fetch(path, { cache: "no-store" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

function renderConnectionError() {
  state.connection = "offline";
  const sysPill = $("header-system");
  setState(sysPill, "off", true);
  setText("header-system-label", "API UNAVAILABLE");
  setStatusRow("sys-scanner", "UNAVAILABLE", "bad");
  setStatusRow("sys-database", "UNAVAILABLE", "bad");
  setText("sys-note", "The dashboard cannot reach the API. Retrying automatically.");
}

async function refresh() {
  try {
    const [status, summary, symbols, signals] = await Promise.all([
      getJSON("/api/status"),
      getJSON("/api/summary"),
      getJSON("/api/symbols"),
      getJSON("/api/signals?limit=50"),
    ]);
    state.connection = "online";
    state.signals = normalizeSignals(signals);
    renderStatus(status);
    renderSummary(summary);
    renderSymbols(symbols);
    renderSignals(signals);
    renderActivity();
    renderSystemStatus();
  } catch (err) {
    // Never surface JS errors in the UI.
    console.warn("dashboard refresh failed", err);
    renderConnectionError();
  }
}

function tick() {
  const a = activity();
  if (a.latest_activity_time) setText("strip-last", ago(a.latest_activity_time));
  if (a.next_expected) {
    setText("strip-next", clock(a.next_expected));
    setText("strip-next-sub", nextScanHint());
  }
  if (state.connection === "online") {
    const note = $("activity-next");
    if (note && $("activity-body").children.length === 1) note.textContent = nextScanHint();
  }
}

refresh();
setInterval(refresh, state.refreshMs);
setInterval(tick, 1000);
