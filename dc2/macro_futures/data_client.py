from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
import pytz
import requests

from dc2.macro_futures.config import FUTURES, TF_LIST

logger = logging.getLogger(__name__)

_ET = pytz.timezone("America/New_York")

_YF_INTERVAL: dict[str, str] = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "1d": "1d",
}

_BINANCE_INTERVAL: dict[str, str] = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "1d": "1d",
}

_BINANCE_URL = "https://api.binance.com/api/v3/klines"


def _to_et_ts(ts: pd.Timestamp) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize(pytz.UTC)
    return t.tz_convert(_ET)


def _rows_to_bars(df: pd.DataFrame) -> list[dict[str, Any]]:
    """DataFrame OHLCV (columns Open,High,Low,Close,Volume) index datetime → dc2 bar dicts."""
    if df is None or df.empty:
        return []
    out: list[dict[str, Any]] = []
    for idx, row in df.iterrows():
        t = _to_et_ts(pd.Timestamp(idx))
        out.append(
            {
                "t": t,
                "o": float(row["Open"]),
                "h": float(row["High"]),
                "l": float(row["Low"]),
                "c": float(row["Close"]),
                "v": float(row["Volume"]),
            }
        )
    out.sort(key=lambda b: b["t"])
    return out


class FuturesDataClient:
    """Multi-TF bar fetcher: CME futures via yfinance; BTC via Binance public API."""

    def _fetch_yfinance_bars(
        self, symbol: str, tf: str, lookback_days: int
    ) -> list[dict[str, Any]]:
        import yfinance as yf

        iv = _YF_INTERVAL.get(tf)
        if not iv:
            logger.warning("yfinance: unsupported TF %s", tf)
            return []

        end_dt = datetime.now(_ET)
        # yfinance intraday window cap: 1m ~7 days max
        if tf == "1m":
            days = min(max(lookback_days, 1), 7)
        else:
            days = max(lookback_days, 1)

        start_dt = end_dt - timedelta(days=days)
        ticker = yf.Ticker(symbol)
        start_s = start_dt.strftime("%Y-%m-%d")
        end_s = (end_dt + timedelta(days=1)).strftime("%Y-%m-%d")
        df = ticker.history(
            start=start_s,
            end=end_s,
            interval=iv,
            auto_adjust=False,
            prepost=False,
        )
        if df.empty:
            return []
        df.columns = [str(c).lower() for c in df.columns]
        df = df.rename(
            columns={
                "open": "Open",
                "high": "High",
                "low": "Low",
                "close": "Close",
                "volume": "Volume",
            }
        )
        for col in ("Open", "High", "Low", "Close", "Volume"):
            if col not in df.columns:
                df[col] = 0.0
        return _rows_to_bars(df)

    def _fetch_binance_bars(self, tf: str, lookback_days: int) -> list[dict[str, Any]]:
        interval = _BINANCE_INTERVAL.get(tf)
        if not interval:
            logger.warning("Binance: unsupported TF %s", tf)
            return []

        params: dict[str, str | int] = {
            "symbol": "BTCUSDT",
            "interval": interval,
            "limit": 500,
        }
        try:
            r = requests.get(_BINANCE_URL, params=params, timeout=30)
            r.raise_for_status()
            raw = r.json()
        except Exception as e:
            logger.warning("Binance klines failed (%s): %s", tf, e)
            return []

        out: list[dict[str, Any]] = []
        for row in raw:
            open_ms = int(row[0])
            t = pd.Timestamp(open_ms, unit="ms", tz=pytz.UTC).tz_convert(_ET)
            out.append(
                {
                    "t": t,
                    "o": float(row[1]),
                    "h": float(row[2]),
                    "l": float(row[3]),
                    "c": float(row[4]),
                    "v": float(row[5]),
                }
            )
        out.sort(key=lambda b: b["t"])
        return out

    def fetch_bars(
        self, symbol: str, tf: str, lookback_days: int = 5
    ) -> list[dict[str, Any]]:
        """
        Returns bar list in dc2 format: t (ET), o,h,l,c,v.
        BTC-USD uses Binance; all others use yfinance.
        """
        if symbol == FUTURES["BTC"]:
            return self._fetch_binance_bars(tf, lookback_days)
        try:
            return self._fetch_yfinance_bars(symbol, tf, lookback_days)
        except Exception as e:
            logger.warning("yfinance failed %s %s: %s", symbol, tf, e)
            return []

    def fetch_all_bars(self, lookback_days: int = 5) -> dict[str, dict[str, list]]:
        """{ES|NQ|YM|BTC: {tf: bars_list}}."""
        out: dict[str, dict[str, list]] = {}
        for key, sym in FUTURES.items():
            out[key] = {}
            for tf in TF_LIST:
                bars = self.fetch_bars(sym, tf, lookback_days)
                out[key][tf] = bars
                if not bars and key != "BTC":
                    logger.warning(
                        "No bars %s %s (%s) — check symbol or yfinance delay",
                        key,
                        tf,
                        sym,
                    )
        return out
