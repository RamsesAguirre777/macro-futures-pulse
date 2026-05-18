"""Screener post-mercado (~4pm ET): universo US (NASDAQ API / Wikipedia), 1D yfinance + intradía Alpaca."""
from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any

import duckdb
import numpy as np
import pandas as pd
import yfinance as yf

from dc2.backtest.db import DB_PATH, get_conn
from dc2.constants import TIMEZONE
from dc2.data_clients import AlpacaHistoricalClient

logger = logging.getLogger(__name__)

FALLBACK_TICKERS: list[str] = [
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "NVDA", "META", "TSLA", "BRK-B", "LLY",
    "V", "UNH", "JPM", "WMT", "XOM", "JNJ", "MA", "PG", "AVGO", "HD",
    "MRK", "COST", "ABBV", "PEP", "KO", "ADBE", "CRM", "CSCO", "ACN", "AMD",
    "INTC", "QCOM", "TXN", "IBM", "ORCL", "NFLX", "INTU", "AMAT", "NOW", "ISRG",
    "BKNG", "ADI", "LRCX", "MU", "PYPL", "COIN", "PLTR", "SPY", "QQQ", "DIA",
]

SCREENER_DDL = """
CREATE TABLE IF NOT EXISTS screener_postmarket (
    fecha DATE,
    ticker VARCHAR,
    direccion VARCHAR,
    close_1d DOUBLE,
    resistencia_1d DOUBLE,
    soporte_1d DOUBLE,
    ema3_1d DOUBLE,
    ema9_1d DOUBLE,
    vol_ratio DOUBLE,
    ema20_15m DOUBLE,
    close_15m DOUBLE,
    ema9_1m DOUBLE,
    ema20_1m DOUBLE,
    created_at TIMESTAMP DEFAULT current_timestamp
)
"""


def _clean_symbol(sym: str) -> str:
    return str(sym).strip().upper().replace(".", "-")


def _is_valid_common_symbol(sym: str) -> bool:
    """Descarta preferreds, slashes, warrants (guión final) y tickers >5 chars (no common típico)."""
    if not sym:
        return False
    if "^" in sym or "/" in sym:
        return False
    if sym.endswith("-"):
        return False
    if len(sym) > 5:
        return False
    return True


def load_universe_nasdaq_api() -> list[str]:
    import requests

    headers = {"User-Agent": "Mozilla/5.0"}
    urls = [
        "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&exchange=nasdaq",
        "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&exchange=nyse",
        "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&exchange=amex",
    ]
    syms: list[str] = []
    for url in urls:
        try:
            r = requests.get(url, headers=headers, timeout=15)
            r.raise_for_status()
            rows = r.json()["data"]["table"]["rows"]
            syms.extend(row["symbol"] for row in rows if row.get("symbol"))
        except Exception as e:
            logger.warning("NASDAQ API falló (%s): %s", url, e)
    out = [_clean_symbol(s) for s in syms if s and str(s).upper() != "NAN"]
    return sorted(s for s in set(out) if _is_valid_common_symbol(s))


def load_universe_from_wikipedia() -> list[str]:
    syms: list[str] = []
    ua = {"User-Agent": "Mozilla/5.0"}
    try:
        sp = pd.read_html(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            storage_options=ua,
        )[0]
        col = "Symbol" if "Symbol" in sp.columns else sp.columns[0]
        syms.extend(sp[col].astype(str).tolist())
    except Exception as e:
        logger.warning("Wikipedia S&P500 falló: %s", e)
    try:
        tables = pd.read_html(
            "https://en.wikipedia.org/wiki/Nasdaq-100",
            storage_options=ua,
        )
        ndq = None
        if len(tables) > 4:
            t4 = tables[4]
            if "Ticker" in t4.columns:
                ndq = t4
        if ndq is None:
            for idx in (4, 3, 2, 5, 1, 0):
                if idx < len(tables):
                    t = tables[idx]
                    if "Ticker" in t.columns:
                        ndq = t
                        break
                    if "Symbol" in t.columns:
                        ndq = t.rename(columns={"Symbol": "Ticker"})
                        break
        if ndq is not None and "Ticker" in ndq.columns:
            syms.extend(ndq["Ticker"].astype(str).tolist())
    except Exception as e:
        logger.warning("Wikipedia Nasdaq-100 falló: %s", e)
    out = [_clean_symbol(s) for s in syms if s and str(s).upper() != "NAN"]
    return sorted(s for s in set(out) if _is_valid_common_symbol(s))


def load_universe() -> list[str]:
    u = load_universe_nasdaq_api()
    if len(u) < 100:
        logger.warning(
            "NASDAQ API insuficiente (%d tickers); fallback Wikipedia.",
            len(u),
        )
        u = load_universe_from_wikipedia()
    if len(u) < 100:
        logger.warning(
            "Universo insuficiente (%d); usando FALLBACK_TICKERS (%d).",
            len(u),
            len(FALLBACK_TICKERS),
        )
        return sorted(
            s
            for s in dict.fromkeys(_clean_symbol(t) for t in FALLBACK_TICKERS)
            if _is_valid_common_symbol(s)
        )
    return sorted(s for s in u if _is_valid_common_symbol(s))


def _yf_slice_for_ticker(df: pd.DataFrame, ticker: str) -> pd.DataFrame | None:
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        if ticker not in df.columns.get_level_values(0):
            return None
        sub = df[ticker].copy()
    else:
        sub = df.copy()
    need = {"Open", "High", "Low", "Close", "Volume"}
    if not need.issubset(set(sub.columns)):
        return None
    sub = sub.dropna(how="all")
    if sub.empty or len(sub) < 9:
        return None
    return sub


def download_1d_all(
    tickers: list[str], chunk_fallback: int = 100
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """Un batch yfinance con todo el universo; si falla, reintenta por lotes."""
    per_ticker: dict[str, pd.DataFrame] = {}
    failures: list[str] = []

    def ingest_raw(raw: pd.DataFrame, batch: list[str]) -> None:
        if raw is None or raw.empty:
            failures.extend(batch)
            return
        for t in batch:
            sl = _yf_slice_for_ticker(raw, t)
            if sl is None or len(sl) < 9:
                failures.append(t)
            else:
                per_ticker[t] = sl

    try:
        raw = yf.download(
            tickers,
            period="3y",
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            threads=False,
            progress=False,
        )
        ingest_raw(raw, tickers)
        if len(per_ticker) + len(failures) < len(tickers) * 0.5:
            raise RuntimeError("batch principal devolvió muy pocos tickers")
    except Exception as e:
        logger.warning("yfinance batch único falló (%s); reintentando por lotes.", e)
        per_ticker.clear()
        failures.clear()
        for i in range(0, len(tickers), chunk_fallback):
            batch = tickers[i : i + chunk_fallback]
            try:
                raw = yf.download(
                    batch,
                    period="3y",
                    interval="1d",
                    group_by="ticker",
                    auto_adjust=True,
                    threads=False,
                    progress=False,
                )
                ingest_raw(raw, batch)
            except Exception as e2:
                logger.warning("yfinance lote %d-%d: %s", i, i + len(batch), e2)
                failures.extend(batch)

    missing = [t for t in tickers if t not in per_ticker and t not in failures]
    failures.extend(missing)
    return per_ticker, sorted(set(failures))


@dataclass
class Row1D:
    ticker: str
    close_1d: float
    high_1d: float
    low_1d: float
    vol_1d: float
    ema3: float
    ema9: float
    ema20v: float
    resistencia_1d: float
    soporte_1d: float
    vol_ratio: float
    ema20_1mo: float  # EMA20 del close mensual


def compute_1d_row(ticker: str, ohlcv: pd.DataFrame) -> Row1D | None:
    try:
        close = ohlcv["Close"].astype(float)
        high = ohlcv["High"].astype(float)
        low = ohlcv["Low"].astype(float)
        vol = ohlcv["Volume"].astype(float)
        if len(close) < 9 or np.isnan(close.iloc[-1]):
            return None
        ema3 = float(close.ewm(span=3, adjust=False).mean().iloc[-1])
        ema9 = float(close.ewm(span=9, adjust=False).mean().iloc[-1])
        ema20v = float(vol.ewm(span=20, adjust=False).mean().iloc[-1])
        hi = float(high.iloc[-1])
        lo = float(low.iloc[-1])
        cl = float(close.iloc[-1])
        v1 = float(vol.iloc[-1])
        rng = hi - lo
        resistencia_1d = hi - rng * 0.33
        soporte_1d = lo + rng * 0.33
        vol_ratio = (v1 / ema20v) if ema20v and not np.isnan(ema20v) else float("nan")
        monthly_close = close.resample("ME").last().dropna()
        ema20_1mo = (
            float(monthly_close.ewm(span=20, adjust=False).mean().iloc[-1])
            if len(monthly_close) >= 20
            else float("nan")
        )
        return Row1D(
            ticker=ticker,
            close_1d=cl,
            high_1d=hi,
            low_1d=lo,
            vol_1d=v1,
            ema3=ema3,
            ema9=ema9,
            ema20v=ema20v,
            resistencia_1d=resistencia_1d,
            soporte_1d=soporte_1d,
            vol_ratio=vol_ratio,
            ema20_1mo=ema20_1mo,
        )
    except Exception:
        return None


def filter_long_1d(r: Row1D) -> bool:
    if not (r.close_1d > r.ema3 and r.ema3 > r.ema9):
        return False
    if not (r.close_1d >= r.resistencia_1d):
        return False
    if not np.isnan(r.ema20_1mo) and r.close_1d <= r.ema20_1mo:
        return False
    return r.vol_1d > r.ema20v


def filter_short_1d(r: Row1D) -> bool:
    if not (r.close_1d < r.ema3 and r.ema3 < r.ema9):
        return False
    if not (r.close_1d <= r.soporte_1d):
        return False
    if not np.isnan(r.ema20_1mo) and r.close_1d >= r.ema20_1mo:
        return False
    return r.vol_1d > r.ema20v


def _bars_on_et_date(bars: list[dict], d: date) -> list[dict]:
    out: list[dict] = []
    for b in bars:
        ts = b["t"]
        if isinstance(ts, pd.Timestamp):
            ts_et = (
                ts.tz_convert(TIMEZONE)
                if ts.tzinfo
                else ts.tz_localize("UTC").tz_convert(TIMEZONE)
            )
        else:
            ts_et = pd.Timestamp(ts).tz_convert(TIMEZONE)
        if ts_et.date() == d:
            out.append(b)
    return sorted(out, key=lambda x: x["t"])


def _alpaca_day_window(fecha: date) -> tuple[datetime, datetime]:
    start = TIMEZONE.localize(datetime.combine(fecha, time(9, 25)))
    end = TIMEZONE.localize(datetime.combine(fecha, time(16, 10)))
    return start, end


def fetch_intraday_metrics(ticker: str, fecha: date) -> dict[str, float] | None:
    start, end = _alpaca_day_window(fecha)
    try:
        bars_15 = AlpacaHistoricalClient.fetch_bars(ticker, "15m", start, end)
        bars_1 = AlpacaHistoricalClient.fetch_bars(ticker, "1m", start, end)
    except Exception:
        return None
    b15 = _bars_on_et_date(bars_15, fecha)
    b1 = _bars_on_et_date(bars_1, fecha)
    if len(b15) < 20 or len(b1) < 30:
        return None
    b15 = b15[-20:]
    b1 = b1[-30:]
    c15 = pd.Series([float(x["c"]) for x in b15])
    c1 = pd.Series([float(x["c"]) for x in b1])
    ema20_15m = float(c15.ewm(span=20, adjust=False).mean().iloc[-1])
    close_15m_last = float(c15.iloc[-1])
    ema9_1m = float(c1.ewm(span=9, adjust=False).mean().iloc[-1])
    ema20_1m = float(c1.ewm(span=20, adjust=False).mean().iloc[-1])
    close_1m_last = float(c1.iloc[-1])
    return {
        "ema20_15m": ema20_15m,
        "close_15m": close_15m_last,
        "ema9_1m": ema9_1m,
        "ema20_1m": ema20_1m,
        "close_1m": close_1m_last,
    }


def filter_long_intraday(m: dict[str, float]) -> bool:
    return m["close_15m"] > m["ema20_15m"] and m["ema9_1m"] > m["ema20_1m"]


def filter_short_intraday(m: dict[str, float]) -> bool:
    return m["close_15m"] < m["ema20_15m"] and m["ema9_1m"] < m["ema20_1m"]


@dataclass
class FinalRow:
    ticker: str
    direccion: str
    r1: Row1D
    intr: dict[str, float | None]


def _print_table(title: str, rows: list[dict[str, Any]], fecha: date) -> None:
    try:
        from tabulate import tabulate

        use_tabulate = True
    except ImportError:
        use_tabulate = False
    print(f"\n{title} [{fecha}]\n")
    if not rows:
        print("(sin candidatos)\n")
        return
    if use_tabulate:
        print(tabulate(rows, headers="keys", floatfmt=".4f"))
    else:
        print(pd.DataFrame(rows).to_string(index=False))
    print()


def ensure_screener_table(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(SCREENER_DDL)


def persist_results(
    conn: duckdb.DuckDBPyConnection, fecha: date, finals: list[FinalRow]
) -> None:
    ensure_screener_table(conn)
    conn.execute("DELETE FROM screener_postmarket WHERE fecha = ?", [fecha])
    for fr in finals:
        conn.execute(
            """
            INSERT INTO screener_postmarket (
                fecha, ticker, direccion, close_1d, resistencia_1d, soporte_1d,
                ema3_1d, ema9_1d, vol_ratio, ema20_15m, close_15m, ema9_1m, ema20_1m
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                fecha,
                fr.ticker,
                fr.direccion,
                fr.r1.close_1d,
                fr.r1.resistencia_1d,
                fr.r1.soporte_1d,
                fr.r1.ema3,
                fr.r1.ema9,
                fr.r1.vol_ratio if not np.isnan(fr.r1.vol_ratio) else None,
                fr.intr.get("ema20_15m"),
                fr.intr.get("close_15m"),
                fr.intr.get("ema9_1m"),
                fr.intr.get("ema20_1m"),
            ],
        )


def _intraday_placeholder() -> dict[str, float | None]:
    return {
        "ema20_15m": None,
        "close_15m": None,
        "ema9_1m": None,
        "ema20_1m": None,
        "close_1m": None,
    }


def run(
    fecha: date,
    solo_1d: bool,
    ticker_override: list[str] | None,
    min_price: float | None = None,
    max_price: float | None = None,
    min_volume: int = 10_000_000,
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if ticker_override:
        universe = sorted(
            s
            for s in (_clean_symbol(t) for t in ticker_override)
            if _is_valid_common_symbol(s)
        )
    else:
        universe = load_universe()
    logger.info("Universo cargado: %d tickers", len(universe))

    yf_map, yf_fail = download_1d_all(universe)
    rows_1d: dict[str, Row1D] = {}
    for t, ohlcv in yf_map.items():
        row = compute_1d_row(t, ohlcv)
        if row is not None:
            rows_1d[t] = row
    logger.info("Con datos yfinance: %d tickers", len(rows_1d))

    if min_price is not None or max_price is not None:
        rows_1d_filtered: dict[str, Row1D] = {}
        for t, r in rows_1d.items():
            if min_price is not None and r.close_1d < min_price:
                continue
            if max_price is not None and r.close_1d > max_price:
                continue
            rows_1d_filtered[t] = r
        rows_1d = rows_1d_filtered
    logger.info("Después de filtro precio: %d tickers", len(rows_1d))

    filtered: dict[str, Row1D] = {}
    for t, r in rows_1d.items():
        if r.vol_1d < min_volume:
            continue
        filtered[t] = r
    rows_1d = filtered
    logger.info("Después de filtro volumen mín: %d tickers", len(rows_1d))

    cand_long = [rows_1d[t] for t in rows_1d if filter_long_1d(rows_1d[t])]
    cand_short = [rows_1d[t] for t in rows_1d if filter_short_1d(rows_1d[t])]
    logger.info("Candidatos 1D — LONG: %d | SHORT: %d", len(cand_long), len(cand_short))

    finals: list[FinalRow] = []

    if solo_1d:
        for r in cand_long:
            finals.append(FinalRow(r.ticker, "LONG", r, _intraday_placeholder()))
        for r in cand_short:
            finals.append(FinalRow(r.ticker, "SHORT", r, _intraday_placeholder()))
    else:
        for r in cand_long:
            intr = fetch_intraday_metrics(r.ticker, fecha)
            if intr is None or not filter_long_intraday(intr):
                continue
            finals.append(FinalRow(r.ticker, "LONG", r, intr))
        for r in cand_short:
            intr = fetch_intraday_metrics(r.ticker, fecha)
            if intr is None or not filter_short_intraday(intr):
                continue
            finals.append(FinalRow(r.ticker, "SHORT", r, intr))

    n_long = sum(1 for fr in finals if fr.direccion == "LONG")
    n_short = sum(1 for fr in finals if fr.direccion == "SHORT")
    logger.info("Candidatos finales — LONG: %d | SHORT: %d", n_long, n_short)

    long_rows: list[dict[str, Any]] = []
    short_rows: list[dict[str, Any]] = []
    for fr in finals:
        if fr.direccion == "LONG":
            e9, e20 = fr.intr.get("ema9_1m"), fr.intr.get("ema20_1m")
            ema_cmp = (
                "Y"
                if e9 is not None and e20 is not None and e9 > e20
                else ("N" if e9 is not None and e20 is not None else "-")
            )
            long_rows.append(
                {
                    "TICKER": fr.ticker,
                    "CLOSE": fr.r1.close_1d,
                    "RES_1D": fr.r1.resistencia_1d,
                    "EMA3": fr.r1.ema3,
                    "EMA9": fr.r1.ema9,
                    "VOL_RATIO": fr.r1.vol_ratio,
                    "EMA20_15M": fr.intr.get("ema20_15m"),
                    "EMA9_1M>EMA20_1M": ema_cmp,
                }
            )
        else:
            e9, e20 = fr.intr.get("ema9_1m"), fr.intr.get("ema20_1m")
            ema_cmp_s = (
                "Y"
                if e9 is not None and e20 is not None and e9 < e20
                else ("N" if e9 is not None and e20 is not None else "-")
            )
            short_rows.append(
                {
                    "TICKER": fr.ticker,
                    "CLOSE": fr.r1.close_1d,
                    "SOP_1D": fr.r1.soporte_1d,
                    "EMA3": fr.r1.ema3,
                    "EMA9": fr.r1.ema9,
                    "VOL_RATIO": fr.r1.vol_ratio,
                    "EMA20_15M": fr.intr.get("ema20_15m"),
                    "EMA9_1M<EMA20_1M": ema_cmp_s,
                }
            )

    _print_table("=== LONG CANDIDATES", long_rows, fecha)
    _print_table("=== SHORT CANDIDATES", short_rows, fecha)

    conn = get_conn()
    try:
        persist_results(conn, fecha, finals)
        logger.info("Guardado en DuckDB: %s (tabla screener_postmarket)", DB_PATH)
    finally:
        conn.close()

    if yf_fail:
        logger.info("yfinance sin datos o incompletos (%d tickers)", len(yf_fail))
        logger.info("Muestra: %s", yf_fail[:50])
        if len(yf_fail) > 50:
            logger.info("... y %d más", len(yf_fail) - 50)


def main() -> None:
    p = argparse.ArgumentParser(description="Screener post-mercado S&P500 + Nasdaq-100")
    p.add_argument("--fecha", type=str, default=None, help="YYYY-MM-DD (default: hoy ET)")
    p.add_argument("--solo-1d", action="store_true", help="Solo filtros 1D (sin Alpaca intradía)")
    p.add_argument("--ticker", type=str, default=None, help="Override: AAPL,MSFT,...")
    p.add_argument("--min-price", type=float, default=None, help="Precio mínimo close 1D (opcional)")
    p.add_argument("--max-price", type=float, default=None, help="Precio máximo close 1D (opcional)")
    p.add_argument(
        "--min-volume",
        type=int,
        default=10_000_000,
        help="Volumen mínimo última vela 1D (default: 10_000_000)",
    )
    args = p.parse_args()
    if args.fecha:
        fecha = date.fromisoformat(args.fecha)
    else:
        fecha = datetime.now(TIMEZONE).date()
    override = None
    if args.ticker:
        override = [x.strip() for x in args.ticker.split(",") if x.strip()]
    run(
        fecha,
        solo_1d=args.solo_1d,
        ticker_override=override,
        min_price=args.min_price,
        max_price=args.max_price,
        min_volume=args.min_volume,
    )


if __name__ == "__main__":
    main()
