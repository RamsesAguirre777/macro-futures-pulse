from __future__ import annotations

"""
print_dashboard.py — Clean stdout for Telegram bot.

Called by mode_9_15, mode_9_28, and mode_930 after computing each ticker.
The subprocess stdout is captured by bb_commands._run_bb_sync and sent to Telegram.
"""

import sys
from datetime import datetime
from typing import Any

from dc2.constants import TIMEZONE

W = 50  # character width for separator lines


def _reconfigure_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def _fmt(v: Any, decimals: int = 4) -> str:
    if v is None:
        return "N/D"
    return f"{float(v):>11,.{decimals}f}"


def _fmt_pct(v: Any) -> str:
    if v is None:
        return "N/D"
    f = float(v)
    sign = "+" if f >= 0 else ""
    return f"{sign}{f:.2f}%"


def _dir_arrow(direction: str) -> str:
    return {"ALCISTA": "▲", "BAJISTA": "▼"}.get(direction, "~")


_EC_LABELS: dict[str, str] = {
    "SIZE_x2":    "★★ SIZE x2",
    "SIZE_x1":    "★  SIZE x1",
    "SKIP":       "✗  SKIP",
    "ZONA_MUERTA": "~  ZM",
    "BLOQUEADO":  "⛔ BLOQUEADO",
    "SIN_DATOS":  "?  SIN_DATOS",
}

_MODO_LABELS: dict[str, str] = {
    "premarket_9_15": "PREMARKET 9:15",
    "premarket_9_28": "PREMARKET 9:28",
    "open_930":       "OPEN 9:30",
}


def _compute_zone(
    precio: float | None,
    bp: float | None,
    int_pos: float | None,
    int_neg: float | None,
    max_pos: float | None,
    max_neg: float | None,
    direction: str,
) -> str:
    """Devuelve en qué zona de targets se encuentra el precio actual."""
    if precio is None or bp is None:
        return "N/D"
    if direction == "ALCISTA":
        if int_pos is None:
            return "N/D"
        if precio < bp:
            return "bajo BP"
        elif precio < int_pos:
            return "T1  (BP → Int+)"
        elif max_pos is None or precio < max_pos:
            return "T2  (Int+ → Max+)"
        else:
            return "T3+ (más allá Max+)"
    elif direction == "BAJISTA":
        if int_neg is None:
            return "N/D"
        if precio > bp:
            return "sobre BP"
        elif precio > int_neg:
            return "T1  (BP → Int-)"
        elif max_neg is None or precio > max_neg:
            return "T2  (Int- → Max-)"
        else:
            return "T3+ (más allá Max-)"
    return "N/D"


def print_premarket_ticker(
    ticker: str,
    td: dict,
    modo: str,
    precio_ref: float | None,
    now_et: datetime | None = None,
) -> None:
    """
    Print a clean single-ticker summary to stdout.

    Parameters
    ----------
    ticker    : stock symbol
    td        : ticker data dict built by the mode (output_data["tickers"][ticker])
    modo      : "premarket_9_15" | "premarket_9_28" | "open_930"
    precio_ref: premarket price or open_930 price used as current reference
    now_et    : current ET timestamp (defaults to now)
    """
    _reconfigure_stdout()

    if now_et is None:
        now_et = datetime.now(TIMEZONE)

    ts = now_et.strftime("%H:%M ET  %Y-%m-%d")
    modo_label = _MODO_LABELS.get(modo, modo.upper())

    bp        = td.get("bp")
    int_pos   = td.get("int_pos")
    int_neg   = td.get("int_neg")
    max_pos   = td.get("max_pos")
    max_neg   = td.get("max_neg")
    direction = td.get("direction", "N/D")
    dist      = td.get("dist")
    gap_pct   = td.get("gap_pct")
    gap_type  = td.get("gap_type", "FLAT")
    range_3d  = td.get("range_3d")

    ema20  = td.get("ema20_pre")
    ema50  = td.get("ema50_pre")
    ema200 = td.get("ema200_pre")
    ema3_1h = td.get("ema3_1h")
    ema9_1h = td.get("ema9_1h")

    # BB levels
    bbt_5m_pre   = td.get("bbt_5m_pre")
    bbb_5m_pre   = td.get("bbb_5m_pre")
    bbt_1h_pre   = td.get("bbt_1h_pre")
    bbb_1h_pre   = td.get("bbb_1h_pre")
    bbt_5m_close = td.get("bbt_5m_close")
    bbb_5m_close = td.get("bbb_5m_close")
    bbt_1h_close = td.get("bbt_1h_close")
    bbb_1h_close = td.get("bbb_1h_close")

    # Prev day data
    prev_day_high = td.get("prev_day_high")
    prev_day_low  = td.get("prev_day_low")
    prev_close    = td.get("prev_close")

    signals     = td.get("signals_3_9") or td.get("signals") or ""
    badge       = td.get("badge_long")
    caution     = td.get("caution_note") or ""
    ec_accion   = td.get("ec_accion")
    ec_score    = td.get("ec_score")
    bb_expande  = td.get("bb_expande_premarket")
    skip        = td.get("skip", False)
    skip_reason = td.get("skip_reason", "")

    # open_930-specific
    open_zone   = td.get("open_zone", "")
    caution_1v3 = td.get("caution_cambio_1v3", "")
    prev_day_chg = td.get("prev_day_change")

    dir_arrow = _dir_arrow(direction)

    gap_str = ""
    if gap_type == "GAP_UP" and gap_pct is not None:
        gap_str = f"  GAP UP {_fmt_pct(gap_pct)}"
    elif gap_type == "GAP_DOWN" and gap_pct is not None:
        gap_str = f"  GAP DOWN {_fmt_pct(gap_pct)}"

    dist_str = (
        f"{dir_arrow}{abs(float(dist)):.4f}"
        if dist is not None else "N/D"
    )
    prc_str = f"{float(precio_ref):,.4f}" if precio_ref is not None else "N/D"

    bp_line_inner = f"BP  {float(bp):.4f}" if bp is not None else "BP  N/D"
    bp_dashes = "─" * max(2, W - len(bp_line_inner) - 4)
    bp_sep = f"  ── {bp_line_inner} {bp_dashes}"

    ec_str    = _EC_LABELS.get(ec_accion or "", ec_accion or "—")
    score_str = f"  [score: {ec_score}]" if ec_score is not None else ""
    badge_str = f"{float(badge):.1f}%" if badge is not None else "N/D"

    precio_label = "Open 9:30 =" if modo == "open_930" else "Precio PM ="

    L: list[str] = []
    L.append("═" * W)
    L.append(f"BB RETROCESO {modo_label} — ${ticker}")
    L.append(f"Fuente: Alpaca  |  {ts}")
    L.append("═" * W)

    if skip:
        L.append(f"  ⛔ SKIP — {skip_reason or 'filtro activado'}")
        L.append("─" * W)

    if bp is not None:
        L.append(f"  Break Point =  {float(bp):>11,.4f}")
    L.append("─" * W)

    if max_pos is not None:
        L.append(f"  Max Pos Exp =  {float(max_pos):>11,.4f}")
    if int_pos is not None:
        L.append(f"  Int Pos Exp =  {float(int_pos):>11,.4f}")
    L.append(bp_sep)
    if int_neg is not None:
        L.append(f"  Int Neg Exp =  {float(int_neg):>11,.4f}")
    if max_neg is not None:
        L.append(f"  Max Neg Exp =  {float(max_neg):>11,.4f}")

    L.append("─" * W)

    L.append(f"  {precio_label:<13} {prc_str:>11}   {dir_arrow} {direction}")
    L.append(f"  Dist BP =      {dist_str:>11}{gap_str}")
    if prev_day_chg is not None:
        L.append(f"  Prev Day Chg = {_fmt_pct(prev_day_chg):>11}")
    if open_zone:
        L.append(f"  Open Zone =    {open_zone:>11}")

    # ── Prev Day High / Low / Close ───────────────────────────────
    if any(v is not None for v in (prev_day_high, prev_day_low, prev_close)):
        L.append("─" * W)
        if prev_day_high is not None:
            L.append(f"  Prev Day High ={float(prev_day_high):>11,.4f}")
        if prev_day_low is not None:
            L.append(f"  Prev Day Low  ={float(prev_day_low):>11,.4f}")
        if prev_close is not None:
            L.append(f"  Prev Day Close={float(prev_close):>11,.4f}")

    # ── EMAs premarket ────────────────────────────────────────────
    if any(v is not None for v in (ema20, ema50, ema200)):
        L.append("─" * W)
        if ema20 is not None:
            L.append(f"  EMA20 PM =     {float(ema20):>11,.4f}")
        if ema50 is not None:
            L.append(f"  EMA50 PM =     {float(ema50):>11,.4f}")
        if ema200 is not None:
            L.append(f"  EMA200 PM =    {float(ema200):>11,.4f}")

    # ── EMA3 / EMA9 en 1H ────────────────────────────────────────
    if any(v is not None for v in (ema3_1h, ema9_1h)):
        if ema3_1h is not None:
            L.append(f"  EMA3  1H  =    {float(ema3_1h):>11,.4f}")
        if ema9_1h is not None:
            L.append(f"  EMA9  1H  =    {float(ema9_1h):>11,.4f}")

    # ── BB Premarket (5M y 1H al corte) ──────────────────────────
    has_bb_pre = any(v is not None for v in (bbt_5m_pre, bbb_5m_pre, bbt_1h_pre, bbb_1h_pre))
    if has_bb_pre:
        L.append("─" * W)
        L.append("  ─ Pre Market BB ─")
        if bbt_1h_pre is not None:
            L.append(f"  BBT 1H PM  =   {float(bbt_1h_pre):>11,.4f}")
        if bbb_1h_pre is not None:
            L.append(f"  BBB 1H PM  =   {float(bbb_1h_pre):>11,.4f}")

    # ── BB Cierre 4pm (5M y 1H al prev close) ────────────────────
    has_bb_close = any(v is not None for v in (bbt_5m_close, bbb_5m_close, bbt_1h_close, bbb_1h_close))
    if has_bb_close:
        L.append("─" * W)
        L.append("  ─ Market Close BB (4pm) ─")
        if bbt_5m_close is not None:
            L.append(f"  BBT 5M Close = {float(bbt_5m_close):>11,.4f}")
        if bbb_5m_close is not None:
            L.append(f"  BBB 5M Close = {float(bbb_5m_close):>11,.4f}")
        if bbt_1h_close is not None:
            L.append(f"  BBT 1H Close = {float(bbt_1h_close):>11,.4f}")
        if bbb_1h_close is not None:
            L.append(f"  BBB 1H Close = {float(bbb_1h_close):>11,.4f}")

    L.append("─" * W)

    if signals:
        L.append(f"  Señales:   {signals}")
    L.append(f"  Badge L:   {badge_str}")
    zone_str = _compute_zone(precio_ref, bp, int_pos, int_neg, max_pos, max_neg, direction)
    L.append(f"  Zona PM:   {zone_str}")

    if caution_1v3:
        L.append(f"  Caution 1v3: {caution_1v3}")

    # caution_note: each caution text uses \n internally; texts joined by " "
    # Split on \n to render each line separately
    if caution:
        L.append("─" * W)
        for line in caution.split("\n"):
            line = line.strip()
            if line:
                L.append(f"  ⚠  {line}")

    L.append("═" * W)

    print("\n".join(L))


def print_live_ticker(
    ticker: str,
    td: dict,
    precio: float | None,
    now_et: datetime | None = None,
) -> None:
    _reconfigure_stdout()
    if now_et is None:
        now_et = datetime.now(TIMEZONE)

    ts = now_et.strftime("%H:%M ET  %Y-%m-%d")

    bp           = td.get("bp")
    int_pos      = td.get("int_pos")
    int_neg      = td.get("int_neg")
    max_pos      = td.get("max_pos")
    max_neg      = td.get("max_neg")
    direction    = td.get("direction", "N/D")
    dist         = td.get("dist")
    open_930     = td.get("open_930")
    session_high = td.get("session_high")
    session_low  = td.get("session_low")
    ema3_1h      = td.get("ema3_1h")
    ema9_1h      = td.get("ema9_1h")
    ema20_5m     = td.get("ema20_5m")
    ema50_5m     = td.get("ema50_5m")
    ema200_5m    = td.get("ema200_5m")
    bbt_5m       = td.get("bbt_5m")
    bbb_5m       = td.get("bbb_5m")
    bbt_1h       = td.get("bbt_1h")
    bbb_1h       = td.get("bbb_1h")
    range_live   = td.get("range_live")

    dir_arrow = _dir_arrow(direction)
    prc_str   = f"{float(precio):,.4f}" if precio is not None else "N/D"
    dist_str  = f"{dir_arrow}{abs(float(dist)):.4f}" if dist is not None else "N/D"

    bp_line_inner = f"BP  {float(bp):.4f}" if bp is not None else "BP  N/D"
    bp_dashes = "─" * max(2, W - len(bp_line_inner) - 4)
    bp_sep = f"  ── {bp_line_inner} {bp_dashes}"

    zone_str = _compute_zone(precio, bp, int_pos, int_neg, max_pos, max_neg, direction)

    L: list[str] = []
    L.append("═" * W)
    L.append(f"BB RETROCESO LIVE — ${ticker}")
    L.append(f"Fuente: yfinance  |  {ts}")
    L.append("═" * W)

    if bp is not None:
        L.append(f"  Break Point =  {float(bp):>11,.4f}")
    L.append("─" * W)

    if max_pos is not None:
        L.append(f"  Max Pos Exp =  {float(max_pos):>11,.4f}")
    if int_pos is not None:
        L.append(f"  Int Pos Exp =  {float(int_pos):>11,.4f}")
    L.append(bp_sep)
    if int_neg is not None:
        L.append(f"  Int Neg Exp =  {float(int_neg):>11,.4f}")
    if max_neg is not None:
        L.append(f"  Max Neg Exp =  {float(max_neg):>11,.4f}")

    L.append("─" * W)
    L.append(f"  Precio Live =  {prc_str:>11}   {dir_arrow} {direction}")
    L.append(f"  Dist BP =      {dist_str:>11}")
    L.append(f"  Zona:          {zone_str}")

    if open_930 is not None:
        L.append(f"  Open 9:30 =    {float(open_930):>11,.4f}")
    if session_high is not None:
        L.append(f"  Session High = {float(session_high):>11,.4f}")
    if session_low is not None:
        L.append(f"  Session Low  = {float(session_low):>11,.4f}")

    L.append("─" * W)

    if any(v is not None for v in (ema20_5m, ema50_5m, ema200_5m)):
        if ema20_5m  is not None: L.append(f"  EMA20  5M =    {float(ema20_5m):>11,.4f}")
        if ema50_5m  is not None: L.append(f"  EMA50  5M =    {float(ema50_5m):>11,.4f}")
        if ema200_5m is not None: L.append(f"  EMA200 5M =    {float(ema200_5m):>11,.4f}")
    if any(v is not None for v in (ema3_1h, ema9_1h)):
        if ema3_1h is not None: L.append(f"  EMA3   1H =    {float(ema3_1h):>11,.4f}")
        if ema9_1h is not None: L.append(f"  EMA9   1H =    {float(ema9_1h):>11,.4f}")

    L.append("─" * W)

    if bbt_5m is not None: L.append(f"  BBT 5M =       {float(bbt_5m):>11,.4f}")
    if bbb_5m is not None: L.append(f"  BBB 5M =       {float(bbb_5m):>11,.4f}")
    if bbt_1h is not None: L.append(f"  BBT 1H =       {float(bbt_1h):>11,.4f}")
    if bbb_1h is not None: L.append(f"  BBB 1H =       {float(bbb_1h):>11,.4f}")

    L.append("═" * W)
    print("\n".join(L))


def print_live_crypto_ticker(
    ticker: str,
    td: dict,
    precio: float | None,
    now_et: datetime | None = None,
) -> None:
    _reconfigure_stdout()
    if now_et is None:
        now_et = datetime.now(TIMEZONE)

    ts = now_et.strftime("%H:%M ET  %Y-%m-%d")

    bp             = td.get("bp")
    int_pos        = td.get("int_pos")
    int_neg        = td.get("int_neg")
    max_pos        = td.get("max_pos")
    max_neg        = td.get("max_neg")
    direction      = td.get("direction", "N/D")
    dist           = td.get("dist")
    open_day       = td.get("open_day")
    high_24h       = td.get("high_24h")
    low_24h        = td.get("low_24h")
    change_24h_pct = td.get("change_24h_pct")
    ema3_1h        = td.get("ema3_1h")
    ema9_1h        = td.get("ema9_1h")
    ema20_5m       = td.get("ema20_5m")
    ema50_5m       = td.get("ema50_5m")
    ema200_5m      = td.get("ema200_5m")
    bbt_5m         = td.get("bbt_5m")
    bbb_5m         = td.get("bbb_5m")
    bbt_1h         = td.get("bbt_1h")
    bbb_1h         = td.get("bbb_1h")

    dir_arrow = _dir_arrow(direction)
    prc_str   = f"{float(precio):,.4f}" if precio is not None else "N/D"
    dist_str  = f"{dir_arrow}{abs(float(dist)):.4f}" if dist is not None else "N/D"

    bp_line_inner = f"BP  {float(bp):.4f}" if bp is not None else "BP  N/D"
    bp_dashes = "─" * max(2, W - len(bp_line_inner) - 4)
    bp_sep = f"  ── {bp_line_inner} {bp_dashes}"

    zone_str = _compute_zone(precio, bp, int_pos, int_neg, max_pos, max_neg, direction)

    L: list[str] = []
    L.append("═" * W)
    L.append(f"BB RETROCESO LIVE CRYPTO — ${ticker}")
    L.append(f"Fuente: Binance  |  {ts}")
    L.append("═" * W)

    if bp is not None:
        L.append(f"  Break Point =  {float(bp):>11,.4f}")
    L.append("─" * W)

    if max_pos is not None:
        L.append(f"  Max Pos Exp =  {float(max_pos):>11,.4f}")
    if int_pos is not None:
        L.append(f"  Int Pos Exp =  {float(int_pos):>11,.4f}")
    L.append(bp_sep)
    if int_neg is not None:
        L.append(f"  Int Neg Exp =  {float(int_neg):>11,.4f}")
    if max_neg is not None:
        L.append(f"  Max Neg Exp =  {float(max_neg):>11,.4f}")

    L.append("─" * W)
    L.append(f"  Precio Live =  {prc_str:>11}   {dir_arrow} {direction}")
    L.append(f"  Dist BP =      {dist_str:>11}")
    L.append(f"  Zona:          {zone_str}")

    if open_day is not None:
        L.append(f"  Open Day =     {float(open_day):>11,.4f}")
    if high_24h is not None:
        L.append(f"  High 24h =     {float(high_24h):>11,.4f}")
    if low_24h is not None:
        L.append(f"  Low 24h  =     {float(low_24h):>11,.4f}")
    if change_24h_pct is not None:
        L.append(f"  Chg  24h =     {_fmt_pct(change_24h_pct):>11}")

    L.append("─" * W)

    if any(v is not None for v in (ema20_5m, ema50_5m, ema200_5m)):
        if ema20_5m  is not None: L.append(f"  EMA20  5M =    {float(ema20_5m):>11,.4f}")
        if ema50_5m  is not None: L.append(f"  EMA50  5M =    {float(ema50_5m):>11,.4f}")
        if ema200_5m is not None: L.append(f"  EMA200 5M =    {float(ema200_5m):>11,.4f}")
    if any(v is not None for v in (ema3_1h, ema9_1h)):
        if ema3_1h is not None: L.append(f"  EMA3   1H =    {float(ema3_1h):>11,.4f}")
        if ema9_1h is not None: L.append(f"  EMA9   1H =    {float(ema9_1h):>11,.4f}")

    L.append("─" * W)

    if bbt_5m is not None: L.append(f"  BBT 5M =       {float(bbt_5m):>11,.4f}")
    if bbb_5m is not None: L.append(f"  BBB 5M =       {float(bbb_5m):>11,.4f}")
    if bbt_1h is not None: L.append(f"  BBT 1H =       {float(bbt_1h):>11,.4f}")
    if bbb_1h is not None: L.append(f"  BBB 1H =       {float(bbb_1h):>11,.4f}")

    L.append("═" * W)
    print("\n".join(L))


def print_live_crypto_ticker(
    ticker: str,
    td: dict,
    precio: float | None,
    now_et: datetime | None = None,
) -> None:
    _reconfigure_stdout()
    if now_et is None:
        now_et = datetime.now(TIMEZONE)

    ts = now_et.strftime("%H:%M ET  %Y-%m-%d")

    bp             = td.get("bp")
    int_pos        = td.get("int_pos")
    int_neg        = td.get("int_neg")
    max_pos        = td.get("max_pos")
    max_neg        = td.get("max_neg")
    direction      = td.get("direction", "N/D")
    dist           = td.get("dist")
    open_day       = td.get("open_day")
    high_24h       = td.get("high_24h")
    low_24h        = td.get("low_24h")
    change_24h_pct = td.get("change_24h_pct")
    ema3_1h        = td.get("ema3_1h")
    ema9_1h        = td.get("ema9_1h")
    ema20_5m       = td.get("ema20_5m")
    ema50_5m       = td.get("ema50_5m")
    ema200_5m      = td.get("ema200_5m")
    bbt_5m         = td.get("bbt_5m")
    bbb_5m         = td.get("bbb_5m")
    bbt_1h         = td.get("bbt_1h")
    bbb_1h         = td.get("bbb_1h")

    dir_arrow = _dir_arrow(direction)
    prc_str   = f"{float(precio):,.4f}" if precio is not None else "N/D"
    dist_str  = f"{dir_arrow}{abs(float(dist)):.4f}" if dist is not None else "N/D"

    bp_line_inner = f"BP  {float(bp):.4f}" if bp is not None else "BP  N/D"
    bp_dashes = "─" * max(2, W - len(bp_line_inner) - 4)
    bp_sep = f"  ── {bp_line_inner} {bp_dashes}"

    zone_str = _compute_zone(precio, bp, int_pos, int_neg, max_pos, max_neg, direction)

    L: list[str] = []
    L.append("═" * W)
    L.append(f"BB RETROCESO LIVE CRYPTO — ${ticker}")
    L.append(f"Fuente: Binance  |  {ts}")
    L.append("═" * W)

    if bp is not None:
        L.append(f"  Break Point =  {float(bp):>11,.4f}")
    L.append("─" * W)

    if max_pos is not None:
        L.append(f"  Max Pos Exp =  {float(max_pos):>11,.4f}")
    if int_pos is not None:
        L.append(f"  Int Pos Exp =  {float(int_pos):>11,.4f}")
    L.append(bp_sep)
    if int_neg is not None:
        L.append(f"  Int Neg Exp =  {float(int_neg):>11,.4f}")
    if max_neg is not None:
        L.append(f"  Max Neg Exp =  {float(max_neg):>11,.4f}")

    L.append("─" * W)
    L.append(f"  Precio Live =  {prc_str:>11}   {dir_arrow} {direction}")
    L.append(f"  Dist BP =      {dist_str:>11}")
    L.append(f"  Zona:          {zone_str}")

    if open_day is not None:
        L.append(f"  Open Day =     {float(open_day):>11,.4f}")
    if high_24h is not None:
        L.append(f"  High 24h =     {float(high_24h):>11,.4f}")
    if low_24h is not None:
        L.append(f"  Low 24h  =     {float(low_24h):>11,.4f}")
    if change_24h_pct is not None:
        L.append(f"  Chg  24h =     {_fmt_pct(change_24h_pct):>11}")

    L.append("─" * W)

    if any(v is not None for v in (ema20_5m, ema50_5m, ema200_5m)):
        if ema20_5m  is not None: L.append(f"  EMA20  5M =    {float(ema20_5m):>11,.4f}")
        if ema50_5m  is not None: L.append(f"  EMA50  5M =    {float(ema50_5m):>11,.4f}")
        if ema200_5m is not None: L.append(f"  EMA200 5M =    {float(ema200_5m):>11,.4f}")
    if any(v is not None for v in (ema3_1h, ema9_1h)):
        if ema3_1h is not None: L.append(f"  EMA3   1H =    {float(ema3_1h):>11,.4f}")
        if ema9_1h is not None: L.append(f"  EMA9   1H =    {float(ema9_1h):>11,.4f}")

    L.append("─" * W)

    if bbt_5m is not None: L.append(f"  BBT 5M =       {float(bbt_5m):>11,.4f}")
    if bbb_5m is not None: L.append(f"  BBB 5M =       {float(bbb_5m):>11,.4f}")
    if bbt_1h is not None: L.append(f"  BBT 1H =       {float(bbt_1h):>11,.4f}")
    if bbb_1h is not None: L.append(f"  BBB 1H =       {float(bbb_1h):>11,.4f}")

    L.append("═" * W)
    print("\n".join(L))
