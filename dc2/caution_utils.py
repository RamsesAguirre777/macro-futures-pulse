from __future__ import annotations

"""
caution_utils.py — RTH filtering + CAUTION multi-TF computation.

Extracted from backtest_engine.py for use inside dc2 without exposing
the full backtesting codebase.
"""

from typing import Dict, List, Optional, Tuple

import pandas as pd

N_VELAS_BB = 20


# ── TF helpers ───────────────────────────────────────────────────────────────

def mid_tf_key(ticker: str, tfs: Optional[Dict[str, pd.DataFrame]] = None) -> str:
    """With live tfs dict: 3m if data exists, else 5m."""
    if tfs is not None:
        d3 = tfs.get("3m")
        if d3 is not None and not d3.empty:
            return "3m"
        return "5m"
    return "3m"


def caution_tf_key_label_pairs(
    ticker: str, tfs: Optional[Dict[str, pd.DataFrame]] = None
) -> List[Tuple[str, str]]:
    """Fixed TF list for CAUTION: 1M, 5M, 15M, 30M, 1H, 1D."""
    return [
        ("1m",  "1M"),
        ("5m",  "5M"),
        ("15m", "15M"),
        ("30m", "30M"),
        ("1h",  "1H"),
        ("1d",  "1D"),
    ]


def caution_tf_labels(
    ticker: str, tfs: Optional[Dict[str, pd.DataFrame]] = None
) -> Tuple[str, ...]:
    return tuple(lab for _, lab in caution_tf_key_label_pairs(ticker, tfs))


# ── RTH filtering ────────────────────────────────────────────────────────────

def rth_mask_vectorized(df: pd.DataFrame) -> pd.Series:
    """Vectorized RTH mask: 09:30–16:00 ET inclusive."""
    ts = df["timestamp"]
    h  = ts.dt.hour
    m  = ts.dt.minute
    not_rth = (h < 9) | ((h == 9) & (m < 30)) | (h > 16) | ((h == 16) & (m > 0))
    return ~not_rth


def build_tfs_rth(
    tfs: Dict[str, pd.DataFrame], ticker: str
) -> Optional[Dict[str, pd.DataFrame]]:
    """Pre-build RTH-only DataFrames per TF. Returns None for BTCUSDT."""
    if ticker.upper() == "BTCUSDT":
        return None
    out: Dict[str, pd.DataFrame] = {}
    mid = mid_tf_key(ticker, tfs)
    for tf in ["1m", mid, "5m", "15m", "30m", "1h"]:
        if tf not in tfs or tfs[tf] is None or tfs[tf].empty:
            continue
        mask = rth_mask_vectorized(tfs[tf])
        out[tf] = tfs[tf][mask].copy()
    return out


# ── BB(20,2) ─────────────────────────────────────────────────────────────────

def _compute_bb(slice_df: pd.DataFrame) -> Tuple[Optional[float], Optional[float]]:
    """EWM BB(20,2) on 'close' column of slice_df."""
    if slice_df.empty or len(slice_df) < N_VELAS_BB:
        return None, None
    close = slice_df["close"].tail(N_VELAS_BB)
    mid   = close.ewm(span=N_VELAS_BB, adjust=False).mean().iloc[-1]
    std   = close.ewm(span=N_VELAS_BB, adjust=False).std().iloc[-1]
    if pd.isna(mid) or pd.isna(std):
        return None, None
    return float(mid + 2 * std), float(mid - 2 * std)


# ── RTH slice until 9:30 ─────────────────────────────────────────────────────

def _get_slice_rth_until_930(
    df: pd.DataFrame,
    fecha,
    open_930: Optional[float],
    n_velas: int = N_VELAS_BB,
    df_rth_pre: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """19 RTH bars from previous days + first RTH bar of fecha."""
    df_rth = df_rth_pre if df_rth_pre is not None else df[rth_mask_vectorized(df)].copy()
    prev_rth  = df_rth[df_rth["date_et"] < fecha].sort_values("timestamp").tail(n_velas - 1)
    today_rth = df_rth[df_rth["date_et"] == fecha].sort_values("timestamp").head(1)
    if prev_rth.empty or today_rth.empty or len(prev_rth) < n_velas - 1:
        return pd.DataFrame()
    return pd.concat([prev_rth, today_rth], ignore_index=True).tail(n_velas)


# ── caution string parsing ────────────────────────────────────────────────────

def _extraer_pares(
    caution_str: Optional[str],
    ticker: str,
    tfs: Optional[Dict[str, pd.DataFrame]] = None,
) -> set:
    labels = caution_tf_labels(ticker, tfs)
    s = str(caution_str or "")
    if s in ("Sin caution real", "Sin caution premarket", "Sin caution",
             "Sin datos BB", "N/A", ""):
        return set()
    out = set()
    for lab in sorted(labels, key=len, reverse=True):
        if f"BBT {lab}" in s:
            out.add(("BBT", lab))
        if f"BBB {lab}" in s:
            out.add(("BBB", lab))
    return out


# ── CAUTION computation ───────────────────────────────────────────────────────

def compute_caution_real_930(
    tfs: Dict[str, pd.DataFrame],
    fecha,
    open_930: Optional[float],
    ticker: str = "",
    tfs_rth: Optional[Dict[str, pd.DataFrame]] = None,
) -> Optional[str]:
    """BB(20,2) over 19 previous RTH bars + first RTH bar of the day."""
    if open_930 is None:
        return None
    partes: List[str] = []
    any_bb = False
    for tf, label in caution_tf_key_label_pairs(ticker, tfs):
        if tf not in tfs or tfs[tf] is None or tfs[tf].empty:
            continue
        df_rth_pre = tfs_rth.get(tf) if tfs_rth else None
        if tf == "1d":
            continue  # 1d handled separately in backtest context
        sl = _get_slice_rth_until_930(tfs[tf], fecha, open_930,
                                       df_rth_pre=df_rth_pre)
        bbt, bbb = _compute_bb(sl)
        if bbt is not None:
            any_bb = True
        if bbt is not None and open_930 > bbt:
            partes.append(f"BBT {label}")
        if bbb is not None and open_930 < bbb:
            partes.append(f"BBB {label}")
    if not any_bb:
        return "Sin datos BB"
    return " | ".join(partes) if partes else "Sin caution real"


def compute_caution_cambio_1v3(
    caution_nota: Optional[str],
    caution_real_930: Optional[str],
    ticker: str,
    tfs: Optional[Dict[str, pd.DataFrame]] = None,
) -> Optional[str]:
    """Compares caution at 9:28 (Milton) vs first RTH bar. Operational delta."""
    if caution_real_930 is None or caution_nota is None:
        return None
    pre_pares  = _extraer_pares(caution_nota, ticker, tfs)
    real_pares = _extraer_pares(caution_real_930, ticker, tfs)
    if pre_pares == real_pares:
        return "igual"
    if not pre_pares and real_pares:
        return "nuevo"
    if pre_pares and not real_pares:
        return "desaparecio"
    if real_pares.issuperset(pre_pares):
        return "escalo"
    return "cambio_tipo"
