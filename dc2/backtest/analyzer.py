from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
import logging

import pandas as pd

from dc2.constants import TIMEZONE

logger = logging.getLogger(__name__)


def _clasificar_vela(
    high: float,
    low: float,
    close: float,
    open_p: float,
    prev_high: float,
    prev_low: float,
) -> str:
    """
    Clasifica la vela respecto a la anterior usando HH/HL/LH/LL + color.
    7 categorías:
      alcista_fuerte   → HH+HL + verde (close >= open)
      alcista_debil    → HH+HL + roja  (close < open)
      bajista_fuerte   → LH+LL + roja
      bajista_debil    → LH+LL + verde
      outside_alcista  → HH+LL (engulfs) + verde
      outside_bajista  → HH+LL (engulfs) + roja
      inside_day       → LH+HL (contenida dentro)
      neutral          → cualquier otra combinación (highs o lows iguales)
    """
    color_verde = close >= open_p
    es_hh = high > prev_high
    es_hl = low > prev_low
    es_lh = high < prev_high
    es_ll = low < prev_low

    if es_hh and es_hl:
        return "alcista_fuerte" if color_verde else "alcista_debil"
    elif es_lh and es_ll:
        return "bajista_fuerte" if not color_verde else "bajista_debil"
    elif es_hh and es_ll:
        return "outside_alcista" if color_verde else "outside_bajista"
    elif es_lh and es_hl:
        return "inside_day"
    else:
        return "neutral"


def _zona_en_vela(precio: float, o: float, h: float, l: float, c: float) -> Dict[str, Any]:
    """
    Retorna {"zona": str, "pct": float}.
    Zonas: zona_alta, media_alta, media_baja, zona_baja
    """
    # Nota: lógica tomada de mi_backtesting_nvda.py (usa high/low).
    high = h
    low = l
    rango = high - low
    if rango <= 0:
        return {"zona": None, "pct": None}
    pct = round((precio - low) / rango, 3)
    resistencia = high - rango * 0.33
    correccion = (high + low) / 2
    soporte = low + rango * 0.33
    if precio >= resistencia:
        zona = "zona_alta"
    elif precio >= correccion:
        zona = "zona_media_alta"
    elif precio >= soporte:
        zona = "zona_media_baja"
    else:
        zona = "zona_baja"
    return {"zona": zona, "pct": pct}


def _estructura_vs_bp(estructura_vela: str, direccion: str) -> str:
    """
    Relaciona clasificación de vela con dirección del BP.
    """
    CONFIRMA_ALC = {"alcista_fuerte", "alcista_debil", "outside_alcista"}
    CONFIRMA_BAJ = {"bajista_fuerte", "bajista_debil", "outside_bajista"}
    NEUTRAL_EST = {"inside_day", "neutral", None}

    if direccion == "DOBLE_DIR" or estructura_vela in NEUTRAL_EST:
        return "neutral"
    if direccion == "ALCISTA" and estructura_vela in CONFIRMA_ALC:
        return "confirma"
    if direccion == "BAJISTA" and estructura_vela in CONFIRMA_BAJ:
        return "confirma"
    return "contradice"


def _ts_to_tz(ts_ms: int) -> pd.Timestamp:
    t = pd.Timestamp(ts_ms, unit="ms")
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.tz_convert(TIMEZONE)


def format_minute(ts_ms: int) -> str:
    t = _ts_to_tz(ts_ms)
    return t.strftime("%H:%M")


def compute_clv_tercio(high: float, low: float, close: float) -> str:
    """Clasifica el cierre dentro del rango de la vela en tercios."""
    if high == low:
        return "middle"
    rango = high - low
    resistencia = high - rango * 0.33
    soporte = low + rango * 0.33
    if close >= resistencia:
        return "upper_third"
    if close <= soporte:
        return "lower_third"
    return "middle"


def compute_retrace_antes_int(
    reg: List[dict],
    idx_int: Optional[int],
    open_930: Optional[float],
    direccion: str,
) -> Optional[float]:
    """Magnitud en $ que fue en contra antes de tocar el INT (alineado a direccion)."""
    if open_930 is None:
        return None
    if direccion not in ("ALCISTA", "BAJISTA"):
        return None
    if idx_int is None:
        return None
    if idx_int == 0:
        return 0.0
    bars_before = reg[:idx_int]
    if not bars_before:
        return 0.0

    if direccion == "ALCISTA":
        worst = min(b["l"] for b in bars_before)
        return max(0.0, open_930 - worst)
    if direccion == "BAJISTA":
        worst = max(b["h"] for b in bars_before)
        return max(0.0, worst - open_930)
    # direccion ya fue validada para ALCISTA/BAJISTA.


def _first_idx_touch_int_pos(
    reg: List[dict], int_pos: float, open_930: Optional[float] = None
) -> Optional[int]:
    # Abrió por encima de INT+ (ext): no contar "toque" solo por estar ya en ext;
    # cuenta el primer cruce hacia abajo que llega a int_pos (low <= nivel).
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
    # Simétrico: abrió por debajo de INT- → toque int_neg solo al cruzar hacia arriba.
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
    idx_pos: Optional[int],
    idx_neg: Optional[int],
    open_930: float,
    int_pos: float,
    int_neg: float,
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


def _toco_bp_antes_int(
    reg: List[dict], bp: float, i_first_int: Optional[int], open_930: float
) -> str:
    DEAD_ZONE_THRESHOLD = 0.25
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
    bars_rth: List[dict],
    bp: float,
    int_pos: float,
    int_neg: float,
    max_pos: float,
    max_neg: float,
    open_930_preserve: Optional[float] = None,
    cerro_bp_tol: float = 0.15,
) -> Dict[str, Any]:
    """Análisis completo de la sesión RTH (lista 1m RTH ya filtrada, no vacía)."""
    reg = bars_rth
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
        lvl = int_pos if primer_mov == "pos" else int_neg
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
        "day_change": round(close - open_930, 2),
        "day_high": round(day_high, 2),
        "day_low": round(day_low, 2),
        "toco_int": toco_int,
        "toco_int_dir": toco_int_dir,
        "minuto_toco_int": minuto_toco_int,
        "toco_max": toco_max,
        "toco_max_dir": toco_max_dir,
        "minuto_toco_max": minuto_toco_max,
        "toco_bp_antes_int": toco_bp_antes,
        "primer_movimiento": primer_mov,
        "cerro_bp": cerro_bp,
        "c_int": c_int_val,
        "c_max": c_max_val,
        "open_930": open_930,
        "dist_pct": dist_pct,
    }


def compute_extra_result_fields(
    bars_rth: List[dict],
    anal: Dict[str, Any],
    direccion: str,
    bp: float,
    int_pos: float,
    int_neg: float,
    max_pos: float,
    max_neg: float,
) -> Dict[str, Any]:
    """Campos adicionales: int_contrario_tocado, minuto_int_contrario, precio_tras_bp_min/max,
    tramo_bp_a_int_minutos, rebote_post_int_cents, segundo_toque_int."""
    bars = bars_rth
    out = {
        "int_contrario_tocado": None,
        "minuto_int_contrario": None,
        "precio_tras_bp_min": None,
        "precio_tras_bp_max": None,
        "tramo_bp_a_int_minutos": None,
        "rebote_post_int_cents": None,
        "retrace_antes_int": None,
        "segundo_toque_int": None,
    }
    if not bars:
        return out

    toco_int = anal.get("toco_int") or ""
    toco_int_dir = (anal.get("toco_int_dir") or "").strip().lower()
    toco_bp_antes = anal.get("toco_bp_antes_int") or ""

    def _idx_touch_pos(level: float) -> Optional[int]:
        for i, b in enumerate(bars):
            if b["h"] >= level:
                return i
        return None

    def _idx_touch_neg(level: float) -> Optional[int]:
        for i, b in enumerate(bars):
            if b["l"] <= level:
                return i
        return None

    oa = anal.get("open_930")
    open_touch = (
        round(float(oa), 2) if oa is not None and oa != "" else None
    )
    idx_ip = _first_idx_touch_int_pos(bars, int_pos, open_touch)
    idx_in = _first_idx_touch_int_neg(bars, int_neg, open_touch)
    idx_mp = _idx_touch_pos(max_pos)
    idx_mn = _idx_touch_neg(max_neg)

    idx_int = None
    if direccion == "ALCISTA":
        idx_int = idx_ip
    elif direccion == "BAJISTA":
        idx_int = idx_in

    out["retrace_antes_int"] = compute_retrace_antes_int(
        reg=bars,
        idx_int=idx_int,
        open_930=open_touch,
        direccion=direccion,
    )

    ei = None
    if idx_ip is not None and idx_in is not None:
        ei = min(idx_ip, idx_in)
    elif idx_ip is not None:
        ei = idx_ip
    elif idx_in is not None:
        ei = idx_in

    # Campo 1: int_contrario_tocado
    if direccion == "DOBLE_DIR":
        out["int_contrario_tocado"] = "N/A"
    elif direccion == "ALCISTA":
        out["int_contrario_tocado"] = (
            "Sí" if toco_int_dir in ("int_neg", "ambos") else "No"
        )
    elif direccion == "BAJISTA":
        out["int_contrario_tocado"] = (
            "Sí" if toco_int_dir in ("int_pos", "ambos") else "No"
        )
    else:
        out["int_contrario_tocado"] = "No"

    # Campo 2: minuto_int_contrario
    if out["int_contrario_tocado"] == "Sí":
        if direccion == "ALCISTA" and idx_in is not None:
            out["minuto_int_contrario"] = format_minute(bars[idx_in]["t"])
        elif direccion == "BAJISTA" and idx_ip is not None:
            out["minuto_int_contrario"] = format_minute(bars[idx_ip]["t"])

    # i_bp: primer toque BP antes del primer INT
    i_bp = None
    if ei is not None and toco_bp_antes == "Sí":
        for i in range(ei):
            b = bars[i]
            if b["l"] <= bp <= b["h"]:
                i_bp = i
                break

    # Campos 3, 4: precio_tras_bp_min, precio_tras_bp_max
    if i_bp is not None and toco_bp_antes == "Sí":
        rest = bars[i_bp + 1 :]
        if rest:
            out["precio_tras_bp_min"] = round(min(b["l"] for b in rest), 2)
            out["precio_tras_bp_max"] = round(max(b["h"] for b in rest), 2)

    # Campo 5: tramo_bp_a_int_minutos
    if (
        i_bp is not None
        and ei is not None
        and toco_bp_antes == "Sí"
        and toco_int == "Sí"
    ):

        def _mins(ts_ms: int) -> int:
            t = _ts_to_tz(ts_ms)
            return t.hour * 60 + t.minute

        min_bp = _mins(bars[i_bp]["t"])
        min_int = _mins(bars[ei]["t"])
        tramo = min_int - min_bp
        out["tramo_bp_a_int_minutos"] = tramo if tramo >= 0 else None

    # Campo 6: rebote_post_int_cents
    if toco_int == "Sí" and ei is not None:
        em = None
        if idx_mp is not None and idx_mn is not None:
            em = min(idx_mp, idx_mn)
        elif idx_mp is not None:
            em = idx_mp
        elif idx_mn is not None:
            em = idx_mn
        idx_max = em if em is not None else (len(bars) - 1)
        slice_post = bars[ei : idx_max + 1]
        primer_mov = (anal.get("primer_movimiento") or "").strip().lower()
        if slice_post:
            use_pos = direccion == "ALCISTA" or (
                direccion == "DOBLE_DIR"
                and (toco_int_dir == "int_pos" or primer_mov == "pos")
            )
            if use_pos:
                min_low = min(b["l"] for b in slice_post)
                rebote = round(int_pos - min_low, 2)
            else:
                max_high = max(b["h"] for b in slice_post)
                rebote = round(max_high - int_neg, 2)
            out["rebote_post_int_cents"] = max(0.0, rebote)

    # Campo 7: segundo_toque_int
    if toco_int == "Sí" and ei is not None:
        rest = bars[ei + 1 :]
        volvio_bp = False
        if direccion == "ALCISTA":
            if rest and min(b["l"] for b in rest) <= bp:
                volvio_bp = True
        elif direccion == "BAJISTA":
            if rest and max(b["h"] for b in rest) >= bp:
                volvio_bp = True
        elif direccion == "DOBLE_DIR":
            if rest:
                lows = [b["l"] for b in rest]
                highs = [b["h"] for b in rest]
                if (lows and min(lows) <= bp) or (highs and max(highs) >= bp):
                    volvio_bp = True

        if volvio_bp:
            segundo = False
            int_correcto_pos = (direccion == "ALCISTA") or (
                direccion == "DOBLE_DIR" and toco_int_dir in ("int_pos", "ambos")
            )
            int_correcto_neg = (direccion == "BAJISTA") or (
                direccion == "DOBLE_DIR" and toco_int_dir in ("int_neg", "ambos")
            )
            for b in rest:
                if int_correcto_pos and b["h"] >= int_pos:
                    segundo = True
                    break
                if int_correcto_neg and b["l"] <= int_neg:
                    segundo = True
                    break
            out["segundo_toque_int"] = "Sí" if segundo else "No"
        else:
            out["segundo_toque_int"] = "No"
    elif toco_int == "No":
        out["segundo_toque_int"] = None

    return out

