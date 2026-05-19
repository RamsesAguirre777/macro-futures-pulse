from __future__ import annotations

"""
Telegram commands for BB Retroceso + postmarket screener.

/screener              — postmarket screener universo completo (~6500 tickers)
/bb TICKER [modo]      — BB Retroceso via dc2.main, ticker individual
                         modos: 930 (default) · 9_28 · 9_15
"""

import asyncio
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pytz
from telegram import Update
from telegram.ext import ContextTypes

from dc2.macro_futures.mode_macro import run_macro_analysis

ROOT = Path(__file__).resolve().parent.parent
_ET = pytz.timezone("America/New_York")

# Two slots: one for screener, one for bb — never blocks the bot itself
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="bb_cmd")

# Valid tickers for /bb (from dc2/constants.py)
TICKERS_BB: list[str] = [
    "NVDA", "QQQ", "SPY", "IWM", "DIA", "GLD", "TLT", "TSLA",
    "AMD", "AAPL", "META", "MSFT", "GOOGL", "AMZN", "COIN", "PLTR", "AVGO",
]
_TICKERS_BB_SET = set(TICKERS_BB)

TG_MAX = 4000  # safety margin below Telegram's 4096 limit


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _chunks(text: str, max_len: int = TG_MAX) -> list[str]:
    if len(text) <= max_len:
        return [text]
    out: list[str] = []
    while text:
        if len(text) <= max_len:
            out.append(text)
            break
        cut = text.rfind("\n", 0, max_len)
        if cut <= 0:
            cut = max_len
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return out


# ─── Screener (solo 1D — no Alpaca intraday) ──────────────────────────────────

def _run_screener_sync(
    min_price: float,
    max_price: float,
    min_volume: int,
) -> str:
    import numpy as np

    from dc2.screener.postmarket_screener import (
        compute_1d_row,
        download_1d_all,
        filter_long_1d,
        filter_short_1d,
        load_universe,
    )

    t0 = time.time()
    universe = load_universe()

    yf_map, _ = download_1d_all(universe)

    rows_1d: dict = {}
    for ticker, ohlcv in yf_map.items():
        row = compute_1d_row(ticker, ohlcv)
        if row is None:
            continue
        if row.close_1d < min_price or row.close_1d > max_price:
            continue
        if row.vol_1d < min_volume:
            continue
        rows_1d[ticker] = row

    cand_long = [r for r in rows_1d.values() if filter_long_1d(r)]
    cand_short = [r for r in rows_1d.values() if filter_short_1d(r)]

    def _vol(r) -> float:
        v = r.vol_ratio
        return float(v) if not np.isnan(v) else 0.0

    cand_long.sort(key=_vol, reverse=True)
    cand_short.sort(key=_vol, reverse=True)

    elapsed = time.time() - t0
    fecha = datetime.now(_ET).strftime("%Y-%m-%d")

    lines = [
        f"📊 SCREENER {fecha}  [solo 1D]",
        f"Universo: {len(universe)} tickers  |  ⏱ {elapsed:.0f}s",
        f"Filtros: ${min_price}–${max_price}  vol≥{min_volume:,}",
    ]

    lines.append(f"\n🟢 LONG ({len(cand_long)}):")
    if cand_long:
        for r in cand_long:
            vol_str = f"VOL×{_vol(r):.1f}" if _vol(r) else "VOL×?"
            lines.append(f"  {r.ticker:<6}  ${r.close_1d:>8,.2f}  {vol_str}")
    else:
        lines.append("  (ninguno)")

    lines.append(f"\n🔴 SHORT ({len(cand_short)}):")
    if cand_short:
        for r in cand_short:
            vol_str = f"VOL×{_vol(r):.1f}" if _vol(r) else "VOL×?"
            lines.append(f"  {r.ticker:<6}  ${r.close_1d:>8,.2f}  {vol_str}")
    else:
        lines.append("  (ninguno)")

    return "\n".join(lines)


async def cmd_screener(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /screener — universo completo NASDAQ+S&P500 ($3-$45, vol≥10M, solo 1D)
    """
    await update.message.reply_text(
        "⏳ Screener universo completo (~6500 tickers, solo 1D)\n"
        "Puede tardar 3–5 min descargando yfinance..."
    )

    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            _executor,
            lambda: _run_screener_sync(3.0, 45.0, 10_000_000),
        )
    except Exception as exc:
        await update.message.reply_text(f"❌ Screener falló: {exc}")
        return

    for chunk in _chunks(result):
        await update.message.reply_text(f"<pre>{chunk}</pre>", parse_mode="HTML")


# ─── BB Retroceso via dc2.main ─────────────────────────────────────────────────

def _run_bb_sync(ticker: str | None, modo: str) -> str:
    cmd = [sys.executable, "-m", "dc2.main", "--modo", modo]
    if ticker:
        cmd += ["--ticker", ticker]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(ROOT),
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        return "❌ Timeout — dc2.main tardó más de 3 min."
    except Exception as exc:
        return f"❌ Error al lanzar proceso: {exc}"

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()

    if proc.returncode != 0:
        tail = err[-1000:] if err else "(sin stderr)"
        return f"❌ dc2.main rc={proc.returncode}\n{tail}"

    return out or err or "(sin output)"


def _detect_modo_by_time() -> str:
    """Detecta el modo correcto según hora ET actual."""
    now = datetime.now(_ET)
    mins = now.hour * 60 + now.minute
    if mins < 9 * 60 + 30:      # antes de 9:30 → premarket
        return "premarket_9_28"
    return "live"                # 9:30 en adelante → BP live yfinance


async def cmd_bb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /bb NVDA          — auto-detect: premarket_9_28 antes de 9:30 ET, live después
    /bb NVDA 930      — open_930
    /bb NVDA 9_28     — premarket_9_28
    /bb NVDA 9_15     — premarket_9_15
    """
    args = context.args or []

    if not args:
        valid = "  ".join(TICKERS_BB)
        await update.message.reply_text(
            f"Uso: /bb <TICKER> [modo]\n\nTickers válidos:\n{valid}\n\n"
            "Modos: 930 · 9_28 · 9_15\n"
            "Sin modo: auto-detect por hora ET (premarket o live)"
        )
        return

    ticker = args[0].upper()
    if ticker not in _TICKERS_BB_SET:
        valid = "  ".join(TICKERS_BB)
        await update.message.reply_text(
            f"❌ Ticker inválido: {ticker}\n\nTickers válidos:\n{valid}"
        )
        return

    if len(args) >= 2:
        raw = args[1].lower()
        if "930" in raw:
            modo = "open_930"
        elif "28" in raw:
            modo = "premarket_9_28"
        elif "15" in raw:
            modo = "premarket_9_15"
        else:
            modo = _detect_modo_by_time()
    else:
        modo = _detect_modo_by_time()

    await update.message.reply_text(f"⏳ BB Retroceso — {ticker}  [{modo}]...")

    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            _executor,
            lambda: _run_bb_sync(ticker, modo),
        )
    except Exception as exc:
        await update.message.reply_text(f"❌ Error: {exc}")
        return

    for chunk in _chunks(result):
        await update.message.reply_text(f"<pre>{chunk}</pre>", parse_mode="HTML")


# ─── Macro futures ─────────────────────────────────────────────────────────────

def _format_macro_table(data: dict) -> str:
    """Builds macro table string directly — thread-safe, no stdout redirect."""
    from dc2.macro_futures.run import (
        _fmt_card_rango,
        _fmt_dist_pct,
        _fmt_gap_line1,
        _fmt_signals_compact,
    )

    w = 54
    now = datetime.now(_ET)
    lines: list[str] = [
        "",
        "═" * w,
        now.strftime("MACRO FUTURES + REFUGIOS — %H:%M ET  %Y-%m-%d"),
        "Session opens: ES/NQ/YM=18:00 ET  |  BTC=00:00 ET",
        "═" * w,
    ]

    futures = data.get("futures", {})
    cards = [(sym, futures[sym]) for sym in ("ES", "NQ", "YM", "BTC", "ZB", "GC") if sym in futures]

    for idx, (sym, r) in enumerate(cards):
        direction = str(r.get("direction", ""))
        zona = str(r.get("zona", ""))
        d_pct = _fmt_dist_pct(r.get("dist_overnight_pct")) or "—"
        bd = r.get("badge")
        bdg_str = f"{float(bd):.1f}" if bd is not None else "N/D"

        lines.append(
            f"  {sym:>5}    {direction:<9}  {zona:>4}    D% {d_pct}    BDG {bdg_str}"
            f"{_fmt_gap_line1(r.get('gap_tipo'), r.get('gap_mag'))}"
        )

        try:
            pr_f = float(r["price"]) if r.get("price") is not None else None
        except (TypeError, ValueError):
            pr_f = None
        prc_part = f"{pr_f:>10,.2f}" if pr_f is not None else f"{'N/D':>10}"
        line2 = f"  PRC {prc_part}    BP "
        line2 += " N/D" if r.get("bp") is None else f"{float(r['bp']):>10,.4f}"
        lines.append(line2)

        if r.get("bp") is None:
            lines.append("  INT  N/D    MAX  N/D")
        else:
            ip, inn = float(r["int_pos"]), float(r["int_neg"])
            mp, mn = float(r["max_pos"]), float(r["max_neg"])
            if sym == "BTC":
                lines.append(f"  INT  ▲{ip:,.0f}  ▼{inn:,.0f}    MAX  ▲{mp:,.0f}  ▼{mn:,.0f}")
            else:
                lines.append(f"  INT  ▲{ip:,.2f}  ▼{inn:,.2f}    MAX  ▲{mp:,.2f}  ▼{mn:,.2f}")

        sig = _fmt_signals_compact(r.get("signals_3_9") or "")
        dia_s = _fmt_card_rango(r.get("price"), r.get("session_high"), r.get("session_low"))
        prv_s = _fmt_card_rango(r.get("price"), r.get("prev_session_high"), r.get("prev_session_low"))
        lines.append(f"  SIG  {sig}    DÍA {dia_s}   PRV {prv_s}")

        if idx < len(cards) - 1:
            lines.append("─" * w)

    lines.append("─" * w)

    vx = data.get("vix") or {}
    spot = vx.get("spot")
    delta = vx.get("delta")
    ddir = vx.get("dir", "N/D")
    nivel = vx.get("nivel", "N/D")
    if spot is None:
        lines.append(f"  VIX    PRC N/D    DELTA N/D    DIR {ddir}    NIVEL {nivel}")
    else:
        d_s = "N/D" if delta is None else f"{float(delta):+.2f}"
        lines.append(f"  VIX    PRC {float(spot):.2f}    DELTA {d_s}    DIR {ddir}    NIVEL {nivel}")

    lines.append("═" * w)
    return "\n".join(lines).strip()


def _run_macro_sync() -> str:
    data = run_macro_analysis(lookback_days=5)
    return _format_macro_table(data)


async def cmd_macro(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /macro — análisis macro futures (ES, NQ, YM, BTC, ZB, GC) + VIX.
    """
    await update.message.reply_text(
        "⏳ Macro futures — descargando datos (puede tardar ~30s)..."
    )

    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(_executor, _run_macro_sync)
    except Exception as exc:
        await update.message.reply_text(f"❌ {exc}")
        return

    for chunk in _chunks(result):
        await update.message.reply_text(f"<pre>{chunk}</pre>", parse_mode="HTML")
