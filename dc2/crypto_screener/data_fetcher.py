"""Descarga OHLCV Binance spot / USDT-M futures vía CCXT."""
from __future__ import annotations

import logging
import time
from typing import Any

import ccxt  # type: ignore[import-untyped]
import pandas as pd

logger = logging.getLogger(__name__)

TIMEFRAMES = ("1d", "1h", "15m", "1m")

_MS_DAY = 86_400_000
_DEFAULT_DAYS_1D = 1100  # >3 años para EMAs + 20 meses mensuales


def make_binance_exchange() -> ccxt.binance:
    ex = ccxt.binance(
        {
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        }
    )
    return ex  # type: ignore[return-value]


def make_binance_futures_exchange() -> ccxt.binance:
    """Binance USDT-M perpetuos (swap). Símbolo formato: BTC/USDT:USDT"""
    ex = ccxt.binance(
        {
            "enableRateLimit": True,
            "options": {"defaultType": "future"},
        }
    )
    return ex  # type: ignore[return-value]


def ohlcv_to_df(rows: list[list], *, utc: bool = True) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(
        rows,
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=utc)
    df = df.set_index("timestamp")
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_index()


def fetch_ohlcv_df(
    exchange,
    symbol: str,
    timeframe: str,
    *,
    since_ms: int | None = None,
    limit: int | None = None,
    max_batches: int = 40,
) -> pd.DataFrame:
    """
    Con ``since_ms``: pagina hacia adelante hasta vacío o ``max_batches``.
    Sin ``since_ms``: una sola petición (últimas ``limit`` velas, default 1000).
    """
    all_rows: list[list[Any]] = []
    batch_limit = min(limit or 1000, 1000)

    if since_ms is None:
        try:
            batch = exchange.fetch_ohlcv(
                symbol, timeframe, since=None, limit=batch_limit
            )
        except Exception as e:
            logger.warning("fetch_ohlcv %s %s: %s", symbol, timeframe, e)
            batch = []
        all_rows.extend(batch or [])
    else:
        since = since_ms
        for _ in range(max_batches):
            try:
                batch = exchange.fetch_ohlcv(
                    symbol, timeframe, since=since, limit=batch_limit
                )
            except Exception as e:
                logger.warning("fetch_ohlcv %s %s: %s", symbol, timeframe, e)
                break
            if not batch:
                break
            all_rows.extend(batch)
            if len(batch) < batch_limit:
                break
            since = batch[-1][0] + 1
            time.sleep(exchange.rateLimit / 1000.0 if exchange.rateLimit else 0)

    df = ohlcv_to_df(all_rows)
    if df.empty:
        return df
    df = df[~df.index.duplicated(keep="last")]
    return df


def since_ms_for_last_n_days(n_days: int) -> int:
    from datetime import datetime, timedelta, timezone

    start = datetime.now(timezone.utc) - timedelta(days=n_days)
    return int(start.timestamp() * 1000)


def fetch_bundle_for_screener(
    exchange,
    symbol: str,
    *,
    days_1d: int = _DEFAULT_DAYS_1D,
) -> dict[str, pd.DataFrame]:
    """1d, 1h, 15m para el scan (23:55 UTC). 1m NO se descarga aquí — solo en open_signal."""
    since = since_ms_for_last_n_days(days_1d)
    out: dict[str, pd.DataFrame] = {}
    out["1d"] = fetch_ohlcv_df(exchange, symbol, "1d", since_ms=since)
    # Intradía: últimos ~35 días bastan para EMA20 1h y EMA9/20 15m
    since_intr = since_ms_for_last_n_days(45)
    out["1h"] = fetch_ohlcv_df(exchange, symbol, "1h", since_ms=since_intr)
    out["15m"] = fetch_ohlcv_df(exchange, symbol, "15m", since_ms=since_intr)
    return out
