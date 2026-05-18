"""Modo backtest dc2 — procesa parquets históricos, escribe DuckDB."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
from pandas.tseries.offsets import BDay

from dc2.constants import TIMEZONE, TF_LIST
from dc2.backtest.db import DB_PATH, get_conn, upsert_dashboard, upsert_resultados

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent.parent.parent / "data" / "processed"


def _df_to_records(df: pd.DataFrame) -> list[dict]:
    """DF(index=t) -> list[dict] con llave 't'."""
    if df is None or df.empty:
        return []
    out = df.reset_index()
    cols = [c for c in ["t", "o", "h", "l", "c", "v"] if c in out.columns]
    return out[cols].to_dict("records")


def _slice_df_to_records(df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> list[dict]:
    """Slice por índice y devuelve list[dict] {t,o,h,l,c,v}."""
    if df is None or df.empty:
        return []
    sliced = df.loc[start:end]
    return _df_to_records(sliced)


def _load_parquet_as_bars(ticker: str, tf: str) -> list:
    """Carga parquet y lo convierte a lista de dicts {t,o,h,l,c,v}.

    Nota: Para backtest de alto volumen, usa `_load_parquet_as_df`.
    """
    path = _DATA_DIR / f"{ticker}_{tf}.parquet"
    if not path.exists():
        return []
    df = pd.read_parquet(path)
    df.columns = [str(c).lower() for c in df.columns]
    if "timestamp" not in df.columns:
        df = df.reset_index()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    if df["timestamp"].dt.tz is None:
        df["timestamp"] = df["timestamp"].dt.tz_localize("UTC")
    df["timestamp"] = df["timestamp"].dt.tz_convert(TIMEZONE)
    col_map = {}
    for long_name, short_name in [
        ("open", "o"),
        ("high", "h"),
        ("low", "l"),
        ("close", "c"),
        ("volume", "v"),
    ]:
        if long_name in df.columns and short_name not in df.columns:
            col_map[long_name] = short_name
    if col_map:
        df = df.rename(columns=col_map)
    for col in ["o", "h", "l", "c", "v"]:
        if col not in df.columns:
            df[col] = 0.0
        else:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    records = df[["timestamp", "o", "h", "l", "c", "v"]].copy()
    records = records.rename(columns={"timestamp": "t"})
    return records.to_dict("records")


def _load_parquet_as_df(ticker: str, tf: str) -> pd.DataFrame:
    """Carga parquet y lo convierte a DataFrame indexado por timestamp ET (tz-aware, sorted).

    Retorna DF con índice `t` (DatetimeIndex) y columnas `o,h,l,c,v` numéricas.
    """
    path = _DATA_DIR / f"{ticker}_{tf}.parquet"
    if not path.exists():
        return pd.DataFrame()

    df = pd.read_parquet(path)
    df.columns = [str(c).lower() for c in df.columns]
    if "timestamp" not in df.columns:
        df = df.reset_index()

    ts = pd.to_datetime(df["timestamp"])
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize("UTC")
    ts = ts.dt.tz_convert(TIMEZONE)
    df = df.drop(columns=["timestamp"], errors="ignore")
    df.insert(0, "t", ts)

    col_map: dict[str, str] = {}
    for long_name, short_name in [
        ("open", "o"),
        ("high", "h"),
        ("low", "l"),
        ("close", "c"),
        ("volume", "v"),
    ]:
        if long_name in df.columns and short_name not in df.columns:
            col_map[long_name] = short_name
    if col_map:
        df = df.rename(columns=col_map)

    for col in ["o", "h", "l", "c", "v"]:
        if col not in df.columns:
            df[col] = 0.0
        else:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    df = df[["t", "o", "h", "l", "c", "v"]].copy()
    df = df.sort_values("t")
    df = df.set_index("t")
    df.index.name = "t"
    return df


async def run_mode_backtest(
    tickers_to_run: list,
    desde: str,
    hasta: str,
    excel: bool = False,
) -> None:
    """Orquestador principal del modo backtest."""
    # Import diferido — process_day_bt se crea en Fase 3
    from dc2.backtest.process_day import process_day_bt

    conn = get_conn()
    try:
        for ticker in tickers_to_run:
            logger.info("=== BACKTEST %s | %s → %s ===", ticker, desde, hasta)

            # Cargar todos los TFs desde parquets
            all_tf: dict[str, pd.DataFrame] = {}
            missing = []
            for tf in TF_LIST:
                all_tf[tf] = _load_parquet_as_df(ticker, tf)
                if all_tf[tf].empty:
                    missing.append(tf)
            if missing:
                logger.warning("%s: parquets faltantes: %s", ticker, missing)

            daily_bars = _load_parquet_as_df(ticker, "1d")
            if daily_bars.empty:
                logger.error("%s: sin parquet 1d — skipping", ticker)
                continue
            daily_bars_records = _df_to_records(daily_bars)

            # Construir lista de días a procesar
            end_ts = pd.Timestamp(hasta, tz=TIMEZONE)
            start_ts = pd.Timestamp(desde, tz=TIMEZONE)
            bdays = pd.bdate_range(start=start_ts, end=end_ts, freq="B")
            trading_days: list[date] = [t.date() for t in bdays]

            ok = 0
            skip_n = 0

            total_days = len(trading_days)
            dash_batch = []
            res_batch = []

            for i, D in enumerate(trading_days, 1):
                try:
                    # Mantener optimización: slicing O(log n + k) por TF y día,
                    # pero handoff a process_day_bt como list[dict] (compat).
                    d0 = pd.Timestamp(D, tz=TIMEZONE).normalize()
                    # Lookback: intradía ~14d bastaba; 1d necesita ≥19 sesiones previas para
                    # get_slice_rth_until_930 (caution_real_930 BB 1D). 45d cal. ≈ 32 sesiones.
                    lb_start = d0 - pd.Timedelta(days=45)
                    # Incluir hasta cierre RTH del día para análisis (16:00).
                    day_end = d0 + pd.Timedelta(hours=16)

                    all_tf_day: dict[str, list] = {
                        tf: _slice_df_to_records(all_tf[tf], lb_start, day_end)
                        for tf in TF_LIST
                    }
                    dash, res = process_day_bt(
                        ticker=ticker,
                        fecha=D,
                        all_tf=all_tf_day,
                        daily_bars=daily_bars_records,
                    )
                    dash["ticker"] = ticker
                    dash["fecha"] = D
                    res["ticker"] = ticker
                    res["fecha"] = D

                    dash_batch.append(dash)
                    res_batch.append(res)

                    if res.get("skip"):
                        skip_n += 1
                    else:
                        ok += 1

                    if len(dash_batch) >= 100:
                        for d in dash_batch:
                            upsert_dashboard(conn, d)
                        for r in res_batch:
                            upsert_resultados(conn, r)
                        dash_batch.clear()
                        res_batch.clear()

                except Exception as exc:  # noqa: BLE001
                    logger.error("%s %s: %s", ticker, D, exc)

                if i % 50 == 0 or i == total_days:
                    print(
                        f"  {ticker}: {i}/{total_days} dias procesados ({D})",
                        flush=True,
                    )

            for d in dash_batch:
                upsert_dashboard(conn, d)
            for r in res_batch:
                upsert_resultados(conn, r)

            logger.info(
                "%s completado: %d días OK | %d skips | total %d",
                ticker,
                ok,
                skip_n,
                ok + skip_n,
            )
    finally:
        conn.close()
    logger.info("DuckDB guardado en: %s", str(DB_PATH))

    if excel:
        from dc2.backtest.exporter import export_excel
        ticker_filter = tickers_to_run[0] if len(tickers_to_run) == 1 else None
        out = export_excel(ticker=ticker_filter, desde=desde, hasta=hasta)
        logger.info("Excel exportado: %s", out)

