from __future__ import annotations

import logging
import time
from datetime import datetime

import numpy as np
import pandas as pd
import pytz

from dc2.constants import TIMEZONE, UTC, N_VELAS_BB
from dc2.utils import _filter_bars_before, _compute_bb_from_closes

logger = logging.getLogger(__name__)


class BPCalculator:
    """Calculates Break Point (BP) as average of EMA3/EMA9 across all timeframes."""

    @staticmethod
    def calculate_bp(bars_dict, cutoff_time: pd.Timestamp | None = None):
        """
        BP = mean(EMA3 per TF) + mean(EMA9 per TF) / 2.

        bars_dict: {"1m": [{"t": TimestampET, "c": float}, ...], ...}
        cutoff_time: filters bars with ts < cutoff_time (strict).
        Returns None if no TF has >= 3 bars.
        """
        ema3_list = []
        ema9_list = []

        for tf, bars in bars_dict.items():
            if not bars:
                continue

            if cutoff_time is not None:
                if isinstance(bars, pd.DataFrame):
                    cutoff = pd.Timestamp(cutoff_time)
                    if cutoff.tzinfo is None:
                        cutoff = cutoff.tz_localize(TIMEZONE)
                    else:
                        cutoff = cutoff.tz_convert(TIMEZONE)
                    df_slice = bars.loc[: cutoff - pd.Timedelta(nanoseconds=1)]
                    bars_filtered = df_slice.reset_index()[["t", "c"]].to_dict("records")
                else:
                    filtered = []
                    for b in bars:
                        ts = b["t"]
                        if ts.tzinfo is None:
                            ts = pd.Timestamp(ts).tz_localize(TIMEZONE)
                        if ts < cutoff_time:
                            filtered.append(b)
                    bars_filtered = filtered
            else:
                if isinstance(bars, pd.DataFrame):
                    bars_filtered = bars.reset_index()[["t", "c"]].to_dict("records")
                else:
                    bars_filtered = bars

            if len(bars_filtered) < 3:
                continue

            closes = pd.Series([float(b["c"]) for b in bars_filtered], dtype=float)
            ema3 = closes.ewm(span=3, adjust=False).mean().iloc[-1]
            ema9 = closes.ewm(span=9, adjust=False).mean().iloc[-1]
            logger.debug("[BP] tf=%s bars=%d ema3=%.4f ema9=%.4f", tf, len(bars_filtered), float(ema3), float(ema9))
            ema3_list.append(float(ema3))
            ema9_list.append(float(ema9))

        if not ema3_list:
            logger.warning("[BP] No TF with >=3 bars — BP=None")
            return None

        avg_ema3 = np.mean(ema3_list)
        avg_ema9 = np.mean(ema9_list)
        bp = (avg_ema3 + avg_ema9) / 2
        logger.info("[BP] tfs_used=%d avg_ema3=%.4f avg_ema9=%.4f BP=%.4f", len(ema3_list), avg_ema3, avg_ema9, bp)
        return bp


class TargetsCalculator:
    """Calculates INT/MAX targets from BP and range_3d."""

    @staticmethod
    def calculate_range_3d(
        ticker,
        daily_bars,
        pm_high=None,
        pm_low=None,
        bars_1h=None,
        cutoff_time=None,
    ):
        """
        Range for targets: BB(20,2) EWM width on 1H pre-cutoff, or fallback to avg(H-L) last 3 days.

        If bars_1h and cutoff_time provided: filters 1H with t < cutoff, last 20 closes,
        BBT-BBB = range_3d. If not enough data or BB is null: avg(H-L) last 3 days from daily_bars.
        """
        attempted_1h = bool(bars_1h) and cutoff_time is not None
        if attempted_1h:
            filtered = _filter_bars_before(bars_1h, cutoff_time)
            if len(filtered) >= N_VELAS_BB:
                closes = [float(b["c"]) for b in filtered[-N_VELAS_BB:]]
                bbt, bbb = _compute_bb_from_closes(closes)
                if bbt is not None and bbb is not None:
                    range_3d = float(bbt) - float(bbb)
                    if range_3d > 0:
                        logger.info("[RANGE3D] %s: BB_width_1H=%.4f", ticker, range_3d)
                        return range_3d

        if not daily_bars:
            return 0.0
        last3 = daily_bars[-3:]
        ranges = [float(b["h"]) - float(b["l"]) for b in last3 if float(b["h"]) > float(b["l"])]
        if not ranges:
            return 0.0
        return sum(ranges) / len(ranges)

    @staticmethod
    def calculate_targets(bp, range_3d, signals=None, *, caution_note=None, gap_type=None):
        """Calculates symmetric INT and MAX targets aligned with the Milton tool."""
        base_int = 0.33 * range_3d
        base_max = 0.66 * range_3d
        result = {
            "int_pos": bp + base_int,
            "int_neg": bp - base_int,
            "max_pos": bp + base_max,
            "max_neg": bp - base_max,
            "int_dist": base_int,
            "max_dist": base_max,
        }
        logger.info(
            "[TARGETS] BP=%.4f range3d=%.4f | int_pos=%.4f int_neg=%.4f max_pos=%.4f max_neg=%.4f",
            bp, range_3d, result["int_pos"], result["int_neg"], result["max_pos"], result["max_neg"],
        )
        return result
