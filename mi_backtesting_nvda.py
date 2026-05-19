"""
Backtesting con datos parquet (bb_retroceso/data/processed/).
Multi-TF BP, BB, signals 3/9, CAUTION, long/short%, playbook BB retroceso.
Genera MI_BACKTESTING_<TICKER>.xlsx.

Uso:
  python mi_backtesting_nvda.py [--ticker TICKER] [--test] [--verify] [--all]
  --ticker: NVDA (default), AMD, etc.
  --test: Solo procesa fechas de oct 2025 para comparar vs CSV Milton.
  --verify: Verifica datos premarket y corte 09:28 para 2025-10-06.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

try:
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
except ImportError:
    openpyxl = None

try:
    import pytz
    ET = pytz.timezone("America/New_York")
    UTC = pytz.UTC
except ImportError:
    ET = None
    UTC = None

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from verificar_dia import analyze_regular_session, format_minute

# =============================================================================
# CONSTANTES
# =============================================================================
DATA_DIR = Path(__file__).parent / "data" / "processed"
INICIO = "2021-01-01"
FIN = datetime.today().strftime("%Y-%m-%d")
TEST_INICIO = "2025-10-01"  # Solo para --test
HORA_CORTE = "09:28"  # Estrictamente anterior a 09:28:00 ET
HORA_CORTE_UTC = "23:58"  # BTCUSDT: velas hasta 23:58 UTC del día anterior (= cierre vela diaria Binance)
N_VELAS_BP = 50
N_VELAS_BB = 20


def mid_tf_key(ticker: str, tfs: Optional[Dict[str, pd.DataFrame]] = None) -> str:
    """Usa parquet 3m si existe; si no, 2m. Con tfs (vivo): 3m si hay datos; si no, 5m."""
    if tfs is not None:
        d3 = tfs.get("3m")
        if d3 is not None and not d3.empty:
            return "3m"
        return "5m"
    return "3m" if (DATA_DIR / f"{ticker}_3m.parquet").exists() else "2m"


def tf_order_for_ticker(ticker: str) -> List[str]:
    """Orden BP multi-TF (grueso → fino), con 3m o 2m según parquet."""
    mid = mid_tf_key(ticker, None)
    return ["1d", "1h", "30m", "15m", "5m", mid, "1m"]


def caution_tf_key_label_pairs(
    ticker: str, tfs: Optional[Dict[str, pd.DataFrame]] = None
) -> List[Tuple[str, str]]:
    """1M, 5M, 15M, 30M, 1H, 1D — fijo, sin 3M/2M dinámico."""
    return [
        ("1m", "1M"),
        ("5m", "5M"),
        ("15m", "15M"),
        ("30m", "30M"),
        ("1h", "1H"),
        ("1d", "1D"),
    ]


def caution_tf_labels(
    ticker: str, tfs: Optional[Dict[str, pd.DataFrame]] = None
) -> Tuple[str, ...]:
    """Solo etiquetas (1M, 5M, 15M, 30M, 1H, 1D) para parsear strings de caution."""
    return tuple(lab for _, lab in caution_tf_key_label_pairs(ticker, tfs))

# Colores Excel
HEADER_FILL = "1F4E79"
ROW_ALT1 = "D9D9D9"
ROW_ALT2 = "FFFFFF"
GREEN = "90EE90"
RED = "FFB6C1"
YELLOW = "FFFF00"
ORANGE = "FFA500"


# =============================================================================
# CARGA INICIAL — Parquets
# =============================================================================
def load_parquet(path: Path, keep_utc: bool = False) -> pd.DataFrame:
    df = pd.read_parquet(path)
    df.columns = [str(c).lower() for c in df.columns]
    if "timestamp" not in df.columns and len(df) > 0:
        df = df.reset_index()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    if df["timestamp"].dt.tz is None:
        df["timestamp"] = df["timestamp"].dt.tz_localize("UTC", ambiguous="infer")
    if not keep_utc and ET and str(df["timestamp"].dt.tz) != str(ET):
        df["timestamp"] = df["timestamp"].dt.tz_convert(ET)
    df["date_et"] = df["timestamp"].dt.date
    return df


def load_all_tfs(ticker: str) -> Dict[str, pd.DataFrame]:
    keep_utc = ticker.upper() == "BTCUSDT"
    tfs = {}
    mid = mid_tf_key(ticker)
    for tf in ["1m", mid, "5m", "15m", "30m", "1h", "1d"]:
        p = DATA_DIR / f"{ticker}_{tf}.parquet"
        if not p.exists():
            cmd = "01_download_binance.py" if ticker.upper() == "BTCUSDT" else "01_download.py y 00_resample.py"
            raise FileNotFoundError(f"Falta {p}. Ejecutar {cmd} {ticker}")
        tfs[tf] = load_parquet(p, keep_utc=keep_utc)
    return tfs


# =============================================================================
# get_premarket_slice
# =============================================================================
def get_premarket_slice(
    df: pd.DataFrame,
    fecha,
    hora_corte: str = "09:28",
) -> pd.DataFrame:
    """Filtra df estrictamente anterior a hora_corte ET (ej. 09:28:00)."""
    df_day = df[df["date_et"] == fecha].copy()
    if df_day.empty:
        return pd.DataFrame()
    hc, mc = map(int, hora_corte.split(":"))
    mask = (df_day["timestamp"].dt.hour < hc) | (
        (df_day["timestamp"].dt.hour == hc) & (df_day["timestamp"].dt.minute < mc)
    )
    return df_day[mask].sort_values("timestamp")


def get_premarket_slice_with_prev(
    df: pd.DataFrame,
    fecha,
    fecha_str: str,
    hora_corte: str,
    n_velas: int,
) -> pd.DataFrame:
    """Últimas n_velas del TF estrictamente anteriores a hora_corte ET (ej. 09:28:00)."""
    cutoff = pd.Timestamp(f"{fecha_str} {hora_corte}:00", tz=ET)
    all_before = df[df["timestamp"] < cutoff].sort_values("timestamp")
    if all_before.empty:
        return pd.DataFrame()
    return all_before.tail(n_velas)


def get_prev_day_slice_until_1600(
    df: pd.DataFrame,
    fecha,
    n_velas: int = 20,
) -> pd.DataFrame:
    """Últimas n_velas hasta 16:00 ET del día ANTERIOR (para BB market close).
    Incluye días previos si hace falta (ej. 1h necesita 20 velas ≈ 2 días)."""
    prev_dates = sorted(df[df["date_et"] < fecha]["date_et"].unique())
    if not prev_dates:
        return pd.DataFrame()
    prev_date = prev_dates[-1]
    prev_str = str(prev_date)
    cutoff = pd.Timestamp(f"{prev_str} 16:00:00", tz=ET)
    all_before = df[df["timestamp"] <= cutoff].sort_values("timestamp")
    if all_before.empty:
        return pd.DataFrame()
    return all_before.tail(n_velas)


def get_premarket_slice_with_prev_utc(
    df: pd.DataFrame,
    fecha,
    fecha_str: str,
    hora_corte_utc: str,
    n_velas: int,
) -> pd.DataFrame:
    """BTCUSDT: últimas n_velas hasta hora_corte_utc (ej. 23:58) del día ANTERIOR en UTC."""
    if not UTC:
        return pd.DataFrame()
    prev_dates = sorted(df[df["date_et"] < fecha]["date_et"].unique())
    if not prev_dates:
        return pd.DataFrame()
    prev_date = prev_dates[-1]
    prev_str = str(prev_date)
    cutoff = pd.Timestamp(f"{prev_str} {hora_corte_utc}:00", tz=UTC)
    all_before = df[df["timestamp"] <= cutoff].sort_values("timestamp")
    if all_before.empty:
        return pd.DataFrame()
    return all_before.tail(n_velas)


def get_prev_day_slice_until_2358_utc(
    df: pd.DataFrame,
    fecha,
    n_velas: int = 20,
) -> pd.DataFrame:
    """BTCUSDT: últimas n_velas hasta 23:58 UTC del día ANTERIOR (= cierre vela diaria Binance)."""
    return get_premarket_slice_with_prev_utc(df, fecha, str(fecha), HORA_CORTE_UTC, n_velas)


def _is_rth_row(row) -> bool:
    """True si timestamp está en RTH (09:30-16:00 ET)."""
    h = row["timestamp"].hour
    m = row["timestamp"].minute
    if h < 9:
        return False
    if h == 9 and m < 30:
        return False
    if h > 16:
        return False
    if h == 16 and m > 0:
        return False
    return True


def rth_mask_vectorized(df: pd.DataFrame) -> pd.Series:
    """Misma lógica que _is_rth_row, vectorizado (sin apply)."""
    ts = df["timestamp"]
    h = ts.dt.hour
    m = ts.dt.minute
    not_rth = (h < 9) | ((h == 9) & (m < 30)) | (h > 16) | ((h == 16) & (m > 0))
    return ~not_rth


def build_tfs_rth(tfs: Dict[str, pd.DataFrame], ticker: str) -> Optional[Dict[str, pd.DataFrame]]:
    """Precalcula solo velas RTH por TF (1 vez al inicio). BTCUSDT no usa RTH ET."""
    if ticker.upper() == "BTCUSDT":
        return None
    out: Dict[str, pd.DataFrame] = {}
    mid = mid_tf_key(ticker, tfs)
    for tf in ["1m", mid, "5m", "15m", "30m", "1h"]:
        if tf not in tfs or tfs[tf] is None or tfs[tf].empty:
            continue
        df = tfs[tf]
        mask = rth_mask_vectorized(df)
        out[tf] = df[mask].copy()
    return out


def get_slice_rth_until_930(
    df: pd.DataFrame,
    fecha,
    tf: str,
    open_930: Optional[float],
    n_velas: int = 20,
    is_btc: bool = False,
    df_rth_pre: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """Definición B: 19 velas RTH de días anteriores + 1ra vela RTH del día actual.
    df_rth_pre: parquet ya filtrado RTH (stocks); None = calcular desde df (BTC) o no aplica (1d)."""
    if tf == "1d":
        prev = df[df["date_et"] < fecha].tail(n_velas - 1)
        if len(prev) < n_velas - 1 or open_930 is None:
            return pd.DataFrame()
        closes = list(prev["close"].values) + [open_930]
        return pd.DataFrame({"close": closes})

    if is_btc:
        df_rth = df[df["date_et"] <= fecha].copy()
    else:
        if df_rth_pre is None:
            df_rth = df[rth_mask_vectorized(df)].copy()
        else:
            df_rth = df_rth_pre

    prev_rth = df_rth[df_rth["date_et"] < fecha].sort_values("timestamp").tail(n_velas - 1)
    today_rth = df_rth[df_rth["date_et"] == fecha].sort_values("timestamp").head(1)

    if prev_rth.empty or today_rth.empty:
        return pd.DataFrame()
    if len(prev_rth) < n_velas - 1:
        return pd.DataFrame()

    combined = pd.concat([prev_rth, today_rth], ignore_index=True).sort_values("timestamp")
    return combined.tail(n_velas)


# =============================================================================
# PASO A — EMA3/EMA9 MULTI-TF → BP
# =============================================================================
def compute_bp_multi_tf(
    tfs: Dict[str, pd.DataFrame],
    fecha,
    fecha_str: str,
    ticker: str = "",
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    is_btc = ticker.upper() == "BTCUSDT"
    ema3_list, ema9_list = [], []
    for tf in tf_order_for_ticker(ticker):
        if tf not in tfs or tfs[tf] is None or tfs[tf].empty:
            continue
        df = tfs[tf]
        n = N_VELAS_BP
        if tf == "1d":
            prev = df[df["date_et"] < fecha].tail(n)
            if prev.empty or len(prev) < 3:
                continue
            close = prev["close"]
        else:
            if is_btc:
                slice_tf = get_premarket_slice_with_prev_utc(df, fecha, fecha_str, HORA_CORTE_UTC, n)
            else:
                slice_tf = get_premarket_slice_with_prev(df, fecha, fecha_str, HORA_CORTE, n)
            if slice_tf.empty or len(slice_tf) < 3:
                continue
            close = slice_tf["close"]
        ema3 = close.ewm(span=3, adjust=False).mean().iloc[-1]
        ema9 = close.ewm(span=9, adjust=False).mean().iloc[-1]
        ema3_list.append(float(ema3))
        ema9_list.append(float(ema9))
    if not ema3_list or not ema9_list:
        return None, None, None
    avg_overbought = sum(ema3_list) / len(ema3_list)
    avg_oversold = sum(ema9_list) / len(ema9_list)
    bp = (avg_overbought + avg_oversold) / 2
    return avg_overbought, avg_oversold, bp


# =============================================================================
# PASO B — BOLLINGER BANDS
# =============================================================================
def compute_bb(df: pd.DataFrame, slice_df: pd.DataFrame) -> Tuple[Optional[float], Optional[float]]:
    """BB(20,2) con EMA — media y std exponenciales (ewm, adjust=False)."""
    if slice_df.empty:
        return None, None
    if len(slice_df) < N_VELAS_BB:
        return None, None
    close = slice_df["close"].tail(N_VELAS_BB)
    mid = close.ewm(span=N_VELAS_BB, adjust=False).mean().iloc[-1]
    std = close.ewm(span=N_VELAS_BB, adjust=False).std().iloc[-1]
    if pd.isna(mid) or pd.isna(std):
        return None, None
    bbt = float(mid + 2 * std)
    bbb = float(mid - 2 * std)
    return bbt, bbb


# =============================================================================
# EMA20/50/200 PRE (1h close hasta 09:28)
# =============================================================================
def compute_ema_pre_1h(
    tfs: Dict[str, pd.DataFrame],
    fecha,
    fecha_str: str,
    ticker: str = "",
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """EMA20, EMA50, EMA200 del close 1h hasta 09:28 (o 23:58 UTC para BTCUSDT)."""
    if "1h" not in tfs or tfs["1h"] is None or tfs["1h"].empty:
        return None, None, None
    is_btc = ticker.upper() == "BTCUSDT"
    if is_btc:
        slice_1h = get_premarket_slice_with_prev_utc(tfs["1h"], fecha, fecha_str, HORA_CORTE_UTC, 200)
    else:
        slice_1h = get_premarket_slice_with_prev(tfs["1h"], fecha, fecha_str, HORA_CORTE, 200)
    if slice_1h.empty:
        return None, None, None
    close = slice_1h["close"]
    ema20_pre = close.ewm(span=20, adjust=False).mean().iloc[-1] if len(close) >= 20 else None
    ema50_pre = close.ewm(span=50, adjust=False).mean().iloc[-1] if len(close) >= 50 else None
    ema200_pre = close.ewm(span=200, adjust=False).mean().iloc[-1] if len(close) >= 200 else None
    return (
        float(ema20_pre) if ema20_pre is not None and not pd.isna(ema20_pre) else None,
        float(ema50_pre) if ema50_pre is not None and not pd.isna(ema50_pre) else None,
        float(ema200_pre) if ema200_pre is not None and not pd.isna(ema200_pre) else None,
    )


# =============================================================================
# PASO C — SIGNALS 3/9
# =============================================================================
def compute_signals_3_9(
    tfs: Dict[str, pd.DataFrame],
    fecha,
    fecha_str: str,
    ticker: str = "",
) -> str:
    is_btc = ticker.upper() == "BTCUSDT"
    parts = []
    for tf in ["15m", "30m", "1h"]:
        if tf not in tfs or tfs[tf] is None or tfs[tf].empty:
            continue
        if is_btc:
            slice_tf = get_premarket_slice_with_prev_utc(tfs[tf], fecha, fecha_str, HORA_CORTE_UTC, 20)
        else:
            slice_tf = get_premarket_slice_with_prev(tfs[tf], fecha, fecha_str, HORA_CORTE, 20)
        if slice_tf.empty or len(slice_tf) < 9:
            continue
        close = slice_tf["close"]
        ema3 = close.ewm(span=3, adjust=False).mean().iloc[-1]
        ema9 = close.ewm(span=9, adjust=False).mean().iloc[-1]
        signal = "up" if ema3 > ema9 else "down"
        parts.append(f"3/9 {signal} {tf}")
    return ", ".join(parts) if parts else ""


# =============================================================================
# PASO D — CAUTION (formato Milton completo)
# BB(20,2) sobre velas hasta 16:00 ET del día ANTERIOR.
# Comparar precio_premarket_928 contra BBT_cierre y BBB_cierre.
# =============================================================================
CAUTION_TEXTS = {
    "BBT 1M": "CAUTION: Price outside of the BBT 1M\nSlightly overbought on 1-minute candles.",
    "BBB 1M": "CAUTION: Price outside of the BBB 1M\nSlightly oversold on 1-minute candles.",
    "BBT 5M": "CAUTION: Price outside of the BBT 5M\nOverbought on 5-minute candles.",
    "BBB 5M": "CAUTION: Price outside of the BBB 5M\nOversold on 5-minute candles, expect a low correction.",
    "BBT 30M": "CAUTION: Price outside of the BBT 30M\nOverbought on 30-minute candles.",
    "BBB 30M": "CAUTION: Price outside of the BBB 30M\nOversold on 30-minute candles, expect a low correction.",
    "BBT 1H": "CAUTION: Price outside of the BBT 1H\nOverbought on 1-hour candles.",
    "BBB 1H": "CAUTION: Price outside of the BBB 1H\nOversold on 1-hour candles, expect a low correction.",
}


def compute_caution(
    tfs: Dict[str, pd.DataFrame],
    fecha,
    fecha_str: str,
    precio_premarket_928: float,
    ticker: str = "",
) -> str:
    """CAUTION: EMA-BB(20,2) del día anterior hasta 16:00 ET (o 23:58 UTC para BTCUSDT).
    Usa caution_tf_key_label_pairs() (incl. 1d y TF mid 3m/2m) con slice hasta 16:00 prev."""
    is_btc = ticker.upper() == "BTCUSDT"
    cautions = []
    any_bb_computed = False
    for tf, label in caution_tf_key_label_pairs(ticker, tfs):
        if tf not in tfs or tfs[tf] is None or tfs[tf].empty:
            continue
        slice_prev = (
            get_prev_day_slice_until_2358_utc(tfs[tf], fecha, N_VELAS_BB)
            if is_btc
            else get_prev_day_slice_until_1600(tfs[tf], fecha, N_VELAS_BB)
        )
        bbt, bbb = compute_bb(tfs[tf], slice_prev)
        if bbt is not None:
            any_bb_computed = True
        if bbt is not None and precio_premarket_928 > bbt:
            cautions.append(f"BBT {label}")
        if bbb is not None and precio_premarket_928 < bbb:
            cautions.append(f"BBB {label}")
    if not any_bb_computed:
        return "Sin datos BB"
    return " | ".join(cautions) if cautions else "Sin caution"


def compute_caution_real_930(
    tfs: Dict[str, pd.DataFrame],
    fecha,
    open_930: Optional[float],
    ticker: str = "",
    tfs_rth: Optional[Dict[str, pd.DataFrame]] = None,
) -> Optional[str]:
    """Definición B: BB(20,2) sobre 19 velas RTH previas + 1ra vela RTH (09:30).
    tfs_rth: DataFrames RTH precalculados (stocks); None para BTC."""
    if open_930 is None:
        return None
    is_btc = ticker.upper() == "BTCUSDT"
    partes = []
    any_bb_computed = False
    for tf, label in caution_tf_key_label_pairs(ticker, tfs):
        if tf not in tfs or tfs[tf] is None or tfs[tf].empty:
            continue
        df_rth_pre = tfs_rth.get(tf) if tfs_rth else None
        slice_rth = get_slice_rth_until_930(
            tfs[tf], fecha, tf, open_930, N_VELAS_BB, is_btc, df_rth_pre=df_rth_pre,
        )
        bbt, bbb = compute_bb(tfs[tf], slice_rth)
        if bbt is not None:
            any_bb_computed = True
        if bbt is not None and open_930 > bbt:
            partes.append(f"BBT {label}")
        if bbb is not None and open_930 < bbb:
            partes.append(f"BBB {label}")
    if not any_bb_computed:
        return "Sin datos BB"
    if not partes:
        return "Sin caution real"
    return " | ".join(partes)


def compute_caution_premarket_open930(
    tfs: Dict[str, pd.DataFrame],
    fecha,
    fecha_str: str,
    open_930: Optional[float],
    ticker: str,
) -> Optional[str]:
    """open_930 vs BB(20,2) con velas premarket (hasta 09:28 ET / corte UTC BTC).
    Mismos TFs que caution_real_930; no usa caution_nota de Milton."""
    if open_930 is None:
        return None
    is_btc = ticker.upper() == "BTCUSDT"
    partes = []
    for tf, label in caution_tf_key_label_pairs(ticker, tfs):
        if tf not in tfs or tfs[tf] is None or tfs[tf].empty:
            continue
        if is_btc:
            slice_pre = get_premarket_slice_with_prev_utc(
                tfs[tf], fecha, fecha_str, HORA_CORTE_UTC, N_VELAS_BB,
            )
        else:
            slice_pre = get_premarket_slice_with_prev(
                tfs[tf], fecha, fecha_str, HORA_CORTE, N_VELAS_BB,
            )
        bbt, bbb = compute_bb(tfs[tf], slice_pre)
        if bbt is not None and open_930 > bbt:
            partes.append(f"BBT {label}")
        if bbb is not None and open_930 < bbb:
            partes.append(f"BBB {label}")
    if not partes:
        return "Sin caution premarket"
    return " | ".join(partes)


def extraer_pares(
    caution_str: Optional[str], ticker: str, tfs: Optional[Dict[str, pd.DataFrame]] = None
) -> set:
    """Set de (BBT|BBB, TF) presentes en el string; el ticker fija 2M vs 3M."""
    labels = caution_tf_labels(ticker, tfs)
    s = str(caution_str or "")
    if s in (
        "Sin caution real",
        "Sin caution premarket",
        "Sin caution",
        "Sin datos BB",
        "N/A",
        "",
    ):
        return set()
    out = set()
    for lab in sorted(labels, key=len, reverse=True):
        if f"BBT {lab}" in s:
            out.add(("BBT", lab))
        if f"BBB {lab}" in s:
            out.add(("BBB", lab))
    return out


def compute_caution_cambio(
    caution_real_930: Optional[str],
    caution_premarket_open: Optional[str],
    ticker: str,
) -> Optional[str]:
    """Compara caution_real_930 vs caution_premarket_open (bandas premarket, mismos TFs).
    No usa caution_nota de Milton."""
    if caution_real_930 is None:
        return None
    pre_pares = extraer_pares(caution_premarket_open, ticker)
    real_pares = extraer_pares(caution_real_930, ticker)
    if pre_pares == real_pares:
        return "igual"
    if not pre_pares and real_pares:
        return "nuevo"
    if pre_pares and not real_pares:
        return "desaparecio"
    if real_pares.issuperset(pre_pares):
        return "escalo"
    return "cambio_tipo"


def compute_caution_cambio_1v3(
    caution_nota: Optional[str],
    caution_real_930: Optional[str],
    ticker: str,
    tfs: Optional[Dict[str, pd.DataFrame]] = None,
) -> Optional[str]:
    """Compara caution_nota (tipo 1, Milton 9:28) vs caution_real_930 (tipo 3, primera vela RTH).
    Esta es la comparación operativa real: lo que ves a las 9:28 vs lo que pasa al abrir."""
    if caution_real_930 is None or caution_nota is None:
        return None
    pre_pares = extraer_pares(caution_nota, ticker, tfs)
    real_pares = extraer_pares(caution_real_930, ticker, tfs)
    if pre_pares == real_pares:
        return "igual"
    if not pre_pares and real_pares:
        return "nuevo"
    if pre_pares and not real_pares:
        return "desaparecio"
    if real_pares.issuperset(pre_pares):
        return "escalo"
    return "cambio_tipo"


def compute_extra_result_fields(
    bars: List[dict],
    anal: Dict[str, Any],
    direccion: str,
    bp: float,
    int_pos: float,
    int_neg: float,
    max_pos: float,
    max_neg: float,
) -> Dict[str, Any]:
    """Campos 1-7: int_contrario_tocado, minuto_int_contrario, precio_tras_bp_min/max,
    tramo_bp_a_int_minutos, rebote_post_int_cents, segundo_toque_int."""
    out = {
        "int_contrario_tocado": None,
        "minuto_int_contrario": None,
        "precio_tras_bp_min": None,
        "precio_tras_bp_max": None,
        "tramo_bp_a_int_minutos": None,
        "rebote_post_int_cents": None,
        "segundo_toque_int": None,
    }
    if not bars:
        return out

    toco_int = anal.get("toco_int") or ""
    toco_int_dir = (anal.get("toco_int_dir") or "").strip().lower()
    toco_bp_antes = anal.get("toco_bp_antes_int") or ""
    minuto_toco_int = anal.get("minuto_toco_int") or ""

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

    idx_ip = _idx_touch_pos(int_pos)
    idx_in = _idx_touch_neg(int_neg)
    idx_mp = _idx_touch_pos(max_pos)
    idx_mn = _idx_touch_neg(max_neg)

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
        out["int_contrario_tocado"] = "Sí" if toco_int_dir in ("int_neg", "ambos") else "No"
    elif direccion == "BAJISTA":
        out["int_contrario_tocado"] = "Sí" if toco_int_dir in ("int_pos", "ambos") else "No"
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
        rest = bars[i_bp + 1:]
        if rest:
            out["precio_tras_bp_min"] = round(min(b["l"] for b in rest), 2)
            out["precio_tras_bp_max"] = round(max(b["h"] for b in rest), 2)

    # Campo 5: tramo_bp_a_int_minutos
    if i_bp is not None and ei is not None and toco_bp_antes == "Sí" and toco_int == "Sí":
        def _mins(ts_ms: int) -> int:
            t = pd.Timestamp(ts_ms, unit="ms")
            if t.tzinfo is None:
                t = t.tz_localize("UTC")
            if ET:
                t = t.tz_convert(ET)
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
            use_pos = direccion == "ALCISTA" or (direccion == "DOBLE_DIR" and (toco_int_dir == "int_pos" or primer_mov == "pos"))
            if use_pos:
                min_low = min(b["l"] for b in slice_post)
                rebote = round(int_pos - min_low, 2)
            else:
                max_high = max(b["h"] for b in slice_post)
                rebote = round(max_high - int_neg, 2)
            out["rebote_post_int_cents"] = max(0.0, rebote)

    # Campo 7: segundo_toque_int
    if toco_int == "Sí" and ei is not None:
        rest = bars[ei + 1:]
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
            int_correcto_pos = (direccion == "ALCISTA") or (direccion == "DOBLE_DIR" and toco_int_dir in ("int_pos", "ambos"))
            int_correcto_neg = (direccion == "BAJISTA") or (direccion == "DOBLE_DIR" and toco_int_dir in ("int_neg", "ambos"))
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


# =============================================================================
# PASO E — LONG% / SHORT%
# Solo premarket (09:28): rot_1m_pre + sobre_ema20_15m. Max 50 pts.
# No rot_1m_reg ni gaps (requieren vela 09:30 que no existe en premarket).
# =============================================================================
def compute_long_short_pct(
    tfs: Dict[str, pd.DataFrame],
    fecha,
    fecha_str: str,
    precio_actual: float,
    ticker: str = "",
) -> Tuple[int, int]:
    is_btc = ticker.upper() == "BTCUSDT"
    up_pre, down_pre = 0, 0
    hora_corte = HORA_CORTE_UTC if is_btc else HORA_CORTE
    slice_fn = (lambda d, f, fs, hc, n: get_premarket_slice_with_prev_utc(d, f, fs, hc, n)) if is_btc else (lambda d, f, fs, hc, n: get_premarket_slice_with_prev(d, f, fs, hc, n))
    # rot_1m_pre: +25 UP o DOWN
    if "1m" in tfs and not tfs["1m"].empty:
        slice_1m = slice_fn(tfs["1m"], fecha, fecha_str, hora_corte, 20)
        if not slice_1m.empty and len(slice_1m) >= 9:
            close = slice_1m["close"]
            ema3 = close.ewm(span=3, adjust=False).mean().iloc[-1]
            ema9 = close.ewm(span=9, adjust=False).mean().iloc[-1]
            if ema3 > ema9:
                up_pre += 25
            else:
                down_pre += 25
    # sobre_ema20_15m: +25 UP o DOWN
    if "15m" in tfs and not tfs["15m"].empty:
        slice_15 = slice_fn(tfs["15m"], fecha, fecha_str, hora_corte, 20)
        if not slice_15.empty and len(slice_15) >= 20:
            ema20 = slice_15["close"].ewm(span=20, adjust=False).mean().iloc[-1]
            if precio_actual > ema20:
                up_pre += 25
            else:
                down_pre += 25
    return min(100, up_pre), min(100, down_pre)


# =============================================================================
# PASO F — DATOS DÍA ANTERIOR
# =============================================================================
def get_prev_day_data(
    tfs: Dict[str, pd.DataFrame],
    fecha,
) -> Dict[str, Any]:
    df_1d = tfs.get("1d")
    out = {
        "prev_day_high": None,
        "prev_day_low": None,
        "prev_day_close": None,
        "prev_prev_close": None,
        "prev_prev_high": None,
        "prev_prev_low": None,
        "prev_day_open": None,
        "prev_prev_open": None,
        "prev_day_change": None,
        "rango_d1": None,
        "rango_d2": None,
        "rango_d3": None,
        "avg_3d": None,
    }
    if df_1d is None or df_1d.empty:
        return out
    prev = df_1d[df_1d["date_et"] < fecha].tail(3)
    if len(prev) < 2:
        return out
    row1 = prev.iloc[-1]
    row0 = prev.iloc[-2]
    out["prev_day_high"] = float(row1["high"])
    out["prev_day_low"] = float(row1["low"])
    out["prev_day_close"] = float(row1["close"])
    out["prev_prev_close"] = float(row0["close"])
    out["prev_prev_high"] = float(row0["high"])
    out["prev_prev_low"] = float(row0["low"])
    out["prev_day_open"] = float(row1["open"])
    out["prev_prev_open"] = float(row0["open"])
    out["prev_day_change"] = (
        (out["prev_day_close"] - out["prev_prev_close"]) / out["prev_prev_close"] * 100
    )
    # Rangos de los 3 días anteriores para avg_3d
    rangos = []
    for i in range(min(3, len(prev))):
        r = prev.iloc[-(1 + i)]
        rng = float(r["high"]) - float(r["low"])
        if i == 0:
            out["rango_d1"] = rng
        elif i == 1:
            out["rango_d2"] = rng
        else:
            out["rango_d3"] = rng
        rangos.append(rng)
    if rangos:
        out["avg_3d"] = sum(rangos) / len(rangos)
    return out


# =============================================================================
# PASO G — TARGETS (con ajuste CAUTION)
# Promedio de 3 días para rango — no solo el día anterior
# =============================================================================
def compute_targets(
    avg_3d: float,
    bp_pred: float,
    caution_nota: str,
    gap_tipo: str,
) -> Tuple[float, float, float, float]:
    rango = avg_3d * 0.70 if avg_3d > 5.0 else avg_3d
    dist_int = rango * 0.33
    dist_max = rango * 0.66

    if "BBT" in caution_nota and gap_tipo == "GAP_UP":
        dist_int_pos = dist_int * 1.25
        dist_int_neg = dist_int
        dist_max_pos = dist_max * 1.25
        dist_max_neg = dist_max
    elif "BBB" in caution_nota and gap_tipo == "GAP_DOWN":
        dist_int_pos = dist_int
        dist_int_neg = dist_int * 1.25
        dist_max_pos = dist_max
        dist_max_neg = dist_max * 1.25
    else:
        dist_int_pos = dist_int_neg = dist_int
        dist_max_pos = dist_max_neg = dist_max

    int_pos = bp_pred + dist_int_pos
    int_neg = bp_pred - dist_int_neg
    max_pos = bp_pred + dist_max_pos
    max_neg = bp_pred - dist_max_neg
    return int_pos, int_neg, max_pos, max_neg


# =============================================================================
# PASO H — INTRADAY: DataFrame 1m RTH → lista de barras para analyze_regular_session
# =============================================================================
def is_rth(row) -> bool:
    h = row["timestamp"].hour
    m = row["timestamp"].minute
    if h < 9:
        return False
    if h == 9 and m < 30:
        return False
    if h > 16:
        return False
    if h == 16 and m > 0:
        return False
    return True


def df_1m_to_bars(df_1m_day: pd.DataFrame, use_all_day: bool = False) -> list:
    """Convierte DataFrame 1m a lista {t, o, h, l, c}. use_all_day=True para BTCUSDT (RTH=00:00-23:59 UTC)."""
    if df_1m_day.empty:
        return []
    if use_all_day:
        df_sel = df_1m_day.sort_values("timestamp")
    else:
        df_rth = df_1m_day[df_1m_day.apply(is_rth, axis=1)]
        if df_rth.empty:
            return []
        df_sel = df_rth.sort_values("timestamp")
    bars = []
    for _, row in df_sel.iterrows():
        ts = row["timestamp"]
        ms = int(ts.timestamp() * 1000)
        bars.append({
            "t": ms,
            "o": float(row["open"]),
            "h": float(row["high"]),
            "l": float(row["low"]),
            "c": float(row["close"]),
        })
    return bars


def _clasificar_vela(
    high: float, low: float, close: float, open_p: float,
    prev_high: float, prev_low: float,
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


def _estructura_vs_bp(estructura: Optional[str], direccion: str) -> str:
    """
    Mapea estructura de vela + dirección BP a confirma/contradice/neutral.
    """
    CONFIRMA_ALC = {"alcista_fuerte", "alcista_debil", "outside_alcista"}
    CONFIRMA_BAJ = {"bajista_fuerte", "bajista_debil", "outside_bajista"}
    NEUTRAL_EST = {"inside_day", "neutral", None}

    if direccion == "DOBLE_DIR" or estructura in NEUTRAL_EST:
        return "neutral"
    if direccion == "ALCISTA" and estructura in CONFIRMA_ALC:
        return "confirma"
    if direccion == "BAJISTA" and estructura in CONFIRMA_BAJ:
        return "confirma"
    return "contradice"


def _zona_en_vela(precio: float, high: float, low: float) -> Tuple[Optional[float], Optional[str]]:
    """
    Calcula pct (0.0=low, 1.0=high) y zona (4 niveles basados en tercios internos candle_levels).
    Niveles internos:
      resistencia = high - (high-low)*0.33
      correccion  = (high+low)/2
      soporte     = low + (high-low)*0.33
    Zonas:
      zona_alta       → precio >= resistencia
      zona_media_alta → correccion <= precio < resistencia
      zona_media_baja → soporte <= precio < correccion
      zona_baja       → precio < soporte
    """
    rango = high - low
    if rango <= 0:
        return None, None
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
    return pct, zona


# =============================================================================
# MAIN LOOP — Procesar día
# =============================================================================
def process_day(
    tfs: Dict[str, pd.DataFrame],
    fecha,
    fecha_str: str,
    ticker: str = "",
    tfs_rth: Optional[Dict[str, pd.DataFrame]] = None,
) -> Tuple[dict, dict]:
    dash = {
        "fecha": fecha_str,
        "long_pct": None,
        "short_pct": None,
        "gap_tipo": None,
        "gap_mag": None,
        "prev_day_high": None,
        "prev_day_low": None,
        "prev_day_close": None,
        "prev_day_change": None,
        "BP_pred": None,
        "INT_POS": None,
        "INT_NEG": None,
        "MAX_POS": None,
        "MAX_NEG": None,
        "pre_mkt_bbt_1h": None,
        "pre_mkt_bbb_1h": None,
        "market_close_bbt_1h": None,
        "market_close_bbb_1h": None,
        "bbt_5m": None,
        "bbb_5m": None,
        "bbt_clf_5m": None,
        "bbb_clf_5m": None,
        "avg_overbought": None,
        "avg_oversold": None,
        "ema20_pre": None,
        "ema50_pre": None,
        "ema200_pre": None,
        "signals_3_9": "",
        "caution_nota": "",
        "n_cautiones": 0,
        "bb_expande_premarket": False,
        "rango_raw": None,
        "rango_usado": None,
    }
    res = {
        "fecha": fecha_str,
        "BP_pred": None,
        "open_930": None,
        "dist": None,
        "dist_pct": None,
        "direccion": None,
        "day_change": None,
        "day_high": None,
        "day_low": None,
        "toco_int": None,
        "toco_int_dir": None,
        "minuto_toco_int": None,
        "toco_max": None,
        "toco_max_dir": None,
        "minuto_toco_max": None,
        "toco_bp_antes_int": None,
        "primer_movimiento": None,
        "cerro_bp": None,
        "skip": "No",
        "razon_skip": "",
        "int_contrario_tocado": None,
        "minuto_int_contrario": None,
        "precio_tras_bp_min": None,
        "precio_tras_bp_max": None,
        "tramo_bp_a_int_minutos": None,
        "rebote_post_int_cents": None,
        "segundo_toque_int": None,
        "caution_real_930": None,
        "caution_premarket_open": None,
        "caution_cambio": None,
        "caution_cambio_1v3": None,
        "open_zone": None,
        "open_pct_en_rango": None,
        "c_int": None,
        "c_max": None,
        "c_int_c_max_ratio": None,
        # Bloque A — Estructura de velas
        "estructura_prev_day": None,
        "estructura_vs_bp": None,
        "estructura_premarket": None,
        "estructura_premarket_vs_bp": None,
        # Bloque B — Posición en vela
        "pct_en_vela_pre": None,
        "zona_precio_en_vela_pre": None,
        "pct_en_vela_d1": None,
        "zona_precio_en_vela_d1": None,
        # Bloque C — Niveles internos
        "resistencia_pre": None,
        "correccion_pre": None,
        "soporte_pre": None,
        "resistencia_d1": None,
        "correccion_d1": None,
        "soporte_d1": None,
    }

    prev = get_prev_day_data(tfs, fecha)
    dash["prev_day_high"] = prev["prev_day_high"]
    dash["prev_day_low"] = prev["prev_day_low"]
    dash["prev_day_close"] = prev["prev_day_close"]
    dash["prev_day_change"] = round(prev["prev_day_change"], 2) if prev["prev_day_change"] is not None else None

    if prev["prev_day_high"] is None or prev["prev_day_low"] is None:
        res["skip"] = "Sí"
        res["razon_skip"] = "sin prev_day"
        return dash, res

    avg_ob, avg_os, bp = compute_bp_multi_tf(tfs, fecha, fecha_str, ticker)
    if bp is None:
        res["skip"] = "Sí"
        res["razon_skip"] = "sin BP"
        return dash, res

    dash["avg_overbought"] = round(avg_ob, 2)
    dash["avg_oversold"] = round(avg_os, 2)
    dash["BP_pred"] = round(bp, 2)
    res["BP_pred"] = round(bp, 2)

    is_btc = ticker.upper() == "BTCUSDT"
    # Premarket BB (hasta 09:28 ET o 23:58 UTC para BTCUSDT)
    if is_btc:
        slice_1h_pre = get_premarket_slice_with_prev_utc(tfs["1h"], fecha, fecha_str, HORA_CORTE_UTC, N_VELAS_BB)
        slice_5m_pre = get_premarket_slice_with_prev_utc(tfs["5m"], fecha, fecha_str, HORA_CORTE_UTC, N_VELAS_BB)
    else:
        slice_1h_pre = get_premarket_slice_with_prev(tfs["1h"], fecha, fecha_str, HORA_CORTE, N_VELAS_BB)
        slice_5m_pre = get_premarket_slice_with_prev(tfs["5m"], fecha, fecha_str, HORA_CORTE, N_VELAS_BB)
    pre_bbt_1h, pre_bbb_1h = compute_bb(tfs["1h"], slice_1h_pre)
    bbt_5m, bbb_5m = compute_bb(tfs["5m"], slice_5m_pre)
    dash["pre_mkt_bbt_1h"] = round(pre_bbt_1h, 2) if pre_bbt_1h is not None else None
    dash["pre_mkt_bbb_1h"] = round(pre_bbb_1h, 2) if pre_bbb_1h is not None else None
    dash["bbt_5m"] = round(bbt_5m, 2) if bbt_5m is not None else None
    dash["bbb_5m"] = round(bbb_5m, 2) if bbb_5m is not None else None
    # Market close BB (día anterior hasta 16:00 ET o 23:58 UTC para BTCUSDT)
    if is_btc:
        slice_1h_prev = get_prev_day_slice_until_2358_utc(tfs["1h"], fecha, N_VELAS_BB)
        slice_5m_prev = get_prev_day_slice_until_2358_utc(tfs["5m"], fecha, N_VELAS_BB)
    else:
        slice_1h_prev = get_prev_day_slice_until_1600(tfs["1h"], fecha, N_VELAS_BB)
        slice_5m_prev = get_prev_day_slice_until_1600(tfs["5m"], fecha, N_VELAS_BB)
    mkt_bbt_1h, mkt_bbb_1h = compute_bb(tfs["1h"], slice_1h_prev)
    bbt_clf_5m, bbb_clf_5m = compute_bb(tfs["5m"], slice_5m_prev)
    dash["market_close_bbt_1h"] = round(mkt_bbt_1h, 2) if mkt_bbt_1h is not None else None
    dash["market_close_bbb_1h"] = round(mkt_bbb_1h, 2) if mkt_bbb_1h is not None else None
    dash["bbt_clf_5m"] = round(bbt_clf_5m, 2) if bbt_clf_5m is not None else None
    dash["bbb_clf_5m"] = round(bbb_clf_5m, 2) if bbb_clf_5m is not None else None

    ema20_pre, ema50_pre, ema200_pre = compute_ema_pre_1h(tfs, fecha, fecha_str, ticker)
    dash["ema20_pre"] = round(ema20_pre, 2) if ema20_pre is not None else None
    dash["ema50_pre"] = round(ema50_pre, 2) if ema50_pre is not None else None
    dash["ema200_pre"] = round(ema200_pre, 2) if ema200_pre is not None else None

    if is_btc:
        slice_1m = get_premarket_slice_with_prev_utc(tfs["1m"], fecha, fecha_str, HORA_CORTE_UTC, 20)
    else:
        slice_1m = get_premarket_slice_with_prev(tfs["1m"], fecha, fecha_str, HORA_CORTE, 20)
    precio_actual = float(slice_1m["close"].iloc[-1]) if not slice_1m.empty else None
    if precio_actual is None:
        res["skip"] = "Sí"
        res["razon_skip"] = "sin precio premarket"
        return dash, res

    # ── BLOQUE ESTRUCTURA + NIVELES (precio_actual = 9:28, slice_1m = premarket) ──

    # Datos día anterior y día -2
    _p1h = prev.get("prev_day_high")
    _p1l = prev.get("prev_day_low")
    _p1o = prev.get("prev_day_open")
    _p1c = prev.get("prev_day_close")
    _p2h = prev.get("prev_prev_high")
    _p2l = prev.get("prev_prev_low")
    _p2o = prev.get("prev_prev_open")
    _p2c = prev.get("prev_prev_close")

    # --- Estructura vela día -1 vs día -2 ---
    if all(v is not None for v in [_p1h, _p1l, _p1o, _p1c, _p2h, _p2l]):
        res["estructura_prev_day"] = _clasificar_vela(_p1h, _p1l, _p1c, _p1o, _p2h, _p2l)

    # --- Vela sintética premarket (high/low de todas las velas 1m hasta 9:28) ---
    _high_pre = None
    _low_pre = None
    _open_pre = None
    _close_pre = None
    if not slice_1m.empty:
        _high_pre = float(slice_1m["high"].max())
        _low_pre = float(slice_1m["low"].min())
        _open_pre = float(slice_1m["open"].iloc[0])   # primera vela premarket
        _close_pre = precio_actual                      # ya calculado

    # --- Estructura vela premarket vs día -1 ---
    if all(v is not None for v in [_high_pre, _low_pre, _p1h, _p1l]):
        res["estructura_premarket"] = _clasificar_vela(
            _high_pre, _low_pre, _close_pre, _open_pre, _p1h, _p1l
        )

    # --- Niveles internos vela premarket ---
    if _high_pre is not None and _low_pre is not None:
        _rango_pre = _high_pre - _low_pre
        if _rango_pre > 0:
            res["resistencia_pre"] = round(_high_pre - _rango_pre * 0.33, 4)
            res["correccion_pre"] = round((_high_pre + _low_pre) / 2, 4)
            res["soporte_pre"] = round(_low_pre + _rango_pre * 0.33, 4)

    # --- Niveles internos vela día -1 ---
    if _p1h is not None and _p1l is not None:
        _rango_d1 = _p1h - _p1l
        if _rango_d1 > 0:
            res["resistencia_d1"] = round(_p1h - _rango_d1 * 0.33, 4)
            res["correccion_d1"] = round((_p1h + _p1l) / 2, 4)
            res["soporte_d1"] = round(_p1l + _rango_d1 * 0.33, 4)

    # --- Posición de precio_actual en vela premarket ---
    if _high_pre is not None and _low_pre is not None:
        _pct_pre, _zona_pre = _zona_en_vela(precio_actual, _high_pre, _low_pre)
        res["pct_en_vela_pre"] = _pct_pre
        res["zona_precio_en_vela_pre"] = _zona_pre

    # --- Posición de precio_actual en vela día -1 ---
    if _p1h is not None and _p1l is not None:
        _pct_d1, _zona_d1 = _zona_en_vela(precio_actual, _p1h, _p1l)
        res["pct_en_vela_d1"] = _pct_d1
        res["zona_precio_en_vela_d1"] = _zona_d1

    dash["signals_3_9"] = compute_signals_3_9(tfs, fecha, fecha_str, ticker)
    dash["caution_nota"] = compute_caution(tfs, fecha, fecha_str, precio_actual, ticker)
    if dash["caution_nota"] == "Sin datos BB":
        print(f"  [WARN] {fecha_str} {ticker}: caution_nota = Sin datos BB (velas insuficientes)")
    dash["n_cautiones"] = dash["caution_nota"].count("CAUTION:") if dash["caution_nota"] and dash["caution_nota"] != "Sin caution" else 0
    # BB expande premarket: banda superior sube y banda inferior baja vs market close
    pm_bbt = dash["pre_mkt_bbt_1h"]
    pm_bbb = dash["pre_mkt_bbb_1h"]
    mc_bbt = dash["market_close_bbt_1h"]
    mc_bbb = dash["market_close_bbb_1h"]
    dash["bb_expande_premarket"] = (
        pm_bbt is not None and pm_bbb is not None and mc_bbt is not None and mc_bbb is not None
        and pm_bbt > mc_bbt and pm_bbb < mc_bbb
    )

    df_1m_day = tfs["1m"][tfs["1m"]["date_et"] == fecha]
    if is_btc:
        bars_rth = df_1m_to_bars(df_1m_day, use_all_day=True)
        open_930 = None
        df_1d_today = tfs["1d"][tfs["1d"]["date_et"] == fecha]
        if not df_1d_today.empty:
            open_930 = round(float(df_1d_today.iloc[0]["open"]), 2)
        if bars_rth and open_930 is None:
            open_930 = round(bars_rth[0]["o"], 2)
    else:
        bars_rth = df_1m_to_bars(df_1m_day, use_all_day=False)
        open_930 = round(bars_rth[0]["o"], 2) if bars_rth else None

    long_pct, short_pct = compute_long_short_pct(tfs, fecha, fecha_str, precio_actual, ticker)
    dash["long_pct"] = long_pct
    dash["short_pct"] = short_pct

    gap_mag = None
    gap_tipo = "FLAT"
    if open_930 is not None and prev["prev_day_close"] is not None:
        gap_mag = round(open_930 - prev["prev_day_close"], 2)
        gap_tipo = "GAP_UP" if gap_mag > 0 else ("GAP_DOWN" if gap_mag < 0 else "FLAT")
    dash["gap_mag"] = gap_mag
    dash["gap_tipo"] = gap_tipo

    if prev["avg_3d"] is None:
        res["skip"] = "Sí"
        res["razon_skip"] = "sin avg_3d (rangos 3 días)"
        return dash, res

    avg_3d = prev["avg_3d"]
    rango_usado = avg_3d * 0.70 if avg_3d > 5.0 else avg_3d
    dash["rango_raw"] = round(avg_3d, 2)
    dash["rango_usado"] = round(rango_usado, 2)

    int_pos, int_neg, max_pos, max_neg = compute_targets(
        avg_3d, bp,
        dash["caution_nota"], gap_tipo,
    )
    dash["INT_POS"] = round(int_pos, 2)
    dash["INT_NEG"] = round(int_neg, 2)
    dash["MAX_POS"] = round(max_pos, 2)
    dash["MAX_NEG"] = round(max_neg, 2)

    # SKIP playbook
    if prev["prev_day_change"] is not None and prev["prev_day_change"] > 9.5 and gap_tipo == "GAP_DOWN":
        res["skip"] = "Sí"
        res["razon_skip"] = "prev_day>9.5+GAP_DOWN"
        res["open_930"] = open_930
        if bars_rth:
            res["day_high"] = round(max(b["h"] for b in bars_rth), 2)
            res["day_low"] = round(min(b["l"] for b in bars_rth), 2)
            res["day_change"] = round(bars_rth[-1]["c"] - bars_rth[0]["o"], 2)
        return dash, res

    if open_930 is None or not bars_rth:
        res["skip"] = "Sí"
        res["razon_skip"] = "sin datos intraday"
        return dash, res

    res["open_930"] = open_930
    res["day_high"] = round(max(b["h"] for b in bars_rth), 2)
    res["day_low"] = round(min(b["l"] for b in bars_rth), 2)
    res["day_change"] = round(bars_rth[-1]["c"] - bars_rth[0]["o"], 2)

    dist = open_930 - bp
    res["dist"] = round(dist, 2)
    res["dist_pct"] = round(dist / bp * 100, 2) if bp else None
    if abs(dist) < 0.25:
        res["direccion"] = "DOBLE_DIR"
    elif dist > 0.25:
        res["direccion"] = "ALCISTA"
    else:
        res["direccion"] = "BAJISTA"

    # --- estructura_vs_bp y estructura_premarket_vs_bp ---
    _dir = res["direccion"]
    res["estructura_vs_bp"] = _estructura_vs_bp(res["estructura_prev_day"], _dir)
    res["estructura_premarket_vs_bp"] = _estructura_vs_bp(res["estructura_premarket"], _dir)

    # --- open_zone y open_pct_en_rango ---
    # open_pct_en_rango: número continuo donde 0.0=BP, 1.0=INT, 2.0=MAX, etc.
    # open_zone: categoría del día según dónde abrió respecto al rango BP→INT→MAX
    # Tercios: cada zona (normal / ext_int_max / ext_beyond_max) se divide en 3 tercios
    # usando la misma distancia dist_bp_to_int como unidad de medida.
    # Ejemplo alcista: 0.0–0.33=normal_1, 0.33–0.66=normal_2, 0.66–1.0=normal_3,
    #                  1.0–1.33=ext_int_max_1, ..., 2.0–2.33=ext_beyond_max_1, etc.

    if abs(dist) < 0.25:
        res["open_zone"] = "doble_dir_zone"
        res["open_pct_en_rango"] = 0.0
    else:
        if res["direccion"] == "ALCISTA":
            dist_bp_to_int = int_pos - bp
            if dist_bp_to_int > 0:
                pct = (open_930 - bp) / dist_bp_to_int
            else:
                pct = None
        else:  # BAJISTA
            dist_bp_to_int = bp - int_neg
            if dist_bp_to_int > 0:
                pct = (bp - open_930) / dist_bp_to_int
            else:
                pct = None

        res["open_pct_en_rango"] = round(pct, 3) if pct is not None else None

        if pct is None:
            res["open_zone"] = None
        elif pct < 0:
            res["open_zone"] = "contra_bp"        # abrió del lado opuesto al esperado
        elif pct < 0.33:
            res["open_zone"] = "normal_t1"        # 1er tercio: pegado al BP
        elif pct < 0.66:
            res["open_zone"] = "normal_t2"        # 2do tercio: zona media
        elif pct < 1.0:
            res["open_zone"] = "normal_t3"        # 3er tercio: cerca del INT
        elif pct < 1.33:
            res["open_zone"] = "ext_int_max_t1"   # acaba de pasar INT
        elif pct < 1.66:
            res["open_zone"] = "ext_int_max_t2"
        elif pct < 2.0:
            res["open_zone"] = "ext_int_max_t3"   # casi en MAX
        elif pct < 2.33:
            res["open_zone"] = "ext_beyond_max_t1" # acaba de pasar MAX
        elif pct < 2.66:
            res["open_zone"] = "ext_beyond_max_t2"
        else:
            res["open_zone"] = "ext_beyond_max_t3" # extensión extrema

    anal = analyze_regular_session(
        bars_rth, bp, int_pos, int_neg, max_pos, max_neg,
        open_930_preserve=open_930,
        cerro_bp_tol=0.15,
    )
    res["toco_int"] = anal["toco_int"]
    res["toco_int_dir"] = anal["toco_int_dir"]
    res["minuto_toco_int"] = anal["minuto_toco_int"]
    res["toco_max"] = anal["toco_max"]
    res["toco_max_dir"] = anal["toco_max_dir"]
    res["minuto_toco_max"] = anal["minuto_toco_max"]
    res["toco_bp_antes_int"] = anal["toco_bp_antes_int"]
    res["primer_movimiento"] = anal["primer_movimiento"]
    res["cerro_bp"] = anal["cerro_bp"]
    res["c_int"] = anal.get("c_int") if anal.get("c_int") != "" else None
    res["c_max"] = anal.get("c_max") if anal.get("c_max") != "" else None
    _ci = res["c_int"]
    _cm = res["c_max"]
    if isinstance(_ci, (int, float)) and isinstance(_cm, (int, float)) and _cm > 0:
        res["c_int_c_max_ratio"] = round(float(_ci) / float(_cm), 3)
    else:
        res["c_int_c_max_ratio"] = None

    if res.get("skip") != "Sí" and bars_rth:
        res["caution_real_930"] = compute_caution_real_930(
            tfs, fecha, res.get("open_930"), ticker, tfs_rth=tfs_rth,
        )
        res["caution_premarket_open"] = compute_caution_premarket_open930(
            tfs, fecha, fecha_str, res.get("open_930"), ticker,
        )
        res["caution_cambio"] = compute_caution_cambio(
            res.get("caution_real_930"),
            res.get("caution_premarket_open"),
            ticker,
        )
        res["caution_cambio_1v3"] = compute_caution_cambio_1v3(
            dash.get("caution_nota"),
            res.get("caution_real_930"),
            ticker,
            tfs,
        )
        extra = compute_extra_result_fields(
            bars_rth, anal, res["direccion"],
            bp, int_pos, int_neg, max_pos, max_neg,
        )
        for k, v in extra.items():
            res[k] = v

    return dash, res


# =============================================================================
# EXCEL
# =============================================================================
def _header_style(ws, row_num: int, num_cols: int):
    fill = PatternFill(start_color=HEADER_FILL, end_color=HEADER_FILL, fill_type="solid")
    font = Font(name="Arial", size=9, bold=True, color="FFFFFF")
    for c in range(1, num_cols + 1):
        cell = ws.cell(row=row_num, column=c)
        cell.fill = fill
        cell.font = font
        cell.alignment = Alignment(horizontal="center", vertical="center")


def _row_fill(ws, row_num: int, num_cols: int, is_alt: bool):
    color = ROW_ALT1 if is_alt else ROW_ALT2
    fill = PatternFill(start_color=color, end_color=color, fill_type="solid")
    for c in range(1, num_cols + 1):
        ws.cell(row=row_num, column=c).fill = fill


def _cond_fill(cell, direccion: str, val, col_type: str):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return
    s = str(val).strip().upper()
    if col_type == "toco_int_dir":
        if direccion == "DOBLE_DIR":
            cell.fill = PatternFill(start_color=YELLOW, end_color=YELLOW, fill_type="solid")
        elif (direccion == "ALCISTA" and s in ("INT_POS", "AMBOS")) or (
            direccion == "BAJISTA" and s in ("INT_NEG", "AMBOS")
        ):
            cell.fill = PatternFill(start_color=GREEN, end_color=GREEN, fill_type="solid")
        else:
            cell.fill = PatternFill(start_color=RED, end_color=RED, fill_type="solid")
    elif col_type == "toco_max_dir":
        if direccion == "DOBLE_DIR":
            cell.fill = PatternFill(start_color=YELLOW, end_color=YELLOW, fill_type="solid")
        elif (direccion == "ALCISTA" and s in ("MAX_POS", "AMBOS")) or (
            direccion == "BAJISTA" and s in ("MAX_NEG", "AMBOS")
        ):
            cell.fill = PatternFill(start_color=GREEN, end_color=GREEN, fill_type="solid")
        else:
            cell.fill = PatternFill(start_color=RED, end_color=RED, fill_type="solid")
    elif col_type == "direccion":
        if s == "ALCISTA":
            cell.fill = PatternFill(start_color=GREEN, end_color=GREEN, fill_type="solid")
        elif s == "BAJISTA":
            cell.fill = PatternFill(start_color=RED, end_color=RED, fill_type="solid")
        elif s == "DOBLE_DIR":
            cell.fill = PatternFill(start_color=YELLOW, end_color=YELLOW, fill_type="solid")
    elif col_type == "skip" and s == "SÍ":
        cell.fill = PatternFill(start_color=RED, end_color=RED, fill_type="solid")
    elif col_type == "toco_bp_antes_int" and s == "SÍ":
        cell.fill = PatternFill(start_color=ORANGE, end_color=ORANGE, fill_type="solid")


def export_excel(dashboard: List[dict], resultados: List[dict], out_path: str) -> None:
    if not openpyxl:
        raise ImportError("openpyxl required")

    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    dash_cols = [
        "fecha", "long_pct", "short_pct", "gap_tipo", "gap_mag",
        "prev_day_high", "prev_day_low", "prev_day_close", "prev_day_change",
        "BP_pred", "INT_POS", "INT_NEG", "MAX_POS", "MAX_NEG",
        "pre_mkt_bbt_1h", "pre_mkt_bbb_1h",
        "market_close_bbt_1h", "market_close_bbb_1h",
        "bbt_5m", "bbb_5m", "bbt_clf_5m", "bbb_clf_5m",
        "avg_overbought", "avg_oversold",
        "ema20_pre", "ema50_pre", "ema200_pre",
        "signals_3_9", "caution_nota", "n_cautiones", "bb_expande_premarket",
        "rango_raw", "rango_usado",
    ]
    ws1 = wb.create_sheet("Dashboard", 0)
    for c, col in enumerate(dash_cols, 1):
        ws1.cell(row=1, column=c, value=col)
    _header_style(ws1, 1, len(dash_cols))
    for r, row in enumerate(dashboard, 2):
        is_alt = (r - 2) % 2 == 1
        for c, col in enumerate(dash_cols, 1):
            v = row.get(col)
            if isinstance(v, float) and col not in ("fecha", "gap_tipo", "signals_3_9", "caution_nota", "n_cautiones", "bb_expande_premarket"):
                v = round(v, 2) if not pd.isna(v) else None
            ws1.cell(row=r, column=c, value=v)
        _row_fill(ws1, r, len(dash_cols), is_alt)
    ws1.freeze_panes = "A2"
    for c in range(1, len(dash_cols) + 1):
        ws1.column_dimensions[get_column_letter(c)].width = 12

    res_cols = [
        "fecha", "BP_pred", "open_930", "dist", "dist_pct", "direccion",
        "open_zone", "open_pct_en_rango",
        "c_int", "c_max", "c_int_c_max_ratio",
        "estructura_prev_day",
        "estructura_vs_bp",
        "estructura_premarket",
        "estructura_premarket_vs_bp",
        "pct_en_vela_pre",
        "zona_precio_en_vela_pre",
        "pct_en_vela_d1",
        "zona_precio_en_vela_d1",
        "resistencia_pre",
        "correccion_pre",
        "soporte_pre",
        "resistencia_d1",
        "correccion_d1",
        "soporte_d1",
        "day_change", "day_high", "day_low",
        "toco_int", "toco_int_dir", "minuto_toco_int",
        "toco_max", "toco_max_dir", "minuto_toco_max",
        "toco_bp_antes_int", "primer_movimiento", "cerro_bp", "skip", "razon_skip",
        "int_contrario_tocado", "minuto_int_contrario",
        "precio_tras_bp_min", "precio_tras_bp_max",
        "tramo_bp_a_int_minutos", "rebote_post_int_cents", "segundo_toque_int",
        "caution_real_930", "caution_premarket_open", "caution_cambio", "caution_cambio_1v3",
    ]
    ws2 = wb.create_sheet("Resultados", 1)
    for c, col in enumerate(res_cols, 1):
        ws2.cell(row=1, column=c, value=col)
    _header_style(ws2, 1, len(res_cols))
    for r, res in enumerate(resultados, 2):
        is_alt = (r - 2) % 2 == 1
        direccion = res.get("direccion") or ""
        for c, col in enumerate(res_cols, 1):
            v = res.get(col)
            cell = ws2.cell(row=r, column=c, value=v)
            if col == "toco_int_dir":
                _cond_fill(cell, direccion, v, "toco_int_dir")
            elif col == "toco_max_dir":
                _cond_fill(cell, direccion, v, "toco_max_dir")
            elif col == "direccion":
                _cond_fill(cell, direccion, v, "direccion")
            elif col == "skip":
                _cond_fill(cell, direccion, v, "skip")
            elif col == "toco_bp_antes_int":
                _cond_fill(cell, direccion, v, "toco_bp_antes_int")
        _row_fill(ws2, r, len(res_cols), is_alt)

    # TOTALES
    total_row = len(resultados) + 2
    skips = sum(1 for r in resultados if r.get("skip") == "Sí")
    no_skip = [r for r in resultados if r.get("skip") != "Sí"]
    alcs = sum(1 for r in resultados if r.get("direccion") == "ALCISTA")
    bajs = sum(1 for r in resultados if r.get("direccion") == "BAJISTA")
    dobles = sum(1 for r in resultados if r.get("direccion") == "DOBLE_DIR")

    def toco_int_ok(r):
        d = r.get("direccion") or ""
        t = (r.get("toco_int_dir") or "").strip().upper()
        if d == "ALCISTA":
            return t in ("INT_POS", "AMBOS")
        if d == "BAJISTA":
            return t in ("INT_NEG", "AMBOS")
        if d == "DOBLE_DIR":
            return t not in ("", "NINGUNO")
        return False

    def toco_max_ok(r):
        d = r.get("direccion") or ""
        t = (r.get("toco_max_dir") or "").strip().upper()
        if d == "ALCISTA":
            return t in ("MAX_POS", "AMBOS")
        if d == "BAJISTA":
            return t in ("MAX_NEG", "AMBOS")
        if d == "DOBLE_DIR":
            return t not in ("", "NINGUNO")
        return False

    n_no_skip = len(no_skip)
    pct_int = (sum(1 for r in no_skip if toco_int_ok(r)) / n_no_skip * 100) if n_no_skip else 0
    pct_max = (sum(1 for r in no_skip if toco_max_ok(r)) / n_no_skip * 100) if n_no_skip else 0
    pct_skip = (skips / len(resultados) * 100) if resultados else 0
    pct_alc = (alcs / len(resultados) * 100) if resultados else 0
    pct_baj = (bajs / len(resultados) * 100) if resultados else 0
    pct_dob = (dobles / len(resultados) * 100) if resultados else 0

    ws2.cell(row=total_row, column=1, value="TOTALES")
    ws2.cell(row=total_row + 1, column=1, value=f"Total dias: {len(resultados)}")
    ws2.cell(row=total_row + 2, column=1, value=f"% toco_int correcto: {pct_int:.1f}%")
    ws2.cell(row=total_row + 3, column=1, value=f"% toco_max correcto: {pct_max:.1f}%")
    ws2.cell(row=total_row + 4, column=1, value=f"% SKIP: {pct_skip:.1f}%")
    ws2.cell(row=total_row + 5, column=1, value=f"% ALCISTA: {pct_alc:.1f}% | BAJISTA: {pct_baj:.1f}% | DOBLE_DIR: {pct_dob:.1f}%")

    ws2.freeze_panes = "A2"
    for c in range(1, len(res_cols) + 1):
        ws2.column_dimensions[get_column_letter(c)].width = 12

    wb.save(out_path)


# =============================================================================
# MAIN
# =============================================================================
def _fmt(x: Any) -> str:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return "N/A"
    if isinstance(x, float):
        return f"{x:.2f}"
    return str(x)


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Backtesting multi-TF BP, BB, signals, CAUTION.")
    ap.add_argument("--ticker", default="NVDA", help="Ticker a procesar (default: NVDA)")
    ap.add_argument("--test", action="store_true", help="Solo oct 2025")
    ap.add_argument("--verify", action="store_true", help="Verifica premarket 2025-10-06")
    ap.add_argument(
        "--all",
        action="store_true",
        help="Corre todos los tickers del data_collector en secuencia",
    )
    return ap.parse_args()


def main(args: argparse.Namespace) -> None:
    TICKERS_ACTIVOS = [
        "NVDA", "QQQ", "SPY", "IWM", "DIA", "GLD", "TLT",
        "TSLA", "AMD", "AAPL", "META", "MSFT", "GOOGL", "AMZN",
        "COIN", "PLTR", "AVGO",
    ]

    tickers_a_correr = TICKERS_ACTIVOS if args.all else [args.ticker.upper()]

    for ticker in tickers_a_correr:
        print(f"\n{'='*60}")
        print(f"PROCESANDO: {ticker}")
        print(f"{'='*60}")

        test_mode = args.test
        _inicio = TEST_INICIO if test_mode else INICIO

        script_dir = Path(__file__).parent
        out_name = (
            f"MI_BACKTESTING_{ticker}_v2.xlsx"
            if ticker == "BTCUSDT"
            else f"MI_BACKTESTING_{ticker}.xlsx"
        )
        out_path = script_dir / out_name

        print(f"Cargando parquets para {ticker}...")
        tfs = load_all_tfs(ticker)
        print("Precalculando velas RTH por TF (una vez)...")
        tfs_rth = build_tfs_rth(tfs, ticker)
        df_1d = tfs["1d"]
        trading_dates = sorted(df_1d["date_et"].unique())
        trading_dates = [d for d in trading_dates if str(d) >= _inicio and str(d) <= FIN]
        total = len(trading_dates)

        if test_mode:
            oct_dates = [d for d in trading_dates if str(d).startswith("2025-10")]
            trading_dates = oct_dates
            total = len(trading_dates)
            print(f"\n--- Modo prueba: {total} dias de oct 2025 ---\n")

        dashboard = []
        resultados = []
        dias_sin_datos = []

        for i, fecha in enumerate(trading_dates):
            fecha_str = str(fecha)
            if (i + 1) % 50 == 0 and not test_mode:
                pct = (i + 1) / total * 100
                print(f"Procesados {i + 1}/{total} ({pct:.0f}%)")

            dash, res = process_day(tfs, fecha, fecha_str, ticker, tfs_rth=tfs_rth)
            if res.get("skip") == "Sí" and res.get("razon_skip") == "sin datos intraday":
                dias_sin_datos.append(fecha_str)

            if test_mode:
                print(f"fecha: {fecha_str}")
                print(f"  caution_nota: {dash.get('caution_nota', '')}")
                print(f"  caution_real_930: {res.get('caution_real_930', 'N/A')}")
                print(f"  caution_cambio: {res.get('caution_cambio', 'N/A')}")
                print(f"  caution_cambio_1v3: {res.get('caution_cambio_1v3', 'N/A')}")
                print("  " + "-" * 28)

            dashboard.append(dash)
            resultados.append(res)

        export_excel(dashboard, resultados, str(out_path))

        skips = sum(1 for r in resultados if r.get("skip") == "Sí")
        print(f"\nDias sin datos: {dias_sin_datos}")
        print(f"Total: {len(resultados)} | Sin datos: {len(dias_sin_datos)} | SKIP: {skips}")
        print(f"Excel guardado: {out_path}")


# =============================================================================
# MODO VERIFICACIÓN — Datos premarket y corte 09:28
# =============================================================================
def run_verification(ticker: str = "NVDA") -> None:
    """Verifica parquets premarket y corte para 2025-10-06."""
    ticker = ticker.upper()
    fecha_str = "2025-10-06"
    fecha = pd.Timestamp(fecha_str).date()
    print("=" * 60)
    print(f"VERIFICACIÓN DATOS PREMARKET Y CORTE 09:28 — {ticker} — 2025-10-06")
    print("=" * 60)

    tfs = load_all_tfs(ticker)
    df_1m = tfs["1m"]

    # 1. Velas del día completo 1m
    df_day = df_1m[df_1m["date_et"] == fecha].sort_values("timestamp")
    if df_day.empty:
        print("\n1. PARQUETS PREMARKET:")
        print("   ERROR: No hay velas 1m para 2025-10-06")
        return

    first = df_day.iloc[0]
    last = df_day.iloc[-1]
    first_ts = first["timestamp"]
    last_ts = last["timestamp"]
    first_hour = f"{first_ts.hour:02d}:{first_ts.minute:02d}:{first_ts.second:02d}"
    last_hour = f"{last_ts.hour:02d}:{last_ts.minute:02d}:{last_ts.second:02d}"

    before_930 = df_day[(df_day["timestamp"].dt.hour < 9) | (
        (df_day["timestamp"].dt.hour == 9) & (df_day["timestamp"].dt.minute < 30)
    )]
    between_4_928 = df_day[
        (df_day["timestamp"].dt.hour >= 4) &
        ((df_day["timestamp"].dt.hour < 9) | (
            (df_day["timestamp"].dt.hour == 9) & (df_day["timestamp"].dt.minute < 28)
        ))
    ]

    print("\n1. PARQUETS PREMARKET (1m):")
    print(f"   Primera vela del dia: {first_ts} | O={first['open']:.2f} H={first['high']:.2f} L={first['low']:.2f} C={first['close']:.2f}")
    print(f"   Ultima vela del dia:  {last_ts} | O={last['open']:.2f} H={last['high']:.2f} L={last['low']:.2f} C={last['close']:.2f}")
    print(f"   Velas antes de 09:30: {len(before_930)}")
    print(f"   Velas entre 04:00-09:28: {len(between_4_928)}")
    print(f"   Hora primera vela del dia: {first_hour} ET")

    # 2. Corte 09:28 — EMA3/EMA9 1m
    cutoff = pd.Timestamp(f"{fecha_str} 09:28:00", tz=ET)
    slice_1m = get_premarket_slice_with_prev(df_1m, fecha, fecha_str, HORA_CORTE, 500)
    if slice_1m.empty:
        print("\n2. CORTE 09:28 — 1m:")
        print("   ERROR: Slice vacio")
    else:
        close = slice_1m["close"]
        ema3 = close.ewm(span=3, adjust=False).mean().iloc[-1]
        ema9 = close.ewm(span=9, adjust=False).mean().iloc[-1]
        last_row = slice_1m.iloc[-1]
        last_ts_slice = last_row["timestamp"]
        last_h_slice = f"{last_ts_slice.hour:02d}:{last_ts_slice.minute:02d}:{last_ts_slice.second:02d}"

        print("\n2. CORTE 09:28 — 1m:")
        print(f"   EMA3 hasta 09:28: {ema3:.2f}")
        print(f"   EMA9 hasta 09:28: {ema9:.2f}")
        print(f"   Ultima vela usada: {last_ts_slice} (hora exacta: {last_h_slice} ET)")
        print(f"   Precio ultima vela (close): {last_row['close']:.2f}")
        print(f"   Velas 1m usadas para EMA: {len(slice_1m)}")
        print(f"   Cutoff estricto: timestamp < {cutoff}")

    # 3. Multi-TF — última vela antes de 09:28
    print("\n3. MULTI-TF — Ultima vela incluida antes de 09:28:")
    for tf_name in ["5m", "15m", "30m", "1h"]:
        if tf_name not in tfs or tfs[tf_name] is None or tfs[tf_name].empty:
            continue
        df = tfs[tf_name]
        slice_tf = get_premarket_slice_with_prev(df, fecha, fecha_str, HORA_CORTE, 200)
        if slice_tf.empty:
            print(f"   {tf_name}: Sin datos")
            continue
        last_row = slice_tf.iloc[-1]
        ts = last_row["timestamp"]
        h = ts.hour
        m = ts.minute
        is_pre = h < 9 or (h == 9 and m < 30)
        tipo = "PREMARKET" if is_pre else "RTH"
        print(f"   {tf_name}: {ts} | close={last_row['close']:.2f} | {tipo}")

    print("\n" + "=" * 60)


if __name__ == "__main__":
    args = _parse_args()
    if args.verify:
        run_verification(args.ticker)
    else:
        main(args)
