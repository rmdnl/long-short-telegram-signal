# 📡 Long Short Telegram Signal Bot

**Bot sinyal LONG / SHORT crypto berbasis Python + Binance Public Market Data + Telegram.**

Bukan bot auto-trading.
Bukan bot yang tiba-tiba jual BTC karena habis mimpi buruk.
Bukan juga mesin pencetak uang. 😂

Bot ini tugasnya cuma satu:

> **Cari setup trading yang memenuhi rule → kirim sinyal ke Telegram → manusia yang ambil keputusan.**

**Signal only. No auto trading. Period.**

---

## 🧠 TL;DR

Bot membaca market Binance secara real-time menggunakan **public market data**, kemudian melakukan analisis multi-timeframe:

```text
1H  →  Bias / arah trend besar
 ↓
15M →  Setup
 ↓
5M  →  Trigger
 ↓
Score ≥ 80
 ↓
📲 Telegram Signal
```

Bot **tidak mempunyai fitur untuk melakukan order**.

Tidak ada:

* ❌ BUY otomatis
* ❌ SELL otomatis
* ❌ Futures
* ❌ Margin
* ❌ Leverage
* ❌ Trading API key
* ❌ Auto execution

Jadi kalau sinyal bilang LONG lalu market malah jatuh...

ya bot cuma bisa bilang:

> "Bang, gue cuma ngasih sinyal. 😭"

---

# 🏗️ Architecture

Secara sederhana:

```text
             Binance Public API
                     │
                     ▼
              Market Data Fetch
                     │
                     ▼
             Multi-Timeframe Data
                     │
          ┌──────────┼──────────┐
          ▼          ▼          ▼
         1H         15M         5M
          │          │           │
          └──────────┼───────────┘
                     ▼
               Signal Engine
                     │
        ┌────────────┼────────────┐
        ▼            ▼            ▼
      Trend        Momentum      Trigger
        │            │            │
        └────────────┼────────────┘
                     ▼
                 Risk Engine
                     │
                     ▼
               Score / 100
                     │
                Score ≥ 80
                     │
                     ▼
              Duplicate Guard
                     │
                     ▼
              Cooldown / Filter
                     │
                     ▼
                  SQLite
                     │
                     ▼
                Telegram 📲
                     │
                     ▼
             Outcome Monitor
                     │
          ┌──────────┼──────────┐
          ▼          ▼          ▼
         TP1        TP2         SL
```

---

# ⏱️ Multi-Timeframe Strategy

Bot menggunakan tiga timeframe.

### 1H → Market Bias

Dipakai untuk menentukan arah trend besar.

LONG membutuhkan:

```text
EMA50 > EMA200
Price > EMA50
```

SHORT:

```text
EMA50 < EMA200
Price < EMA50
```

---

### 15M → Setup

Digunakan untuk memastikan struktur trend masih sesuai dengan bias 1H.

LONG:

```text
EMA50 > EMA200
ADX ≥ 22
+DI > -DI
```

SHORT:

```text
EMA50 < EMA200
ADX ≥ 22
-DI > +DI
```

---

### 5M → Trigger

Di sini bot mencari candle yang benar-benar melakukan breakout/trigger.

LONG:

```text
Bullish candle
Close > previous high
Volume confirmation
RSI recovery > 50
```

SHORT:

```text
Bearish candle
Close < previous low
Volume confirmation
RSI breakdown < 50
```

Bot menggunakan **closed candle**, bukan candle yang masih berjalan.

Karena candle berjalan itu kadang suka berubah pikiran. 😌

---

# 📊 Indicators

Bot sengaja menggunakan indikator yang relatif sederhana.

Core indicators:

```text
EMA 50
EMA 200

RSI 14

ADX 14
+DI / -DI

ATR 14

Volume SMA 20
```

Tidak ada parade indikator sampai chart kelihatan seperti panel kontrol NASA.

Tidak menggunakan:

```text
❌ MACD
❌ Stochastic
❌ CCI
❌ Bollinger Bands
❌ Supertrend
❌ Ichimoku
❌ Machine Learning
❌ Sentiment AI
```

Kecuali memang nanti ada perubahan strategi yang disengaja.

---

# 🎯 Signal Score

Score adalah **confidence-style setup score**, bukan probabilitas kemenangan.

Range:

```text
0 ─────────────── 100
```

Komponen:

| Komponen     |   Bobot |
| ------------ | ------: |
| HTF Bias     |      20 |
| EMA Trend    |      15 |
| ADX          |      15 |
| DI Direction |      10 |
| RSI          |      15 |
| 5M Trigger   |      10 |
| Volume       |      10 |
| Risk / RR    |       5 |
| **TOTAL**    | **100** |

Minimum signal **default**:

```text
MIN_SCORE = 80
```

Nilai ini dapat diubah melalui environment configuration.

Jadi:

```text
Score 79
→ NO SIGNAL

Score 80
→ Signal eligible

Score 94
→ Signal eligible
```

Score tinggi **bukan berarti win rate 94%**.

Jangan lihat angka `/100` lalu langsung beli Lambo. 🗿

---

# 🛡️ Anti False-Signal Protection

Bot punya beberapa lapisan proteksi.

### Closed Candle Only

Tidak mengevaluasi candle yang masih berjalan sebagai trigger final.

### Repeated Candle Guard

Bot bisa scan setiap 60 detik, tetapi candle 5M yang sama tidak akan diproses berulang kali.

Contoh:

```text
10:01 → candle 10:00 diproses
10:02 → SKIP
10:03 → SKIP
10:04 → SKIP
10:05 → candle baru → proses lagi
```

### Duplicate Signal Protection

Signal ID bersifat deterministik:

```text
SYMBOL|TIMEFRAME|CANDLE_CLOSE_TS|DIRECTION
```

Signal yang sama tidak dikirim berkali-kali.

### Cooldown

Cooldown digunakan untuk mencegah bot terlalu agresif mengeluarkan sinyal beruntun pada market yang sama.

### Opposite Signal Protection

Bot juga menjaga agar sinyal berlawanan tidak muncul sembarangan pada symbol yang sama.

### Overextension Filter

Setup yang sudah terlalu jauh dari EMA50 berdasarkan ATR dapat ditolak.

### Minimum RR

Setup dengan risk/reward yang tidak memenuhi rule tidak diteruskan.

---

# 💰 Risk Management

Risk engine menggunakan:

```text
ATR 14
```

Stop loss menggunakan pendekatan:

```text
Structure-first
       ↓
ATR fallback
```

Target:

```text
TP1 = 1.5R
TP2 = 2.5R
```

Contoh sederhana:

```text
Entry = 100
SL    = 98

Risk = 2

TP1 = 103
TP2 = 105
```

Jadi:

```text
TP1 = +1.5R
TP2 = +2.5R
```

Bukan:

> "Kayaknya naik 10%, gas."

😂

---

# 📲 Telegram

Ketika signal valid, bot mengirim format seperti:

```text
🟢 LONG BTCUSDT

Timeframe: 5M

Entry: ...
SL: ...
TP1: ...
TP2: ...

RR: ...
Score: .../100

Setup:
...

Confirmations:
✓ HTF bullish
✓ EMA trend
✓ ADX
✓ DI
✓ RSI
✓ Volume
✓ 5M trigger

Invalidation:
...

Signal only • No auto trading
```

Telegram token dan Chat ID dibaca dari environment.

Tidak ditulis hardcoded di source code.

---

# 🔔 Startup Notification

Saat bot berhasil start, Telegram dapat menerima notifikasi startup.

Tujuannya sederhana:

```text
Bot hidup?
        │
        ├── YES → lanjut
        │
        └── NO  → cari masalah
```

Jadi kita tidak perlu menebak apakah VPS masih hidup atau sudah berubah menjadi batu bata digital. 🧱

---

# 📈 Outcome Monitor

Bot bukan cuma mengirim signal lalu menghilang ke horizon.

Signal yang sudah delivered dapat dipantau untuk:

```text
TP1
TP2
SL
```

Outcome disimpan ke SQLite.

Urutannya:

```text
Signal
  ↓
Telegram delivered
  ↓
Market monitoring
  ↓
TP1 / TP2 / SL
  ↓
SQLite state
  ↓
Telegram outcome notification
```

Jika TP2 tercapai, TP1 dianggap tercapai terlebih dahulu.

Untuk candle yang sama-sama menyentuh TP dan SL, rule yang digunakan adalah:

```text
SL wins
```

Ini sengaja konservatif.

Tidak ada:

> "Kayaknya TP duluan."

Market tidak peduli kayaknya. 😭

---

# 🔄 Outcome Monitoring Reliability

Outcome monitor memiliki mekanisme **catch-up** untuk membantu melanjutkan monitoring setelah downtime atau ketika proses sebelumnya belum mengevaluasi seluruh candle yang relevan.

Window monitoring tetap mengikuti:

```text
SIGNAL_MAX_AGE_HOURS = 48
```

Data candle diambil menggunakan pagination Binance Public Market Data sehingga monitoring tidak bergantung pada satu fetch kecil saja.

Alurnya:

```text
Signal
  ↓
Outcome monitor
  ↓
48h monitoring window
  ↓
Paginated candle fetch
  ↓
Chronological evaluation
  ↓
TP1 / TP2 / SL
```

Cursor hanya dimajukan setelah candle berhasil dievaluasi.

Jika evaluasi mengalami kegagalan:

```text
Fetch / evaluation gagal
        ↓
Cursor tidak maju
        ↓
Candle tetap dapat dicoba kembali
```

Outcome retry juga dipisahkan berdasarkan level:

```text
TP1 attempts
TP2 attempts
SL attempts
```

Jadi jika retry TP1 sudah habis, monitoring TP2 dan SL **tidak otomatis ikut berhenti**.

Batas monitoring tetap mengikuti `SIGNAL_MAX_AGE_HOURS`. Signal yang sudah berada di luar TTL tidak dikejar tanpa batas.

---

# 🗄️ SQLite = Source of Truth

SQLite digunakan untuk menyimpan state penting seperti:

* signal history
* delivery state
* delivery attempts
* outcome state
* TP1 hit
* TP2 hit
* SL hit
* timestamps
* replay cursor
* outcome retry attempts
* startup metadata
* cooldown state

Tujuannya supaya restart bot tidak membuat bot lupa semuanya.

---

# 🔄 Restart Safety

Bot dirancang agar state penting tetap tersedia setelah restart.

Contohnya:

```text
Bot hidup
   ↓
Signal dikirim
   ↓
Server restart 💀
   ↓
Bot hidup lagi
   ↓
SQLite dibaca
   ↓
State dilanjutkan
```

Jadi bukan:

```text
Restart
↓
"Siapa saya?"
↓
"BTC itu apa?"
```

😂

---

# 📡 Telegram Delivery Reliability

Pengiriman Telegram menggunakan retry terbatas.

Bot menangani beberapa kondisi seperti:

```text
Timeout
5xx
429
Retry-After
```

Retry maksimum:

```text
3 attempts
```

Backoff:

```text
1s
2s
4s
```

Delivery state juga disimpan di SQLite.

Namun ada satu limitation teknis yang perlu jujur disebut:

```text
Telegram berhasil menerima pesan
        ↓
Process crash
        ↓
SQLite belum sempat mencatat sukses
        ↓
Restart
        ↓
Pesan berpotensi dikirim ulang
```

Artinya sistem memiliki **rare crash window** yang secara teori dapat menyebabkan duplicate notification.

Tidak ada klaim "exactly once" absolut.

---

# 🔐 Security Boundary

Bot ini sengaja dibangun sebagai **signal-only system**.

Tidak ada:

```text
Binance API trading key
Order endpoint
Futures order
Margin
Leverage
Position execution
```

Bot hanya membutuhkan market data publik Binance.

Boundary-nya:

```text
                 BINANCE
                    │
             Public Market Data
                    │
                    ▼
             SIGNAL ENGINE
                    │
                    ▼
               TELEGRAM
```

Tidak ada jalur:

```text
Signal → Binance Order
```

Karena memang tidak dibuat.

---

# 🧪 Testing

Project menggunakan automated test suite.

Validasi terbaru:

```text
470 tests passed
1 warning
```

Test mencakup antara lain:

* indicator calculation
* strategy logic
* signal engine
* closed candle
* no-lookahead
* scoring
* risk calculation
* duplicate protection
* cooldown
* opposite signal
* Telegram delivery
* retry handling
* outcome monitor
* outcome catch-up
* per-level outcome retry
* SQLite persistence
* SQLite migration
* logger
* security guard
* dashboard endpoints (read-only)
* dashboard dynamic symbols
* dashboard authentication

Sebelum deployment, test dijalankan dengan:

```bash
pytest -q
```

---

# 🧮 Backtest Reality Check

Nah, bagian yang sering disembunyikan README proyek trading:

**hasil backtest tidak dijamin profitable.**

Baseline V1 pada dataset historis:

```text
Period:
Jan 2025 → Jun 2026

Symbols:
9

Signals:
1,827

Closed:
1,354

Win Rate:
25.2%

Profit Factor:
0.829

Total R:
-173.6R

Expectancy:
-0.128R

Max Drawdown:
193.8R
```

Jadi V1 jelas belum menjadi strategi profitable.

---

# 🧪 V2 Candidate

Exit experiment dengan ATR trailing menghasilkan:

```text
Closed:
537

Win Rate:
44.5%

Profit Factor:
0.922

Expectancy:
-0.0364R

Total R:
-19.57R

Max Drawdown:
67.30R
```

OOS:

```text
Profit Factor:
0.922

Total R:
-19.57R
```

Kesimpulannya:

> **V2 candidate berhasil divalidasi secara teknis dan menunjukkan perbaikan terhadap baseline, tetapi belum boleh disebut strategi profitable.**

Alias:

```text
❌ "Strategi sakti"
❌ "Win rate dijamin"
❌ "Auto cuan"

✅ Candidate yang sudah diuji
```

Market tetap punya hak veto. 🗿

---

# 🪙 Supported Symbols

Default:

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

Bot memproses symbol secara berkala menggunakan public market data.

---

# ⚙️ Default Parameters

Beberapa parameter utama:

```text
EMA Fast       = 50
EMA Slow       = 200

RSI            = 14
RSI Midline    = 50

ADX            = 14
ADX Minimum    = 22

ATR            = 14

Volume SMA     = 20
Volume Ratio   = 1.2

TP1            = 1.5R
TP2            = 2.5R

Minimum Score  = 80

Cooldown       = 3 trigger candles
```

Parameter dapat dikontrol melalui konfigurasi environment/project.

**Catatan:** nilai di atas adalah default/reference configuration. Production environment dapat melakukan override melalui `.env`.

---

# 🖥️ Installation

## Clone

```bash
git clone https://github.com/rmdnl/long-short-telegram-signal.git
cd long-short-telegram-signal
```

## Python

Project dikembangkan untuk:

```text
Python 3.12+
```

Buat virtual environment:

```bash
python -m venv .venv
```

Aktifkan:

### Linux

```bash
source .venv/bin/activate
```

### Windows PowerShell

```powershell
.venv\Scripts\Activate.ps1
```

Install dependency:

```bash
pip install -r requirements.txt
```

---

# 🔧 Environment

Copy:

```bash
.env.example
```

menjadi:

```bash
.env
```

Isi credential Telegram sesuai environment.

Contoh konsep:

```env
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

**Jangan commit `.env`.**

Gitignore sudah disiapkan untuk mencegah credential masuk repository.

---

# 🧪 Dry Run

Untuk menjalankan satu cycle:

```bash
python -m app.main --cycles 1
```

Mode ini berguna untuk memastikan:

```text
Config
↓
Market data
↓
Signal engine
↓
Telegram
```

berjalan sesuai harapan.

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

Check:

```bash
sudo systemctl status long-short-signal
```

Logs:

```bash
journalctl -u long-short-signal -f
```

---

# 📊 Dashboard (Read-Only Monitor)

Dashboard web **monitoring saja** untuk memantau bot yang sedang berjalan.

**Read-only. Bukan bagian dari trading engine.**

## Purpose

```text
Signal Bot (existing)     →  menghasilkan sinyal, menyimpan ke SQLite
Dashboard (dashboard/)    →  membaca SQLite, menampilkan status
```

Dashboard duduk **di samping** bot, bukan di dalamnya.

## Monitoring-Only Architecture

Dashboard **tidak pernah**:

```text
❌ Binance trading endpoint
❌ API key / API secret handling
❌ Order execution
❌ BUY / SELL / CLOSE / CANCEL / EXECUTE button
❌ Futures / Margin / Leverage
❌ Withdrawal
❌ Mengubah SignalEngine / Scanner / Risk / Indicator / Telegram delivery
❌ Mengubah .env production
❌ Auto deploy / git commit / git push
```

Database dibuka dalam mode **read-only**:

```text
file:signals.db?mode=ro
```

Parameterized SQL. Tidak ada `INSERT` / `UPDATE` / `DELETE` / `ALTER` / `DROP`.

## Dynamic Symbol Configuration (PENTING)

Dashboard **tidak punya daftar symbol sendiri**.

Config bot adalah **single source of truth**.

Dashboard membaca `config.symbols` dari sistem konfigurasi bot yang sama.

`.env`:

```env
SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT
```

Setelah restart/reload, dashboard otomatis menampilkan:

```text
BTCUSDT
ETHUSDT
SOLUSDT
```

Jika `.env` berubah menjadi:

```env
SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,DOGEUSDT
```

Setelah restart/reload, dashboard otomatis menampilkan 5 symbol tersebut.

**Tidak ada** `DASHBOARD_SYMBOLS`.

**Tidak ada** hardcoded list.

**Tidak perlu edit source code** dashboard saat symbol berubah.

## Database Access

Membaca `signals.db` (SQLite) yang sudah dipakai bot.

Saat DB kosong, kolom lama hilang, atau field outcome `NULL`, dashboard tetap berjalan (menampilkan `N/A`).

Tidak pernah mengubah database.

## Local Startup

```bash
python -m dashboard.app
```

Default:

```text
http://127.0.0.1:8080
```

## Environment Variables

Dashboard configuration **terpisah** dari trading configuration:

```env
DASHBOARD_HOST=127.0.0.1
DASHBOARD_PORT=8080
DASHBOARD_AUTH_ENABLED=false
DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD=
```

Default bind: `127.0.0.1`. **Bukan** `0.0.0.0`.

## Authentication (Optional)

```env
DASHBOARD_AUTH_ENABLED=true
DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD=your_strong_password
```

Jika auth diaktifkan tetapi password kosong → **gagal dengan aman** (tidak start).

## API Endpoints

Semua endpoint `GET` saja, read-only:

```text
GET /
GET /api/health
GET /api/status
GET /api/symbols
GET /api/signals
GET /api/signals/latest
GET /api/outcomes
GET /api/summary
GET /api/activity
```

`/api/symbols` mengikuti `config.symbols` bot.

## SSH Tunnel

Dashboard tidak perlu dipublikasikan.

```bash
ssh -L 8080:127.0.0.1:8080 ubuntu@YOUR_SERVER
```

Lalu buka:

```text
http://127.0.0.1:8080
```

## systemd Example

Service **terpisah** dari `long-short-telegram-signal.service`.

```ini
[Unit]
Description=Long Short Signal Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/long-short-telegram-signal
Environment="PYTHONUNBUFFERED=1"
ExecStart=/home/ubuntu/long-short-telegram-signal/.venv/bin/python -m dashboard.app
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Jangan mengganti service bot yang sudah ada.

Contoh file: `dashboard/dashboard.service.example`.

## Security Model

```text
                 BINANCE
                    │
             Public Market Data
                    │
                    ▼
             SIGNAL ENGINE
                    │
                    ▼
               TELEGRAM
                    │
                    ▼
                 SQLite
                    │
                    ▼   (read-only)
              DASHBOARD
```

Dashboard adalah **observer**.

Tidak ada jalur dari dashboard ke Binance order.

---

# 📁 Project Structure

Gambaran sederhananya:

```text
long-short-telegram-signal/
│
├── app/
│   ├── main.py
│   ├── config.py
│   ├── models.py
│   ├── indicators.py
│   ├── signal_engine.py
│   ├── strategy.py
│   ├── risk.py
│   ├── filters.py
│   ├── scanner.py
│   ├── telegram_bot.py
│   ├── outcome_monitor.py
│   ├── signal_store.py
│   ├── logger.py
│   └── trading_guard.py
│
├── dashboard/
│   ├── __init__.py
│   ├── app.py
│   ├── config.py
│   ├── db.py
│   ├── service.py
│   ├── dashboard.service.example
│   ├── templates/
│   │   └── index.html
│   └── static/
│       ├── style.css
│       └── app.js
│
├── tests/
│
├── .env.example
├── requirements.txt
└── README.md
```

---

# 🧠 Design Philosophy

Project ini sengaja tidak dibuat dengan filosofi:

> "Tambahin indikator lagi biar makin akurat."

Karena 17 indikator bukan berarti 17 kali lebih pintar.

Fokusnya:

```text
Simple rules
+
Multi-timeframe confirmation
+
Risk management
+
Anti-duplicate
+
Persistent state
+
Reproducible testing
+
No-lookahead
+
Signal-only execution boundary
```

Lebih baik rule sedikit tapi bisa diaudit daripada chart berubah menjadi hutan indikator. 🌲📈

---

# 🚧 Known Limitations

Project ini masih memiliki beberapa limitation:

### 1. Signal-only

Tidak melakukan trading otomatis.

### 2. Single Telegram Chat ID

Saat ini delivery diarahkan ke satu Chat ID.

### 3. Outcome crash window

Ada kemungkinan duplicate notification yang sangat jarang jika process crash tepat setelah Telegram sukses tetapi sebelum SQLite mencatat state.

### 4. Outcome monitoring TTL

Outcome monitor melakukan catch-up terhadap candle tertutup dalam window hingga TTL signal.

Monitoring tetap dibatasi oleh:

```text
SIGNAL_MAX_AGE_HOURS = 48
```

Signal yang sudah berada di luar TTL tidak dikejar tanpa batas.

### 5. No message editing / recall

Pesan Telegram yang sudah dikirim tidak diedit atau ditarik kembali.

### 6. No delivery guarantee

Telegram API tetap merupakan external dependency.

### 7. Backtest belum profitable

Hasil V2 candidate masih:

```text
PF < 1
Total R < 0
```

Jadi belum ada alasan untuk mengklaim edge yang sudah terbukti.

---

# 🚨 Important

Ini adalah software untuk menghasilkan **informational trading signals**.

Bukan financial advice.

Jangan menggunakan hasil signal sebagai jaminan profit.

Jangan mempertaruhkan uang yang tidak sanggup hilang.

Dan terutama:

```text
Score 100/100
≠
100% WIN
```

Market tidak membaca README.

---

# 🤝 Contributing

Sebelum mengubah strategy:

```text
1. Buat hypothesis
2. Tentukan experiment
3. Gunakan dataset yang sama
4. Hindari lookahead
5. Backtest
6. Walk-forward / OOS
7. Audit robustness
8. Baru pertimbangkan perubahan
```

Jangan:

```text
Backtest jelek
↓
ubah parameter
↓
backtest lagi
↓
ketemu bagus
↓
"ANJIR STRATEGI BARU 🚀"
```

Itu namanya bisa saja cuma curve fitting. 😭

---

# 🧪 Project Status

```text
Architecture             ✅
Signal Engine            ✅
Multi-Timeframe          ✅
Risk Engine              ✅
Anti-Duplicate           ✅
Cooldown                 ✅
SQLite Persistence       ✅
Telegram Delivery        ✅
Outcome Monitor          ✅
Outcome Catch-up         ✅
Per-Level Retry          ✅
Security Guard           ✅
Automated Tests          ✅
VPS Deployment           ✅
Backtest                 ✅
Walk-Forward Validation  ✅
Profitable Strategy      ❌
Auto Trading              ❌
```

Status saat ini:

> **Production signal bot dengan V2 candidate yang sudah divalidasi secara teknis, outcome monitoring yang lebih tahan terhadap downtime, tetapi belum terbukti memiliki profitabilitas yang konsisten.**

---

# 🧃 Final Boss

```text
Market:
"Lu yakin?"

Bot:
"Secara statistik gue masih belajar."

Market:
"Long atau short?"

Bot:
"Setup memenuhi rule."

Market:
"Lu beli?"

Bot:
"ENGGAK. GUE CUMA NGASIH SINYAL."

Market:
"Terus siapa yang pencet?"

Bot:
"Manusia."

Market:
"HAHAHAHA."

Bot:
"😭"
```

---

## 📡 Signal only.

## No auto trading.

## No magic.

## Just rules, data, risk management, dan sedikit harapan. 🗿📊

---

**Repository:** `rmdnl/long-short-telegram-signal`

**Built with:** Python • Binance Public Market Data • SQLite • Telegram • pytest
