from __future__ import annotations
import logging
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from dc2.constants import TICKERS, TIMEZONE, TF_LIST
from dc2.utils import (
    _filter_bars_before,
    _test_cutoff_from_1m,
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

_DATA_DIR = Path(__file__).parent.parent.parent / "data" / "processed"


async def run_mode_test(tickers_to_run: list, date_str: str, output_data: dict) -> None:
    import backtest_engine as _mbnv

    _mbnv.DATA_DIR = _DATA_DIR

    logger.info(f"=== MODO: test | tickers: {len(tickers_to_run)} ===")
    bars_by_ticker = {}
    alpaca_1m_ok = 0
    for ticker in tickers_to_run:
        bars_by_ticker[ticker] = {}
        for tf in TF_LIST:
            try:
                bars_by_ticker[ticker][tf] = AlpacaHistoricalClient.fetch_bars(
                    ticker,
                    tf,
                    (datetime.now(TIMEZONE) - timedelta(days=3)).strftime(
                        "%Y-%m-%d"
                    ),
                    datetime.now(TIMEZONE).strftime("%Y-%m-%d"),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Advertencia: %s %s fetch: %s", ticker, tf, exc)
                bars_by_ticker[ticker][tf] = []
        if bars_by_ticker[ticker].get("1m"):
            alpaca_1m_ok += 1
        else:
            logger.warning("Advertencia: %s: sin barras 1m desde Alpaca", ticker)

    banner_cutoff = None
    for ticker in tickers_to_run:
        try:
            bars_dict = bars_by_ticker[ticker]
            cutoff_et, cutoff_label = _test_cutoff_from_1m(bars_dict)
            if banner_cutoff is None:
                banner_cutoff = cutoff_label

            bars_1m_pre = _filter_bars_before(
                bars_dict.get("1m", []), cutoff_et
            )
            if not bars_1m_pre:
                logger.warning(
                    "Advertencia: %s: sin barras 1m antes del cutoff test", ticker
                )
                continue

            if not _validate_premarket_bars(
                ticker, bars_dict.get("1m", []), cutoff_et
            ):
                logger.warning(
                    "%s: _validate_premarket_bars falló en modo test — continúa de todas formas",
                    ticker,
                )

            bp = BPCalculator.calculate_bp(
                bars_dict, cutoff_time=pd.Timestamp(cutoff_et)
            )
            if bp is None:
                logger.warning("Advertencia: %s: BP no calculable", ticker)
                continue

            today_date_et = datetime.now(TIMEZONE).date()
            prev_date_et = today_date_et - timedelta(days=1)

            daily_bars = AlpacaHistoricalClient.fetch_bars(
                ticker,
                "1d",
                (datetime.now(TIMEZONE) - timedelta(days=10)).strftime(
                    "%Y-%m-%d"
                ),
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
                prev_day_change_pct = (
                    ((c_last - c_prev) / c_prev * 100.0) if c_prev else 0.0
                )
            else:
                prev_day_change_pct = 0.0

            precio_premarket = float(bars_1m_pre[-1]["c"])
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

            if prev_close > 0:
                gap_pct = ((precio_premarket - prev_close) / prev_close) * 100.0
                gap_type = (
                    "GAP_UP"
                    if gap_pct > 0
                    else ("GAP_DOWN" if gap_pct < 0 else "FLAT")
                )
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

            signals_str = compute_signals_3_9(bars_dict, cutoff_et)
            badge_long = compute_badge_long(
                bars_dict,
                cutoff_time=cutoff_et,
                now_time=datetime.now(TIMEZONE),
                precio=float(precio_premarket),
            )
            caution_note = compute_caution_note(
                bars_dict,
                precio_premarket,
                cutoff_prev_1600,
                ticker=ticker,
            )

            range_3d = TargetsCalculator.calculate_range_3d(ticker, daily_prev)
            targets = TargetsCalculator.calculate_targets(
                bp, range_3d, [caution_note, gap_type]
            )

            skip_reason = None
            if SkipFilter.apply_skip_filters(
                ticker, prev_day_change_pct, gap_pct, [gap_type]
            ):
                skip_reason = "Filtro SKIP activado"

            dist_pm = precio_premarket - bp
            direction_pm = direction_from_dist(dist_pm)

            precio_ref = precio_premarket
            _ec_block = compute_ema_conviction_block(
                bars_dict=bars_dict,
                cutoff_et=cutoff_et,
                cutoff_prev_1600=cutoff_prev_1600,
                precio_ref=precio_ref,
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
                "ema20_pre": ema20_pre,
                "ema50_pre": ema50_pre,
                "ema200_pre": ema200_pre,
                "ema_align": ema_align,
                "ema_sr": ema_sr,
                "bb_expande_premarket": bb_expande_premarket,
                "ec_score": ec_score,
                "ec_accion": ec_accion,
                "ec_size": ec_size,
                "test_cutoff_last_bar": cutoff_label,
                "rango_pm": rango_pm,
                "rango_prev_day": rango_prev_day,
            }

        except Exception as e:
            logger.error("Error procesando %s (test): %s", ticker, str(e))
            continue

    cutoffs_map = {
        t: output_data["tickers"][t].get("test_cutoff_last_bar")
        for t in output_data["tickers"]
        if output_data["tickers"][t].get("test_cutoff_last_bar")
    }
    output_data["test_meta"] = {
        "banner_line": (
            f"MODO TEST — cutoff (ultima barra 1m): {banner_cutoff or 'N/A'}"
        ),
        "cutoffs_by_ticker": cutoffs_map,
    }

    n_run = len(tickers_to_run)
    td_pre = output_data.get("tickers", {})
    bp_n = sum(1 for t in td_pre if td_pre[t].get("bp") is not None)
    ec_n = sum(1 for t in td_pre if td_pre[t].get("ec_score") is not None)

    finalize_dashboard_output(output_data)
    write_json(output_data, f"test_{date_str}.json")
    write_context_compressed(output_data, date_str, "test")
    write_ai_dashboard(output_data, date_str, "test")

    dash_path = _DATA_DIR.parent.parent / "outputs" / (
        f"ai_dashboard_prompt_{date_str}_test.txt"
    )
    logger.info("[!] MODO TEST — %s", (banner_cutoff or "sin cutoff"))
    logger.info("%s", "-" * 40)
    logger.info("Alpaca OK (1m): %s/%s tickers con datos", alpaca_1m_ok, n_run)
    logger.info("BP calculados: %s/%s", bp_n, n_run)
    logger.info("Scores EC: %s/%s", ec_n, n_run)
    logger.info("Archivo generado: %s", dash_path)
    logger.info(
        "Nota: caution_cambio_1v3 solo se calcula en --modo open_930 "
        "(requiere premarket_9_28 + open RTH)."
    )
    logger.info("%s", "-" * 40)
