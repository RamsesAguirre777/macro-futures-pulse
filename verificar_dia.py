"""
Verifica un día: 1m Polygon, filtro 9:30-16:00 ET.

Modo manual:
  python verificar_dia.py NVDA 2026-02-27 183.06 185.24 180.89 187.42 178.71

Modo test (sin tocar CSV; requiere niveles):
  python verificar_dia.py --ticker NVDA --test 2025-10-23 --levels 183.06 185.24 180.89 187.42 178.71

Modo batch:
  python verificar_dia.py --ticker NVDA --csv ruta/archivo.csv
  python verificar_dia.py --ticker NVDA --csv ruta/archivo.csv --force
    (--force: reprocesa todas las filas; si open_930 ya existe, se conserva y se recalcula el resto)

  python verificar_dia.py --ticker INTC --sheet Hoja1 --csv ruta/archivo.xlsx --force
    (procesa solo la hoja indicada; las demás hojas quedan intactas)
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests

try:
    import openpyxl  # type: ignore
except ImportError:
    openpyxl = None

try:
    import pytz
    ET = pytz.timezone("America/New_York")
except ImportError:
    ET = None

API_KEY = "JEHzudOxWa2cjAqLi4DNGJItUBAe84Xs"

# Columnas nuevas al final del CSV (orden fijo)
NEW_CSV_COLUMNS = [
    "toco_int_dir",
    "toco_max_dir",
    "primer_movimiento",
    "minuto_toco_int",
    "minuto_toco_max",
    "toco_bp_antes_int",
]

DEAD_ZONE_THRESHOLD = 0.25


def is_regular_session(hour: int, minute: int) -> bool:
    """9:30 AM <= time <= 4:00 PM ET."""
    if hour < 9:
        return False
    if hour == 9 and minute < 30:
        return False
    if hour > 16:
        return False
    if hour == 16 and minute > 0:
        return False
    return True


def fetch_minute_bars(ticker: str, fecha: str, api_key: str) -> list:
    """Fetch 1m bars from Polygon for date. Returns list of {t, o, h, l, c}."""
    url = (
        f"https://api.polygon.io/v2/aggs/ticker/{ticker}"
        f"/range/1/minute/{fecha}/{fecha}"
        f"?adjusted=true&sort=asc&limit=50000&apiKey={api_key}"
    )
    r = requests.get(url)
    if not r.ok:
        return []
    data = r.json()
    return data.get("results") or []


def ts_to_et(ts_ms: int) -> Tuple[int, int]:
    t = pd.Timestamp(ts_ms, unit="ms")
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    t_et = t.tz_convert(ET) if ET else t
    return t_et.hour, t_et.minute


def format_minute(ts_ms: int) -> str:
    t = pd.Timestamp(ts_ms, unit="ms")
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    t_et = t.tz_convert(ET) if ET else t
    return t_et.strftime("%H:%M")


def filter_regular_bars(bars: List[dict]) -> List[dict]:
    reg = []
    for b in bars:
        h, m = ts_to_et(b["t"])
        if is_regular_session(h, m):
            reg.append(b)
    return reg


def _first_idx_touch_int_pos(
    reg: List[dict], int_pos: float, open_930: Optional[float] = None
) -> Optional[int]:
    if open_930 is not None and open_930 > int_pos:
        for i, b in enumerate(reg):
            if b["l"] <= int_pos:
                return i
        return None
    for i, b in enumerate(reg):
        if b["h"] >= int_pos:
            return i
    return None


def _first_idx_touch_int_neg(
    reg: List[dict], int_neg: float, open_930: Optional[float] = None
) -> Optional[int]:
    if open_930 is not None and open_930 < int_neg:
        for i, b in enumerate(reg):
            if b["h"] >= int_neg:
                return i
        return None
    for i, b in enumerate(reg):
        if b["l"] <= int_neg:
            return i
    return None


def _first_idx_touch_max_pos(reg: List[dict], max_pos: float) -> Optional[int]:
    for i, b in enumerate(reg):
        if b["h"] >= max_pos:
            return i
    return None


def _first_idx_touch_max_neg(reg: List[dict], max_neg: float) -> Optional[int]:
    for i, b in enumerate(reg):
        if b["l"] <= max_neg:
            return i
    return None


def _dir_from_indices(idx_pos: Optional[int], idx_neg: Optional[int]) -> str:
    if idx_pos is None and idx_neg is None:
        return "ninguno"
    if idx_pos is None:
        return "int_neg" if idx_neg is not None else "ninguno"
    if idx_neg is None:
        return "int_pos"
    if idx_pos == idx_neg:
        return "ambos"
    return "int_pos" if idx_pos < idx_neg else "int_neg"


def _max_dir_from_indices(idx_p: Optional[int], idx_n: Optional[int]) -> str:
    if idx_p is None and idx_n is None:
        return "ninguno"
    if idx_p is None:
        return "max_neg" if idx_n is not None else "ninguno"
    if idx_n is None:
        return "max_pos"
    if idx_p == idx_n:
        return "ambos"
    return "max_pos" if idx_p < idx_n else "max_neg"


def _earliest_int_idx(idx_pos: Optional[int], idx_neg: Optional[int]) -> Optional[int]:
    if idx_pos is None and idx_neg is None:
        return None
    if idx_pos is None:
        return idx_neg
    if idx_neg is None:
        return idx_pos
    return min(idx_pos, idx_neg)


def _earliest_max_idx(idx_p: Optional[int], idx_n: Optional[int]) -> Optional[int]:
    if idx_p is None and idx_n is None:
        return None
    if idx_p is None:
        return idx_n
    if idx_n is None:
        return idx_p
    return min(idx_p, idx_n)


def _primer_movimiento(
    idx_pos: Optional[int], idx_neg: Optional[int], open_930: float, int_pos: float, int_neg: float
) -> str:
    if idx_pos is None and idx_neg is None:
        return ""
    if idx_pos is None:
        return "neg"
    if idx_neg is None:
        return "pos"
    if idx_pos < idx_neg:
        return "pos"
    if idx_neg < idx_pos:
        return "neg"
    # mismo índice: ambos en la misma barra — desempate por cercanía al open
    d_pos = abs(open_930 - int_pos)
    d_neg = abs(open_930 - int_neg)
    return "pos" if d_pos <= d_neg else "neg"


def _toco_bp_antes_int(reg: List[dict], bp: float, i_first_int: Optional[int], open_930: float) -> str:
    if i_first_int is None:
        return "No"
    if abs(open_930 - bp) < DEAD_ZONE_THRESHOLD:
        return "DEAD_ZONE"
    for i in range(i_first_int):
        b = reg[i]
        if b["l"] <= bp <= b["h"]:
            return "Sí"
    return "No"


def _dist_pct_str(open_930: float, bp: float) -> str:
    if bp is None or (isinstance(bp, float) and (bp != bp or bp == 0)):
        return ""
    pct = 100.0 * (open_930 - bp) / bp
    return f"{pct:+.2f}"


def analyze_regular_session(
    reg: List[dict],
    bp: float,
    int_pos: float,
    int_neg: float,
    max_pos: float,
    max_neg: float,
    open_930_preserve: Optional[float] = None,
    cerro_bp_tol: float = 0.50,
) -> Dict[str, Any]:
    """Métricas para sesión regular ya filtrada (no vacía).

    Si open_930_preserve no es None, se usa como open_930 (CSV existente)
    para dist_pct, c_int, c_max, primer_movimiento, DEAD_ZONE, etc.;
    la primera barra de Polygon solo alimenta toques de nivel y close.
    """
    if open_930_preserve is not None:
        open_930 = round(float(open_930_preserve), 2)
    else:
        open_930 = round(float(reg[0]["o"]), 2)
    close = float(reg[-1]["c"])
    day_high = max(b["h"] for b in reg)
    day_low = min(b["l"] for b in reg)

    idx_ip = _first_idx_touch_int_pos(reg, int_pos, open_930)
    idx_in = _first_idx_touch_int_neg(reg, int_neg, open_930)
    idx_mp = _first_idx_touch_max_pos(reg, max_pos)
    idx_mn = _first_idx_touch_max_neg(reg, max_neg)

    toco_int_dir = _dir_from_indices(idx_ip, idx_in)
    toco_max_dir = _max_dir_from_indices(idx_mp, idx_mn)

    toco_int = "Sí" if toco_int_dir != "ninguno" else "No"
    toco_max = "Sí" if toco_max_dir != "ninguno" else "No"

    ei = _earliest_int_idx(idx_ip, idx_in)
    em = _earliest_max_idx(idx_mp, idx_mn)

    minuto_toco_int = format_minute(reg[ei]["t"]) if ei is not None else ""
    minuto_toco_max = format_minute(reg[em]["t"]) if em is not None else ""

    primer_mov = _primer_movimiento(idx_ip, idx_in, open_930, int_pos, int_neg)

    toco_bp_antes = _toco_bp_antes_int(reg, bp, ei, open_930)

    cerro_bp = "Sí" if abs(close - bp) <= cerro_bp_tol else "No"

    # c_int: distancia $ al nivel int del primer toque (según primer_mov)
    if ei is None:
        c_int_val = ""
    else:
        if primer_mov == "pos":
            lvl = int_pos
        else:
            lvl = int_neg
        c_int_val = round(abs(open_930 - lvl), 2)

    # c_max: mismo con max según cuál tocó primero
    if em is None:
        c_max_val = ""
    else:
        if idx_mp is None:
            lvl_m = max_neg
        elif idx_mn is None:
            lvl_m = max_pos
        elif idx_mp < idx_mn:
            lvl_m = max_pos
        elif idx_mn < idx_mp:
            lvl_m = max_neg
        else:
            d_p = abs(open_930 - max_pos)
            d_n = abs(open_930 - max_neg)
            lvl_m = max_pos if d_p <= d_n else max_neg
        c_max_val = round(abs(open_930 - lvl_m), 2)

    dist_pct = _dist_pct_str(open_930, bp)

    return {
        "open_930": open_930,
        "day_high": round(day_high, 2),
        "day_low": round(day_low, 2),
        "close": round(close, 2),
        "toco_int_dir": toco_int_dir,
        "toco_max_dir": toco_max_dir,
        "toco_int": toco_int,
        "toco_max": toco_max,
        "primer_movimiento": primer_mov,
        "minuto_toco_int": minuto_toco_int,
        "minuto_toco_max": minuto_toco_max,
        "toco_bp_antes_int": toco_bp_antes,
        "cerro_bp": cerro_bp,
        "c_int": c_int_val,
        "c_max": c_max_val,
        "dist_pct": dist_pct,
    }


def fecha_to_iso_for_api(fecha_raw: Any) -> Optional[str]:
    """Convierte celda fecha a YYYY-MM-DD solo para Polygon; None si inválida."""
    if fecha_raw is None or (isinstance(fecha_raw, float) and pd.isna(fecha_raw)):
        return None
    s = str(fecha_raw).strip()
    if not s or s.lower() == "nan":
        return None
    ts = pd.to_datetime(s, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.strftime("%Y-%m-%d")


def row_bp(row: pd.Series) -> Optional[float]:
    """sistema_bp si válido; si no bp_imagen."""
    for col in ("sistema_bp", "bp_imagen"):
        if col not in row.index:
            continue
        v = row[col]
        if v is None or (isinstance(v, float) and pd.isna(v)):
            continue
        try:
            f = float(v)
            if f == f:
                return f
        except (TypeError, ValueError):
            continue
    return None


def row_float(row: pd.Series, col: str) -> Optional[float]:
    if col not in row.index:
        return None
    v = row[col]
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def open_930_is_complete(val: Any) -> bool:
    if val is None:
        return False
    if isinstance(val, float) and pd.isna(val):
        return False
    s = str(val).strip()
    if not s or s.upper() == "SIN_DATOS":
        return False
    try:
        float(s)
        return True
    except ValueError:
        return False


def parse_existing_open_930(val: Any) -> Optional[float]:
    """Float redondeado si open_930 es numérico válido; si no, None."""
    if not open_930_is_complete(val):
        return None
    try:
        return round(float(str(val).strip()), 2)
    except (TypeError, ValueError):
        return None


def sin_datos_row() -> Dict[str, Any]:
    return {
        "open_930": "SIN_DATOS",
        "toco_int_dir": "SIN_DATOS",
        "toco_max_dir": "SIN_DATOS",
        "toco_int": "SIN_DATOS",
        "toco_max": "SIN_DATOS",
        "primer_movimiento": "SIN_DATOS",
        "minuto_toco_int": "SIN_DATOS",
        "minuto_toco_max": "SIN_DATOS",
        "toco_bp_antes_int": "SIN_DATOS",
        "cerro_bp": "SIN_DATOS",
        "c_int": "SIN_DATOS",
        "c_max": "SIN_DATOS",
        "dist_pct": "SIN_DATOS",
    }


def print_verification_console(
    ticker: str, fecha: str, bp: float, int_pos: float, int_neg: float, max_pos: float, max_neg: float, m: Dict[str, Any]
) -> None:
    print()
    print(f"Verificación: {ticker} {fecha}")
    print("=" * 50)
    print(f"open_930:          {m.get('open_930')}")
    print(f"day_high:          {m.get('day_high', '—')}")
    print(f"day_low:           {m.get('day_low', '—')}")
    print(f"close:             {m.get('close', '—')}")
    print()
    print("Campos calculados:")
    for k in (
        "toco_int_dir",
        "toco_max_dir",
        "toco_int",
        "toco_max",
        "primer_movimiento",
        "minuto_toco_int",
        "minuto_toco_max",
        "toco_bp_antes_int",
        "cerro_bp",
        "c_int",
        "c_max",
        "dist_pct",
    ):
        print(f"  {k}: {m.get(k, '—')}")
    print()
    print("Niveles de referencia:")
    print(f"  BP: {bp:.2f}  INT_POS {int_pos:.2f}  INT_NEG {int_neg:.2f}")
    print(f"  MAX_POS {max_pos:.2f}  MAX_NEG {max_neg:.2f}")
    print()


def assign_metrics_to_row(df: pd.DataFrame, i: int, metrics: Dict[str, Any]) -> None:
    for k, v in metrics.items():
        if k in ("day_high", "day_low", "close"):
            continue
        if k not in df.columns:
            df[k] = ""
        df.at[i, k] = v


def run_manual(
    api_key: str,
    ticker: str,
    fecha: str,
    bp: float,
    int_pos: float,
    int_neg: float,
    max_pos: float,
    max_neg: float,
) -> None:
    bars = fetch_minute_bars(ticker, fecha, api_key)
    if not bars:
        print("Sin datos para esa fecha.")
        sys.exit(1)
    reg = filter_regular_bars(bars)
    if not reg:
        print("Sin velas en horario regular (9:30-16:00 ET).")
        sys.exit(1)
    m = analyze_regular_session(reg, bp, int_pos, int_neg, max_pos, max_neg)
    print_verification_console(ticker, fecha, bp, int_pos, int_neg, max_pos, max_neg, m)


def run_test(
    api_key: str,
    ticker: str,
    fecha: str,
    bp: float,
    int_pos: float,
    int_neg: float,
    max_pos: float,
    max_neg: float,
) -> None:
    fecha_iso = fecha_to_iso_for_api(fecha)
    if not fecha_iso:
        print("Fecha inválida.")
        sys.exit(1)
    bars = fetch_minute_bars(ticker, fecha_iso, api_key)
    if not bars:
        print("Sin datos Polygon para esa fecha (test).")
        sys.exit(1)
    reg = filter_regular_bars(bars)
    if not reg:
        print("Sin velas regulares (test).")
        sys.exit(1)
    m = analyze_regular_session(reg, bp, int_pos, int_neg, max_pos, max_neg)
    print("=== MODO TEST (CSV no modificado) ===")
    print_verification_console(ticker, fecha_iso, bp, int_pos, int_neg, max_pos, max_neg, m)


def _batch_ext(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def _read_batch_dataframe(path: str, sheet_name: Optional[str]) -> Tuple[pd.DataFrame, List[str]]:
    ext = _batch_ext(path)
    if ext == ".csv":
        df = pd.read_csv(path)
        return df, list(df.columns)

    if ext in (".xlsx", ".xlsm", ".xls"):
        if not sheet_name:
            raise RuntimeError("Para .xlsx/.xlsm/.xls debes indicar --sheet (nombre de la hoja).")
        if openpyxl is None:
            raise RuntimeError("Dependencia openpyxl requerida para .xlsx. Ejecuta: pip install openpyxl")
        df = pd.read_excel(path, sheet_name=sheet_name, dtype=str)
        # Elimina columnas "Unnamed: ..." que a veces aparecen por hojas con celdas vacías
        df = df.loc[:, ~df.columns.isna()]
        return df, list(df.columns)

    raise RuntimeError(f"Extensión no soportada: {ext} (solo .csv o .xlsx)")


def _write_batch_dataframe(
    path: str,
    sheet_name: str,
    df: pd.DataFrame,
    out_cols: List[str],
) -> None:
    if openpyxl is None:
        raise RuntimeError("Dependencia openpyxl requerida para .xlsx. Ejecuta: pip install openpyxl")

    wb = openpyxl.load_workbook(path)
    if sheet_name not in wb.sheetnames:
        raise RuntimeError(f"No existe la hoja '{sheet_name}' en {path}. Hojas: {wb.sheetnames}")
    ws = wb[sheet_name]

    # Mantiene intactas las demás hojas; sobrescribe SOLO la hoja procesada
    if ws.max_row >= 1:
        ws.delete_rows(1, ws.max_row)

    # Normaliza valores para evitar NaN en Excel
    def norm(v: Any) -> Any:
        if v is None:
            return ""
        try:
            if isinstance(v, float) and pd.isna(v):
                return ""
        except Exception:
            pass
        return v

    df_out = df.reindex(columns=out_cols)

    for j, col in enumerate(out_cols, start=1):
        ws.cell(row=1, column=j, value=col)

    for i in range(len(df_out)):
        for j, col in enumerate(out_cols, start=1):
            ws.cell(row=i + 2, column=j, value=norm(df_out.iloc[i][col]))

    wb.save(path)


def run_batch(
    api_key: str,
    ticker: str,
    csv_path: str,
    force: bool = False,
    sheet_name: Optional[str] = None,
) -> None:
    path = os.path.abspath(csv_path)
    if not os.path.isfile(path):
        print(f"No existe el archivo: {path}")
        sys.exit(1)

    ext = _batch_ext(path)
    df, original_columns = _read_batch_dataframe(path, sheet_name=sheet_name)

    processed = skipped = sin_datos = errors = 0

    for i in range(len(df)):
        row = df.iloc[i]
        fecha_disp = row["fecha"] if "fecha" in df.columns else ""
        fecha_iso = fecha_to_iso_for_api(row["fecha"]) if "fecha" in df.columns else None

        if not force and open_930_is_complete(row.get("open_930")):
            print(f"Procesando {fecha_disp}... [skip] open_930 ya completo")
            skipped += 1
            continue

        preserve_open = parse_existing_open_930(row.get("open_930")) if force else None

        if not fecha_iso:
            print(f"Procesando {fecha_disp}... [ERR] fecha invalida")
            errors += 1
            continue

        bp = row_bp(row)
        int_pos = row_float(row, "int_pos")
        int_neg = row_float(row, "int_neg")
        max_pos = row_float(row, "max_pos")
        max_neg = row_float(row, "max_neg")

        if None in (bp, int_pos, int_neg, max_pos, max_neg):
            print(f"Procesando {fecha_disp}... [ERR] faltan BP o niveles")
            errors += 1
            continue

        try:
            bars = fetch_minute_bars(ticker, fecha_iso, api_key)
            if not bars:
                print(f"Procesando {fecha_disp}... SIN_DATOS")
                sd = sin_datos_row()
                if preserve_open is not None:
                    sd["open_930"] = preserve_open
                for c in NEW_CSV_COLUMNS:
                    if c not in df.columns:
                        df[c] = ""
                assign_metrics_to_row(df, i, sd)
                sin_datos += 1
                continue

            reg = filter_regular_bars(bars)
            if not reg:
                print(f"Procesando {fecha_disp}... SIN_DATOS")
                sd = sin_datos_row()
                if preserve_open is not None:
                    sd["open_930"] = preserve_open
                for c in NEW_CSV_COLUMNS:
                    if c not in df.columns:
                        df[c] = ""
                assign_metrics_to_row(df, i, sd)
                sin_datos += 1
                continue

            m = analyze_regular_session(
                reg, bp, int_pos, int_neg, max_pos, max_neg, open_930_preserve=preserve_open
            )
            for c in NEW_CSV_COLUMNS:
                if c not in df.columns:
                    df[c] = ""
            assign_metrics_to_row(df, i, m)
            if preserve_open is not None:
                print(f"Procesando {fecha_disp}... [OK] (open_930 preservado)")
            else:
                print(f"Procesando {fecha_disp}... [OK]")
            processed += 1
        except Exception as e:
            print(f"Procesando {fecha_disp}... [ERR] {e}")
            errors += 1

    # Orden columnas: originales en orden, luego nuevas al final (solo las que falten al final)
    tail = [c for c in NEW_CSV_COLUMNS if c in df.columns and c not in original_columns]
    out_cols = [c for c in original_columns if c in df.columns] + tail
    # columnas que existían ni en original ni en tail (p.ej. añadidas manualmente) — mantener al final
    rest = [c for c in df.columns if c not in out_cols]
    out_cols = out_cols + rest

    if ext == ".csv":
        df.to_csv(path, index=False, columns=out_cols)
    else:
        if not sheet_name:
            raise RuntimeError("Para .xlsx/.xlsm/.xls debes indicar --sheet.")
        _write_batch_dataframe(path, sheet_name=sheet_name, df=df, out_cols=out_cols)
    print()
    print("Resumen batch:")
    if force:
        print("  Modo: --force (todas las filas; open_930 existente preservado cuando aplica)")
    print(f"  Procesadas OK: {processed}")
    print(f"  Saltadas (open_930 ya completo): {skipped}")
    print(f"  Sin datos Polygon / sin velas RTH: {sin_datos}")
    print(f"  Errores / filas inválidas: {errors}")
    print(f"  CSV guardado: {path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verifica niveles con Polygon 1m (manual, --test o batch --csv)"
    )
    parser.add_argument("--csv", default=None, help="Ruta CSV/XLSX modo batch (requiere --ticker)")
    parser.add_argument(
        "--sheet",
        default=None,
        help="Nombre de la hoja (obligatorio si --csv es .xlsx/.xlsm/.xls)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Batch: reprocesar todas las filas; no sobrescribir open_930 si ya tiene valor numérico",
    )
    parser.add_argument("--ticker", default=None, help="Ticker (obligatorio con --csv o --test)")
    parser.add_argument("--test", metavar="FECHA", default=None, help="Validación un día; no escribe CSV")
    parser.add_argument(
        "--levels",
        nargs=5,
        type=float,
        metavar=("BP", "INT_POS", "INT_NEG", "MAX_POS", "MAX_NEG"),
        default=None,
        help="Requerido con --test: bp int_pos int_neg max_pos max_neg",
    )
    parser.add_argument("--api-key", default=None, help="Polygon API key")
    parser.add_argument(
        "manual_args",
        nargs="*",
        help="Modo manual: ticker fecha bp int_pos int_neg max_pos max_neg",
    )

    args = parser.parse_args()
    api_key = args.api_key or os.environ.get("POLYGON_API_KEY", API_KEY)

    # Modo batch
    if args.csv:
        if not args.ticker:
            print("Modo batch: indica --ticker NVDA (u otro).")
            sys.exit(2)
        ext = os.path.splitext(args.csv)[1].lower()
        if ext in (".xlsx", ".xlsm", ".xls") and not args.sheet:
            print("Para modo batch con Excel debes indicar --sheet (nombre de la hoja).")
            sys.exit(2)
        run_batch(
            api_key,
            args.ticker.upper(),
            args.csv,
            force=args.force,
            sheet_name=args.sheet,
        )
        return

    # Modo test
    if args.test is not None:
        if not args.ticker:
            print("Modo --test: indica --ticker.")
            sys.exit(2)
        if args.levels is None or len(args.levels) != 5:
            print(
                "Modo --test: añade --levels BP INT_POS INT_NEG MAX_POS MAX_NEG\n"
                "Ej: python verificar_dia.py --ticker NVDA --test 2025-10-23 "
                "--levels 183.06 185.24 180.89 187.42 178.71"
            )
            sys.exit(2)
        bp, int_pos, int_neg, max_pos, max_neg = args.levels
        run_test(api_key, args.ticker.upper(), args.test, bp, int_pos, int_neg, max_pos, max_neg)
        return

    # Modo manual (7 argumentos posicionales)
    if len(args.manual_args) != 7:
        parser.print_help()
        print("\nModo manual requiere 7 argumentos: ticker fecha bp int_pos int_neg max_pos max_neg")
        sys.exit(2)

    ticker, fecha, bp_s, ip_s, in_s, mp_s, mn_s = args.manual_args
    try:
        bp = float(bp_s)
        int_pos = float(ip_s)
        int_neg = float(in_s)
        max_pos = float(mp_s)
        max_neg = float(mn_s)
    except ValueError:
        print("Niveles numéricos inválidos.")
        sys.exit(2)

    fecha_iso = fecha_to_iso_for_api(fecha)
    if not fecha_iso:
        print("Fecha inválida.")
        sys.exit(2)

    run_manual(api_key, ticker.upper(), fecha_iso, bp, int_pos, int_neg, max_pos, max_neg)


if __name__ == "__main__":
    main()
