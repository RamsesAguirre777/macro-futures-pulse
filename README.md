# macro-futures-pulse

Real-time macro futures monitor: ES, NQ, YM (CME Globex via yfinance) and BTC (Binance public API).

Every 5 minutes it fetches multi-timeframe bars, calculates a frozen Break Point (BP) for the session, derives directional bias, INT/MAX price targets, a conviction score (0-10), and a momentum badge (0-100). Results are printed as a structured console table and saved as JSON snapshots.

## Concepts

### Break Point (BP)

BP is calculated as the average of EMA3 and EMA9 across all timeframes, **frozen at session open + 1 minute**. This gives a stable reference level for the session, not a moving target.

```
BP = (mean(EMA3 across all TFs) + mean(EMA9 across all TFs)) / 2
Frozen at: CME session_open + 1min (not premarket)
```

> **yfinance delay note:** ES/NQ/YM data is ~10-15 min delayed. BP calculated in the
> first few minutes of a session may reflect slightly stale prices. BTC via Binance
> is real-time and unaffected.

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

Threshold = `ZM_THRESHOLD_PCT × int_dist` ≈ 11% of INT distance. If INT distance is
zero (no range data), falls back to ±$0.25.

### Targets

```
INT = BP ± (range_3d × 0.33)   — intermediate target
MAX = BP ± (range_3d × 0.66)   — max extension
```

`range_3d` = Bollinger Band (20, 2) EWM width on 1H pre-cutoff. Falls back to
avg(High − Low) of the last 3 daily bars if 1H data is insufficient.

### Conviction Score (EC)

Structural score on a 0-10 scale. Base 5.0, adjusted by:

| Factor | Range | Logic |
|--------|-------|-------|
| EMA alignment vs EMA20/50/200 on 1H | ±2.0 pts | `sobre_3` / `bajo_3` aligns with direction |
| GAP type and magnitude | ±2.0 pts | GAP in direction = positive; against = negative |
| Prev day change | ±2.0 pts | Trend continuation adds; reversal subtracts |
| BB expansion | +0.5 pts | Bollinger Band expanding = momentum confirmation |
| Caution count | ±1.5 pts | See caution section below |

Returns `-1.0` (BLOQUEADO) if price already opened beyond the INT target — no room left.

| Score | Label | Meaning |
|-------|-------|---------|
| ≥ 7   | PREMIUM | High-confidence setup |
| 5–6   | OPERABLE | Tradeable with standard size |
| ≤ 4   | SKIP | Edge is insufficient |
| −1.0  | BLOQUEADO | Price opened past INT, skip |

#### Caution Count

A caution is triggered when price is outside a Bollinger Band (upper or lower) on a
given timeframe at session open. Monitored TFs: 1m, 5m, 30m, 1H (max 4 cautions).

- **0 cautions** → +1.5 pts (clean open, no overextension)
- **4 cautions BAJISTA** → +1.5 pts (fully extended short-side = adds conviction)
- **4 cautions ALCISTA** → −0.5 pts (fully extended long-side = fading momentum)

### Badge

Momentum score 0-100 combining snapshot state (at session open) and current state:

| Check | Points | When |
|-------|--------|------|
| EMA9 > EMA20 on 1m | 25 | At session open (snapshot) |
| Price > EMA20 on 15m | 25 | At session open (snapshot) |
| EMA9 > EMA20 on 1m | 25 | Current |
| Gap up or green candle per TF | 5 each | Current — TFs: 1d, 1h, 30m, 15m, 5m |

A badge ≥ 75 indicates strong momentum alignment across timeframes.

### Open Zone

Where price opened relative to the BP → INT range, expressed as T1/T2/T3/EXT/ZM:

| Zone | % into BP→INT range | Meaning |
|------|---------------------|---------|
| ZM | < threshold | Dead zone — no clear bias |
| T1 | 0–33% | Early in range |
| T2 | 33–67% | Mid-range open |
| T3 | 67–100% | Late in range |
| EXT | > 100% | Opened beyond INT |

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

## Output Reference

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

| Field | Meaning |
|-------|---------|
| `ALCISTA / BAJISTA / ZONA_MUERTA` | Direction relative to BP |
| `T1 / T2 / T3 / EXT / ZM` | Open zone within BP→INT range |
| `D%` | Current price distance from BP as % |
| `BDG` | Momentum badge score (0–100) |
| `PRC` | Current price |
| `BP` | Break Point frozen at session open + 1min |
| `INT ▲ / ▼` | Intermediate target above / below |
| `MAX ▲ / ▼` | Max extension target above / below |
| `SIG` | EMA3/9 crossover per TF: ▲ bullish, ▼ bearish |
| `DAY ▲ / ▼` | % remaining to session high / low |
| `PRV ▲ / ▼` | % remaining to previous session high / low |

JSON snapshots are saved to `output/macro_YYYY-MM-DD_HH-MM.json` on each poll.

## Extending / Calibrating

The structural logic (BP calculation, target math, direction, conviction framework)
is fully published here. The per-ticker calibration weights — derived from backtesting
on years of intraday data — are not included in this release.

To adapt the conviction score to your own instruments:

1. Override `conviction_score()` in `dc2/indicators.py` with your own weights per factor.
2. Add per-ticker logic via the `ticker` parameter already present in the function signature.
3. Backtest the `ec_score` thresholds (PREMIUM / OPERABLE / SKIP) against your own data
   before using them live.

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
