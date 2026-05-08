from __future__ import annotations

from pathlib import Path

FUTURES: dict[str, str] = {
    "ES": "ES=F",  # S&P 500
    "NQ": "NQ=F",  # NASDAQ
    "YM": "YM=F",  # Dow Jones
    "BTC": "BTC-USD",  # via Binance in data_client (not yfinance)
}
TF_LIST: list[str] = ["1m", "5m", "15m", "30m", "1h", "1d"]
TIMEZONE: str = "America/New_York"
POLL_INTERVAL_SEC: int = 300  # every 5 min
OUTPUT_DIR: str = str(Path(__file__).resolve().parent / "output")
