from __future__ import annotations
from datetime import datetime
from typing import Optional, Tuple
import pandas as pd
import numpy as np
import logging

from dc2.caution_utils import caution_tf_key_label_pairs

from dc2.constants import (
    REBOTE_UMBRAL_PCT,
    VELOCIDAD_REGLA,
    SEGUNDO_TOQUE_REGLA,
    FAMILIAS,
    N_VELAS_BB,
    ZM_THRESHOLD_PCT,
    CAUTION_TEXTS,
    PREV_DAY_RULES,
    _OPEN_ZONE_SKIP_RULES,
    _OPEN_ZONE_WARN_RULES,
    ZONE_SIZE_HINT,
    ALC_T3_SOLO_INT_TICKERS,
)
from dc2.utils import (
    _compute_bb_from_closes,
    _filter_bars_before,
    _filter_bars_upto_inclusive,
    _get_igual_baj_rate,
)
from dc2.premium_detector import PremiumDetector

logger = logging.getLogger(__name__)


def _get_prev_day_hint(ticker: str, prev_day_pct: float | None, direction: str) -> str:
    """
    Retorna hint operativo basado en prev_day_change.
    direction: 'ALCISTA' | 'BAJISTA' | 'ZONA_MUERTA'
    Retorna: 'SIZE_x2' | 'SIZE_x1' | 'REDUCIR_x0.5' | 'SKIP' | 'neutral'
    """
    if prev_day_pct is None:
        return "neutral"

    rules = PREV_DAY_RULES.get(ticker, PREV_DAY_RULES["DEFAULT"])

    if direction == "ALCISTA":
        if prev_day_pct < rules.get("alc_skip_bajo", -3.0):
            return "SKIP"
        if prev_day_pct < 0:
            return "REDUCIR_x0.5"
        if prev_day_pct >= rules.get("alc_size2_bajo", 2.0):
            return "SIZE_x2"
        return "SIZE_x1"

    if direction == "BAJISTA":
        if prev_day_pct > rules.get("baj_skip_positivo", 0.0):
            return "SKIP"
        if prev_day_pct < rules.get("baj_size2_alto", -3.0):
            return "SIZE_x2"
        if prev_day_pct < 0:
            return "SIZE_x1"
        return "REDUCIR_x0.5"

    # ZONA_MUERTA — prev_day no determina dirección pero sí confirma régimen
    if prev_day_pct >= 2.0:
        return "regime_alcista"
    if prev_day_pct <= -3.0:
        return "regime_bajista"
    return "neutral"


def evaluar_rebote(rebote_actual: float, int_dist: float) -> str:
    """
    Evalúa si el rebote post-INT justifica salir o si el sesgo cambió.
    rebote_actual: distancia en $ que el precio retrocedió desde el INT
    int_dist: distancia BP→INT calculada en premarket
    Retorna: 'mantener' | 'salir' | 'sesgo_cambia'
    """
    if int_dist <= 0:
        return "mantener"
    pct = rebote_actual / int_dist
    if pct >= 1.0:
        return "sesgo_cambia"
    if pct >= REBOTE_UMBRAL_PCT:
        return "salir"
    return "mantener"


def compute_badge_long(
    bars_dict: dict,
    cutoff_time: datetime,
    now_time: datetime,
    precio: float | None = None,
) -> float:
    """
    Momentum UP vs DOWN como tabla 0–100: score retornado = columna UP.
    UP + DOWN = 100 siempre (cada check reparte sus pts entre SI→UP y NO→DOWN).

    Anterior (sesión/corte): checks 1–2 con cutoff_time.
    Regular (tiempo actual): checks 3–8 con now_time.
    """
    up = 0.0

    # CHECK 1 — 9/20 en 1M (anterior) — 25 pts
    bars_1m = bars_dict.get("1m", [])
    if bars_1m:
        filtered = _filter_bars_before(bars_1m, cutoff_time)
        if len(filtered) >= 20:
            closes = pd.Series([float(b["c"]) for b in filtered], dtype=float)
            ema9 = float(closes.ewm(span=9, adjust=False).mean().iloc[-1])
            ema20 = float(closes.ewm(span=20, adjust=False).mean().iloc[-1])
            if ema9 > ema20:
                up += 25.0

    # CHECK 2 — precio > EMA20 en 15M (anterior) — 25 pts
    bars_15m = bars_dict.get("15m", [])
    if bars_15m and precio is not None:
        filtered = _filter_bars_before(bars_15m, cutoff_time)
        if len(filtered) >= 20:
            closes = pd.Series([float(b["c"]) for b in filtered], dtype=float)
            ema20_15m = float(closes.ewm(span=20, adjust=False).mean().iloc[-1])
            if precio > ema20_15m:
                up += 25.0

    # CHECK 3 — 9/20 en 1M (regular) — 25 pts
    if bars_1m:
        filtered = _filter_bars_before(bars_1m, now_time)
        if len(filtered) >= 20:
            closes = pd.Series([float(b["c"]) for b in filtered], dtype=float)
            ema9 = float(closes.ewm(span=9, adjust=False).mean().iloc[-1])
            ema20 = float(closes.ewm(span=20, adjust=False).mean().iloc[-1])
            if ema9 > ema20:
                up += 25.0

    # CHECKS 4–8 — gap o color por TF (regular) — 5 pts c/u
    GAP_THRESHOLD = 0.0005
    for tf in ("1d", "1h", "30m", "15m", "5m"):
        bars = bars_dict.get(tf, [])
        if not bars:
            continue
        filtered = _filter_bars_before(bars, now_time)
        if len(filtered) < 2:
            continue
        last = filtered[-1]
        prev = filtered[-2]
        open_p = float(last["o"])
        close_p = float(last["c"])
        prev_close = float(prev["c"])
        if prev_close == 0:
            continue
        gap_pct = (open_p - prev_close) / prev_close
        if gap_pct > GAP_THRESHOLD:
            up += 5  # gap up
        elif gap_pct < -GAP_THRESHOLD:
            pass  # gap down → DOWN implícito
        elif close_p > open_p:
            up += 5  # sin gap, vela verde
        # vela roja o doji → DOWN implícito, no suma

    return float(up)
def compute_signals_3_9(bars_dict: dict, cutoff_time: datetime) -> str:
    """Port de compute_signals_3_9 (mi_backtesting_nvda.py) usando barras dict."""
    parts = []
    for tf in ["15m", "30m", "1h"]:
        if tf not in bars_dict:
            continue
        bars_tf = bars_dict[tf]
        if isinstance(bars_tf, pd.DataFrame):
            if bars_tf.empty:
                continue
        else:
            if not bars_tf:
                continue
        filtered = _filter_bars_before(bars_tf, cutoff_time)
        if len(filtered) < 9:
            continue
        tail20 = filtered[-N_VELAS_BB:]
        closes = pd.Series([float(b["c"]) for b in tail20], dtype=float)
        ema3 = float(closes.ewm(span=3, adjust=False).mean().iloc[-1])
        ema9 = float(closes.ewm(span=9, adjust=False).mean().iloc[-1])
        signal = "up" if ema3 > ema9 else "down"
        parts.append(f"3/9 {signal} {tf}")
    return ", ".join(parts) if parts else ""
def compute_caution_note(
    bars_dict: dict,
    precio_premarket: float,
    cutoff_prev_1600: datetime,
    ticker: str = "",
) -> str:
    """Port de compute_caution (mi_backtesting_nvda.py) para stocks.
    Compara precio_premarket vs BB(20,2) construido con barras hasta 16:00 prev.
    TFs fijos 1m–1d (caution_tf_key_label_pairs en mi_backtesting_nvda).
    """
    cautions = []
    tf_pairs = (
        caution_tf_key_label_pairs(ticker)
        if ticker
        else [
            ("1m", "1M"),
            ("5m", "5M"),
            ("15m", "15M"),
            ("30m", "30M"),
            ("1h", "1H"),
            ("1d", "1D"),
        ]
    )
    for tf, label in tf_pairs:
        if tf not in bars_dict:
            continue
        bars_tf = bars_dict[tf]
        if isinstance(bars_tf, pd.DataFrame):
            if bars_tf.empty:
                continue
        else:
            if not bars_tf:
                continue
        filtered_prev = _filter_bars_upto_inclusive(bars_tf, cutoff_prev_1600)
        closes_prev = [b["c"] for b in filtered_prev]
        bbt, bbb = _compute_bb_from_closes(closes_prev)
        if bbt is not None and precio_premarket > bbt:
            key = f"BBT {label}"
            cautions.append(CAUTION_TEXTS.get(key, key))
        if bbb is not None and precio_premarket < bbb:
            key = f"BBB {label}"
            cautions.append(CAUTION_TEXTS.get(key, key))
    return " ".join(cautions) if cautions else "Sin caution"
def ema_alignment(
    open_p: float,
    e20: float,
    e50: float,
    e200: float,
    pct: float = 0.001,
) -> str:
    """
    Clasifica la posición del precio respecto a EMA20/50/200.
    Retorna: 'sobre_3' | 'bajo_3' | 'entre' | 'at_any'
    pct = umbral de tolerancia AT (0.1% del precio)
    """
    def zone(o, e):
        if not e or e == 0:
            return "unknown"
        d = (o - e) / e
        return "above" if d > pct else ("below" if d < -pct else "at")

    zones = [zone(open_p, e20), zone(open_p, e50), zone(open_p, e200)]
    if "at" in zones:
        return "at_any"
    if all(z == "above" for z in zones):
        return "sobre_3"
    if all(z == "below" for z in zones):
        return "bajo_3"
    return "entre"


def ema_sr_context(
    precio: float,
    e20: float | None,
    e50: float | None,
    e200: float | None,
) -> str:
    """
    Evalúa cada EMA como soporte o resistencia dinámica.
    Devuelve string legible para context_compressed.
    Ejemplo: "EMA20 soporte +0.82% | EMA50 soporte +3.21% | EMA200 resistencia -5.14%"
    """
    parts = []
    for label, ema in (("EMA20", e20), ("EMA50", e50), ("EMA200", e200)):
        if not ema:
            continue
        dist_pct = (precio - ema) / ema * 100
        role = "soporte" if dist_pct > 0 else "resistencia"
        parts.append(f"{label} {role} {dist_pct:+.2f}%")
    return " | ".join(parts) if parts else ""


def precio_en_rango(precio: float, high: float, low: float) -> dict:
    """Spatial position of price within [low, high] range.
    pct: 0.0 = at low, 1.0 = at high.
    label: cerca_max (>=75%), cerca_min (<=25%), zona_media otherwise.
    """
    rango = high - low
    if rango <= 0:
        return {"pct": 0.5, "label": "sin_rango"}
    pct = (precio - low) / rango
    pct = max(0.0, min(1.0, pct))
    if pct >= 0.75:
        label = "cerca_max"
    elif pct <= 0.25:
        label = "cerca_min"
    else:
        label = "zona_media"
    return {"pct": round(pct, 3), "label": label}
def conviction_score(
    dir_ph: str,
    gap_tipo: str,
    gap_mag: float,
    pdc: float,
    ema_align: str,
    bb_expande: bool | None = None,
    n_cautiones: int = 0,
    caution_1v3: str | None = None,
    ticker: str = "",
    open_930: float | None = None,
    int_pos: float | None = None,
    int_neg: float | None = None,
) -> float:
    """
    Score de convicción EC v2: 0-10.
    Base 5.0 + EMA(±2) + GAP(±2) + PrevDay(±2) + bb_expande(+0.5)
    + Milton cautiones(±1.5) + 1v3(±4) cuando disponible.

    score ≥7 → SIZE x2 (PREMIUM)
    5-6    → SIZE x1 (operable)
    ≤4     → SKIP

    Retorna -1.0 si el precio ya abrió más allá del INT (BLOQUEADO).
    """
    score = 5.0

    # ── EMA alignment (±2.0 pts) ──────────────────────────────────
    if dir_ph == "BAJISTA":
        if ema_align == "bajo_3":              score += 2.0
        elif ema_align == "sobre_3":           score -= 1.0
        elif ema_align in ("entre", "at_any"): score -= 0.5
    elif dir_ph == "ALCISTA":
        if ema_align == "sobre_3":             score += 2.0
        elif ema_align == "bajo_3":            score -= 1.0
        elif ema_align in ("entre", "at_any"): score -= 0.5

    # ── GAP tipo y magnitud (±2.0 pts) ────────────────────────────
    mag = abs(float(gap_mag or 0))
    if dir_ph == "BAJISTA":
        if gap_tipo == "GAP_DOWN":
            score += 2.0 if mag > 3 else (1.5 if mag > 1 else 0.5)
        elif gap_tipo == "GAP_UP":
            score += 1.5 if mag > 3 else (-0.5 if mag > 1 else 0.0)
    elif dir_ph == "ALCISTA":
        if gap_tipo == "GAP_UP":
            score += 1.5 if mag > 3 else (1.0 if mag > 1 else 0.5)
        elif gap_tipo == "GAP_DOWN":
            score += -1.5 if mag > 3 else (-0.5 if mag > 1 else 0.5)

    # ── Prev day change (±2.0 pts) ────────────────────────────────
    pdc_f = float(pdc or 0)
    if dir_ph == "BAJISTA":
        if pdc_f < -3:    score += 1.5
        elif pdc_f < -1:  score += 2.0
        elif pdc_f < 0:   score += 1.0
        elif pdc_f < 1:   score += 0.5
        elif pdc_f < 3:   score -= 0.5
        else:             score -= 1.5
    elif dir_ph == "ALCISTA":
        if pdc_f > 5:     score += 2.0   # muy fuerte = MAX casi garantizado
        elif pdc_f > 2:   score += 1.5   # fuerte = excelente
        elif pdc_f > 0:   score += 0.5   # tranquilo = normal
        elif pdc_f > -1:  score += 0.0   # neutro
        elif pdc_f > -3:  score -= 0.5   # caída moderada = precaución
        else:             score -= 1.5   # caída fuerte = precaución máxima

    # ── bb_expande bonus (+0.5) ───────────────────────────────────
    if bb_expande is True:
        score += 0.5

    # ── NUEVO: Cautiones de Milton 9:28 (±1.5 pts) ───────────────
    # n_cautiones = número de bandas BB tocadas premarket (0-4)
    if dir_ph in ("ALCISTA", "BAJISTA"):
        if n_cautiones == 0:
            # Sin cautiones = precio limpio sin sobreextensión
            score += 1.5
        elif n_cautiones == 4 and dir_ph == "BAJISTA":
            # Cuádruple bajista = momentum confirmado en todas las bandas
            score += 1.5
        elif n_cautiones == 4 and dir_ph == "ALCISTA":
            # Cuádruple alcista = sobreextendido
            score -= 0.5
        elif n_cautiones == 1:
            # Una caución — verificar si contradice la dirección
            # BBT siendo bajista o BBB siendo alcista = contradice
            score += 0.0  # neutral, se ajusta abajo si contradice

    # ── NUEVO: 1v3 estado al abrir 9:30 (±4.0 pts) ───────────────
    # Solo disponible en modo open_930
    if caution_1v3 is not None and dir_ph != "ZONA_MUERTA":

        # escalo ALC: neutro (0 pts). igual BAJ: sin penalización por ticker (setup premium).

        if caution_1v3 == "escalo" and dir_ph == "BAJISTA" and ticker == "NVDA":
            score -= 4.0  # NVDA único: escalo BAJ también es SKIP

        # PREMIUM — sube el score
        elif caution_1v3 == "escalo" and dir_ph == "BAJISTA" and ticker in {"META", "AMZN", "GLD", "TLT"}:
            score += 2.0  # Escalo BAJ premium en estos tickers

        elif n_cautiones == 4 and dir_ph == "BAJISTA" and caution_1v3 in ("escalo", "cambio_tipo"):
            score += 1.0  # n_caut=4 BAJ universal

    # ── NUEVO: DD + 1v3 ───────────────────────────────────────────
    # Si dir_ph es ZONA_MUERTA y 1v3=igual/nuevo → +3 (regla universal)
    if dir_ph == "ZONA_MUERTA" and caution_1v3 in ("igual", "nuevo"):
        score += 3.0

    # ── NUEVO: Bloqueo por precio más allá del INT ─────────────────
    # Si el precio ya pasó el objetivo al abrir → BLOQUEADO (-1 especial)
    if open_930 is not None:
        if dir_ph == "ALCISTA" and int_pos is not None and open_930 > int_pos:
            return -1.0  # BLOQUEADO — abrió más allá del INT
        if dir_ph == "BAJISTA" and int_neg is not None and open_930 < int_neg:
            return -1.0  # BLOQUEADO — abrió más allá del INT
        # Recorrido mínimo $0.30
        if dir_ph == "ALCISTA" and int_pos is not None:
            if (int_pos - open_930) < 0.30:
                return -1.0  # BLOQUEADO — sin recorrido
        if dir_ph == "BAJISTA" and int_neg is not None:
            if (open_930 - int_neg) < 0.30:
                return -1.0  # BLOQUEADO — sin recorrido

    return round(max(0.0, min(10.0, score)), 1)
def direction_from_dist(dist: float, int_dist: float = 0.0) -> str:
    """
    Dirección según open vs BP.
    ZM = ZM_THRESHOLD_PCT * int_dist (≈11% del int_dist); sin int_dist → ±$0.25.
    """
    threshold = (int_dist * ZM_THRESHOLD_PCT) if int_dist > 0 else 0.25
    if dist > threshold:
        return "ALCISTA"
    if dist < -threshold:
        return "BAJISTA"
    return "ZONA_MUERTA"


def compute_open_zone(
    open_930: float,
    bp: float,
    int_pos: float | None,
    int_neg: float | None,
    direction: str,
    int_dist: float = 0.0,
) -> tuple[str | None, float | None]:
    """
    open_zone y open_pct_en_rango al abrir (9:30). Sin INT válidos → (None, None).
    pct > 1.0 → ext_int_max (live simplificado, sin sub-tercios INT→MAX).
    """
    threshold = (int_dist * ZM_THRESHOLD_PCT) if int_dist > 0 else 0.25
    dist_abs = abs(open_930 - bp)
    if dist_abs < threshold:
        return "doble_dir_zone", 0.0

    if direction == "ALCISTA":
        if int_pos is None:
            return None, None
        rango = int_pos - bp
        if rango <= 0:
            return "doble_dir_zone", 0.0
        pct = (open_930 - bp) / rango
    elif direction == "BAJISTA":
        if int_neg is None:
            return None, None
        rango = bp - int_neg
        if rango <= 0:
            return "doble_dir_zone", 0.0
        pct = (bp - open_930) / rango
    else:
        return "doble_dir_zone", 0.0

    if pct <= 0:
        return "doble_dir_zone", round(pct, 4)
    if pct <= 0.333:
        zone = "normal_t1"
    elif pct <= 0.667:
        zone = "normal_t2"
    elif pct <= 1.0:
        zone = "normal_t3"
    else:
        zone = "ext_int_max"
    return zone, round(pct, 4)
def get_rebote_umbral(int_dist: float) -> float:
    """
    Umbral de rebote post-INT como 33% del int_dist del día.
    Adaptativo — no usa dólares fijos por ticker.

    Si el precio rebota más de este valor desde el INT → cerrar.
    int_dist = distancia BP→INT calculada en premarket.
    """
    if int_dist <= 0:
        return 0.0
    return int_dist * REBOTE_UMBRAL_PCT
def _match_open_zone_dir_spec(spec: str | None, direction: str, gap_type: str) -> bool:
    if spec is None:
        return True
    if spec == "BAJISTA+GAP_DOWN":
        return direction == "BAJISTA" and gap_type == "GAP_DOWN"
    return direction == spec
def _open_zone_skip_match(
    ticker: str,
    open_zone: str | None,
    caution_1v3: str | None,
    direction: str,
    gap_type: str,
    rule: tuple[list[str], list[str], str, str | None, str],
) -> bool:
    tickers_r, zones_r, c1_r, dir_r, _motivo = rule
    if ticker not in tickers_r or not open_zone or open_zone not in zones_r:
        return False
    if c1_r != "cualquier":
        if caution_1v3 != c1_r:
            return False
    if not _match_open_zone_dir_spec(dir_r, direction, gap_type):
        return False
    return True
def _gestion_live_fragments(ticker: str, direction: str) -> list[str]:
    """Textos no estándar para dashboard (velocidad / 2º toque)."""
    out: list[str] = []
    std_vel_alc, std_vel_baj = True, True
    std_2t_alc, std_2t_baj = True, True

    vr = VELOCIDAD_REGLA.get(ticker)
    if vr:
        alc_c, baj_c, nota = vr
        if direction == "ALCISTA" and alc_c != std_vel_alc:
            out.append(f">30min→{'cerrar_INT' if alc_c else 'NO_cerrar'} ({nota})")
        elif direction == "BAJISTA" and baj_c != std_vel_baj:
            out.append(f">30min→{'cerrar_INT' if baj_c else 'NO_cerrar'} ({nota})")

    sr = SEGUNDO_TOQUE_REGLA.get(ticker)
    if sr:
        e_alc, e_baj, nota = sr
        if (e_alc, e_baj) != (std_2t_alc, std_2t_baj):
            parts = []
            if e_alc != std_2t_alc:
                parts.append(f"2do_toque→{'exit_ALC' if e_alc else 'no_exit_ALC'}")
            if e_baj != std_2t_baj:
                parts.append(f"2do_toque→{'exit_BAJ' if e_baj else 'no_exit_BAJ'}")
            if parts:
                out.append(" | ".join(parts) + f" ({nota})")
    return out
def _gestion_open_930_line(ticker: str, td: dict, direction: str) -> str | None:
    """Línea GESTIÓN en context_compressed: solo si hay algo no estándar."""
    chunks: list[str] = []

    # ext_int_max: precio abrió más allá del INT — INT es cruce inminente
    open_zone = td.get("open_zone")
    if open_zone == "ext_int_max":
        int_level = (
            td.get("int_pos") if direction == "ALCISTA"
            else td.get("int_neg")
        )
        if int_level is not None:
            chunks.append(
                f"ABRIÓ MÁS ALLÁ DEL INT (${float(int_level):.2f}) — "
                f"si cruza de vuelta hacia BP → CERRAR"
            )

    ru = td.get("rebote_umbral")
    if ru is not None and abs(float(ru) - 1.50) > 0.001:
        chunks.append(f"umbral_rebote=${float(ru):.2f}")
    chunks.extend(_gestion_live_fragments(ticker, direction))
    for w in td.get("evaluation", {}).get("warnings", []):
        if (
            "Zona+1v3:" in w
            or "escalo en t1/t2" in w
            or "escalo ALC — operable" in w
        ):
            chunks.append(w)
    if not chunks:
        return None
    return " | ".join(chunks)


def compute_cruces(tickers_data: dict) -> dict:
    """Lecturas de par por familias (Hallazgo Maestro v21)."""
    cruces = {}
    for familia, members in FAMILIAS.items():
        dirs = {}
        for t in members:
            if t in tickers_data:
                dirs[t] = tickers_data[t].get("direction", "ZONA_MUERTA")
        if len(dirs) < 2:
            cruces[familia] = "SIN_DATOS"
            continue
        vals = list(dirs.values())
        tickers_str = list(dirs.keys())
        if all(v == "ALCISTA" for v in vals):
            cruces[familia] = (
                f"AMBOS_ALC — {' + '.join(tickers_str)} alineados alcistas"
            )
        elif all(v == "BAJISTA" for v in vals):
            cruces[familia] = (
                f"AMBOS_BAJ — {' + '.join(tickers_str)} alineados bajistas"
            )
        elif all(v == "ZONA_MUERTA" for v in vals):
            cruces[familia] = "AMBOS_ZM — sector indeciso"
        else:
            resumen = " | ".join([f"{t}={d}" for t, d in dirs.items()])
            cruces[familia] = f"DIVERGENCIA — {resumen}"

    for refugio in ("TLT", "GLD"):
        if refugio not in tickers_data:
            cruces["MACRO_REFUGIOS"] = f"⚠️ {refugio} sin datos — Patrón V incompleto"
            return cruces

    tlt_dir = tickers_data.get("TLT", {}).get("direction", "ZONA_MUERTA")
    gld_dir = tickers_data.get("GLD", {}).get("direction", "ZONA_MUERTA")
    if tlt_dir == "ALCISTA" and gld_dir == "ALCISTA":
        cruces["MACRO_REFUGIOS"] = (
            "⚠️ PANIC SIGNAL — TLT+GLD ambos ALC = SKIP alcistas"
        )
    elif tlt_dir == "BAJISTA" and gld_dir == "BAJISTA":
        cruces["MACRO_REFUGIOS"] = (
            "✅ RISK-ON MÁXIMO — TLT+GLD ambos BAJ = full size alcistas"
        )
    elif tlt_dir == "BAJISTA" and gld_dir == "ALCISTA":
        cruces["MACRO_REFUGIOS"] = (
            "⚠️ INFLACIÓN/USD DÉBIL — GLD ALC con máxima convicción"
        )
    elif tlt_dir == "ALCISTA" and gld_dir == "BAJISTA":
        cruces["MACRO_REFUGIOS"] = "⚠️ HUIDA A USD — bajistas USD-sensibles"
    else:
        cruces["MACRO_REFUGIOS"] = (
            "NEUTRAL — refugios en ZM, contexto indeciso"
        )
    return cruces


def finalize_dashboard_output(output_data: dict) -> None:
    """skips SkipFilter, PremiumDetector, elimina tier SKIP, cruces."""
    output_data.setdefault("skips", {})
    tickers = output_data.setdefault("tickers", {})
    for t in list(tickers.keys()):
        td = tickers[t]
        # ZM tickers: nunca mover a skips como string — mode_930 los necesita como dict
        if td.get("ec_accion") == "ZONA_MUERTA":
            continue
        if td.get("skip"):
            output_data["skips"][t] = td.get("skip_reason", "Skip")
    for t in list(tickers.keys()):
        td = tickers[t]
        if td.get("skip"):
            continue
        ev = PremiumDetector.evaluate(t, td)
        tickers[t]["evaluation"] = ev
        if ev.get("tier") == "SKIP":
            # ZM pre open_930: conservar fila completa para mode_930
            if ev.get("reason") == "ZONA_MUERTA — esperar open_930":
                continue
            output_data["skips"][t] = ev.get("reason", "SKIP")
            del tickers[t]
    output_data["cruces"] = compute_cruces(output_data.get("tickers", {}))


def compute_ema_conviction_block(
    bars_dict: dict,
    cutoff_et,
    cutoff_prev_1600,
    precio_ref: float,
    direction: str,
    gap_type: str,
    gap_pct: float,
    prev_day_change_pct: float,
    caution_note: str | None,
    caution_1v3: str | None,
    ticker: str,
    open_930: float | None = None,
    int_pos: float | None = None,
    int_neg: float | None = None,
) -> dict:
    """
    Calcula EMAs 20/50/200, ema_align, ema_sr, bb_expande_premarket,
    ec_score, ec_accion, ec_size.
    Centraliza el bloque duplicado en los 4 modos.
    Retorna dict con todas esas claves.
    """
    from dc2.constants import N_VELAS_BB
    from dc2.utils import (
        _filter_bars_before,
        _filter_bars_upto_inclusive,
        _compute_bb_from_closes,
    )

    bars_1h_for_ema = _filter_bars_before(bars_dict.get("1h", []), cutoff_et)
    bars_1m_for_ema = _filter_bars_before(bars_dict.get("1m", []), cutoff_et)
    bars_1m_prev = _filter_bars_upto_inclusive(
        bars_dict.get("1m", []), cutoff_prev_1600
    )

    closes_ema = pd.Series([float(b["c"]) for b in bars_1h_for_ema], dtype=float)

    ema20_pre = ema50_pre = ema200_pre = None
    if len(closes_ema) >= 20:
        ema20_pre = round(
            float(closes_ema.ewm(span=20, adjust=False).mean().iloc[-1]), 4
        )
    if len(closes_ema) >= 50:
        ema50_pre = round(
            float(closes_ema.ewm(span=50, adjust=False).mean().iloc[-1]), 4
        )
    if len(closes_ema) >= 200:
        ema200_pre = round(
            float(closes_ema.ewm(span=200, adjust=False).mean().iloc[-1]), 4
        )

    bb_expande_premarket = None
    if len(bars_1m_for_ema) >= N_VELAS_BB and len(bars_1m_prev) >= N_VELAS_BB:
        bbt_now, bbb_now = _compute_bb_from_closes([b["c"] for b in bars_1m_for_ema])
        bbt_prev, bbb_prev = _compute_bb_from_closes([b["c"] for b in bars_1m_prev])
        if all(v is not None for v in [bbt_now, bbb_now, bbt_prev, bbb_prev]):
            bb_expande_premarket = (bbt_now - bbb_now) > (bbt_prev - bbb_prev)

    ema_align = "unknown"
    ema_sr = ""
    ec_score = None
    ec_accion = "SIN_DATOS"
    ec_size = 1.0

    if all(v is not None for v in [ema20_pre, ema50_pre, ema200_pre]):
        ema_align = ema_alignment(precio_ref, ema20_pre, ema50_pre, ema200_pre)
        ema_sr = ema_sr_context(precio_ref, ema20_pre, ema50_pre, ema200_pre)
        _n_caut_ec = (
            caution_note.count("BBT") + caution_note.count("BBB")
            if caution_note
            else 0
        )
        ec_score = conviction_score(
            direction,
            gap_type,
            abs(gap_pct),
            prev_day_change_pct,
            ema_align,
            bb_expande_premarket,
            n_cautiones=_n_caut_ec,
            caution_1v3=caution_1v3,
            ticker=ticker,
            open_930=open_930,
            int_pos=int_pos,
            int_neg=int_neg,
        )
        if ec_score == -1.0:
            ec_accion = "BLOQUEADO"
            ec_size = 0.0
        elif direction == "ZONA_MUERTA" and caution_1v3 not in ("igual", "nuevo"):
            ec_accion = "ZONA_MUERTA"
            ec_size = 0.0
        else:
            sc_int = round(ec_score)
            if sc_int <= 4:
                ec_accion = "SKIP"
                ec_size = 0.0
            elif sc_int >= 7:
                ec_accion = "SIZE_x2"
                ec_size = 2.0
            else:
                ec_accion = "SIZE_x1"
                ec_size = 1.0

    if ec_score is None and direction == "ZONA_MUERTA":
        ec_accion = "ZONA_MUERTA"
        ec_size = 0.0

    return {
        "ema20_pre": ema20_pre,
        "ema50_pre": ema50_pre,
        "ema200_pre": ema200_pre,
        "ema_align": ema_align,
        "ema_sr": ema_sr,
        "bb_expande_premarket": bb_expande_premarket,
        "ec_score": ec_score,
        "ec_accion": ec_accion,
        "ec_size": ec_size,
    }
