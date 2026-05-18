from __future__ import annotations
import logging
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from dc2.constants import TICKERS, TIMEZONE, TF_LIST
from dc2.utils import (
    _filter_bars_before,
    _validate_premarket_bars,
)
from dc2.indicators import (
    compute_badge_long,
    compute_caution_note,
    compute_signals_3_9,
    precio_en_rango,
    direction_from_dist,
    compute_ema_conviction_block,
    _get_prev_day_hint,
    finalize_dashboard_output,
)
from dc2.data_clients import (
    AlpacaHistoricalClient,
    BPCalculator,
    TargetsCalculator,
    SkipFilter,
)
from dc2.outputs import write_json, write_context_compressed, write_ai_dashboard

logger = logging.getLogger(__name__)


async def run_mode_9_28(tickers_to_run: list, date_str: str, output_data: dict) -> None:
    logger.info(f"=== MODO: premarket_9_28 | tickers: {len(tickers_to_run)} ===")
    # ── Barras premarket via histórico IEX (sin WebSocket) ──
    # IEX histórico entrega barras premarket del día en curso
    # sin restricción. Más confiable que WS en free tier.
    bars_by_ticker = {}
    for ticker in tickers_to_run:
        bars_by_ticker[ticker] = {}
        _end_hist = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
        for tf in TF_LIST:
            days_back = 365 if tf == "1h" else 2
            bars_by_ticker[ticker][tf] = AlpacaHistoricalClient.fetch_bars(
                ticker,
                tf,
                (
                    datetime.now(TIMEZONE) - timedelta(days=days_back)
                ).strftime("%Y-%m-%d"),
                _end_hist,
            )

    for ticker in tickers_to_run:
        try:
            open_price: float | None = None  # en open_930 se asigna el open RTH
            bars_dict = bars_by_ticker[ticker]
            cutoff_et = datetime.now(TIMEZONE).replace(
                hour=9, minute=28, second=0, microsecond=0
            )

            # ── Logging técnico: velas por TF (previo a filtros/cutoff) ──
            for tf, bars in bars_dict.items():
                n = len(bars)
                if n > 0:
                    first_ts = str(bars[0]["t"])[:16]
                    last_ts = str(bars[-1]["t"])[:16]
                else:
                    first_ts = last_ts = "sin datos"
                logger.debug(f"[{ticker}] {tf}: {n} velas | {first_ts} → {last_ts}")

            bars_1m_pre = _filter_bars_before(bars_dict.get("1m", []), cutoff_et)
            if not bars_1m_pre:
                continue
            if not _validate_premarket_bars(
                ticker, bars_dict.get("1m", []), cutoff_et
            ):
                # Log pero no skip — BP con datos parciales
                # es mejor que no tener BP
                pass

            bp = BPCalculator.calculate_bp(
                bars_dict, cutoff_time=pd.Timestamp(cutoff_et)
            )
            if bp is None:
                logger.warning("[%s] BP=None — verificar barras disponibles", ticker)
                continue

            today_date_et = datetime.now(TIMEZONE).date()
            prev_date_et = today_date_et - timedelta(days=1)

            # Daily bars previos (excluye el “día de hoy” incompleto)
            daily_bars = AlpacaHistoricalClient.fetch_bars(
                ticker,
                "1d",
                (datetime.now(TIMEZONE) - timedelta(days=10)).strftime("%Y-%m-%d"),
                datetime.now(TIMEZONE).strftime("%Y-%m-%d"),
            )
            bars_dict["1d"] = _filter_bars_before(daily_bars, cutoff_et)
            daily_prev = [b for b in daily_bars if b["t"].date() < today_date_et]
            daily_prev.sort(key=lambda x: x["t"])

            prev_close = float(daily_prev[-1]["c"]) if daily_prev else 0.0
            prev_day_high = float(daily_prev[-1]["h"]) if daily_prev else None
            prev_day_low  = float(daily_prev[-1]["l"]) if daily_prev else None
            if len(daily_prev) >= 2:
                c_last = float(daily_prev[-1]["c"])
                c_prev = float(daily_prev[-2]["c"])
                prev_day_change_pct = ((c_last - c_prev) / c_prev * 100.0) if c_prev else 0.0
            else:
                prev_day_change_pct = 0.0

            precio_premarket = float(bars_1m_pre[-1]["c"])
            precio_pm_actual = precio_premarket
            # Synthetic premarket candle high/low (4am to cutoff)
            _bars_pm_4am = [b for b in bars_1m_pre if b["t"].hour >= 4]
            pm_high = max((float(b["h"]) for b in _bars_pm_4am), default=None)
            pm_low  = min((float(b["l"]) for b in _bars_pm_4am), default=None)
            rango_pm = (
                precio_en_rango(precio_premarket, pm_high, pm_low)
                if (pm_high is not None and pm_low is not None)
                else {"pct": 0.5, "label": "sin_datos"}
            )
            rango_prev_day = (
                precio_en_rango(precio_premarket, prev_day_high, prev_day_low)
                if (prev_day_high is not None and prev_day_low is not None)
                else {"pct": 0.5, "label": "sin_datos"}
            )
            logger.info(
                f"[{ticker}] BP={round(bp,4)} | "
                f"precio_pm={round(precio_pm_actual,4)} | "
                f"cutoff={cutoff_et.strftime('%H:%M:%S')}"
            )

            # GAP (vs prev_close)
            if prev_close > 0:
                gap_pct = ((precio_premarket - prev_close) / prev_close) * 100.0
                gap_type = "GAP_UP" if gap_pct > 0 else ("GAP_DOWN" if gap_pct < 0 else "FLAT")
            else:
                gap_pct = 0.0
                gap_type = "FLAT"

            cutoff_prev_1600 = datetime(
                prev_date_et.year,
                prev_date_et.month,
                prev_date_et.day,
                16,
                0,
                0,
                tzinfo=TIMEZONE,
            )

            # Port full: signals_3/9, badge_long y caution_nota
            signals_str = compute_signals_3_9(bars_dict, cutoff_et)
            badge_long = compute_badge_long(
                bars_dict,
                cutoff_time=cutoff_et,
                now_time=datetime.now(TIMEZONE),
                precio=float(precio_premarket),
            )
            caution_note = compute_caution_note(
                bars_dict, precio_premarket, cutoff_prev_1600, ticker=ticker
            )

            # Targets con asimetría por caution_nota + gap_type
            range_3d = TargetsCalculator.calculate_range_3d(
                ticker,
                daily_prev,
                pm_high=pm_high,
                pm_low=pm_low,
                bars_1h=bars_dict.get("1h", []),
                cutoff_time=cutoff_et,
            )
            targets = TargetsCalculator.calculate_targets(bp, range_3d, [caution_note, gap_type])
            logger.info(
                "[%s] TARGETS: int_pos=%.4f int_neg=%.4f max_pos=%.4f max_neg=%.4f",
                ticker,
                targets.get("int_pos"),
                targets.get("int_neg"),
                targets.get("max_pos"),
                targets.get("max_neg"),
            )

            logger.debug(
                f"[{ticker}] targets: "
                f"INT+={round(targets['int_pos'],2)} INT-={round(targets['int_neg'],2)} | "
                f"MAX+={round(targets['max_pos'],2)} MAX-={round(targets['max_neg'],2)} | "
                f"range_3d={round(range_3d,3)}"
            )

            # Aplicar filtros SKIP
            skip_reason = None
            if SkipFilter.apply_skip_filters(ticker, prev_day_change_pct, gap_pct, [gap_type]):
                skip_reason = "Filtro SKIP activado"

            dist_pm = precio_premarket - bp
            direction_pm = direction_from_dist(dist_pm)

            logger.info(
                f"[{ticker}] precio_pm={round(precio_pm_actual,4)} | "
                f"dist_bp={round(precio_pm_actual - bp, 4)} | "
                f"dir={direction_pm} | gap={gap_type} {round(gap_pct,3)}%"
            )
            _pm28_int_tgt = (
                targets["int_pos"]
                if direction_pm == "ALCISTA"
                else targets["int_neg"]
            )
            _pm28_recorrido = (
                round(abs(precio_pm_actual - _pm28_int_tgt), 4)
                if _pm28_int_tgt is not None
                else None
            )
            logger.info(
                f"[{ticker}] BP={round(bp, 4)} | "
                f"INT_POS={round(targets['int_pos'], 4)} | "
                f"INT_NEG={round(targets['int_neg'], 4)} | "
                f"MAX_POS={round(targets['max_pos'], 4)} | "
                f"MAX_NEG={round(targets['max_neg'], 4)} | "
                f"recorrido_int={_pm28_recorrido if _pm28_recorrido is not None else 'N/A'}"
            )

            _ec_block = compute_ema_conviction_block(
                bars_dict=bars_dict,
                cutoff_et=cutoff_et,
                cutoff_prev_1600=cutoff_prev_1600,
                precio_ref=precio_premarket,
                direction=direction_pm,
                gap_type=gap_type,
                gap_pct=gap_pct,
                prev_day_change_pct=prev_day_change_pct,
                caution_note=caution_note,
                caution_1v3=None,
                ticker=ticker,
            )
            ema20_pre = _ec_block["ema20_pre"]
            ema50_pre = _ec_block["ema50_pre"]
            ema200_pre = _ec_block["ema200_pre"]
            ema_align = _ec_block["ema_align"]
            ema_sr = _ec_block["ema_sr"]
            bb_expande_premarket = _ec_block["bb_expande_premarket"]
            ec_score = _ec_block["ec_score"]
            ec_accion = _ec_block["ec_accion"]
            ec_size = _ec_block["ec_size"]

            logger.info(
                f"[{ticker}] caution='{caution_note}' | "
                f"signals='{signals_str}' | "
                f"badge={round(badge_long,1)}% | "
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
                    f"align={ema_align} | dir={direction_pm} | "
                    f"prev_day={round(prev_day_change_pct,2)}% | "
                    f"n_caut={_n_caut_ec if '_n_caut_ec' in dir() else '?'}"
                )

            # Guardar datos
            output_data["tickers"][ticker] = {
                "bp": bp,
                "int_pos": targets["int_pos"],
                "int_neg": targets["int_neg"],
                "max_pos": targets["max_pos"],
                "max_neg": targets["max_neg"],
                "int_dist": targets["int_dist"],
                "max_dist": targets["max_dist"],
                "gap_pct": gap_pct,
                "gap_type": gap_type,
                "prev_day_change": prev_day_change_pct,
                "signals": signals_str,
                "signals_3_9": signals_str,
                "badge_long": round(badge_long, 2),
                "caution_note": caution_note,
                "skip": skip_reason is not None,
                "skip_reason": skip_reason,
                "dist": dist_pm,
                "direction": direction_pm,
                "prev_day_hint": _get_prev_day_hint(
                    ticker,
                    float(prev_day_change_pct)
                    if prev_day_change_pct not in (None, "", "nan")
                    else None,
                    direction_pm,
                ),
                "ema20_pre":            ema20_pre,
                "ema50_pre":            ema50_pre,
                "ema200_pre":           ema200_pre,
                "ema_align":            ema_align,
                "ema_sr":              ema_sr,
                "bb_expande_premarket": bb_expande_premarket,
                "ec_score":             ec_score,
                "ec_accion":            ec_accion,
                "ec_size":              ec_size,
                "rango_pm":             rango_pm,
                "rango_prev_day":       rango_prev_day,
            }

        except Exception as e:
            logger.error(f"[{ticker}] Error procesando: {e}", exc_info=True)
            continue

    tickers_ok = output_data.get("tickers", {})
    skips_n = sum(1 for t in tickers_ok.values() if t.get("skip"))
    zm_n = sum(1 for t in tickers_ok.values() if t.get("ec_accion") == "ZONA_MUERTA")
    x2_n = sum(1 for t in tickers_ok.values() if t.get("ec_accion") == "SIZE_x2")
    x1_n = sum(1 for t in tickers_ok.values() if t.get("ec_accion") == "SIZE_x1")
    logger.info(
        f"=== MODO premarket_9_28 completado | "
        f"OK={len(tickers_ok)} | SKIP={skips_n} | ZM={zm_n} | "
        f"SIZE_x2={x2_n} | SIZE_x1={x1_n} ==="
    )
    # ZM permanece en tickers (no se promueve a skips) hasta open_930 — ver finalize_dashboard_output
    finalize_dashboard_output(output_data)
    write_json(output_data, f"premarket_9_28_{date_str}.json")
    write_context_compressed(output_data, date_str, "premarket_9_28")
    write_ai_dashboard(output_data, date_str, "premarket_9_28")
