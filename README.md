# 📡 Macro Futures Pulse

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Data: yfinance + Binance](https://img.shields.io/badge/data-yfinance%20%2B%20Binance-orange.svg)]()
[![No API Keys](https://img.shields.io/badge/API%20keys-not%20required-brightgreen.svg)]()
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Status: Active](https://img.shields.io/badge/Status-Active-brightgreen.svg)]()

> **Real-time macro futures monitor for ES, NQ, YM (CME Globex) and BTC. Polls every 5 minutes, computes a session-frozen Break Point, directional bias, INT/MAX targets, conviction score and momentum badge. No API keys required.**

---

## 🌟 What It Does

- **4 instruments covered** — ES · NQ · YM via yfinance (free, ~15min delay) + BTC via Binance (real-time)
- **Session-frozen BP** — Break Point locked at session open + 1 minute, not a moving target
- **Directional bias** — `BULLISH / BEARISH / DEAD_ZONE` relative to BP with a dynamic threshold
- **INT/MAX targets** — derived from BB(20,2) EWM range on 1H data
- **Conviction score (0–10)** — structural edge score based on EMA alignment, GAP, prev-day trend, BB expansion and caution count
- **Momentum badge (0–100)** — cross-timeframe momentum alignment at open and current state
- **Open zone classification** — T1 / T2 / T3 / EXT / ZM — where price opened within the BP→INT range
- **JSON snapshots** — every poll writes `output/macro_YYYY-MM-DD_HH-MM.json`

---

## 🚀 Quick Start

```bash
git clone https://github.com/RamsesAguirre777/macro-futures-pulse.git
cd macro-futures-pulse
pip install -r requirements.txt

python -m dc2.macro_futures.run
```

No `.env`, no API keys. Polls every 5 minutes and prints a structured console table.

---

## 🎯 Key Concepts

### Break Point (BP)

Average of EMA3 and EMA9 across all timeframes, **frozen at session open + 1 minute**.

```
BP = (mean(EMA3 across all TFs) + mean(EMA9 across all TFs)) / 2
Frozen at: CME session_open + 1min  |  BTC: 00:00 UTC daily
```

> ⚠️ yfinance delay note: ES/NQ/YM data is ~10-15 min delayed. BTC via Binance is real-time.

### Session Opens

| Symbol | Session Open |
|---|---|
| ES / NQ / YM | 18:00 ET (CME Globex) — no Saturday open |
| BTC | 00:00 UTC daily (~20:00 ET EDT) |

### Direction

```
price > BP + threshold   →  BULLISH    (ALCISTA)
price < BP - threshold   →  BEARISH    (BAJISTA)
|price - BP| < threshold →  DEAD ZONE  (ZONA_MUERTA)
```

Threshold = `ZM_THRESHOLD_PCT × int_dist` (~11% of INT distance). Falls back to ±$0.25 if INT distance is zero.

### Targets

```
range_3d  =  BB(20,2) EWM width on 1H pre-cutoff
             (fallback: avg High − Low of last 3 daily bars)

INT  =  BP ± (range_3d × 0.33)   — intermediate target
MAX  =  BP ± (range_3d × 0.66)   — max extension
```

### Conviction Score (0–10)

Structural edge score. Base 5.0, adjusted by:

| Factor | Range | Logic |
|---|---|---|
| EMA alignment vs EMA20/50/200 on 1H | ±2.0 pts | Alignment with direction adds, against subtracts |
| GAP type and magnitude | ±2.0 pts | GAP in direction = positive; against = negative |
| Prev-day change | ±2.0 pts | Trend continuation adds; reversal subtracts |
| BB expansion | +0.5 pts | Bollinger Band expanding = momentum confirmation |
| Caution count | ±1.5 pts | See below |

Returns `-1.0` (BLOCKED) if price already opened beyond the INT target.

| Score | Label | Action |
|---|---|---|
| ≥ 7 | **PREMIUM** | High-confidence setup |
| 5–6 | **OPERABLE** | Tradeable with standard size |
| ≤ 4 | **SKIP** | Insufficient edge |
| −1.0 | **BLOCKED** | Price opened past INT — no room left |

#### Caution Count

Triggered when price is outside a BB band on a given TF at session open. Monitored TFs: 1m, 5m, 30m, 1H (max 4).

| Count | Effect |
|---|---|
| 0 cautions | +1.5 pts — clean open, no overextension |
| 4 cautions BEARISH | +1.5 pts — fully extended short-side |
| 4 cautions BULLISH | −0.5 pts — fully extended long-side, fading momentum |

### Momentum Badge (0–100)

| Check | Points | When |
|---|---|---|
| EMA9 > EMA20 on 1m | 25 | At session open (snapshot) |
| Price > EMA20 on 15m | 25 | At session open (snapshot) |
| EMA9 > EMA20 on 1m | 25 | Current |
| Gap up or green candle per TF | 5 each | Current — TFs: 1d, 1h, 30m, 15m, 5m |

Badge ≥ 75 = strong momentum alignment across timeframes.

### Open Zone

Where price opened relative to the BP→INT range:

| Zone | Position | Meaning |
|---|---|---|
| ZM | < threshold | Dead zone — no clear bias |
| T1 | 0–33% into range | Early in range |
| T2 | 33–67% into range | Mid-range open |
| T3 | 67–100% into range | Late in range |
| EXT | > 100% | Opened beyond INT |

---

## 📊 Console Output

```
══════════════════════════════════════════════════════
MACRO FUTURES — 09:45 ET  2025-01-15
Session opens: ES/NQ/YM=18:00 ET  |  BTC=00:00 ET
══════════════════════════════════════════════════════
     ES    BULLISH     T2    D% +0.412%    BDG 75.0
  PRC   5,842.25    BP   5,818.0000
  INT  ▲5,873.75  ▼5,762.25    MAX  ▲5,929.25  ▼5,706.75
  SIG  15m▲ 30m▲ 1h▲    DAY ▲0.45% ▼0.31%   PRV ▲1.22% ▼0.88%
──────────────────────────────────────────────────────
     NQ    BEARISH     T1    D% -0.231%    BDG 35.0
  ...
══════════════════════════════════════════════════════
```

| Field | Meaning |
|---|---|
| `BULLISH / BEARISH / DEAD_ZONE` | Direction relative to BP |
| `T1 / T2 / T3 / EXT / ZM` | Open zone within BP→INT range |
| `D%` | Current price distance from BP as % |
| `BDG` | Momentum badge score (0–100) |
| `PRC` | Current price |
| `BP` | Break Point frozen at session open + 1min |
| `INT ▲ / ▼` | Intermediate target above / below |
| `MAX ▲ / ▼` | Max extension target above / below |
| `SIG` | EMA3/9 crossover per TF: ▲ bullish ▼ bearish |
| `DAY ▲ / ▼` | % remaining to session high / low |
| `PRV ▲ / ▼` | % remaining to previous session high / low |

JSON snapshots → `output/macro_YYYY-MM-DD_HH-MM.json`

---

## 🌐 Data Sources

| Symbol | Source | Latency |
|---|---|---|
| ES, NQ, YM | yfinance (free) | ~10–15 min delayed |
| BTC | Binance public REST API | Real-time |

---

## 🏗️ Architecture

```
macro-futures-pulse/
├── dc2/
│   ├── constants.py          — TF_LIST, ZM_THRESHOLD_PCT, structural constants
│   ├── data_clients.py       — BPCalculator, TargetsCalculator
│   ├── indicators.py         — conviction_score, badge, signals, ema_alignment
│   ├── utils.py              — bar filtering, BB(20,2) EWM calculation
│   └── macro_futures/
│       ├── config.py         — FUTURES dict, POLL_INTERVAL_SEC, OUTPUT_DIR
│       ├── data_client.py    — FuturesDataClient (yfinance + Binance)
│       ├── mode_macro.py     — core analysis loop per symbol
│       └── run.py            — polling entry point, console table renderer
└── requirements.txt
```

---

## 📝 Notes

- **Calibration weights** for the conviction score (derived from backtesting on years of intraday data) are not included in this release. To adapt to your own instruments, override `conviction_score()` in `dc2/indicators.py` with your own weights per factor and backtest the PREMIUM / OPERABLE / SKIP thresholds before using live.
- **Pattern V macro semaphore** (TLT + GLD cross-asset regime filter) is planned but not yet implemented — the BP / direction / conviction infrastructure is the prerequisite.

---

## 📄 License

MIT — free to use, modify and distribute.

---

<div align="center">
  <p>Built for traders who need macro context before the equity open.</p>
  <a href="https://github.com/RamsesAguirre777">GitHub</a>
</div>
