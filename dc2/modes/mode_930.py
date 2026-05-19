from __future__ import annotations
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from backtest_engine import (
    build_tfs_rth,
    compute_caution_cambio_1v3,
    compute_caution_real_930,
    rth_mask_vectorized,
)

from dc2.constants import TIMEZONE, OPEN_930_TF_LIST, N_VELAS_BB
from dc2.utils import (
    _bars_list_to_mi_dataframe,
    _filter_bars_before,
    _filter_bars_upto_inclusive,
    _compute_bb_from_closes,
)
from dc2.indicators import (
    precio_en_rango,
    direction_from_dist,
    compute_ema_conviction_block,
    compute_open_zone,
    get_rebote_umbral,
    _get_prev_day_hint,
    evaluar_rebote,
    finalize_dashboard_output,
)
from dc2.premium_detector import PremiumDetector
from dc2.data_clients import AlpacaHistoricalClient, AlpacaSnapshotClient
from dc2.outputs import write_json, write_context_compressed, write_ai_dashboard
from dc2.print_dashboard import print_premarket_ticker

logger = logging.getLogger(__name__)


async def run_mode_930(tickers_to_run: list, date_str: str) -> None:
    logger.info(f"=== MODO: open_930 | tickers: {len(tickers_to_run)} ===")
    # Leer datos premarket_9_28
    try:
        with open(f"outputs/premarket_9_28_{date_str}.json", "r", encoding="utf-8") as f:
            premarket_data = json.load(f)
    except FileNotFoundError:
        logger.warning("Error: No se encontraron datos premarket_9_28")
        return

    # Obtener precios de apertura
    premarket_data["tickers_order"] = tickers_to_run
    for ticker in tickers_to_run:
        try:
            open_price: float | None = None
            if ticker not in premarket_data["tickers"]:
                if ticker in premarket_data.get("skips", {}):
                    td = premarket_data["skips"][ticker]
                    if not isinstance(td, dict):
                        logger.warning(
                            f"[{ticker}] skips sin dict (legacy) — omitido open_930"
                        )
                        continue
                else:
                    logger.warning(
                        f"[{ticker}] sin fila en premarket_9_28.json — omitido open_930"
                    )
                    continue
            else:
                td = premarket_data["tickers"][ticker]
            open_price = AlpacaSnapshotClient.get_open_price(ticker)
            if open_price is None:
                logger.warning("%s: sin open_930 (Alpaca)", ticker)
                continue
            bp = td["bp"]
            dist = open_price - bp
            _int_dist = float(td.get("int_dist") or 0.0)
            direction = direction_from_dist(dist, int_dist=_int_dist)

            td["open_930"] = open_price
            td["dist"] = dist
            td["direction"] = direction

            oz, opct = compute_open_zone(
                open_price,
                bp,
                td.get("int_pos"),
                td.get("int_neg"),
                direction,
                int_dist=_int_dist,
            )
            td["open_zone"] = oz
            td["open_pct_en_rango"] = opct
            td["rebote_umbral"] = get_rebote_umbral(_int_dist)

            logger.info(
                f"[{ticker}] open_930={open_price} | "
                f"open_zone={oz} | "
                f"dist_open={round(open_price - bp, 4)}"
            )
            _int_tgt = (
                td.get("int_pos")
                if direction == "ALCISTA"
                else td.get("int_neg")
            )
            _recorrido_int = (
                round(abs(open_price - _int_tgt), 4)
                if _int_tgt is not None
                else None
            )
            logger.info(
                f"[{ticker}] BP={round(bp, 4)} | "
                f"INT_POS={round(td.get('int_pos') or 0, 4)} | "
                f"INT_NEG={round(td.get('int_neg') or 0, 4)} | "
                f"MAX_POS={round(td.get('max_pos') or 0, 4)} | "
                f"MAX_NEG={round(td.get('max_neg') or 0, 4)} | "
                f"recorrido_int={_recorrido_int if _recorrido_int is not None else 'N/A'}"
            )

            direction_pm = direction
            gap_type = td["gap_type"]
            gap_pct = td["gap_pct"]
            prev_day_change_pct = td["prev_day_change"]

            prev_date_et = datetime.now(TIMEZONE).date() - timedelta(days=1)
            cutoff_et = datetime.now(TIMEZONE).replace(
                hour=9, minute=28, second=0, microsecond=0
            )
            cutoff_prev_1600 = datetime(
                prev_date_et.year,
                prev_date_et.month,
                prev_date_et.day,
                16,
                0,
                0,
                tzinfo=TIMEZONE,
            )

            precio_premarket = None
            end_fetch = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
            bars_dict: dict[str, list] = {}
            for tf in OPEN_930_TF_LIST:
                if tf == "1d":
                    # 1D: fetch 10 días igual que premarket_9_28 (L3600-3605)
                    start_fetch = (
                        datetime.now(TIMEZONE) - timedelta(days=10)
                    ).strftime("%Y-%m-%d")
                elif tf == "1h":
                    # 1H: fetch 365 días para tener suficientes velas BB (L3551)
                    start_fetch = (
                        datetime.now(TIMEZONE) - timedelta(days=365)
                    ).strftime("%Y-%m-%d")
                else:
                    # 1m/5m/15m/30m: 2 días es suficiente
                    start_fetch = (
                        datetime.now(TIMEZONE) - timedelta(days=2)
                    ).strftime("%Y-%m-%d")
                try:
                    bars_dict[tf] = AlpacaHistoricalClient.fetch_bars(
                        ticker, tf, start_fetch, end_fetch
                    )
                except Exception as _fetch_exc:
                    logger.warning(
                        f"[{ticker}] fetch {tf} falló: {_fetch_exc} — usando lista vacía"
                    )
                    bars_dict[tf] = []

            # 1D: filtrar barras hasta cutoff (excluir el día de hoy incompleto)
            if "1d" in bars_dict:
                bars_dict["1d"] = _filter_bars_before(bars_dict["1d"], cutoff_et)

            bars_1m_pre = _filter_bars_before(bars_dict.get("1m", []), cutoff_et)
            # Synthetic premarket candle high/low (4am to cutoff)
            _bars_pm_4am = [b for b in bars_1m_pre if b["t"].hour >= 4]
            pm_high = max((float(b["h"]) for b in _bars_pm_4am), default=None)
            pm_low  = min((float(b["l"]) for b in _bars_pm_4am), default=None)
            precio_premarket = float(bars_1m_pre[-1]["c"]) if bars_1m_pre else None

            # prev day high/low desde 1d (última vela daily previa a cutoff)
            prev_day_high = float(bars_dict["1d"][-1]["h"]) if bars_dict.get("1d") else None
            prev_day_low  = float(bars_dict["1d"][-1]["l"]) if bars_dict.get("1d") else None

            rango_pm = (
                precio_en_rango(float(precio_premarket), pm_high, pm_low)
                if (precio_premarket is not None and pm_high is not None and pm_low is not None)
                else {"pct": 0.5, "label": "sin_datos"}
            )
            rango_prev_day = (
                precio_en_rango(float(precio_premarket), prev_day_high, prev_day_low)
                if (precio_premarket is not None and prev_day_high is not None and prev_day_low is not None)
                else {"pct": 0.5, "label": "sin_datos"}
            )

            # ── Logging técnico: velas por TF ─────────────────────────
            for tf, bars in bars_dict.items():
                n = len(bars)
                if n > 0:
                    first_ts = str(bars[0]["t"])[:16]
                    last_ts = str(bars[-1]["t"])[:16]
                else:
                    first_ts = last_ts = "sin datos"
                logger.debug(f"[{ticker}] {tf}: {n} velas | {first_ts} → {last_ts}")

            td["caution_real_930"] = None
            td["caution_cambio_1v3"] = None
            tfs_live = {
                tf: _bars_list_to_mi_dataframe(bars_dict.get(tf, []))
                for tf in OPEN_930_TF_LIST
            }
            try:
                fecha_today = datetime.now(TIMEZONE).date()
                ts_930 = pd.Timestamp(
                    datetime.now(TIMEZONE).replace(
                        hour=9, minute=30, second=0, microsecond=0
                    )
                )
                _inyecto_vela_930 = False
                for tf in list(tfs_live.keys()):
                    df_tf = tfs_live[tf]
                    if df_tf is None or df_tf.empty:
                        continue
                    if "date_et" not in df_tf.columns:
                        continue
                    mask_hoy = df_tf["date_et"] == fecha_today
                    ya_rth_hoy = False
                    if mask_hoy.any():
                        sub_hoy = df_tf.loc[mask_hoy]
                        ya_rth_hoy = bool(rth_mask_vectorized(sub_hoy).any())
                    if ya_rth_hoy:
                        continue
                    fila = pd.DataFrame(
                        [
                            {
                                "timestamp": ts_930,
                                "open": float(open_price),
                                "high": float(open_price),
                                "low": float(open_price),
                                "close": float(open_price),
                                "volume": 0.0,
                                "date_et": fecha_today,
                            }
                        ]
                    )
                    tfs_live[tf] = (
                        pd.concat([df_tf, fila], ignore_index=True)
                        .sort_values("timestamp")
                        .reset_index(drop=True)
                    )
                    _inyecto_vela_930 = True
                if _inyecto_vela_930:
                    logger.info(
                        f"[{ticker}] vela RTH inyectada: open_930={open_price} | ts={ts_930}"
                    )
                tfs_rth_l = build_tfs_rth(tfs_live, ticker)
                cr930 = compute_caution_real_930(
                    tfs_live,
                    fecha_today,
                    open_price,
                    ticker,
                    tfs_rth=tfs_rth_l,
                )
                td["caution_real_930"] = cr930
                td["caution_cambio_1v3"] = compute_caution_cambio_1v3(
                    td.get("caution_note"),
                    cr930,
                    ticker,
                    tfs_live,
                )
                logger.info(
                    f"[{ticker}] caution_real_930='{cr930}' | "
                    f"1v3='{td.get('caution_cambio_1v3')}'"
                )
                if cr930 == "Sin datos BB":
                    logger.warning(
                        f"[{ticker}] caution_real_930='Sin datos BB' — "
                        f"parquets RTH sin datos suficientes"
                    )
            except Exception as _caut_exc:
                logger.warning("  [caution 1v3] %s: %s", ticker, _caut_exc)

            _caution_1v3_val = td.get("caution_cambio_1v3")
            _dir_for_score = (
                direction_from_dist(open_price - bp, int_dist=_int_dist)
                if open_price is not None and _caution_1v3_val is not None
                else direction_pm
            )
            _ec_block = compute_ema_conviction_block(
                bars_dict=bars_dict,
                cutoff_et=cutoff_et,
                cutoff_prev_1600=cutoff_prev_1600,
                precio_ref=float(open_price),
                direction=_dir_for_score,
                gap_type=gap_type,
                gap_pct=gap_pct,
                prev_day_change_pct=prev_day_change_pct,
                caution_note=td.get("caution_real_930"),
                caution_1v3=_caution_1v3_val,
                ticker=ticker,
                open_930=float(open_price),
                int_pos=td.get("int_pos"),
                int_neg=td.get("int_neg"),
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
                f"[{ticker}] caution='{td.get('caution_note')}' | "
                f"signals='{td.get('signals_3_9')}' | "
                f"badge={round(td.get('badge_long', 0.0),1)}% | "
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
                    f"prev_day={round(prev_day_change_pct,2)}% | "
                    f"n_caut={_n_caut_ec if '_n_caut_ec' in dir() else '?'}"
                )

            td["ema20_pre"] = ema20_pre
            td["ema50_pre"] = ema50_pre
            td["ema200_pre"] = ema200_pre
            td["ema_align"] = ema_align
            td["ema_sr"] = ema_sr
            td["bb_expande_premarket"] = bb_expande_premarket
            td["ec_score"] = ec_score
            td["ec_accion"] = ec_accion
            td["ec_size"] = ec_size
            td["rango_pm"] = rango_pm
            td["rango_prev_day"] = rango_prev_day

            # Re-evaluar con open_zone + 1v3 ya cargados (coherente antes de finalize)
            td["evaluation"] = PremiumDetector.evaluate(ticker, td)

            _pdc_oz = td.get("prev_day_change")
            _prev_f_oz = (
                float(_pdc_oz)
                if _pdc_oz not in (None, "", "nan")
                else None
            )
            td["prev_day_hint"] = _get_prev_day_hint(
                ticker, _prev_f_oz, td.get("direction", "ZONA_MUERTA")
            )
            td["rebote_hint"] = evaluar_rebote(
                float(td.get("rebote_actual", 0)),
                float(td.get("int_dist", 0)),
            )

            logger.info(
                f"[{ticker}] open_930 resultado: ec_accion={ec_accion} | "
                f"ec_score={ec_score}"
            )
            print_premarket_ticker(
                ticker,
                td,
                "open_930",
                float(open_price),
                datetime.now(TIMEZONE),
            )

        except Exception as e:
            logger.error(f"[{ticker}] Error procesando: {e}", exc_info=True)
            continue

    premarket_data.setdefault("skips", {})
    td_map_930 = premarket_data.get("tickers", {})
    n_univ_930 = len(tickers_to_run)
    ec_skip_n = sum(
        1
        for t in tickers_to_run
        if td_map_930.get(t, {}).get("ec_accion") == "SKIP"
    )
    zm_n = sum(
        1
        for t in tickers_to_run
        if td_map_930.get(t, {}).get("ec_accion") == "ZONA_MUERTA"
    )
    x2_n = sum(
        1
        for t in tickers_to_run
        if td_map_930.get(t, {}).get("ec_accion") == "SIZE_x2"
    )
    x1_n = sum(
        1
        for t in tickers_to_run
        if td_map_930.get(t, {}).get("ec_accion") == "SIZE_x1"
    )
    logger.info(
        f"=== MODO open_930 completado | "
        f"OK={n_univ_930} | SKIP={ec_skip_n} | ZM={zm_n} | "
        f"SIZE_x2={x2_n} | SIZE_x1={x1_n} ==="
    )
    finalize_dashboard_output(premarket_data)
    write_json(premarket_data, f"open_930_{date_str}.json")
    write_context_compressed(premarket_data, date_str, "open_930")
    write_ai_dashboard(premarket_data, date_str, "open_930")
