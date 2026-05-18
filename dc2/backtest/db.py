"""DuckDB storage para modo backtest dc2."""
from __future__ import annotations

from pathlib import Path

import duckdb

DB_PATH = Path.home() / "AppData" / "Local" / "bb_retroceso" / "backtest.duckdb"

SCHEMA_DASHBOARD = """
CREATE TABLE IF NOT EXISTS backtest_dashboard (
    ticker        VARCHAR NOT NULL,
    fecha         DATE    NOT NULL,
    -- premarket basics
    gap_tipo      VARCHAR,
    gap_mag       DOUBLE,
    prev_day_high DOUBLE,
    prev_day_low  DOUBLE,
    prev_day_close DOUBLE,
    prev_day_change DOUBLE,
    prev_day_open DOUBLE,
    pm_high       DOUBLE,
    pm_low        DOUBLE,
    prev_prev_close DOUBLE,
    prev_prev_high  DOUBLE,
    prev_prev_low   DOUBLE,
    -- rangos
    rango_d1      DOUBLE,
    rango_d2      DOUBLE,
    rango_d3      DOUBLE,
    avg_3d        DOUBLE,
    rango_raw     DOUBLE,
    rango_usado   DOUBLE,
    -- BP y targets
    bp            DOUBLE,
    int_pos       DOUBLE,
    int_neg       DOUBLE,
    max_pos       DOUBLE,
    max_neg       DOUBLE,
    -- BB raw values
    pre_mkt_bbt_1h      DOUBLE,
    pre_mkt_bbb_1h      DOUBLE,
    market_close_bbt_1h DOUBLE,
    market_close_bbb_1h DOUBLE,
    bbt_5m        DOUBLE,
    bbb_5m        DOUBLE,
    bbt_clf_5m    DOUBLE,
    bbb_clf_5m    DOUBLE,
    bb_expande_premarket BOOLEAN,
    -- EMAs
    ema20_pre     DOUBLE,
    ema50_pre     DOUBLE,
    ema200_pre    DOUBLE,
    ema_align     VARCHAR,
    ema_sr        VARCHAR,
    -- conviction block
    ec_score      DOUBLE,
    ec_accion     VARCHAR,
    ec_size       DOUBLE,
    -- dc2 specifics
    badge_long    DOUBLE,
    rango_pm      VARCHAR,
    rango_pm_pct  DOUBLE,
    rango_prev_day      VARCHAR,
    rango_prev_day_pct  DOUBLE,
    signals_3_9   VARCHAR,
    caution_nota  VARCHAR,
    n_cautiones   INTEGER,
    skip          BOOLEAN,
    skip_reason   VARCHAR,
    prev_day_hint VARCHAR,
    premium_label VARCHAR,
    premium_score DOUBLE,
    PRIMARY KEY (ticker, fecha)
)
"""

SCHEMA_RESULTADOS = """
CREATE TABLE IF NOT EXISTS backtest_resultados (
    ticker        VARCHAR NOT NULL,
    fecha         DATE    NOT NULL,
    -- open
    bp            DOUBLE,
    open_930      DOUBLE,
    dist          DOUBLE,
    dist_pct      DOUBLE,
    direccion     VARCHAR,
    open_zone     VARCHAR,
    open_pct_en_rango DOUBLE,
    -- CLV
    prev_clv      VARCHAR,
    or_clv        VARCHAR,
    -- estructura velas
    estructura_prev_day         VARCHAR,
    estructura_vs_bp            VARCHAR,
    estructura_premarket        VARCHAR,
    estructura_premarket_vs_bp  VARCHAR,
    pct_en_vela_pre             DOUBLE,
    zona_precio_en_vela_pre     VARCHAR,
    pct_en_vela_d1              DOUBLE,
    zona_precio_en_vela_d1      VARCHAR,
    resistencia_pre  DOUBLE,
    correccion_pre   DOUBLE,
    soporte_pre      DOUBLE,
    resistencia_d1   DOUBLE,
    correccion_d1    DOUBLE,
    soporte_d1       DOUBLE,
    -- c_int / c_max
    c_int             DOUBLE,
    c_max             DOUBLE,
    c_int_c_max_ratio DOUBLE,
    -- intraday summary
    day_change    DOUBLE,
    day_high      DOUBLE,
    day_low       DOUBLE,
    -- toco
    toco_int          BOOLEAN,
    toco_int_dir      VARCHAR,
    minuto_toco_int   INTEGER,
    toco_max          BOOLEAN,
    toco_max_dir      VARCHAR,
    minuto_toco_max   INTEGER,
    toco_bp_antes_int BOOLEAN,
    primer_movimiento VARCHAR,
    cerro_bp          BOOLEAN,
    -- extras
    int_contrario_tocado    BOOLEAN,
    minuto_int_contrario    INTEGER,
    precio_tras_bp_min      DOUBLE,
    precio_tras_bp_max      DOUBLE,
    tramo_bp_a_int_minutos  INTEGER,
    rebote_post_int_cents   DOUBLE,
    segundo_toque_int       BOOLEAN,
    retrace_antes_int       DOUBLE,
    -- caution intraday
    caution_real_930     VARCHAR,
    caution_cambio_1v3   VARCHAR,
    -- skip
    skip        BOOLEAN,
    razon_skip  VARCHAR,
    PRIMARY KEY (ticker, fecha)
)
"""


def get_conn() -> duckdb.DuckDBPyConnection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Si el archivo existe y está bloqueado, borrarlo y recrear
    try:
        conn = duckdb.connect(str(DB_PATH))
    except duckdb.IOException:
        import os

        try:
            os.remove(str(DB_PATH))
        except OSError:
            pass
        conn = duckdb.connect(str(DB_PATH))
    conn.execute(SCHEMA_DASHBOARD)
    conn.execute(SCHEMA_RESULTADOS)

    # Backward-compatible schema upgrade (para DBs ya creadas).
    existing_cols_resultados = {
        row[0]
        for row in conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'backtest_resultados'
            """
        ).fetchall()
    }
    upgrades_resultados: dict[str, str] = {
        "prev_clv": "VARCHAR",
        "or_clv": "VARCHAR",
        "retrace_antes_int": "DOUBLE",
    }
    for col, col_type in upgrades_resultados.items():
        if col not in existing_cols_resultados:
            try:
                conn.execute(
                    f"ALTER TABLE backtest_resultados ADD COLUMN {col} {col_type}"
                )
            except Exception:
                # La columna puede ya existir si otro proceso agregó el schema.
                # No es fatal para el backtest.
                pass

    existing_cols_dashboard = {
        row[0]
        for row in conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'backtest_dashboard'
            """
        ).fetchall()
    }
    upgrades_dashboard: dict[str, str] = {
        "pm_high": "DOUBLE",
        "pm_low": "DOUBLE",
    }
    for col, col_type in upgrades_dashboard.items():
        if col not in existing_cols_dashboard:
            try:
                conn.execute(
                    f"ALTER TABLE backtest_dashboard ADD COLUMN {col} {col_type}"
                )
            except Exception:
                pass
    return conn


def upsert_dashboard(conn: duckdb.DuckDBPyConnection, row: dict) -> None:
    """INSERT OR REPLACE en backtest_dashboard."""
    conn.execute(
        "DELETE FROM backtest_dashboard WHERE ticker = ? AND fecha = ?",
        [row["ticker"], row["fecha"]],
    )
    cols = list(row.keys())
    placeholders = ", ".join(["?"] * len(cols))
    col_names = ", ".join(cols)
    conn.execute(
        f"INSERT INTO backtest_dashboard ({col_names}) VALUES ({placeholders})",
        [row[c] for c in cols],
    )


def upsert_resultados(conn: duckdb.DuckDBPyConnection, row: dict) -> None:
    """INSERT OR REPLACE en backtest_resultados."""
    conn.execute(
        "DELETE FROM backtest_resultados WHERE ticker = ? AND fecha = ?",
        [row["ticker"], row["fecha"]],
    )
    cols = list(row.keys())
    placeholders = ", ".join(["?"] * len(cols))
    col_names = ", ".join(cols)
    conn.execute(
        f"INSERT INTO backtest_resultados ({col_names}) VALUES ({placeholders})",
        [row[c] for c in cols],
    )

