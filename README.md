# macro-futures-pulse

Real-time macro futures monitor: ES, NQ, YM (CME Globex via yfinance) and BTC (Binance public API).

Polls every 5 minutes and prints a structured console table with direction, targets, conviction score, momentum badge, and EMA3/9 signals per timeframe.

## Concepts

### Break Point (BP)

BP is calculated as the average of EMA3 and EMA9 across all timeframes, **frozen at session open + 1 minute**. This gives a stable reference level for the session, not a moving target.

```
BP = mean([EMA3_tf for tf in TF_LIST] + [EMA9_tf for tf in TF_LIST]) / 2
Frozen at: CME session_open + 1min (not premarket)
```

### Session Open Logic

| Symbol | Session Open |
|--------|-------------|
| ES / NQ / YM | 18:00 ET (CME Globex). No Saturday open. |
| BTC | 00:00 UTC daily (~20:00 ET EDT) |

### Direction

```
price > BP + threshold   → ALCISTA     (bullish)
price < BP - threshold   → BAJISTA     (bearish)
|price - BP| < threshold → ZONA_MUERTA (dead zone, wait)
```

> These string labels are the literal values returned by `direction_from_dist()`.

Threshold = `ZM_THRESHOLD_PCT × int_dist` ≈ 11% of INT distance.

### Targets

```
INT = BP ± (range_3d × 0.33)   — intermediate target
MAX = BP ± (range_3d × 0.66)   — max extension
```

`range_3d` = BB(20,2) EWM width on 1H, fallback to avg(H-L) last 3 daily bars.

### Conviction Score (EC)

0-10 scale combining:
- **EMA alignment** vs EMA20/50/200 on 1H (±2 pts)
- **GAP type and magnitude** (±2 pts)
- **Prev day change** (±2 pts)
- **BB expansion** (+0.5 pts)
- **Caution count** — Bollinger Band touches across TFs (±1.5 pts)

Returns `-1.0` (BLOQUEADO) if price already opened beyond the INT target.

| Score | Action |
|-------|--------|
| ≥ 7   | PREMIUM |
| 5-6   | OPERABLE |
| ≤ 4   | SKIP |

### Badge

Momentum score 0-100 combining:
- EMA9 > EMA20 on 1m at session open (25 pts)
- Price > EMA20 on 15m at session open (25 pts)
- EMA9 > EMA20 on 1m current (25 pts)
- Gap up or green candle per TF: 1d/1h/30m/15m/5m (5 pts each)

### Pattern V — Macro Semaphore (planned)

A cross-asset filter using TLT (bonds) and GLD (gold) directions to classify
the macro regime (risk-on, risk-off, inflation, USD flight, neutral) before
evaluating individual futures. Not yet implemented in this release — the
infrastructure (BP, direction, conviction) is the prerequisite.

## Data Sources

| Symbol | Source | Latency |
|--------|--------|---------|
| ES, NQ, YM | yfinance (free) | ~10-15 min delayed |
| BTC | Binance public REST API | Real-time |

No API keys required.

## Installation

```bash
pip install -r requirements.txt
```

## Usage

```bash
# From repo root
python -m dc2.macro_futures.run

# Or directly
python dc2/macro_futures/run.py
```

## Output Example

```
══════════════════════════════════════════════════════
MACRO FUTURES — 09:45 ET  2025-01-15
Session opens: ES/NQ/YM=18:00 ET  |  BTC=00:00 ET
══════════════════════════════════════════════════════
     ES    ALCISTA     T2    D% +0.412%    BDG 75.0
  PRC   5,842.25    BP   5,818.0000
  INT  ▲5,873.75  ▼5,762.25    MAX  ▲5,929.25  ▼5,706.75
  SIG  15m▲ 30m▲ 1h▲    DAY ▲0.45% ▼0.31%   PRV ▲1.22% ▼0.88%
──────────────────────────────────────────────────────
     NQ    BAJISTA     T1    D% -0.231%    BDG 35.0
  ...
══════════════════════════════════════════════════════
```

JSON snapshots are saved to `output/macro_YYYY-MM-DD_HH-MM.json` on each poll.

## Structure

```
macro-futures-pulse/
├── dc2/
│   ├── constants.py       — structural constants (TF_LIST, ZM_THRESHOLD_PCT, etc.)
│   ├── data_clients.py    — BPCalculator, TargetsCalculator
│   ├── indicators.py      — conviction_score, badge, signals, ema_alignment
│   ├── utils.py           — bar filtering, BB calculation
│   └── macro_futures/
│       ├── config.py      — FUTURES dict, POLL_INTERVAL_SEC, OUTPUT_DIR
│       ├── data_client.py — FuturesDataClient (yfinance + Binance)
│       ├── mode_macro.py  — core analysis loop per symbol
│       └── run.py         — polling entry point, console table
└── requirements.txt
```

## License

MIT
