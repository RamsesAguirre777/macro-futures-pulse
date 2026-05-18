"""DuckDB separado para crypto screener (no backtest.duckdb)."""
from __future__ import annotations

from datetime import date
from typing import Any

import duckdb

from dc2.backtest.db import DB_PATH
from dc2.crypto_screener.compute import CryptoRow, strength_score_long, strength_score_short

CRYPTO_DB_PATH = DB_PATH.parent / "crypto_screener.duckdb"

SCREENER_CRYPTO_DAILY_DDL = """
CREATE TABLE IF NOT EXISTS screener_crypto_daily (
    fecha DATE NOT NULL,
    symbol VARCHAR NOT NULL,
    direccion VARCHAR NOT NULL,
    regime_applied VARCHAR,
    close_1d DOUBLE,
    resistencia_1d DOUBLE,
    soporte_1d DOUBLE,
    bp_long DOUBLE,
    bp_short DOUBLE,
    ema3_1d DOUBLE,
    ema9_1d DOUBLE,
    ema3_monthly DOUBLE,
    ema9_monthly DOUBLE,
    ema20_monthly DOUBLE,
    volume_usdt_24h DOUBLE,
    avg_volume_usdt_20d DOUBLE,
    close_1h DOUBLE,
    ema20_1h DOUBLE,
    ema9_15m DOUBLE,
    ema20_15m DOUBLE,
    strength_score DOUBLE,
    size_hint VARCHAR,
    created_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (fecha, symbol, direccion)
)
"""

SCREENER_CRYPTO_OPEN_DDL = """
CREATE TABLE IF NOT EXISTS screener_crypto_open_0001 (
    fecha DATE NOT NULL,
    symbol VARCHAR NOT NULL,
    direccion_scan VARCHAR NOT NULL,
    open_signal VARCHAR NOT NULL,
    first_1m_close DOUBLE,
    bp_used DOUBLE,
    gap_pct DOUBLE,
    note VARCHAR,
    created_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (fecha, symbol, direccion_scan)
)
"""


def get_crypto_conn() -> duckdb.DuckDBPyConnection:
    CRYPTO_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(CRYPTO_DB_PATH))
    conn.execute(SCREENER_CRYPTO_DAILY_DDL)
    conn.execute(SCREENER_CRYPTO_OPEN_DDL)
    return conn


def persist_daily_scan(
    conn: duckdb.DuckDBPyConnection,
    fecha: date,
    regime: str,
    longs: list[CryptoRow],
    shorts: list[CryptoRow],
    *,
    size_hint_default: str = "normal",
) -> None:
    conn.execute(
        "DELETE FROM screener_crypto_daily WHERE fecha = ?",
        [fecha],
    )
    rows: list[tuple] = []
    for r in longs:
        sc = strength_score_long(r)
        rows.append(
            (
                fecha,
                r.symbol,
                "LONG",
                regime,
                r.close_1d,
                r.resistencia_1d,
                r.soporte_1d,
                r.bp_long,
                r.bp_short,
                r.ema3_1d,
                r.ema9_1d,
                r.ema3_monthly,
                r.ema9_monthly,
                r.ema20_monthly,
                r.volume_usdt_24h,
                r.avg_volume_usdt_20d,
                r.close_1h,
                r.ema20_1h,
                r.ema9_15m,
                r.ema20_15m,
                sc,
                size_hint_default,
            )
        )
    for r in shorts:
        sc = strength_score_short(r)
        rows.append(
            (
                fecha,
                r.symbol,
                "SHORT",
                regime,
                r.close_1d,
                r.resistencia_1d,
                r.soporte_1d,
                r.bp_long,
                r.bp_short,
                r.ema3_1d,
                r.ema9_1d,
                r.ema3_monthly,
                r.ema9_monthly,
                r.ema20_monthly,
                r.volume_usdt_24h,
                r.avg_volume_usdt_20d,
                r.close_1h,
                r.ema20_1h,
                r.ema9_15m,
                r.ema20_15m,
                sc,
                size_hint_default,
            )
        )
    if not rows:
        return
    conn.executemany(
        """
        INSERT INTO screener_crypto_daily (
            fecha, symbol, direccion, regime_applied,
            close_1d, resistencia_1d, soporte_1d, bp_long, bp_short,
            ema3_1d, ema9_1d, ema3_monthly, ema9_monthly, ema20_monthly,
            volume_usdt_24h, avg_volume_usdt_20d,
            close_1h, ema20_1h, ema9_15m, ema20_15m,
            strength_score, size_hint
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def persist_open_signals(
    conn: duckdb.DuckDBPyConnection,
    fecha: date,
    records: list[dict[str, Any]],
) -> None:
    conn.execute(
        "DELETE FROM screener_crypto_open_0001 WHERE fecha = ?",
        [fecha],
    )
    if not records:
        return
    batch = [
        (
            fecha,
            rec["symbol"],
            rec["direccion_scan"],
            rec["open_signal"],
            rec.get("first_1m_close"),
            rec.get("bp_used"),
            rec.get("gap_pct"),
            rec.get("note"),
        )
        for rec in records
    ]
    conn.executemany(
        """
        INSERT INTO screener_crypto_open_0001 (
            fecha, symbol, direccion_scan, open_signal,
            first_1m_close, bp_used, gap_pct, note
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        batch,
    )


def load_daily_rows_for_open(
    conn: duckdb.DuckDBPyConnection, fecha: date
) -> list[dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT symbol, direccion, bp_long, bp_short, close_1d
        FROM screener_crypto_daily
        WHERE fecha = ?
        """,
        [fecha],
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]
