# 🚨 Long/Short Telegram Signal Bot

> **Crypto signal bot yang tugasnya cuma satu: nyari setup, ngitung level, terus teriak ke Telegram.**
>
> Bukan trader. Bukan cenayang. Bukan mesin ATM.
> **Signal only. No auto trading.** 🫡

[![Python](https://img.shields.io/badge/Python-3.12+-blue?logo=python)](https://www.python.org/)
[![Binance](https://img.shields.io/badge/Data-Binance%20Public-yellow?logo=binance)](https://www.binance.com/)
[![Telegram](https://img.shields.io/badge/Output-Telegram-2CA5E0?logo=telegram)](https://telegram.org/)

---

## 🧠 Jadi bot ini ngapain?

Bot ini mantengin market Binance pakai **public market data**, lalu mencari setup LONG/SHORT berdasarkan kombinasi:

* 📈 EMA 50 / EMA 200
* 💪 ADX 14 + +DI / -DI
* 🧠 RSI 14
* 🌋 ATR 14
* 📊 Volume SMA 20
* 🕐 Multi-timeframe 1H → 15M → 5M
* 🎯 Entry zone
* 🛑 Stop Loss
* 💰 TP1 / TP2
* 🧮 Quality score 0–100

Kalau semua syarat lewat, bot kirim signal ke Telegram.

Kalau nggak lewat?

> **NO SIGNAL. Duduk manis. Jangan maksa market.** 🗿

Bot **tidak memasang order**, tidak melakukan buy/sell, tidak menggunakan leverage, dan tidak membutuhkan Binance trading API key.

---

# 🎯 Signal Logic

## LONG

### 1H bias

* EMA 50 > EMA 200
* Price berada di atas EMA 50

### 15M setup

* EMA 50 > EMA 200
* ADX ≥ 22
* +DI > -DI
* RSI melakukan recovery melewati midline 50

### 5M trigger

* Candle bullish
* Close menembus high candle sebelumnya
* Volume memenuhi threshold

### Filter tambahan

* Price tidak terlalu jauh dari EMA 50
* Risk/reward valid
* Score memenuhi minimum

Kalau semuanya lolos:

> 🟢 **LONG**

---

## SHORT

Kebalikannya:

### 1H bias

* EMA 50 < EMA 200
* Price berada di bawah EMA 50

### 15M setup

* EMA 50 < EMA 200
* ADX ≥ 22
* -DI > +DI
* RSI turun melewati midline 50

### 5M trigger

* Candle bearish
* Close menembus low candle sebelumnya
* Volume memenuhi threshold

Kalau semua lolos:

> 🔴 **SHORT**

---

# 🧮 Quality Score

Score **bukan probabilitas kemenangan**.

Jangan lihat:

> Score 95 = 95% pasti cuan

❌ Nope.

Score adalah ukuran kualitas setup berdasarkan komponen yang terpenuhi.

| Komponen     |   Bobot |
| ------------ | ------: |
| 1H HTF Bias  |      20 |
| EMA Trend    |      15 |
| ADX Strength |      15 |
| DI Direction |      10 |
| RSI Momentum |      15 |
| 5M Trigger   |      10 |
| Volume       |      10 |
| Risk/Reward  |       5 |
| **Total**    | **100** |

Default:

```env
MIN_SCORE=80
```

Jadi bot bukan tipe:

> “RSI nyentuh 49.9, GAS BROOO 🚀”

Bot lebih ke:

> “Tunggu. Checklist dulu.” ☕🗿

---

# 🎯 Entry, SL & TP

Risk engine menghitung level secara dinamis menggunakan ATR.

Default:

```text
ATR       = 14
SL        = 1.5 × ATR
TP1       = 1.5R
TP2       = 2.5R
```

Contoh LONG:

```text
TP2  ─────────────────── 🚀
TP1  ─────────────── 💰

Entry ─────────────── 🎯

SL   ──────────────── 🛑
```

Untuk SHORT arahnya dibalik.

### Catatan audit

Risk engine memang memiliki fungsi untuk mencari swing high/swing low.

Namun pada production signal path saat ini, `SignalEngine` tidak mengirim swing level tersebut ke `calculate_stop_loss()`.

Jadi **SL production saat ini adalah ATR-based**, bukan automatic structure-first SL.

README tidak akan pura-pura bilang fitur itu sudah aktif. 😎

---

# 📡 Telegram

Telegram hanya digunakan sebagai output signal.

Contoh:

```text
🚨 LONG

BTCUSDT
TIMEFRAME: 5M

Entry:
...

SL:
...

TP1:
...

TP2:
...

RR:
...

Score:
.../100

Setup:
...

Confirmation:
• 1H trend
• 15M trend
• ADX / DI
• RSI
• 5M trigger
• Volume

Invalidation:
...

Signal only • No auto trading
```

Token Telegram berasal dari environment variable.

Tidak di-hard-code di source.

---

# 🔐 Security

Bagian ini sengaja dibuat agak paranoid.

Karena bot trading tanpa paranoid itu resep buat headline buruk. 😅

Bot:

* ❌ tidak membuat order
* ❌ tidak buy
* ❌ tidak sell
* ❌ tidak menggunakan futures
* ❌ tidak menggunakan margin
* ❌ tidak menggunakan leverage
* ❌ tidak melakukan withdrawal
* ❌ tidak membutuhkan Binance private trading API key
* ✅ menggunakan public market data
* ✅ Telegram token dari environment
* ✅ deterministic signal ID
* ✅ SQLite persistence
* ✅ trading-execution guard
* ✅ bounded Telegram retry

Trading-execution guard memindai source `app/` dan `backtest/` untuk pola execution tertentu.

Kalau terdeteksi:

> **Bot tidak boleh startup.** 🛑

Intinya:

> Kalau suatu hari ada yang iseng nyelipin fungsi order, bot diharapkan bilang **“NOPE.”**

---

# 🛡️ Anti-noise

Bot memiliki beberapa guard:

* closed-candle validation
* multi-timeframe synchronization
* stale-data protection
* duplicate signal ID
* repeated 5M candle protection
* overextension filter
* volume confirmation
* minimum score
* minimum R:R
* per-symbol isolation
* bounded network retries
* Telegram 429 handling

Bot default scan setiap:

```env
SCAN_INTERVAL_SECONDS=60
```

Tapi signal dievaluasi berdasarkan **closed 5M candle**.

Jadi bot boleh bangun tiap menit.

Candle yang sama tidak boleh diproses berkali-kali.

Kalau log muncul:

```text
REPEATED_CANDLE
```

jangan panik.

Bot bukan mati.

Bot cuma bilang:

> “Candle yang itu udah gue proses, bang.” 🗿

---

# 🧪 Testing

Run:

```bash
pytest -q
```

Coverage:

```bash
pytest --cov=app --cov-report=term-missing
```

Test suite mencakup area seperti:

* signal engine
* indicator logic
* risk calculation
* filters
* no-lookahead
* closed candle
* score boundaries
* deterministic signal ID
* trading guard
* Telegram delivery
* logging
* invalid-data paths

---

# 🚀 Installation

## 1. Clone

```bash
git clone https://github.com/rmdnl/long-short-telegram-signal.git
cd long-short-telegram-signal
```

## 2. Python

Recommended:

```text
Python 3.12+
```

Linux:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
```

Windows:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\activate
```

## 3. Install dependency

```bash
pip install -r requirements.txt
```

## 4. Environment

Linux:

```bash
cp .env.example .env
```

Windows:

```powershell
copy .env.example .env
```

Isi:

```env
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=your_real_token
TELEGRAM_CHAT_ID=your_chat_id

SIGNAL_ONLY=true
```

---

# 🧪 Dry Run

Test engine tanpa mengirim Telegram:

```bash
python -m app.main --dry-run --cycles 1
```

Beberapa cycle:

```bash
python -m app.main --dry-run --cycles 10
```

Run terus:

```bash
python -m app.main
```

---

# 🖥️ VPS / systemd

Contoh service:

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

Enable:

```bash
sudo systemctl daemon-reload
sudo systemctl enable long-short-signal
sudo systemctl start long-short-signal
```

Monitor:

```bash
sudo journalctl -u long-short-signal -f
```

---

# ⚙️ Default Parameters

| Parameter         |   Default |
| ----------------- | --------: |
| EMA Fast          |        50 |
| EMA Slow          |       200 |
| RSI               |        14 |
| RSI Midline       |        50 |
| ADX               |        14 |
| ADX Minimum       |        22 |
| ATR               |        14 |
| SL ATR            |      1.5× |
| TP1               |      1.5R |
| TP2               |      2.5R |
| Volume SMA        |        20 |
| Volume Multiplier |      1.2× |
| Minimum Score     |        80 |
| Max EMA Distance  |     2 ATR |
| Cooldown config   | 3 candles |
| Scan Interval     |    60 sec |

Default symbols:

```text
BTCUSDT
ETHUSDT
BNBUSDT
SOLUSDT
XRPUSDT
DOGEUSDT
ADAUSDT
AVAXUSDT
LINKUSDT
```

---

# 🗂️ Project Structure

```text
.
├── app/
│   ├── main.py
│   ├── scanner.py
│   ├── signal_engine.py
│   ├── strategy.py
│   ├── indicators.py
│   ├── risk_engine.py
│   ├── signal_filter.py
│   ├── market_data.py
│   ├── market_regime.py
│   ├── signal_store.py
│   ├── telegram_bot.py
│   ├── trading_guard.py
│   ├── config.py
│   ├── logger.py
│   └── models.py
│
├── backtest/
├── tests/
├── .env.example
├── .gitignore
└── requirements.txt
```

---

# 📊 Backtest Reality Check

Project ini sudah melalui backtest multi-symbol dan multi-timeframe.

Dan hasilnya tidak disembunyikan.

Baseline historis menunjukkan strategi awal **belum profitable secara robust**.

Eksperimen exit berbasis ATR trail memperbaiki beberapa statistik dibanding baseline, tetapi hasil out-of-sample tetap belum cukup untuk menyebut strategi ini sebagai sistem profit yang terbukti.

Jadi:

> **Backtest bukan surat cinta dari market.**

Market bisa berubah.

Karena itu status project adalah **candidate**, bukan:

```text
100% PROFIT
GUARANTEED
BUY MY COURSE
```

😂

---

# ⚠️ Known Limitations

### 1. Structure-first SL belum aktif di production path

Fungsi swing high/low tersedia di risk engine, tetapi `SignalEngine` saat ini menggunakan ATR-based SL.

### 2. Cooldown function belum menjadi production gate utama

`signal_filter.py` memiliki fungsi cooldown, tetapi scanner production menggunakan repeated-candle protection dan belum menjadikan fungsi cooldown tersebut sebagai gate utama signal generation.

### 3. Opposite-signal helper belum menjadi production gate utama

Helper untuk mendeteksi signal berlawanan tersedia, tetapi belum menjadi filter utama dalam production scan path.

### 4. SignalStore reconstruction belum selesai

`get_signal()` saat ini belum melakukan parsing penuh dari SQLite row kembali menjadi object `Signal`.

### 5. Telegram delivery failure

Jika Telegram gagal setelah bounded retry, signal tetap dapat disimpan secara lokal.

Keuntungannya:

* tidak spam retry
* tidak membuat duplicate loop

Konsekuensinya:

* signal yang belum sampai Telegram dapat dianggap sudah diproses

Ini layak diperbaiki jika reliability delivery menjadi prioritas utama.

### 6. Telegram masih single chat ID

Saat ini desainnya sederhana:

```text
1 bot → 1 configured chat ID
```

### 7. Tidak ada auto trading

Dan ini **bukan bug**.

Memang desainnya begitu. 😎

---

# 🧭 Philosophy

Project ini sengaja tidak memasukkan 47 indikator sampai chart kelihatan seperti dashboard pesawat.

Tidak ada:

* MACD tambahan
* Stochastic
* CCI
* Bollinger
* Ichimoku
* Supertrend
* ML
* sentiment magic
* “AI predicts Bitcoin 99.7%”

Core:

```text
Trend
+
Momentum
+
Strength
+
Volume
+
Trigger
+
Risk
=
Signal
```

Simple bukan berarti asal.

---

# 🤝 Contributing

Kalau mau menambah fitur:

1. Jangan merusak signal-only boundary.
2. Jangan memasukkan trading execution.
3. Tambahkan test.
4. Jangan menggunakan future candle.
5. Jaga deterministic behavior kalau memungkinkan.
6. Dokumentasikan perubahan strategy.
7. Jangan mengklaim profitabilitas tanpa evidence.

Kalau mau bikin auto-trading:

> **Bikin repo lain.** 🧱

Repo ini memang sengaja dipagari supaya signal tetap signal.

---

# ⚠️ Disclaimer

This software is for research, education, and informational purposes only.

Crypto trading involves substantial risk of loss. Historical backtests, signal scores, and technical indicators do not guarantee future results.

The bot does not provide financial advice and does not execute trades.

**You are responsible for every trading decision you make.**

---

# 🗿 Final Boss Summary

```text
Market:
    "Gue mau naik."

Bot:
    "Bukti?"

Market:
    "..."

Bot:
    "NO SIGNAL."
```

Dan honestly...

**itu justru inti bot ini.** 😎📡

---

**Signal only • No auto trading**
