from __future__ import annotations
import logging
from datetime import datetime, date
from pathlib import Path

import pandas as pd
import numpy as np

from backtest_engine import (
    build_tfs_rth,
    compute_caution_cambio_1v3,
    compute_caution_real_930,
    mid_tf_key,
)

from dc2.constants import TIMEZONE, UTC, TF_LIST, N_VELAS_BB
from dc2.utils import (
    _bars_list_to_mi_dataframe,
    _filter_bars_before,
    _filter_bars_upto_inclusive,
    _compute_bb_from_closes,
)
from dc2.indicators import (
    compute_badge_long,
    compute_caution_note,
    compute_signals_3_9,
    conviction_score,
    ema_alignment,
    ema_sr_context,
    direction_from_dist,
    compute_ema_conviction_block,
)
from dc2.premium_detector import _caution_note_short
from dc2.data_clients import BPCalculator, TargetsCalculator

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent.parent / "data" / "processed"


def _load_parquet_as_bars(
    ticker: str, tf: str, since: pd.Timestamp | None = None
) -> list:
    path = DATA_DIR / f"{ticker}_{tf}.parquet"
    if not path.exists():
        return []
    df = pd.read_parquet(path)
    df.columns = [str(c).lower() for c in df.columns]
    if "timestamp" not in df.columns:
        df = df.reset_index()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    if df["timestamp"].dt.tz is None:
        df["timestamp"] = df["timestamp"].dt.tz_localize("UTC")
    df["timestamp"] = df["timestamp"].dt.tz_convert(TIMEZONE)

    if since is not None:
        df = df[df["timestamp"] >= since]

    col_map = {}
    for long, short in [
        ("open", "o"),
        ("high", "h"),
        ("low", "l"),
        ("close", "c"),
        ("volume", "v"),
    ]:
        if long in df.columns and short not in df.columns:
            col_map[long] = short
    if col_map:
        df = df.rename(columns=col_map)

    for col in ["o", "h", "l", "c", "v"]:
        if col not in df.columns:
            df[col] = 0.0
        else:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    records = df[["timestamp", "o", "h", "l", "c", "v"]].copy()
    records = records.rename(columns={"timestamp": "t"})
    return records.to_dict("records")

def _open_930_from_bars_1m(bars_1m: list, day: date) -> float | None:
    """Open de la primera vela 1m con t >= 09:30 ET del día `day` (sesión regular)."""
    if not bars_1m:
        return None
    day_start = TIMEZONE.localize(datetime(day.year, day.month, day.day, 9, 30, 0))
    day_end = TIMEZONE.localize(datetime(day.year, day.month, day.day, 16, 0, 0))
    best_ts = None
    first_o: float | None = None
    for b in bars_1m:
        t = b["t"]
        if getattr(t, "tzinfo", None) is None:
            t = TIMEZONE.localize(t)
        else:
            t = t.astimezone(TIMEZONE)
        if t.date() != day or t < day_start or t >= day_end:
            continue
        if best_ts is None or t < best_ts:
            best_ts = t
            first_o = float(b["o"])
    return first_o

def _short_caution_cell(s: str | None, max_len: int = 22) -> str:
    if not s:
        return "—"
    one = s.replace("\n", " ").strip()
    return one if len(one) <= max_len else one[: max_len - 2] + ".."


def _pm_range(bars_1m: list, day: date) -> tuple[float | None, float | None]:
    """High/Low premarket 4:00am–9:28am ET."""
    highs, lows = [], []
    open_4am = TIMEZONE.localize(datetime(day.year, day.month, day.day, 4, 0, 0))
    cutoff   = TIMEZONE.localize(datetime(day.year, day.month, day.day, 9, 28, 0))
    for b in bars_1m:
        t = b["t"]
        if getattr(t, "tzinfo", None) is None:
            t = TIMEZONE.localize(t)
        else:
            t = t.astimezone(TIMEZONE)
        if t.date() == day and open_4am <= t < cutoff:
            highs.append(float(b["h"]))
            lows.append(float(b["l"]))
    return (max(highs) if highs else None, min(lows) if lows else None)


def _price_level(price: float | None, bp: float, int_pos: float, int_neg: float) -> str:
    """T1/T2/T3 label según posición respecto a BP."""
    if price is None:
        return "—"
    rng_pos = int_pos - bp
    rng_neg = bp - int_neg
    zm_pos  = bp + rng_pos * 0.11
    zm_neg  = bp - rng_neg * 0.11
    t1_pos  = bp + rng_pos / 3
    t2_pos  = bp + rng_pos * 2 / 3
    t1_neg  = bp - rng_neg / 3
    t2_neg  = bp - rng_neg * 2 / 3

    if price >= int_pos:   return "INT+"
    elif price >= t2_pos:  return "T3+"
    elif price >= t1_pos:  return "T2+"
    elif price >= zm_pos:  return "T1+"
    elif price > zm_neg:   return "ZM"
    elif price >= t2_neg:  return "T1-"
    elif price >= t1_neg:  return "T2-"
    elif price >= int_neg: return "T3-"
    else:                  return "INT-"


def _fmt_signals(raw: str) -> str:
    """Convierte '3/9 up 15m, 3/9 down 30m, 3/9 up 1h' → '15m ↑  30m ↓  1h ↑'"""
    if not raw:
        return "—"
    parts = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        tf = chunk.split()[-1]  # último token = timeframe
        arrow = "↑" if "up" in chunk else "↓"
        parts.append(f"{tf} {arrow}")
    return "  ".join(parts)


def _cau1v3_accion(raw: str | None) -> str:
    """Traduce caution_cambio_1v3 a decisión operativa."""
    if raw is None:
        return "—"
    r = raw.strip().lower()
    if "sin caution" in r or "sin cambio" in r or r == "":
        return "ENTRAR"
    if "cambio" in r or "bbt" in r or "bbb" in r:
        return "ESPERAR"
    return "ESPERAR"

def run_verify_mode(ticker: str) -> None:
    """Últimos 30 días hábiles: BP, targets; open 9:30 + caution_real_930 + 1v3 (parquets)."""
    from pandas.tseries.offsets import BDay

    end_ts = pd.Timestamp.now(tz=TIMEZONE).normalize()
    bdays_idx = pd.bdate_range(end=end_ts, periods=30, freq="B")
    load_from = end_ts - pd.Timedelta(days=90)
    trading_days: list[date] = [t.date() for t in bdays_idx]

    all_tf: dict[str, list] = {}
    for tf in TF_LIST:
        all_tf[tf] = _load_parquet_as_bars(ticker, tf, since=load_from)
        if not all_tf[tf]:
            logger.error(
                "Parquet no encontrado: %s_%s.parquet (buscando en: %s)",
                ticker,
                tf,
                DATA_DIR,
            )
            return

    # Para daily usar también el parquet 1d
    daily_bars = _load_parquet_as_bars(ticker, "1d", since=load_from)
    if not daily_bars:
        # Fallback: derivar daily desde 1h si existe
        bars_1h = _load_parquet_as_bars(ticker, "1h", since=load_from)
        if bars_1h:
            df_1h = pd.DataFrame(bars_1h)
            df_1h["date"] = df_1h["t"].dt.date
            daily_bars = []
            for d, grp in df_1h.groupby("date"):
                daily_bars.append({
                    "t": pd.Timestamp(d, tz=TIMEZONE),
                    "o": float(grp["o"].iloc[0]),
                    "h": float(grp["h"].max()),
                    "l": float(grp["l"].min()),
                    "c": float(grp["c"].iloc[-1]),
                    "v": float(grp["v"].sum()),
                })
    daily_bars.sort(key=lambda x: x["t"])

    mid = mid_tf_key(ticker)
    all_tf_open: dict[str, list] = dict(all_tf)
    all_tf_open[mid] = _load_parquet_as_bars(ticker, mid, since=load_from)
    all_tf_open["1d"] = daily_bars
    open_keys = list(dict.fromkeys(["1m", mid, "5m", "15m", "30m", "1h", "1d"]))
    tfs_live_full = {
        k: _bars_list_to_mi_dataframe(all_tf_open[k])
        for k in open_keys
        if k in all_tf_open and all_tf_open[k]
    }
    tfs_rth_once = build_tfs_rth(tfs_live_full, ticker)
    can_1v3 = bool(all_tf_open[mid]) and bool(daily_bars)
    if not can_1v3:
        logger.warning(
            "[verify] Sin parquet mid (%s) o 1d — caution_real_930/1v3 omitidos (%s)",
            mid,
            DATA_DIR,
        )
    elif tfs_rth_once is None:
        logger.warning("[verify] build_tfs_rth None — 1v3 omitido")

    rows_ok = []
    skipped = 0
    caution_days = 0

    for D in trading_days:
        cutoff_et = TIMEZONE.localize(datetime(D.year, D.month, D.day, 9, 28, 0, 0))
        prev_bday = (pd.Timestamp(D) - BDay(1)).date()
        cutoff_prev_1600 = TIMEZONE.localize(
            datetime(prev_bday.year, prev_bday.month, prev_bday.day, 16, 0, 0, 0)
        )

        bars_dict: dict[str, list] = {}
        for tf in TF_LIST:
            bars_dict[tf] = _filter_bars_before(all_tf[tf], cutoff_et)

        bp = BPCalculator.calculate_bp(bars_dict, cutoff_time=pd.Timestamp(cutoff_et))
        bars_1m_pre = _filter_bars_before(bars_dict.get("1m", []), cutoff_et)
        if bp is None or not bars_1m_pre:
            skipped += 1
            continue

        bars_dict["1d"] = _filter_bars_before(daily_bars, cutoff_et)

        precio_premarket = float(bars_1m_pre[-1]["c"])

        daily_prev = [b for b in daily_bars if b["t"].date() < D]
        daily_prev.sort(key=lambda x: x["t"])
        prev_close = float(daily_prev[-1]["c"]) if daily_prev else 0.0
        if prev_close > 0:
            gap_pct = ((precio_premarket - prev_close) / prev_close) * 100.0
            gap_type = "GAP_UP" if gap_pct > 0 else ("GAP_DOWN" if gap_pct < 0 else "FLAT")
        else:
            gap_pct = 0.0
            gap_type = "FLAT"

        signals_str = compute_signals_3_9(bars_dict, cutoff_et)
        badge_long = compute_badge_long(
            bars_dict,
            cutoff_time=cutoff_et,
            now_time=cutoff_et,
            precio=float(precio_premarket),
        )
        caution_full = compute_caution_note(
            bars_dict, precio_premarket, cutoff_prev_1600, ticker=ticker
        )
        caution_disp = _caution_note_short(caution_full)
        if caution_full.strip() != "Sin caution":
            caution_days += 1

        range_3d = TargetsCalculator.calculate_range_3d(ticker, daily_prev)
        targets = TargetsCalculator.calculate_targets(bp, range_3d, [caution_full, gap_type])

        prev_day_change_pct = 0.0
        if len(daily_prev) >= 2:
            c_last = float(daily_prev[-1]["c"])
            c_prev = float(daily_prev[-2]["c"])
            prev_day_change_pct = (
                ((c_last - c_prev) / c_prev * 100.0) if c_prev else 0.0
            )

        dist_pm = precio_premarket - bp
        direction_pm = direction_from_dist(dist_pm)

        open_price = _open_930_from_bars_1m(all_tf["1m"], D)
        direction_ec = (
            direction_from_dist(open_price - bp)
            if open_price is not None
            else direction_pm
        )

        cr930: str | None = None
        c1v3: str | None = None
        if can_1v3 and tfs_rth_once is not None and open_price is not None:
            try:
                cr930 = compute_caution_real_930(
                    tfs_live_full,
                    D,
                    open_price,
                    ticker,
                    tfs_rth=tfs_rth_once,
                )
                c1v3 = compute_caution_cambio_1v3(
                    caution_full, cr930, ticker, tfs_live_full
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[verify 1v3] %s: %s", D, exc)

        # Dirección real al abrir (con 1v3 disponible usa open_930, si no usa premarket)
        _dir_for_score = (
            direction_from_dist(open_price - bp)
            if open_price is not None and c1v3 is not None
            else direction_ec
        )

        # verify usa all_tf["1h"] como fuente principal para EMAs
        if "1h" not in bars_dict:
            bars_dict["1h"] = _filter_bars_before(all_tf["1h"], cutoff_et)

        _ec_block = compute_ema_conviction_block(
            bars_dict=bars_dict,
            cutoff_et=cutoff_et,
            cutoff_prev_1600=cutoff_prev_1600,
            precio_ref=open_price if open_price is not None else precio_premarket,
            direction=_dir_for_score,
            gap_type=gap_type,
            gap_pct=gap_pct,
            prev_day_change_pct=prev_day_change_pct,
            caution_note=caution_full,
            caution_1v3=c1v3,
            ticker=ticker,
            open_930=open_price,
            int_pos=targets["int_pos"],
            int_neg=targets["int_neg"],
        )
        ema_align = _ec_block["ema_align"]
        ema_sr = _ec_block["ema_sr"]
        ec_score = _ec_block["ec_score"]
        ec_accion = _ec_block["ec_accion"]
        ec_size = _ec_block["ec_size"]

        # Prev day high/low
        prev_high = float(daily_prev[-1]["h"]) if daily_prev else None
        prev_low = float(daily_prev[-1]["l"]) if daily_prev else None

        # Premarket sintético 4am–9:28
        pm_high, pm_low = _pm_range(all_tf["1m"], D)

        # PREV DAY: mostrar solo el nivel más cercano al precio actual
        if prev_high is not None and prev_low is not None:
            dist_to_ph = abs(precio_premarket - prev_high)
            dist_to_pl = abs(precio_premarket - prev_low)
            if dist_to_ph <= dist_to_pl:
                prev_day_label = f"H {'↑' if precio_premarket >= prev_high else '↓'}"
            else:
                prev_day_label = f"L {'↑' if precio_premarket >= prev_low else '↓'}"
        else:
            prev_day_label = "—"

        # PM sintético: mostrar solo el nivel más cercano
        if pm_high is not None and pm_low is not None:
            dist_to_mh = abs(precio_premarket - pm_high)
            dist_to_ml = abs(precio_premarket - pm_low)
            if dist_to_mh <= dist_to_ml:
                pm_label = f"H {'↑' if precio_premarket >= pm_high else '↓'}"
            else:
                pm_label = f"L {'↑' if precio_premarket >= pm_low else '↓'}"
        else:
            pm_label = "—"

        rows_ok.append(
            (
                D.strftime("%Y-%m-%d"),
                bp, signals_str, caution_disp, badge_long,
                targets["int_pos"], targets["int_neg"],
                targets["max_pos"], targets["max_neg"],
                open_price,
                _short_caution_cell(cr930, 28),
                c1v3,
                gap_pct, gap_type, precio_premarket, dist_pm, direction_pm,
                prev_day_change_pct,
                ema_align, ema_sr, ec_score, ec_accion,
                prev_high, prev_low, prev_close,
                pm_high, pm_low,
                prev_day_label, pm_label,
            )
        )

    # ── OUTPUT TARJETAS ──────────────────────────────────────────
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    WIDE = 70
    DIV  = "═" * WIDE
    SEP  = "─" * WIDE

    print(f"\n  VERIFY {ticker}  │  {trading_days[0]}  →  {trading_days[-1]}\n")

    for row in rows_ok:
        (
            fdt, bp, sig, caut, badge,
            ip, ine, mp, mn,
            op930, cau930_s, c1v3,
            gap_pct, gap_type, precio_pm, dist_pm, dir_pm,
            prev_chg,
            ema_a, ema_sr_s, ecs, eca,
            prev_high, prev_low, prev_close,
            pm_high, pm_low,
            prev_day_label, pm_label,
        ) = row

        def _ps(v): return f"${v:.2f}" if v is not None else "—"  # noqa: ANN001,E701
        def _ds(v): return f"{v:+.2f}" if v is not None else "—"  # noqa: ANN001,E701

        nivel_pm  = _price_level(precio_pm, bp, ip, ine)
        nivel_930 = _price_level(op930,     bp, ip, ine)
        op_s  = f"${op930:.2f}" if op930 is not None else "—"
        ecs_s = str(ecs) if ecs is not None else "—"

        print(DIV)
        print(f"  {fdt}  {ticker}  │  BP {_ps(bp)}  │  GAP {gap_pct:+.2f}% {gap_type}  │  PM {_ps(precio_pm)}")
        print(SEP)
        print(f"  TARGETS   │  INT+ {_ps(ip)}  INT- {_ps(ine)}  MAX+ {_ps(mp)}  MAX- {_ps(mn)}")
        print(f"  NIVELES   │  PM→ {nivel_pm:<6}  OPEN9:30→ {nivel_930:<6}")
        print(f"  PREMARKET │  Dir: {dir_pm:<4}  dist BP: {_ds(dist_pm)}  Badge: {badge:.1f}%  PrevChg: {prev_chg:+.2f}%")
        print(f"  PREV DAY  │  {prev_day_label}")
        print(f"  PM 4-9:28 │  {pm_label}")
        print(f"  EMA 3v9   │  {_fmt_signals(sig)}")
        print(f"  CAU-PM    │  {caut if caut else '—'}")
        print(f"  OPEN9:30  │  {op_s}  CAU-930: {cau930_s}  CAU-1v3: {_cau1v3_accion(c1v3)}")
        print(f"  EC BLOCK  │  Score: {ecs_s}  Accion: {eca}  EMA: {ema_a}  SR: {ema_sr_s}")

    print(DIV)
    print()
    print(f"  Total días:         {len(trading_days)}")
    print(f"  Procesados OK:      {len(rows_ok)}")
    print(f"  Skips (sin datos):  {skipped}")
    print(f"  Días con caution:   {caution_days}")
    print(DIV)


_GRUPOS = {
    "MACRO":      ["TLT", "GLD"],
    "ETFs":       ["QQQ", "SPY", "DIA", "IWM"],
    "SEMIS":      ["NVDA", "AMD"],
    "MEGA-TECH":  ["META", "AAPL", "MSFT", "AMZN", "GOOGL", "TSLA"],
}

_SEMAFORO_MACRO = {
    ("ALC", "ALC"): "⚠  PANIC MODE — ambos ALC → SKIP todos los alcistas",
    ("BAJ", "BAJ"): "✅ RISK-ON MÁXIMO — ambos BAJ → full size alcistas",
    ("ALC", "BAJ"): "→  Huida a USD — TLT ALC / GLD BAJ",
    ("BAJ", "ALC"): "🔥 Inflación — TLT BAJ / GLD ALC → GLD ALC máxima conviction",
    ("ZM",  "ZM"):  "～  Macro indeciso — señales individuales",
}


async def run_verify_today(tickers: list[str]) -> None:
    """Output compacto de hoy para todos los tickers agrupados por familia."""
    from pandas.tseries.offsets import BDay
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    end_ts = pd.Timestamp.now(tz=TIMEZONE).normalize()
    D = end_ts.date()
    load_from = end_ts - pd.Timedelta(days=90)

    WIDE = 78
    DIV  = "═" * WIDE
    SEP  = "─" * WIDE

    print(f"\n{'═'*WIDE}")
    print(f"  VERIFY TODAY  │  {D}  │  09:28 ET")
    print(f"{'═'*WIDE}")

    results: dict[str, dict] = {}

    for ticker in tickers:
        all_tf: dict[str, list] = {}
        ok = True
        for tf in TF_LIST:
            bars = _load_parquet_as_bars(ticker, tf, since=load_from)
            if not bars:
                ok = False
                break
            all_tf[tf] = bars
        if not ok:
            results[ticker] = {"error": True}
            continue

        daily_bars = _load_parquet_as_bars(ticker, "1d", since=load_from)
        if not daily_bars:
            results[ticker] = {"error": True}
            continue
        daily_bars.sort(key=lambda x: x["t"])

        cutoff_et = TIMEZONE.localize(datetime(D.year, D.month, D.day, 9, 28, 0))
        prev_bday = (pd.Timestamp(D) - BDay(1)).date()
        cutoff_prev_1600 = TIMEZONE.localize(
            datetime(prev_bday.year, prev_bday.month, prev_bday.day, 16, 0, 0)
        )

        bars_dict: dict[str, list] = {
            tf: _filter_bars_before(all_tf[tf], cutoff_et) for tf in TF_LIST
        }
        bars_dict["1d"] = _filter_bars_before(daily_bars, cutoff_et)

        bp = BPCalculator.calculate_bp(bars_dict, cutoff_time=pd.Timestamp(cutoff_et))
        bars_1m_pre = _filter_bars_before(bars_dict.get("1m", []), cutoff_et)
        if bp is None or not bars_1m_pre:
            results[ticker] = {"error": True}
            continue

        precio_pm = float(bars_1m_pre[-1]["c"])
        daily_prev = [b for b in daily_bars if b["t"].date() < D]
        daily_prev.sort(key=lambda x: x["t"])
        prev_close = float(daily_prev[-1]["c"]) if daily_prev else 0.0
        prev_high  = float(daily_prev[-1]["h"]) if daily_prev else None
        prev_low   = float(daily_prev[-1]["l"]) if daily_prev else None

        gap_pct  = ((precio_pm - prev_close) / prev_close * 100.0) if prev_close else 0.0
        gap_type = "GAP_UP" if gap_pct > 0 else ("GAP_DOWN" if gap_pct < 0 else "FLAT")
        prev_chg = 0.0
        if len(daily_prev) >= 2:
            c1, c2 = float(daily_prev[-1]["c"]), float(daily_prev[-2]["c"])
            prev_chg = (c1 - c2) / c2 * 100.0 if c2 else 0.0

        caution_full = compute_caution_note(
            bars_dict, precio_pm, cutoff_prev_1600, ticker=ticker
        )
        badge_long = compute_badge_long(
            bars_dict,
            cutoff_time=cutoff_et,
            now_time=cutoff_et,
            precio=float(precio_pm),
        )
        signals_str = compute_signals_3_9(bars_dict, cutoff_et)
        range_3d   = TargetsCalculator.calculate_range_3d(ticker, daily_prev)
        targets    = TargetsCalculator.calculate_targets(bp, range_3d, [caution_full, gap_type])

        dist_pm   = precio_pm - bp
        dir_pm    = direction_from_dist(dist_pm)
        nivel_pm  = _price_level(precio_pm, bp, targets["int_pos"], targets["int_neg"])

        pm_high, pm_low = _pm_range(all_tf["1m"], D)
        # PREV DAY: mostrar solo el nivel más cercano al precio actual
        if prev_high is not None and prev_low is not None:
            dist_to_ph = abs(precio_pm - prev_high)
            dist_to_pl = abs(precio_pm - prev_low)
            if dist_to_ph <= dist_to_pl:
                prev_day_label = f"H {'↑' if precio_pm >= prev_high else '↓'}"
            else:
                prev_day_label = f"L {'↑' if precio_pm >= prev_low else '↓'}"
        else:
            prev_day_label = "—"

        # PM sintético: mostrar solo el nivel más cercano
        if pm_high is not None and pm_low is not None:
            dist_to_mh = abs(precio_pm - pm_high)
            dist_to_ml = abs(precio_pm - pm_low)
            if dist_to_mh <= dist_to_ml:
                pm_label = f"H {'↑' if precio_pm >= pm_high else '↓'}"
            else:
                pm_label = f"L {'↑' if precio_pm >= pm_low else '↓'}"
        else:
            pm_label = "—"

        _ec = compute_ema_conviction_block(
            bars_dict=bars_dict,
            cutoff_et=cutoff_et,
            cutoff_prev_1600=cutoff_prev_1600,
            precio_ref=precio_pm,
            direction=dir_pm,
            gap_type=gap_type,
            gap_pct=gap_pct,
            prev_day_change_pct=prev_chg,
            caution_note=caution_full,
            caution_1v3=None,
            ticker=ticker,
            open_930=None,
            int_pos=targets["int_pos"],
            int_neg=targets["int_neg"],
        )
        ec_score  = _ec["ec_score"]
        ec_accion = _ec["ec_accion"]

        caut_short = _caution_note_short(caution_full)
        ecs_s = str(ec_score) if ec_score is not None else "—"

        results[ticker] = {
            "bp": bp, "nivel": nivel_pm, "dir": dir_pm,
            "badge": badge_long, "cau": caut_short,
            "signals": _fmt_signals(signals_str),
            "ec_score": ecs_s, "ec_accion": ec_accion,
            "prev_day_label": prev_day_label,
            "pm_label": pm_label,
            "error": False,
        }

    # Semáforo macro
    tlt = results.get("TLT", {})
    gld = results.get("GLD", {})
    if not tlt.get("error") and not gld.get("error"):
        key = (tlt.get("dir", "ZM"), gld.get("dir", "ZM"))
        semaforo = _SEMAFORO_MACRO.get(key, "～  Macro indeciso")
        print(f"\n  ▸ MACRO — SEMÁFORO TLT + GLD")
        print(SEP)
        for t in ["TLT", "GLD"]:
            r = results[t]
            print(f"  {t:<5} │ BP ${r['bp']:>8.2f} │ {r['dir']:<4} {r['nivel']:<6} │ Badge {r['badge']:4.1f}% │ {r['cau']}")
        print(f"\n  {semaforo}\n")

    # Grupos
    HDR = f"  {'TICKER':<6} │ {'BP':>8} │ {'NIVEL':<6} │ {'BADGE':>5} │ {'CAU-PM':<20} │ {'EC':<12} │ {'SIGNALS':<18} │ PH PM"
    for grupo, tks in _GRUPOS.items():
        tks_en_grupo = [t for t in tks if t in tickers and t not in ("TLT", "GLD")]
        if not tks_en_grupo:
            continue
        print(f"  ▸ {grupo}")
        print(SEP)
        print(HDR)
        print(SEP)
        for t in tks_en_grupo:
            r = results.get(t, {})
            if r.get("error"):
                print(f"  {t:<6} │ sin datos")
                continue
            ec_s = f"{r['ec_score']} {r['ec_accion']}"
            niveles_s = f"{r['prev_day_label']} {r['pm_label']}"
            print(
                f"  {t:<6} │ ${r['bp']:>8.2f} │ {r['nivel']:<6} │ {r['badge']:4.1f}% │ "
                f"{r['cau']:<20} │ {ec_s:<12} │ {r['signals']:<18} │ {niveles_s}"
            )
        print()

    # Resumen
    alc  = sum(1 for r in results.values() if not r.get("error") and r.get("dir") == "ALC")
    baj  = sum(1 for r in results.values() if not r.get("error") and r.get("dir") == "BAJ")
    zm   = sum(1 for r in results.values() if not r.get("error") and r.get("dir") == "ZM")
    skip = sum(1 for r in results.values() if not r.get("error") and r.get("ec_score") in ("3", "4", "—"))
    print(DIV)
    print(f"  ALC: {alc}  BAJ: {baj}  ZM: {zm}  SKIP(EC≤4): {skip}")
    print(DIV)
