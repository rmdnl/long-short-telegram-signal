# 🚨 Long/Short Telegram Signal Bot

> **This bot does not buy your bags.**
> It just screams LONG or SHORT into Telegram and lets you decide whether you want to do something about it. 🫡

```text
Market:  "Bro,pls pump."
Bot:     "Based on what."
Market:  " vibes "
Bot:     "NO SIGNAL." 🗿
```

[![Python](https://img.shields.io/badge/Python-3.12+-blue?logo=python)](https://www.python.org/)
[![Binance](https://img.shields.io/badge/Data-Binance%20Public-yellow?logo=binance)](https://www.binance.com/)
[![Telegram](https://img.shields.io/badge/Output-Telegram-2CA5E0?logo=telegram)](https://telegram.org/)

---

## TL;DR

Signal-only. It reads public Binance klines, runs a 1H → 15M → 5M checklist, and posts a fully-formatted setup to Telegram if the checklist passes. It never places an order, has no Binance private API key, and has a guard that physically refuses to boot if someone adds one.

The parts that used to be *documented-but-not-wired* are now actually in the production path — see [What changed](#-what-changed-honestly).

---

## 🧠 Architecture

```text
 Binance public REST klines
            │
            ▼
   market_data.py          fetch + parse (rate-limit aware)
            │
            ▼
   data_validation.py      closed-candle cutoff, MTF sync, stale-data
            │
            ▼
   indicators.py           EMA/ADX/DI/RSI/ATR/VolSMA via pandas
            │
            ▼
   strategy.py             bias + setup + trigger predicates
            │
            ▼
   signal_engine.py        orchestration, scoring, risk/reward
            │
            ├── risk_engine.py        entry zone, structure-first SL, TP
            └── signal_filter.py      cooldown, dedup, staleness
            │
            ▼
   scanner.py              per-symbol gate + isolation
            │
            ▼
   signal_store.py         SQLite (source of truth)
            │
            ▼
   telegram_bot.py         HTTP POST (requests) → your chat
```

Everything is deterministic given the same candles. No wall-clock in signal decisions, no network in the backtest.

---

## 🔀 The 1H → 15M → 5M Flow

| TF   | Role       | What it decides                                        |
| ---- | ---------- | ------------------------------------------------------ |
| 1H   | **Bias**   | Which direction is even allowed (LONG / SHORT / NEUTRAL) |
| 15M  | **Setup**  | Whether a pullback is worth taking (trend, ADX, DI, RSI) |
| 5M   | **Trigger**| The actual closed-candle confirmation + entry + volume  |

**1H is the boss.** If 1H says NEUTRAL, the engine returns `None` immediately. No amount of 5M excitement overrides a flat higher-timeframe.

**5M is the trigger reference.** Signals are only ever evaluated on a *closed* 5M candle. The forming candle is never read — see [No-lookahead](#-no-lookahead-i-mean-it).

### LONG

| Step | Condition                                                        |
| ---- | ---------------------------------------------------------------- |
| 1H   | EMA50 > EMA200, price above EMA50                                |
| 15M  | EMA50 > EMA200, ADX ≥ 22, +DI > -DI, RSI crossing back over 50   |
| 5M   | Bullish close breaking prior candle high, volume ≥ 1.2× SMA      |
| Extra| Not overextended from EMA50, score ≥ 80, R:R valid                |

### SHORT

Exact mirror: EMA50 < EMA200, -DI > +DI, RSI crossing down through 50, bearish 5M close breaking the prior low.

---

## 📐 Indicators

| Indicator | Length | Role                                  |
| --------- | -----: | ------------------------------------- |
| EMA       |     50 | Trend + overextension anchor           |
| EMA       |    200 | Macro trend filter                     |
| ADX       |     14 | Trend *strength* gate (≥ 22)           |
| +DI/-DI   |     14 | Direction confirmation                 |
| RSI       |     14 | Pullback exhaustion vs midline (50)    |
| ATR       |     14 | Volatility-scaled risk geometry       |
| Volume SMA|     20 | Participation confirmation (1.2×)     |

That is the whole list. No MACD. No Bollinger. No Ichimoku. No "AI predicts 99.7%". The chart stays readable. 🧘

---

## 🧮 Scoring

**Score is NOT a win probability.** It is a setup-quality measure. Please stop treating the number as a percentage. It isn't.

| Component     | Weight |
| ------------- | -----: |
| 1H HTF bias   |     20 |
| EMA trend     |     15 |
| ADX strength  |     15 |
| DI direction  |     10 |
| RSI momentum  |     15 |
| 5M trigger    |     10 |
| Volume        |     10 |
| Risk/reward   |      5 |
| **Total**     | **100** |

Default gate: `MIN_SCORE=80`. Signals below the gate are dropped and logged with a reason, not silently discarded.

---

## 🎯 Risk Management: Entry, SL, TP

**Stop loss is structure-first, ATR-fallback.** This is the real order of operations in `risk_engine.calculate_stop_loss()` as wired in `signal_engine.generate_signal()`:

```text
1. Structure
     LONG  → recent 15M swing LOW   (must sit below the entry zone)
     SHORT → recent 15M swing HIGH  (must sit above the entry zone)

2. ATR fallback
     LONG  → entry_mid − (ATR × 1.5)
     SHORT → entry_mid + (ATR × 1.5)
```

A swing level is **rejected** (falling back to ATR) if it sits on the wrong side of the entry or would produce a near-zero risk. Structure must actually protect the trade to be used.

All swing detection reads the **closed 15M setup candles only** — the same series the rest of the engine uses, already cut off by `data_validation`. No future candle is involved.

```text
LONG:

TP2  ─────────────────── 🚀   entry_mid + 2.5R
TP1  ─────────────── 💰      entry_mid + 1.5R
Entry ─────────────── 🎯     entry_mid
SL   ──────────────── 🛑     swing low, else entry_mid − 1.5×ATR
```

TP levels are derived *from* the stop, so the R:R ratios hold regardless of which SL path was taken.

---

## 🛡️ Protections (all active in production)

### Closed-candle only

`data_validation.cutoff_candles()` drops any candle whose close time is after the decision instant. Indicators, ATR, volume, DI — all computed on the post-cutoff series. A signal can never be triggered by a bar that is still forming.

### Cooldown (`COOLDOWN_CANDLES=3`)

A real production gate, evaluated per symbol before signal generation, using the **same helper** the backtest and the test suite use:

```text
allowed  ⟺  (now − last_signal_trigger_candle_open) ≥ 3 × 5m
```

Measured in closed 5M trigger candles on a shared time base, so it is exact and restart-deterministic. `cooldown=0` disables it (never blocks forever).

### Opposite-signal protection

While a symbol is inside its cooldown window, a signal in the *opposite* direction is blocked with a distinct reason code (`OPPOSITE_SIGNAL_BLOCKED` vs `COOLDOWN_ACTIVE`) so you can tell them apart in the logs. Outside the cooldown, a genuine bias flip is allowed — flipping direction is legitimate when the higher timeframe flips.

### Duplicate suppression

Signal IDs are deterministic:

```text
SYMBOL|TIMEFRAME|CANDLE_OPEN_TIME|DIRECTION
```

The same candle + direction can never produce a different ID, which is what makes dedup and restart-safety possible. Three layers: in-memory emitted-ID set → per-symbol processed-candle memory → SQLite.

### Stale data, overextension, volume, min-score

All live. All logged with a reason code rather than a shrug.

---

## 📡 Telegram

Output only, via direct HTTPS `POST` to the Bot API using `requests`. No `python-telegram-bot` — the whole integration is one endpoint and a formatted string, so the dependency was removed.

Message contains direction, symbol, timeframe, entry zone, SL, TP1, TP2, R:R, score, confirmation checklist, and invalidation, ending with `Signal only • No auto trading`.

Retry handling inside `telegram_bot.py`:

| Condition        | Behaviour                                    |
| ---------------- | -------------------------------------------- |
| HTTP 200         | success                                      |
| HTTP 429         | backoff + retry (bounded)                    |
| HTTP 5xx / timeout | backoff + retry (bounded)                |
| Other 4xx        | immediate failure (retrying is pointless)   |
| `TELEGRAM_ENABLED=false` | no network call at all, logs instead |

The token is read from the environment and is never logged or hard-coded.

---

## 💾 Persistence & Restart Safety

SQLite is the source of truth. Two **independent** state axes are stored per signal:

| Column              | Axis           | Values                                  |
| ------------------- | -------------- | --------------------------------------- |
| `status`            | Trade lifecycle| `ACTIVE`, `TP1_HIT`, `STOPPED`, ...     |
| `delivery_status`   | Message delivery| `PENDING`, `DELIVERED`, `FAILED`       |

They are deliberately separate. A signal can be a perfectly valid ACTIVE setup whose Telegram message has not arrived yet — collapsing the two would let a failed send masquerade as delivered.

**The core fix:** duplicate suppression keys on `delivery_status == DELIVERED`, not on row existence.

```text
start → scan → signal → Telegram fail → process dies
                                           ↓
                                     next start
                                           ↓
                        undelivered signal is retried, NOT
                        treated as processed ✅
```

- A failed send is recorded (`delivery_attempts += 1`) and stays non-`DELIVERED`.
- On startup, `_retry_pending_deliveries()` retries every undelivered signal.
- Retries are **bounded** by `DELIVERY_MAX_ATTEMPTS = 3` — no infinite retry, no spam.
- In-memory scanner state is cleared for that symbol so the identical deterministic signal can be regenerated, but **only for that symbol** (other symbols' cooldown and dedup state are untouched).
- A delivered signal is never sent again, across any number of restarts.

`SignalStore.get_signal()` fully reconstructs a `Signal` from a row: LONG and SHORT, exact `Decimal` precision, timezone-aware timestamps preserved, deterministic ID unchanged.

---

## 🔐 Security Boundary

Deliberately paranoid, because unhedged trading bots make bad headlines.

| The bot does NOT            | The bot DOES                          |
| -------------------------- | ------------------------------------- |
| ❌ place orders             | ✅ read public market data            |
| ❌ buy or sell              | ✅ take the token from the environment|
| ❌ use futures              | ✅ generate deterministic IDs         |
| ❌ use margin               | ✅ persist to SQLite                  |
| ❌ use leverage             | ✅ run a boot-time execution guard     |
| ❌ withdraw anything        | ✅ bound every retry                  |
| ❌ need a Binance private API key | ✅ never touch a trading endpoint |

`trading_guard.py` scans `app/` and `backtest/` for execution patterns at startup. If it trips, **the bot refuses to start** and exits non-zero.

```text
clean = true
violations = 0
```

---

## 🧪 Testing

```bash
pytest -q
```

```text
384 passed
```

Coverage:

```bash
pytest --cov=app --cov-report=term-missing
```

Suites cover the signal engine, indicators, risk engine, filters, no-lookahead and closed-candle enforcement, score boundaries, deterministic IDs, the trading guard, Telegram delivery (including 429/5xx/timeout), logging propagation, SQLite round-trip, restart/dedup, and per-symbol isolation.

---

## 🚀 Installation

```bash
git clone https://github.com/rmdnl/long-short-telegram-signal.git
cd long-short-telegram-signal
python3.12 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

```env
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=your_real_token
TELEGRAM_CHAT_ID=your_chat_id
SIGNAL_ONLY=true
DRY_RUN=false
```

---

## 🧪 Dry Run

```bash
python -m app.main --dry-run --cycles 1     # one cycle, no Telegram
python -m app.main --dry-run --cycles 10    # ten cycles
python -m app.main                          # run until Ctrl+C
```

---

## 🖥️ VPS / systemd

```ini
[Unit]
Description=Long Short Telegram Signal Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/long-short-telegram-signal
Environment="PYTHONUNBUFFERED=1"
ExecStart=/home/ubuntu/long-short-telegram-signal/.venv/bin/python -m app.main
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now long-short-signal
sudo journalctl -u long-short-signal -f
```

Application logs propagate to `journalctl`, to the log file, and to stdout. Handlers are installed once — repeated initialization does not duplicate output.

---

## ⚙️ Parameters

| Parameter                  |   Default |
| -------------------------- | -------: |
| EMA Fast / Slow            |  50 / 200 |
| RSI / Midline              |  14 / 50  |
| ADX / ADX Minimum          |  14 / 22  |
| ATR                        |      14  |
| SL ATR (fallback)          |     1.5× |
| TP1 / TP2                  | 1.5R / 2.5R |
| Volume SMA / Multiplier    |  20 / 1.2× |
| Minimum Score              |      80  |
| Max EMA Distance           |  2 ATR   |
| Cooldown                   |  3 candles |
| Max Data Age               |  30 sec  |
| Scan Interval              |   60 sec  |
| Delivery Max Attempts      |      3   |

Default symbols: `BTCUSDT, ETHUSDT, BNBUSDT, SOLUSDT, XRPUSDT, DOGEUSDT, ADAUSDT, AVAXUSDT, LINKUSDT`

---

## 🗂️ Project Structure

```text
app/
  main.py           entry point, delivery orchestration, bounded retry
  scanner.py        per-symbol gate, isolation, reason codes
  signal_engine.py  orchestration + scoring + structure-first SL
  strategy.py       bias / setup / trigger predicates
  indicators.py     pandas indicators
  risk_engine.py    entry zone, swing detection, SL, TP
  signal_filter.py  cooldown, dedup, staleness, closed-candle
  market_data.py    Binance public klines
  market_regime.py  regime classification
  data_validation.py closed-candle cutoff + MTF sync
  signal_store.py   SQLite persistence + delivery state
  telegram_bot.py   HTTP delivery with bounded retry
  trading_guard.py  boot-time execution-pattern guard
  config.py         env-driven config with validation
  logger.py         handler-safe logging setup
  models.py         Candle / Signal / enums
backtest/           closed-only walk-forward harness
tests/              384 tests
```

---

## 📊 Backtest Reality

Being straight with you, because inflated numbers are how signal bots become urban legends.

**Research/backtest results and live signal generation are different things.** The backtest harness is a research tool: it drives the live engine over historical closed-candle windows with no wall-clock and no network. Passing it is a *sanity check on the code*, not evidence of profitability.

The honest state: the historical baseline is **not robustly profitable**, and out-of-sample results are not strong enough to call this a proven edge. It is a candidate, not:

```text
100% PROFIT
GUARANTEED
BUY MY COURSE
```

```text
backtest / research results   ≠   live signal generation
```

A backtest win does not mean a live win. A backtest *loss* does not mean the signals are useless — it means the edge is unproven. Nobody knows which until the market weighs in. 🎲

---

## ⚠️ Known Limitations

1. **No proven edge.** The strategy is not demonstrated profitable. Treat output as research, not a recommendation.
2. **Backtest ≠ live.** Fill assumptions, latency, slippage, and fee dynamics are only partly modelled. Live results will differ.
3. **Single chat ID.** `1 bot → 1 chat`. No multi-user support, no topics, no threads.
4. **Cooldown is restored from SQLite on restart.** The cooldown gate runs in-memory per symbol, but `main()` calls `scanner.restore_cooldown_state()` at startup from `SignalStore.get_last_delivered_by_symbol()` so a restart cannot be used to bypass the cooldown window. The `delivery_status` axis is authoritative for "never send this twice"; cooldown timing is a separate liveness guard.
5. **No live order management.** No position tracking, no partial fills, no trailing logic outside the backtest simulator. This is by design.
6. **Public endpoints only.** Rate limits and exchange-side outages will show up as `MARKET_DATA_ERROR` or `RATE_LIMITED` rejections rather than trades.
7. **No auto trading.** And this is not a bug. It is the point. 😎

---

## 🧭 Philosophy

Fewer indicators, stricter gates, honest documentation.

```text
Trend + Momentum + Strength + Volume + Trigger + Risk = Signal
```

A bot that says "no" 95% of the time and is right about 100% of those is worth more than one that says "yes" constantly.

---

## 🤝 Contributing

1. Do not break the signal-only boundary.
2. Do not add trading execution. Do not add a private API key.
3. Do not use future candles. Ever.
4. Add a regression test with every fix.
5. Keep decisions deterministic where possible.
6. Document any strategy change.
7. Do not claim profitability without evidence.

If you want to build the auto-trading version: **use a different repo.** 🧱 This one is fenced on purpose.

---

## ⚠️ Disclaimer

This software is for research, education, and informational purposes only.

Crypto trading involves substantial risk of loss. Historical backtests, signal scores, and technical indicators do not guarantee future results.

The bot does not provide financial advice and does not execute trades.

**You are responsible for every trading decision you make.**

---

## 🗿 Final Boss Summary

```text
Market:  "I feel bullish."
Bot:     "ADX?"
Market:  "What's ADX."
Bot:     "…"
Market:  "..."
Bot:     "NO SIGNAL."
```

And honestly? That restraint is the whole product. 📡

---

**Signal only • No auto trading**
