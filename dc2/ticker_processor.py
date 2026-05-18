"""
Helpers reutilizables entre modos — encapsulan bloques duplicados de data_collector.main().
Sin persistencia de output: solo cálculos y logging.
"""
from __future__ import annotations

import logging

import pandas as pd

from dc2.constants import N_VELAS_BB, TIMEZONE
from dc2.utils import (
    _compute_bb_from_closes,
    _filter_bars_before,
    _filter_bars_upto_inclusive,
)
from dc2.indicators import (
    conviction_score,
    direction_from_dist,
    ema_alignment,
)

logger = logging.getLogger(__name__)


def compute_emas(
    bars_dict: dict,
    cutoff_et,
    ticker: str = "",
    precio_premarket: float | None = None,
    open_price: float | None = None,
) -> tuple[float | None, float | None, float | None]:
    """
    Calcula EMA20/50/200 sobre barras 1H hasta cutoff_et.
    Retorna (ema20_pre, ema50_pre, ema200_pre).
    Si hay menos de 20 velas 1H, ema20_pre (y posiblemente ema50/200) quedan None.

    El parámetro ticker se pasa solo para logging.
    """
    # ── EMA 20 / 50 / 200 — barras 1H premarket (régimen real) ─
    bars_1m_for_ema = _filter_bars_before(
        bars_dict.get("1m", []), cutoff_et
    )
    bars_1h_for_ema = _filter_bars_before(
        bars_dict.get("1h", []), cutoff_et
    )
    closes_ema = pd.Series(
        [float(b["c"]) for b in bars_1h_for_ema], dtype=float
    )
    ema20_pre = None
    ema50_pre = None
    ema200_pre = None
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

    precio_ref_for_log = (
        open_price if (open_price is not None) else precio_premarket
    )
    ema_align_for_log = (
        ema_alignment(precio_ref_for_log, ema20_pre, ema50_pre, ema200_pre)
        if all(v is not None for v in [ema20_pre, ema50_pre, ema200_pre])
        else "unknown"
    )
    logger.info(
        f"[{ticker}] EMAs 1h: velas={len(closes_ema)} | "
        f"EMA20={ema20_pre} | EMA50={ema50_pre} | EMA200={ema200_pre} | "
        f"align={ema_align_for_log}"
    )
    if ema200_pre is None:
        logger.warning(
            f"[{ticker}] EMA200 null — {len(closes_ema)} velas 1h "
            f"(necesita ≥200) — score EC no se calculará"
        )
    # NOTA: bars_1m_for_ema sigue siendo necesario para bb_expande_premarket
    return ema20_pre, ema50_pre, ema200_pre


def compute_bb_expande(
    bars_dict: dict,
    cutoff_et,
    cutoff_prev_1600,
    bars_1m_for_ema: list,
    ticker: str = "",
    caution_note: str = "",
    signals_str: str = "",
    badge_long: float = 0.0,
) -> bool | None:
    """
    Calcula bb_expande_premarket comparando ancho BB actual vs cierre día anterior.
    Retorna True/False/None.

    bars_1m_for_ema ya viene calculado — no recalcular.
    """
    # ── bb_expande_premarket ────────────────────────────────
    bb_expande_premarket = None
    bars_1m_prev = _filter_bars_upto_inclusive(
        bars_dict.get("1m", []), cutoff_prev_1600
    )
    if len(bars_1m_for_ema) >= N_VELAS_BB and len(bars_1m_prev) >= N_VELAS_BB:
        bbt_now, bbb_now = _compute_bb_from_closes(
            [b["c"] for b in bars_1m_for_ema]
        )
        bbt_prev, bbb_prev = _compute_bb_from_closes(
            [b["c"] for b in bars_1m_prev]
        )
        if all(
            v is not None for v in [bbt_now, bbb_now, bbt_prev, bbb_prev]
        ):
            bb_expande_premarket = (bbt_now - bbb_now) > (bbt_prev - bbb_prev)

    logger.info(
        f"[{ticker}] caution='{caution_note}' | "
        f"signals='{signals_str}' | "
        f"badge={round(badge_long, 1)}% | "
        f"bb_expande={bb_expande_premarket}"
    )
    return bb_expande_premarket


def compute_score_ec(
    ticker: str,
    direction_pm: str,
    gap_type: str,
    gap_pct: float,
    prev_day_change_pct: float,
    caution_note: str | None,
    ema20_pre: float | None,
    ema50_pre: float | None,
    ema200_pre: float | None,
    bb_expande_premarket: bool | None,
    precio_premarket: float | None,
    open_price: float | None = None,
    caution_1v3: str | None = None,
    bp: float | None = None,
    int_pos: float | None = None,
    int_neg: float | None = None,
    signals_str: str = "",
    badge_long: float = 0.0,
) -> dict:
    """
    Calcula Score EC y devuelve dict con ec_score, ec_accion, ec_size, ema_align.

    Modos premarket (9_15, 9_28, test): open_price=None, caution_1v3=None, bp=None
    Modo open_930: open_price=precio_real, caution_1v3=estado_real, bp=bp_calculado
    """
    # ── Conviction Score EC ─────────────────────────────────
    ema_align = "unknown"
    ec_score = None
    ec_accion = "SIN_DATOS"
    ec_size = 1.0

    # precio de referencia: open_price en open_930,
    # precio_premarket en los otros dos modos
    precio_ref = (
        open_price if (open_price is not None) else precio_premarket
    )

    _caution_1v3_val = caution_1v3
    _n_caut_ec = (caution_note or "").count("BBT") + (caution_note or "").count(
        "BBB"
    )
    # Dirección real al abrir para el score cuando hay 1v3
    _dir_for_score = (
        direction_from_dist(open_price - bp)
        if open_price is not None
        and caution_1v3 is not None
        and bp is not None
        else direction_pm
    )

    if all(v is not None for v in [ema20_pre, ema50_pre, ema200_pre]):
        ema_align = ema_alignment(
            precio_ref, ema20_pre, ema50_pre, ema200_pre
        )
        ec_score = conviction_score(
            _dir_for_score,
            gap_type,
            abs(gap_pct),
            prev_day_change_pct,
            ema_align,
            bb_expande_premarket,
            n_cautiones=_n_caut_ec,
            caution_1v3=_caution_1v3_val,
            ticker=ticker,
            open_930=open_price,
            int_pos=int_pos,
            int_neg=int_neg,
        )
        if ec_score == -1.0:
            ec_accion = "BLOQUEADO"
            ec_size = 0.0
        elif _dir_for_score == "ZONA_MUERTA" and _caution_1v3_val not in (
            "igual",
            "nuevo",
        ):
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

    if ec_score is None and _dir_for_score == "ZONA_MUERTA":
        ec_accion = "ZONA_MUERTA"
        ec_size = 0.0

    logger.info(
        f"[{ticker}] caution='{caution_note}' | "
        f"signals='{signals_str}' | "
        f"badge={round(badge_long, 1)}% | "
        f"bb_expande={bb_expande_premarket}"
    )

    if ec_score is None and ec_accion == "SIN_DATOS":
        logger.warning(
            f"[{ticker}] score EC = SIN_DATOS | "
            f"ema20={ema20_pre} ema50={ema50_pre} ema200={ema200_pre}"
        )
    else:
        accion_label = {
            "SIZE_x2": "★★ SIZE x2",
            "SIZE_x1": "★ SIZE x1",
            "SKIP": "✗ SKIP",
            "ZONA_MUERTA": "~ ZM",
            "BLOQUEADO": "⛔ BLOQUEADO",
        }.get(ec_accion, ec_accion)
        logger.info(
            f"[{ticker}] score={ec_score} | {accion_label} | "
            f"align={ema_align} | dir={_dir_for_score} | "
            f"prev_day={round(prev_day_change_pct, 2)}% | "
            f"n_caut={_n_caut_ec if '_n_caut_ec' in dir() else '?'}"
        )

    return {
        "ec_score": ec_score,
        "ec_accion": ec_accion,
        "ec_size": ec_size,
        "ema_align": ema_align,
    }
