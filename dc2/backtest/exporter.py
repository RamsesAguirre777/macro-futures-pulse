"""dc2/backtest/exporter.py — Optional Excel export from backtest DuckDB."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import duckdb

try:
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    _HAS_OPENPYXL = True
except ImportError:
    _HAS_OPENPYXL = False

from dc2.backtest.db import DB_PATH

# ── Color palette ─────────────────────────────────────────────────────────────
_GREEN_DARK   = "FF1B5E20"
_GREEN_MED    = "FF2E7D32"
_GREEN_LIGHT  = "FFC8E6C9"
_RED_DARK     = "FFB71C1C"
_RED_MED      = "FFC62828"
_RED_LIGHT    = "FFFFCDD2"
_YELLOW       = "FFFFF9C4"
_GREY_HEADER  = "FF37474F"
_WHITE        = "FFFFFFFF"

_FONT_HEADER  = Font(bold=True, color="FFFFFFFF", size=10)
_FONT_BODY    = Font(size=9)
_ALIGN_CENTER = Alignment(horizontal="center", vertical="center", wrap_text=False)
_ALIGN_LEFT   = Alignment(horizontal="left",   vertical="center")


def _fill(hex_color: str) -> "PatternFill":
    return PatternFill(fill_type="solid", fgColor=hex_color)


def _header_row(ws, columns: list[str]) -> None:
    for col_idx, col_name in enumerate(columns, start=1):
        cell = ws.cell(row=1, column=col_idx, value=col_name)
        cell.font  = _FONT_HEADER
        cell.fill  = _fill(_GREY_HEADER)
        cell.alignment = _ALIGN_CENTER


def _auto_width(ws, min_w: int = 8, max_w: int = 30) -> None:
    for col in ws.columns:
        col_letter = get_column_letter(col[0].column)
        length = max(
            (len(str(cell.value or "")) for cell in col),
            default=min_w,
        )
        ws.column_dimensions[col_letter].width = min(max(length + 2, min_w), max_w)


def _apply_conditional_dashboard(ws, df_cols: list[str], data: list[list]) -> None:
    """Color cells in dashboard sheet based on field semantics."""
    col_map = {name: idx + 1 for idx, name in enumerate(df_cols)}

    for row_idx, row in enumerate(data, start=2):
        row_dict = dict(zip(df_cols, row))

        # skip → red row
        if row_dict.get("skip"):
            for col_idx in range(1, len(df_cols) + 1):
                ws.cell(row=row_idx, column=col_idx).fill = _fill(_RED_LIGHT)
            continue

        # badge_long coloring
        if "badge_long" in col_map:
            bl = row_dict.get("badge_long")
            if bl is not None:
                c = ws.cell(row=row_idx, column=col_map["badge_long"])
                if bl >= 70:
                    c.fill = _fill(_GREEN_LIGHT)
                elif bl <= 30:
                    c.fill = _fill(_RED_LIGHT)

        # gap_tipo coloring
        if "gap_tipo" in col_map:
            gt = row_dict.get("gap_tipo")
            c = ws.cell(row=row_idx, column=col_map["gap_tipo"])
            if gt == "GAP_UP":
                c.fill = _fill(_GREEN_LIGHT)
            elif gt == "GAP_DOWN":
                c.fill = _fill(_RED_LIGHT)

        # premium_label
        if "premium_label" in col_map:
            pl = row_dict.get("premium_label", "")
            c = ws.cell(row=row_idx, column=col_map["premium_label"])
            if pl and "PREMIUM" in str(pl).upper():
                c.fill   = _fill(_GREEN_LIGHT)
                c.font   = Font(bold=True, size=9)

        # bb_expande_premarket
        if "bb_expande_premarket" in col_map:
            bb = row_dict.get("bb_expande_premarket")
            c = ws.cell(row=row_idx, column=col_map["bb_expande_premarket"])
            if bb is True:
                c.fill = _fill(_YELLOW)


def _apply_conditional_resultados(ws, df_cols: list[str], data: list[list]) -> None:
    """Color cells in resultados sheet based on field semantics."""
    col_map = {name: idx + 1 for idx, name in enumerate(df_cols)}

    for row_idx, row in enumerate(data, start=2):
        row_dict = dict(zip(df_cols, row))

        # skip → red row
        if row_dict.get("skip"):
            for col_idx in range(1, len(df_cols) + 1):
                ws.cell(row=row_idx, column=col_idx).fill = _fill(_RED_LIGHT)
            continue

        # direccion coloring
        if "direccion" in col_map:
            d = row_dict.get("direccion")
            c = ws.cell(row=row_idx, column=col_map["direccion"])
            if d == "ALCISTA":
                c.fill = _fill(_GREEN_LIGHT)
            elif d == "BAJISTA":
                c.fill = _fill(_RED_LIGHT)
            elif d == "ZONA_MUERTA":
                c.fill = _fill(_YELLOW)

        # toco_int / toco_max
        for field in ("toco_int", "toco_max"):
            if field in col_map:
                v = row_dict.get(field)
                c = ws.cell(row=row_idx, column=col_map[field])
                if v is True:
                    c.fill = _fill(_GREEN_LIGHT)
                elif v is False:
                    c.fill = _fill(_RED_LIGHT)

        # cerro_bp
        if "cerro_bp" in col_map:
            v = row_dict.get("cerro_bp")
            c = ws.cell(row=row_idx, column=col_map["cerro_bp"])
            if v is True:
                c.fill = _fill(_YELLOW)

        # caution_cambio_1v3
        if "caution_cambio_1v3" in col_map:
            cc = row_dict.get("caution_cambio_1v3", "")
            c = ws.cell(row=row_idx, column=col_map["caution_cambio_1v3"])
            if cc == "escalo":
                c.fill = _fill(_GREEN_LIGHT)
            elif cc == "desaparecio":
                c.fill = _fill(_RED_LIGHT)
            elif cc == "nuevo":
                c.fill = _fill(_YELLOW)

        # c_int / c_max ratio
        if "c_int_c_max_ratio" in col_map:
            ratio = row_dict.get("c_int_c_max_ratio")
            c = ws.cell(row=row_idx, column=col_map["c_int_c_max_ratio"])
            if ratio is not None:
                if ratio >= 1.5:
                    c.fill = _fill(_GREEN_LIGHT)
                elif ratio <= 0.5:
                    c.fill = _fill(_RED_LIGHT)


def export_excel(
    output_path: Optional[str | Path] = None,
    ticker: Optional[str] = None,
    desde: Optional[str] = None,
    hasta: Optional[str] = None,
) -> Path:
    """
    Export backtest DuckDB → Excel with two sheets:
      - Dashboard   (backtest_dashboard)
      - Resultados  (backtest_resultados)

    Filters: ticker, desde (YYYY-MM-DD), hasta (YYYY-MM-DD).
    Returns the path of the generated .xlsx file.
    """
    if not _HAS_OPENPYXL:
        raise RuntimeError(
            "openpyxl not installed — run: pip install openpyxl"
        )

    conn = duckdb.connect(str(DB_PATH), read_only=True)

    def _build_query(table: str) -> tuple[str, list]:
        wheres, params = [], []
        if ticker:
            wheres.append("ticker = ?")
            params.append(ticker)
        if desde:
            wheres.append("fecha >= ?")
            params.append(desde)
        if hasta:
            wheres.append("fecha <= ?")
            params.append(hasta)
        where_clause = f"WHERE {' AND '.join(wheres)}" if wheres else ""
        return f"SELECT * FROM {table} {where_clause} ORDER BY ticker, fecha", params

    dash_sql, dash_params = _build_query("backtest_dashboard")
    res_sql,  res_params  = _build_query("backtest_resultados")

    dash_rel = conn.execute(dash_sql, dash_params)
    dash_cols = [d[0] for d in dash_rel.description]
    dash_data = dash_rel.fetchall()

    res_rel  = conn.execute(res_sql, res_params)
    res_cols = [d[0] for d in res_rel.description]
    res_data = res_rel.fetchall()

    conn.close()

    # ── Build workbook ─────────────────────────────────────────────────────
    wb = openpyxl.Workbook()
    wb.remove(wb.active)  # remove default sheet

    # ── Dashboard sheet ────────────────────────────────────────────────────
    ws_dash = wb.create_sheet("Dashboard")
    _header_row(ws_dash, dash_cols)
    for row in dash_data:
        ws_dash.append(list(row))
    _apply_conditional_dashboard(ws_dash, dash_cols, [list(r) for r in dash_data])
    ws_dash.freeze_panes = "C2"
    _auto_width(ws_dash)

    # ── Resultados sheet ───────────────────────────────────────────────────
    ws_res = wb.create_sheet("Resultados")
    _header_row(ws_res, res_cols)
    for row in res_data:
        ws_res.append(list(row))
    _apply_conditional_resultados(ws_res, res_cols, [list(r) for r in res_data])
    ws_res.freeze_panes = "C2"
    _auto_width(ws_res)

    # ── Summary sheet ──────────────────────────────────────────────────────
    ws_sum = wb.create_sheet("Resumen", 0)
    _write_summary(ws_sum, dash_data, dash_cols, res_data, res_cols)

    # ── Save ───────────────────────────────────────────────────────────────
    if output_path is None:
        suffix = f"_{ticker}" if ticker else ""
        fname  = f"backtest{suffix}.xlsx"
        output_path = DB_PATH.parent / fname
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(output_path))
    return output_path


def _write_summary(ws, dash_data, dash_cols, res_data, res_cols) -> None:
    """Write a quick stats summary sheet."""
    dc = {name: idx for idx, name in enumerate(dash_cols)}
    rc = {name: idx for idx, name in enumerate(res_cols)}

    total_days  = len(dash_data)
    skipped     = sum(1 for r in dash_data if r[dc.get("skip", -1)] if dc.get("skip") is not None else False)
    alcistas    = sum(1 for r in res_data  if r[rc.get("direccion", -1)] == "ALCISTA")
    bajistas    = sum(1 for r in res_data  if r[rc.get("direccion", -1)] == "BAJISTA")
    zona_muerta = sum(1 for r in res_data  if r[rc.get("direccion", -1)] == "ZONA_MUERTA")

    toco_int_true  = sum(1 for r in res_data if r[rc.get("toco_int", -1)] is True)
    toco_max_true  = sum(1 for r in res_data if r[rc.get("toco_max", -1)] is True)
    res_valid      = [r for r in res_data if not r[rc.get("skip", -1)]] if rc.get("skip") is not None else res_data

    toco_int_rate = (
        round(toco_int_true / len(res_valid) * 100, 1) if res_valid else 0.0
    )
    toco_max_rate = (
        round(toco_max_true / len(res_valid) * 100, 1) if res_valid else 0.0
    )

    rows = [
        ["BB Retroceso — Backtest Summary", ""],
        ["", ""],
        ["Total días",      total_days],
        ["Días skipped",    skipped],
        ["Días válidos",    total_days - skipped],
        ["", ""],
        ["Alcistas",        alcistas],
        ["Bajistas",        bajistas],
        ["Zona muerta",     zona_muerta],
        ["", ""],
        ["Tocó INT (%)",    toco_int_rate],
        ["Tocó MAX (%)",    toco_max_rate],
    ]

    for row_idx, (label, value) in enumerate(rows, start=1):
        c_label = ws.cell(row=row_idx, column=1, value=label)
        c_value = ws.cell(row=row_idx, column=2, value=value)
        if row_idx == 1:
            c_label.font = Font(bold=True, size=12)
        elif label:
            c_label.font = Font(bold=True, size=10)
            c_value.font = Font(size=10)

    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 14
