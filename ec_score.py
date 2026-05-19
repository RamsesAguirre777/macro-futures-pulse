"""
EC Score: rankea tickers por probabilidad de ir directo a INT sin tocar BP.
Momentos acumulativos: 9:15 → 9:28 → 9:30. Pre-9:30 filtra t2/t3/ext; a las 9:30 se confirma.
"""
from __future__ import annotations

import sys
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

TICKER_MODIFIERS: dict[str, float] = {
    "TSLA": 1.10,
    "QQQ": 1.08,
    "COIN": 1.07,
    "MSFT": 1.07,
    "META": 1.06,
    "SPY": 1.06,
    "AMD": 1.00,
    "DIA": 1.00,
    "AMZN": 0.99,
    "GLD": 0.97,
    "AVGO": 0.96,
    "AAPL": 0.95,
    "TLT": 0.93,
    "IWM": 0.92,
    "GOOGL": 0.91,
    "NVDA": 0.88,
}

_OPEN_ZONE_BASE: dict[str, int] = {
    "normal_t1": 0,
    "normal_t2": 25,
    "normal_t3": 38,
    "ext_int_max": 50,
    "ZONA_MUERTA": 0,
}

_SQUEEZE_PTS: dict[int, int] = {0: 0, 1: 3, 2: 5, 3: 8, 4: 14, 5: 20}

_SKIP_ZONE_NIVEL = "⚫ SKIP_ZONE"


def _infer_momento(ctx: dict[str, Any]) -> str:
    if "squeeze_alineado" in ctx:
        return "9:30"
    if "open_zone" in ctx:
        return "9:28"
    return "9:15"


def _open_zone_base_pts(ctx: dict[str, Any]) -> int:
    if ctx.get("direccion") == "ZONA_MUERTA":
        return _OPEN_ZONE_BASE["ZONA_MUERTA"]
    oz = ctx.get("open_zone")
    if not oz:
        return 0
    if oz in _OPEN_ZONE_BASE:
        return _OPEN_ZONE_BASE[oz]
    if isinstance(oz, str) and oz.startswith("ext_int_max"):
        return _OPEN_ZONE_BASE["ext_int_max"]
    return 0


def _badge_pts(badge_pct: float | None) -> int:
    if badge_pct is None:
        return 0
    if badge_pct >= 80:
        return 15
    if badge_pct >= 60:
        return 10
    if badge_pct >= 40:
        return 5
    return 0


def _signals_pts(signals_up: int | None) -> int:
    if signals_up is None:
        return 0
    if signals_up >= 3:
        return 15
    if signals_up == 2:
        return 10
    if signals_up == 1:
        return 5
    return 0


def _gap_pts(gap_type: str | None, direccion: str | None) -> int:
    if not gap_type:
        return 0
    g = str(gap_type).upper()
    if g == "FLAT":
        return 4
    if direccion == "ALCISTA" and g == "GAP_UP":
        return 8
    if direccion == "BAJISTA" and g == "GAP_DOWN":
        return 8
    if g in ("GAP_UP", "GAP_DOWN"):
        return -8
    return 0


def _pm_prev_penalty(ctx: dict[str, Any]) -> int:
    d = ctx.get("direccion")
    if d not in ("ALCISTA", "BAJISTA"):
        return 0
    pen = 0
    pm = ctx.get("pm_zona")
    prev = ctx.get("prev_zona")
    if d == "BAJISTA" and pm == "cerca_max":
        pen -= 2
    if d == "ALCISTA" and pm == "cerca_min":
        pen -= 2
    if d == "BAJISTA" and prev == "cerca_max":
        pen -= 2
    if d == "ALCISTA" and prev == "cerca_min":
        pen -= 2
    return max(pen, -4)


def _squeeze_pts(squeeze: int | None) -> int:
    if squeeze is None:
        return 0
    s = max(0, min(5, int(squeeze)))
    return _SQUEEZE_PTS.get(s, 0)


def _map_direccion_salida(direccion: str | None) -> str:
    if direccion == "ALCISTA":
        return "LONG"
    if direccion == "BAJISTA":
        return "SHORT"
    return "ZM"


def _nivel(score: int) -> str:
    if score < 40:
        return "🔴 SKIP"
    if score < 65:
        return "🟡 OPERAR"
    return "🟢 PREMIUM"


def _nivel_compact_emoji(nivel: str) -> str:
    if "🟢" in nivel:
        return "🟢"
    if "🟡" in nivel:
        return "🟡"
    if "🔴" in nivel:
        return "🔴"
    if "⚫" in nivel:
        return "⚫"
    return "?"


def _open_zone_short(ctx: dict[str, Any]) -> str:
    oz = ctx.get("open_zone")
    if not oz:
        return "?"
    if oz == "normal_t2":
        return "t2"
    if oz == "normal_t3":
        return "t3"
    if oz == "ext_int_max" or (isinstance(oz, str) and oz.startswith("ext_int_max")):
        return "ext"
    if oz == "normal_t1":
        return "t1"
    return str(oz)


def _fmt_px(v: Any) -> str:
    if v is None:
        return "N/A"
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return "N/A"


def _ctx_targets_line(ctx: dict[str, Any], direccion_salida: str) -> str:
    bp = _fmt_px(ctx.get("bp"))
    if direccion_salida == "LONG":
        int_v = _fmt_px(ctx.get("int_pos"))
        max_v = _fmt_px(ctx.get("max_pos"))
    elif direccion_salida == "SHORT":
        int_v = _fmt_px(ctx.get("int_neg"))
        max_v = _fmt_px(ctx.get("max_neg"))
    else:
        int_v = max_v = "N/A"
    return f"BP={bp}  INT={int_v}  MAX={max_v}"


def calcular_ec(ctx: dict[str, Any], ticker: str) -> dict[str, Any]:
    ticker_u = str(ticker).upper()
    momento = _infer_momento(ctx)
    direccion = ctx.get("direccion")
    oz = ctx.get("open_zone")

    if (
        oz == "normal_t1"
        or oz == "ZONA_MUERTA"
        or direccion == "ZONA_MUERTA"
    ):
        mod = TICKER_MODIFIERS.get(ticker_u, 1.0)
        ozb = _open_zone_base_pts(ctx)
        return {
            "ticker": ticker_u,
            "score": 0,
            "nivel": _SKIP_ZONE_NIVEL,
            "direccion": _map_direccion_salida(direccion),
            "momento": momento,
            "breakdown": {
                "open_zone_base": ozb,
                "ticker_modifier": mod,
                "open_zone_pts": 0,
                "squeeze_pts": 0,
                "badge_pts": 0,
                "signals_pts": 0,
                "gap_pts": 0,
                "pm_prev_penalty": 0,
                "score_raw": 0.0,
            },
        }

    mod = TICKER_MODIFIERS.get(ticker_u, 1.0)

    open_zone_base = _open_zone_base_pts(ctx)
    open_zone_pts_full = round(open_zone_base * mod) if open_zone_base else 0

    squeeze_pts_full = _squeeze_pts(ctx.get("squeeze_alineado"))
    badge_pts = _badge_pts(ctx.get("badge_pct"))
    signals_pts = _signals_pts(ctx.get("signals_up"))
    gap_pts = _gap_pts(ctx.get("gap_type"), direccion)
    pm_prev = _pm_prev_penalty(ctx)

    open_zone_pts = open_zone_pts_full
    squeeze_pts = squeeze_pts_full
    if momento == "9:15":
        open_zone_pts = 0
        squeeze_pts = 0
    elif momento == "9:28":
        squeeze_pts = 0

    score_raw = (
        open_zone_pts
        + squeeze_pts
        + badge_pts
        + signals_pts
        + gap_pts
        + pm_prev
    )
    score = int(min(100, max(0, round(score_raw))))

    breakdown: dict[str, Any] = {
        "open_zone_base": open_zone_base,
        "ticker_modifier": mod,
        "open_zone_pts": open_zone_pts,
        "squeeze_pts": squeeze_pts,
        "badge_pts": badge_pts,
        "signals_pts": signals_pts,
        "gap_pts": gap_pts,
        "pm_prev_penalty": pm_prev,
        "score_raw": score_raw,
    }

    return {
        "ticker": ticker_u,
        "score": score,
        "nivel": _nivel(score),
        "direccion": _map_direccion_salida(direccion),
        "momento": momento,
        "breakdown": breakdown,
    }


def display_ec(resultado: dict[str, Any]) -> str:
    t = resultado.get("ticker", "?")
    d = resultado.get("direccion", "ZM")
    nivel = resultado.get("nivel", "")
    score = resultado.get("score", 0)
    b = resultado.get("breakdown") or {}
    line = (
        f"  zona={b.get('open_zone_pts', 0)}  squeeze={b.get('squeeze_pts', 0)}  "
        f"badge={b.get('badge_pts', 0)}  signals={b.get('signals_pts', 0)}  "
        f"gap={b.get('gap_pts', 0)}  pm={b.get('pm_prev_penalty', 0)}"
    )
    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{t}  |  {d}  {nivel}  {score}\n"
        f"{line}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )


def rankear_tickers(lista: list[dict]) -> list[dict]:
    """
    lista: cada elemento es {"ticker": str, "ctx": dict}
    Retorna lista ordenada por score desc, solo los que pasaron el hard filter.
    """
    out: list[dict] = []
    for item in lista:
        ticker = item["ticker"]
        ctx = item["ctx"]
        r = calcular_ec(ctx, ticker)
        if r["nivel"] == _SKIP_ZONE_NIVEL:
            continue
        row = {**r, "ctx": ctx}
        out.append(row)
    out.sort(key=lambda x: x["score"], reverse=True)
    return out


def display_ranking(ranked: list[dict]) -> str:
    lines = [
        "══════════════════════════════════",
        "9:28 CANDIDATES (t2/t3/ext)",
        "══════════════════════════════════",
    ]
    if not ranked:
        lines.append("→ SIN CANDIDATOS: todos en t1/ZM")
        lines.append("══════════════════════════════════")
        return "\n".join(lines)

    for i, row in enumerate(ranked, start=1):
        t = row["ticker"]
        d = row["direccion"]
        score = row["score"]
        em = _nivel_compact_emoji(row["nivel"])
        ctx = row.get("ctx") or {}
        zshort = _open_zone_short(ctx)
        targets = _ctx_targets_line(ctx, d)
        lines.append(
            f"{i}. {t:<5} {d:<5} {score:>3} {em}  {zshort} | {targets}"
        )
        b = row.get("breakdown") or {}
        lines.append(
            f"        zona={b.get('open_zone_pts', 0)}  badge={b.get('badge_pts', 0)}  "
            f"signals={b.get('signals_pts', 0)}  gap={b.get('gap_pts', 0)}  "
            f"squeeze={b.get('squeeze_pts', 0)}  pm={b.get('pm_prev_penalty', 0)}"
        )

    lines.append("──────────────────────────────────")
    top = ranked[0]["ticker"]
    lines.append(f"→ WATCH: {top} — primera vela 5min a las 9:30")
    lines.append("══════════════════════════════════")
    return "\n".join(lines)


if __name__ == "__main__":
    ctx_tsla = {
        "badge_pct": 88.0,
        "signals_up": 3,
        "gap_type": "GAP_UP",
        "prev_day_pct": 0.35,
        "pm_zona": "medio",
        "prev_zona": "medio",
        "open_zone": "normal_t3",
        "dist": 0.95,
        "direccion": "ALCISTA",
        "bp": 248.40,
        "int_pos": 251.20,
        "int_neg": 244.10,
        "max_pos": 254.80,
        "max_neg": 241.50,
    }
    ctx_qqq = {
        "badge_pct": 72.0,
        "signals_up": 3,
        "gap_type": "GAP_DOWN",
        "prev_day_pct": -0.18,
        "pm_zona": "cerca_min",
        "prev_zona": "medio",
        "open_zone": "normal_t2",
        "dist": 0.42,
        "direccion": "BAJISTA",
        "bp": 505.25,
        "int_pos": 508.90,
        "int_neg": 501.40,
        "max_pos": 511.20,
        "max_neg": 498.10,
    }
    ctx_nvda = {
        "badge_pct": 83.3,
        "signals_up": 3,
        "gap_type": "GAP_UP",
        "pm_zona": "cerca_max",
        "prev_zona": "cerca_max",
        "open_zone": "normal_t1",
        "dist": 0.55,
        "direccion": "ALCISTA",
        "bp": 128.50,
        "int_pos": 130.20,
        "int_neg": 126.80,
        "max_pos": 132.10,
        "max_neg": 125.40,
    }

    lista = [
        {"ticker": "TSLA", "ctx": ctx_tsla},
        {"ticker": "QQQ", "ctx": ctx_qqq},
        {"ticker": "NVDA", "ctx": ctx_nvda},
    ]
    ranked = rankear_tickers(lista)
    print(display_ranking(ranked))
    print()
    r_nvda = calcular_ec(ctx_nvda, "NVDA")
    print("NVDA (t1) filtrado — calcular_ec directo:")
    print(r_nvda)
