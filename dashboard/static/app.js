const $ = (id) => document.getElementById(id);

const state = {
  refreshMs: 10000,
  symbols: [],
  status: null,
  summary: null,
  signals: [],
};

function safeNumber(v, fallback = 0) {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

function duration(sec) {
  if (sec == null || !Number.isFinite(Number(sec))) return "--";
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
  const diff = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (diff < 60) return `${Math.floor(diff)}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  return `${Math.floor(diff / 3600)}h ago`;
}

function clock(iso) {
  if (!iso) return "--";
  const d = new Date(iso);
  return d.toLocaleTimeString([], {hour:"2-digit", minute:"2-digit", second:"2-digit", hour12:false});
}

function setText(id, value) {
  const el = $(id);
  if (el) el.textContent = value;
}

function renderStatus(data) {
  state.status = data;
  const a = data?.bot_activity || {};
  const online = a.status_label === "ONLINE";

  setText("bot-status", online ? "ONLINE" : (a.status_label || "OFFLINE"));
  setText("hero-status", online ? "ONLINE" : (a.status_label || "OFFLINE"));
  setText("bot-cycle", a.scan_cycle != null ? `CYCLE ${a.scan_cycle}` : "CYCLE --");
  setText("uptime", duration(a.uptime_seconds));
  setText("last-scan", ago(a.latest_activity_time));
  setText("last-scan-meta", clock(a.latest_activity_time));
  setText("next-scan", a.next_expected ? clock(a.next_expected) : "--");
  setText("telegram", data.telegram_enabled ? "ENABLED" : "OFF");
  setText("database", data.available ? "CONNECTED" : "OFFLINE");
  setText("min-score", data.min_score ?? 75);

  const dot = document.querySelector(".status-dot");
  const conn = $("connection-dot");
  if (dot) dot.classList.toggle("off", !online);
  if (conn) conn.classList.toggle("off", !online);
  setText("connection-label", online ? "LIVE" : "STALE");
}

function renderSummary(data) {
  state.summary = data || {};
  const delivered = safeNumber(data?.telegram_delivered);
  const failed = safeNumber(data?.telegram_failed);
  const total = delivered + failed;
  setText("delivered", delivered);
  setText("failed", failed);
  const pct = total ? (delivered / total) * 100 : 0;
  const bar = $("delivery-bar");
  if (bar) bar.style.width = `${pct}%`;

  setText("tp1", data?.tp1_hit ?? 0);
  setText("tp2", data?.tp2_hit ?? 0);
  setText("stopped", data?.stopped ?? 0);
  setText("expired", data?.expired ?? 0);
}

function normalizeSymbols(payload) {
  if (Array.isArray(payload)) return payload;
  if (Array.isArray(payload?.symbols)) return payload.symbols;
  return [];
}

function renderSymbols(payload) {
  const symbols = normalizeSymbols(payload);
  state.symbols = symbols;
  const grid = $("symbol-grid");
  if (!grid) return;
  setText("symbol-count", `${symbols.length} symbols`);

  if (!symbols.length) {
    grid.innerHTML = `<div class="empty-state"><div class="empty-icon">⌁</div><strong>No symbol data</strong><span>Waiting for the scanner.</span></div>`;
    return;
  }

  grid.innerHTML = symbols.map(s => {
    const symbol = s.symbol || s.name || "--";
    const dir = String(s.direction || s.side || "").toUpperCase();
    const score = s.score == null ? null : Number(s.score);
    const scoreText = Number.isFinite(score) ? Math.round(score) : "—";
    const bias = s.htf_bias || s.bias || "—";
    const status = s.status || s.setup || "WAIT";
    const bars = Math.max(0, Math.min(5, Math.round((safeNumber(score) / 100) * 5)));
    return `
      <div class="symbol-card">
        <div class="symbol-top">
          <span class="symbol">${symbol}</span>
          <span class="direction ${dir === "LONG" ? "long" : dir === "SHORT" ? "short" : ""}">${dir || "WAIT"}</span>
        </div>
        <div class="symbol-score ${Number.isFinite(score) ? "good" : "na"}">${scoreText}</div>
        <div class="symbol-sub">${bias} · ${status}</div>
        <div class="symbol-bars">${[0,1,2,3,4].map(i => `<span class="${i < bars ? "on" : ""}"></span>`).join("")}</div>
      </div>`;
  }).join("");
}

function normalizeSignals(payload) {
  if (Array.isArray(payload)) return payload;
  if (Array.isArray(payload?.signals)) return payload.signals;
  if (Array.isArray(payload?.items)) return payload.items;
  return [];
}

function renderSignals(payload) {
  const signals = normalizeSignals(payload);
  state.signals = signals;
  const feed = $("signal-feed");
  if (!feed) return;

  if (!signals.length) {
    feed.innerHTML = `<div class="empty-state"><div class="empty-icon">⌁</div><strong>No signals yet</strong><span>The scanner is running. Waiting for a setup to qualify.</span></div>`;
    return;
  }

  feed.innerHTML = signals.slice(0, 20).map(s => {
    const dir = String(s.direction || s.side || "").toUpperCase();
    const symbol = s.symbol || "--";
    const score = s.score == null ? "—" : Math.round(Number(s.score));
    const time = s.created_at || s.signal_time || s.timestamp || s.time;
    const setup = s.setup || s.status || "SIGNAL";
    const rr = s.rr != null ? `RR ${s.rr}` : "";
    return `
      <div class="signal-row">
        <div>
          <div class="signal-symbol">${symbol}</div>
          <div class="signal-time">${clock(time)}</div>
        </div>
        <div>
          <span class="signal-dir ${dir === "LONG" ? "long" : dir === "SHORT" ? "short" : ""}">${dir || "SIGNAL"}</span>
        </div>
        <div class="signal-meta">${setup} ${rr}</div>
        <div class="signal-score">${score}</div>
      </div>`;
  }).join("");
}

async function getJSON(path) {
  const r = await fetch(path, {cache:"no-store"});
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return r.json();
}

async function refresh() {
  try {
    const [status, summary, symbols, signals] = await Promise.all([
      getJSON("/api/status"),
      getJSON("/api/summary"),
      getJSON("/api/symbols"),
      getJSON("/api/signals"),
    ]);
    renderStatus(status);
    renderSummary(summary);
    renderSymbols(symbols);
    renderSignals(signals);
  } catch (err) {
    console.error(err);
    setText("connection-label", "RETRY");
    const dot = $("connection-dot");
    if (dot) dot.classList.add("off");
  }
}

function tickClock() {
  const d = new Date();
  setText("clock", d.toLocaleTimeString([], {hour:"2-digit", minute:"2-digit", second:"2-digit", hour12:false}));
  if (state.status) {
    const a = state.status.bot_activity || {};
    setText("last-scan", ago(a.latest_activity_time));
  }
}

setInterval(tickClock, 1000);
setInterval(refresh, state.refreshMs);
tickClock();
refresh();
