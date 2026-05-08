"""Structural constants for macro-futures-pulse."""
import pytz

TIMEZONE = pytz.timezone("America/New_York")
UTC = pytz.UTC

TF_LIST = ["1m", "5m", "15m", "30m", "1h", "1d"]
N_VELAS_BB = 20

# Dead zone: 33% of first third (BP→t1) ≈ 11% of int_dist
ZM_THRESHOLD_PCT = 0.333 * 0.333

# Ticker families for cross-market correlation analysis
FAMILIES = {
    "SEMIS": ["NVDA", "AMD"],
    "ADS": ["META", "GOOGL"],
    "CLOUD": ["MSFT", "AMZN"],
    "BETA": ["TSLA", "IWM"],
    "INDICES": ["QQQ", "DIA"],
}

CAUTION_TEXTS = {
    "BBT 1M": "CAUTION: Price outside of the BBT 1M\nSlightly overbought on 1-minute candles.",
    "BBB 1M": "CAUTION: Price outside of the BBB 1M\nSlightly oversold on 1-minute candles.",
    "BBT 5M": "CAUTION: Price outside of the BBT 5M\nOverbought on 5-minute candles.",
    "BBB 5M": "CAUTION: Price outside of the BBB 5M\nOversold on 5-minute candles, expect a low correction.",
    "BBT 30M": "CAUTION: Price outside of the BBT 30M\nOverbought on 30-minute candles.",
    "BBB 30M": "CAUTION: Price outside of the BBB 30M\nOversold on 30-minute candles, expect a low correction.",
    "BBT 1H": "CAUTION: Price outside of the BBT 1H\nOverbought on 1-hour candles.",
    "BBB 1H": "CAUTION: Price outside of the BBB 1H\nOversold on 1-hour candles, expect a low correction.",
}
