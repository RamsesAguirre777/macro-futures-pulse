from __future__ import annotations
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from ec_score import display_ranking, rankear_tickers
except ImportError:
    display_ranking = None
    rankear_tickers = None

from dc2.constants import TICKERS, TICKERS_MACRO, TIMEZONE, FAMILIAS
from dc2.premium_detector import (
    _caution_note_short,
    _dir_label,
    _score_detail,
    _score_label,
)
from dc2.indicators import _gestion_open_930_line

logger = logging.getLogger(__name__)


def _ensure_outputs_dir() -> None:
    Path("outputs").mkdir(parents=True, exist_ok=True)

def _signals_display(ticker_data: dict) -> str:
    return ticker_data.get("signals_3_9") or ticker_data.get("signals", "")

def _format_context_compressed_body(data: dict, mode: str) -> str:
    """Texto legible compartido por context_compressed y ai_dashboard."""
    today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    time_label = {
        "premarket_9_15": "9:15 AM",
        "premarket_9_28": "9:28 AM",
        "open_930": "9:30 AM",
        "test": "test (cutoff dinámico)",
    }.get(mode, "9:28 AM")
    tickers_line = data.get("tickers_order") or TICKERS
    parts: list[str] = []
    parts.append(f"═══ BB RETROCESO — {today} {time_label} ═══\n")
    parts.append("MACRO: ")
    macro = data.get("macro") or {}
    parts.append(
        " | ".join(
            f"{ticker}={macro.get(ticker, {}).get('price', 0)} "
            f"({float(macro.get(ticker, {}).get('change', 0)):.2f}%)"
            for ticker in TICKERS_MACRO
        )
        + "\n"
    )
    parts.append("Por ticker (excluir SKIPs):\n")
    for ticker in tickers_line:
        if ticker in data["tickers"] and not data["tickers"][ticker].get("skip"):
            td = data["tickers"][ticker]
            sig = _signals_display(td)
            line = (
                f"{ticker}: BP={td['bp']:.2f} | "
                f"INT±{td['int_dist']:.2f} | "
                f"MAX±{td['max_dist']:.2f} | "
                f"GAP={td['gap_type']} {td['gap_pct']:.2f}% | "
                f"prev_day={td['prev_day_change']:.2f}% | "
                f"signals={sig} | "
                f"badge={td['badge_long']}% | "
                f"caution={_caution_note_short(td['caution_note'])} | "
                f"ema_sr={td.get('ema_sr', '')}"
                f" | pm={td.get('rango_pm', {}).get('label', '?')}({int(td.get('rango_pm', {}).get('pct', 0.5)*100)}%)"
                f" | prev={td.get('rango_prev_day', {}).get('label', '?')}({int(td.get('rango_prev_day', {}).get('pct', 0.5)*100)}%)"
            )
            if "dist" in td and "direction" in td:
                line += f" | dir={td['direction']} dist={td['dist']:.2f}"
            parts.append(line + "\n")
    parts.append("SKIP ALERTS:\n")
    for ticker in tickers_line:
        if ticker in data["tickers"] and data["tickers"][ticker].get("skip"):
            parts.append(
                f"{ticker}: {data['tickers'][ticker]['skip_reason']}\n"
            )
    if mode == "open_930":
        parts.append("\nOPEN 9:30:\n")
        for ticker in tickers_line:
            if ticker in data["tickers"] and "open_930" in data["tickers"][ticker]:
                td = data["tickers"][ticker]
                direction = td["direction"]
                dist = td["dist"]
                int_pos = td.get("int_pos")
                int_neg = td.get("int_neg")
                max_pos = td.get("max_pos")
                max_neg = td.get("max_neg")
                open_p = td.get("open_930")
                open_z = td.get("open_zone")
                opct = td.get("open_pct_en_rango")
                pct_s = (
                    f"{int(round(100 * float(opct)))}%"
                    if opct is not None
                    else "—"
                )
                line_head = f"{ticker}: {direction}"
                if open_z:
                    line_head += f" | ZONA: {open_z} ({pct_s})"
                line_head += f" | dist={dist:.2f}"
                c1v3 = td.get("caution_cambio_1v3")
                if c1v3:
                    emoji_1v3 = {
                        "igual": "🟰",
                        "nuevo": "🆕",
                        "escalo": "📈" if td.get("direction") == "ALCISTA" else "📉",
                        "desaparecio": "💨",
                        "cambio_tipo": "🔄",
                    }.get(c1v3, "❓")
                    line_head += f" | 1v3={emoji_1v3}{c1v3}"
                parts.append(line_head + "\n")
                if direction == "BAJISTA" and int_neg is not None and open_p is not None:
                    rec_i = abs(float(open_p) - float(int_neg))
                    tail = f"   → INT_NEG={float(int_neg):.2f} (recorrido: ${rec_i:.2f})"
                    if max_neg is not None:
                        tail += f" | MAX_NEG={float(max_neg):.2f}"
                    if open_z == "ext_int_max":
                        tail += " ← PRECIO YA BAJO INT — INT es nivel de cierre si cruza de vuelta"
                    parts.append(tail + "\n")
                elif direction == "ALCISTA" and int_pos is not None and open_p is not None:
                    rec_i = abs(float(int_pos) - float(open_p))
                    tail = f"   → INT_POS={float(int_pos):.2f} (recorrido: ${rec_i:.2f})"
                    if max_pos is not None:
                        tail += f" | MAX_POS={float(max_pos):.2f}"
                    if open_z == "ext_int_max":
                        tail += " ← PRECIO YA SUPERÓ INT — INT es nivel de cierre si cruza de vuelta"
                    parts.append(tail + "\n")
                elif int_pos is not None and int_neg is not None:
                    mp = f"{float(max_pos):.2f}" if max_pos is not None else "—"
                    mn = f"{float(max_neg):.2f}" if max_neg is not None else "—"
                    parts.append(
                        f"   → INT_POS={float(int_pos):.2f} INT_NEG={float(int_neg):.2f} "
                        f"| MAX_POS={mp} MAX_NEG={mn}\n"
                    )
                gest = _gestion_open_930_line(ticker, td, direction)
                if gest:
                    parts.append(f"   ⚠ GESTIÓN: {gest}\n")

        # EC RANKING — candidatos t2/t3/ext para primera vela 9:30
        candidatos = []
        for ticker in tickers_line:
            td = data["tickers"].get(ticker, {})
            if td.get("skip") or not td.get("open_zone"):
                continue
            direction = td.get("direction", "")
            if direction not in ("ALCISTA", "BAJISTA"):
                continue
            sig_raw = td.get("signals_3_9", "") or ""
            m = re.search(r"(\d+)/9 up", sig_raw)
            signals_up = int(m.group(1)) if m else 0

            ctx = {
                "open_zone": td.get("open_zone"),
                "direccion": direction,
                "badge_pct": float(td.get("badge_long") or 0),
                "signals_up": signals_up,
                "gap_type": td.get("gap_type"),
                "squeeze_alineado": td.get("squeeze_alineado"),
                "pm_zona": td.get("rango_pm", {}).get("label"),
                "prev_zona": td.get("rango_prev_day", {}).get("label"),
                "bp": td.get("bp"),
                "int_pos": td.get("int_pos"),
                "int_neg": td.get("int_neg"),
                "max_pos": td.get("max_pos"),
                "max_neg": td.get("max_neg"),
            }
            candidatos.append({"ticker": ticker, "ctx": ctx})

        if candidatos:
            ranked = rankear_tickers(candidatos)
            if ranked:
                parts.append("\n" + display_ranking(ranked) + "\n")
    return "".join(parts)

def write_json(data: dict, filename: str) -> None:
    _ensure_outputs_dir()
    path = Path("outputs") / filename
    # Merge into existing file to avoid single-ticker runs wiping full results
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = json.load(f)
            existing.setdefault("tickers", {}).update(data.get("tickers", {}))
            existing.setdefault("skips", {}).update(data.get("skips", {}))
            # Update non-merge keys only if present in new data
            for k in ("macro", "tickers_order", "cruces"):
                if k in data:
                    existing[k] = data[k]
            data = existing
        except Exception:
            pass  # corrupted file — overwrite
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)

def write_context_compressed(data: dict, date_str: str, modo: str) -> None:
    # Antes: context_compressed_preliminar_{fecha}.txt — usar context_compressed_{fecha}_premarket_9_15.txt
    _ensure_outputs_dir()
    text = _format_context_compressed_body(data, modo)
    path = Path("outputs") / f"context_compressed_{date_str}_{modo}.txt"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

def write_ai_dashboard(data: dict, date_str: str, modo: str) -> None:
    """
    Genera el archivo ai_dashboard_prompt en el nuevo formato legible.
    Versión 2 — Score EC integrado con 1v3 + Milton.
    """
    _ensure_outputs_dir()
    time_label = {
        "premarket_9_15": "9:15 AM",
        "premarket_9_28": "9:28 AM",
        "open_930":       "9:30 AM",
        "test":           "TEST",
    }.get(modo, "9:28 AM")

    tickers_line = data.get("tickers_order") or TICKERS
    tickers_data = data.get("tickers", {})
    macro = data.get("macro", {})
    parts: list[str] = []

    # ── ENCABEZADO ────────────────────────────────────────────────
    parts.append(f"{'━'*50}\n")
    parts.append(f"BB RETROCESO — {date_str} {time_label}\n")
    parts.append(f"{'━'*50}\n\n")

    # ── MACRO ─────────────────────────────────────────────────────
    parts.append("MACRO:\n")
    for t in TICKERS_MACRO:
        if t in macro:
            chg = macro[t].get('change', 0) or 0
            price = macro[t].get('price', 0) or 0
            signo = "+" if chg >= 0 else ""
            parts.append(f"  {t:<5} {price:.2f}  ({signo}{chg:.2f}%)\n")
    parts.append("  [Agregar aquí: sentimiento Grok + futuros YM/NQ/ES si disponibles]\n")
    parts.append("\n")

    # ── Separar tickers por categoría ─────────────────────────────
    bloqueados = []
    oportunidades = []   # score ≥5 o DD+1v3
    sin_oportunidad = []
    zona_muerta_sin_1v3 = []

    for ticker in tickers_line:
        td = tickers_data.get(ticker)
        if td is None:
            # Está en skips
            razon = data.get("skips", {}).get(ticker, "sin datos")
            if "ZONA_MUERTA" in razon:
                zona_muerta_sin_1v3.append((ticker, razon))
            else:
                sin_oportunidad.append((ticker, razon))
            continue

        ec_score  = td.get("ec_score", 0) or 0
        ec_accion = td.get("ec_accion", "SKIP") or "SKIP"

        if ec_accion == "BLOQUEADO":
            bloqueados.append(ticker)
        elif ec_accion in ("SKIP",) and not (
            td.get("caution_cambio_1v3") in ("igual", "nuevo") and
            td.get("direction") == "ZONA_MUERTA"
        ):
            razon_skip = td.get("skip_reason") or "score insuficiente"
            ev = td.get("evaluation", {})
            if ev.get("tier") == "SKIP":
                razon_skip = ev.get("reason", razon_skip)
            sin_oportunidad.append((ticker, razon_skip))
        else:
            oportunidades.append(ticker)

    # ── OPORTUNIDADES ─────────────────────────────────────────────
    if oportunidades:
        parts.append("OPORTUNIDADES DEL DÍA:\n")
        parts.append(f"{'─'*50}\n")

        # Ordenar por score descendente
        def sort_key(t):
            td = tickers_data.get(t, {})
            s = td.get("ec_score", 0) or 0
            return -s

        oportunidades.sort(key=sort_key)

        for ticker in oportunidades:
            td = tickers_data.get(ticker, {})
            ec_score   = td.get("ec_score", 0) or 0
            ec_accion  = td.get("ec_accion", "SKIP") or "SKIP"
            direction  = td.get("direction", "") or ""
            c1v3       = td.get("caution_cambio_1v3")
            open_930   = td.get("open_930")
            bp         = td.get("bp", 0) or 0
            int_pos    = td.get("int_pos", 0) or 0
            int_neg    = td.get("int_neg", 0) or 0
            max_pos    = td.get("max_pos", 0) or 0
            max_neg    = td.get("max_neg", 0) or 0
            n_caut     = (td.get("caution_note", "") or "").count("BBT") + \
                         (td.get("caution_note", "") or "").count("BBB")
            ema_align  = td.get("ema_align", "") or ""
            gap_type   = td.get("gap_type", "") or ""
            gap_pct    = td.get("gap_pct", 0) or 0
            prev_day   = td.get("prev_day_change", 0) or 0
            signals    = td.get("signals_3_9", "") or ""

            label = _score_label(ec_score, ec_accion)

            parts.append(f"\n━━━ {ticker} {'━'*(20-len(ticker))}\n")
            parts.append(f"SCORE: {ec_score:.1f}  {label}\n")

            if modo == "open_930":
                oz = td.get("open_zone")
                opct = td.get("open_pct_en_rango")
                ru = td.get("rebote_umbral")
                if oz is not None:
                    pct_d = int(round(100 * float(opct))) if opct is not None else 0
                    ru_s = f"${float(ru):.2f}" if ru is not None else "—"
                    parts.append(
                        f"  {direction} | ZONA: {oz} ({pct_d}%) | umbral_rebote={ru_s}\n"
                    )
                gv = _gestion_open_930_line(ticker, td, direction)
                if gv:
                    parts.append(f"  REGLAS VIVO: {gv}\n")

            # Dirección y orden
            if direction == "ZONA_MUERTA":
                # DD — mostrar ambos lados
                open_ref = open_930 if open_930 else bp
                parts.append("ZONA NEUTRA al abrir — espera el primer movimiento\n")
                parts.append(f"  Si BAJA del BP (${bp:.2f}) → SHORT\n")
                parts.append(f"    Objetivo 1: ${int_neg:.2f}  |  Objetivo 2 (MAX): ${max_neg:.2f}\n")
                parts.append(f"    Recorrido:  ${abs(int_neg - open_ref):.2f} al obj1 / "
                             f"${abs(max_neg - open_ref):.2f} al obj2\n")
                parts.append(f"  Si SUBE del BP (${bp:.2f}) → LONG\n")
                parts.append(f"    Objetivo 1: ${int_pos:.2f}  |  Objetivo 2 (MAX): ${max_pos:.2f}\n")
                parts.append(f"    Recorrido:  ${abs(int_pos - open_ref):.2f} al obj1 / "
                             f"${abs(max_pos - open_ref):.2f} al obj2\n")
            elif direction == "ALCISTA":
                open_ref = open_930 if open_930 else bp
                parts.append("LONG (comprar)\n")
                parts.append(f"  Objetivo 1: ${int_pos:.2f}  |  Objetivo 2 (MAX): ${max_pos:.2f}\n")
                parts.append(f"  Recorrido:  ${abs(int_pos - open_ref):.2f} al obj1 / "
                             f"${abs(max_pos - open_ref):.2f} al obj2\n")
            elif direction == "BAJISTA":
                open_ref = open_930 if open_930 else bp
                parts.append("SHORT (vender)\n")
                parts.append(f"  Objetivo 1: ${int_neg:.2f}  |  Objetivo 2 (MAX): ${max_neg:.2f}\n")
                parts.append(f"  Recorrido:  ${abs(int_neg - open_ref):.2f} al obj1 / "
                             f"${abs(max_neg - open_ref):.2f} al obj2\n")

            # Señales
            sig_parts = []
            ups = signals.count("up")
            downs = signals.count("down")
            if ups == 3:
                sig_parts.append("señales 3/9 todas alcistas")
            elif downs == 3:
                sig_parts.append("señales 3/9 todas bajistas")
            elif ups > downs:
                sig_parts.append("señales 3/9 mayoria alcistas")
            elif downs > ups:
                sig_parts.append("señales 3/9 mayoria bajistas")

            if n_caut == 0:
                sig_parts.append("0 cautiones Milton")
            elif n_caut == 4:
                sig_parts.append("4 cautiones Milton")
            elif n_caut > 0:
                sig_parts.append(f"{n_caut} cautiones Milton")

            if c1v3:
                sig_parts.append(f"1v3={c1v3}")

            ev = td.get("evaluation", {})
            warnings = ev.get("warnings", [])

            if sig_parts:
                parts.append(f"  Contexto: {' | '.join(sig_parts)}\n")

            rango_pm      = td.get("rango_pm", {})
            rango_prev    = td.get("rango_prev_day", {})
            pm_label      = rango_pm.get("label", "")
            prev_label    = rango_prev.get("label", "")
            pm_pct        = int(rango_pm.get("pct", 0.5) * 100)
            prev_pct      = int(rango_prev.get("pct", 0.5) * 100)
            if pm_label and pm_label != "sin_datos":
                parts.append(f"  Rango PM: {pm_label} ({pm_pct}%) | Prev day: {prev_label} ({prev_pct}%)\n")

            # Desglose del score
            detail = _score_detail(ema_align, gap_type, gap_pct, prev_day,
                                   n_caut, c1v3, direction, ticker)
            if detail:
                parts.append(f"  Por qué: {detail}\n")

            # Warnings
            for w in warnings:
                parts.append(f"  ⚠️  {w}\n")

            # Aviso de recorrido corto
            if direction == "ALCISTA" and open_930 and int_pos:
                umbral_corto = max(0.60, float(td.get("int_dist", 1.0)) * 0.25)
                if 0 < (int_pos - open_930) < umbral_corto:
                    parts.append(f"  ⚠️  Recorrido corto al objetivo — solo ${int_pos-open_930:.2f}\n")
            elif direction == "BAJISTA" and open_930 and int_neg:
                umbral_corto = max(0.60, float(td.get("int_dist", 1.0)) * 0.25)
                if 0 < (open_930 - int_neg) < umbral_corto:
                    parts.append(f"  ⚠️  Recorrido corto al objetivo — solo ${open_930-int_neg:.2f}\n")
    else:
        parts.append("SIN OPORTUNIDADES CLARAS ESTE MOMENTO\n")

    # ── BLOQUEADOS ────────────────────────────────────────────────
    if bloqueados:
        parts.append(f"\n{'─'*50}\n")
        parts.append("BLOQUEADOS (abrieron más allá del objetivo):\n")
        for ticker in bloqueados:
            td = tickers_data.get(ticker, {})
            ec_score = td.get("ec_score", 0) or 0
            parts.append(f"  {ticker}: score {ec_score:.1f} pero precio ya pasó el objetivo — no operar\n")

    # ── FAMILIAS / CRUCES ─────────────────────────────────────────
    cruces = data.get("cruces", {})
    cruces_relevantes = {k: v for k, v in cruces.items()
                         if v and "SIN_DATOS" not in v and "NEUTRAL" not in v}
    if cruces_relevantes:
        parts.append(f"\n{'─'*50}\n")
        parts.append("CONTEXTO DE MERCADO:\n")
        for familia, estado in cruces_relevantes.items():
            parts.append(f"  {familia}: {estado}\n")

    # ── SIN OPORTUNIDAD ───────────────────────────────────────────
    if sin_oportunidad or zona_muerta_sin_1v3:
        parts.append(f"\n{'─'*50}\n")
        parts.append("SIN OPORTUNIDAD HOY:\n")
        for ticker, razon in sin_oportunidad:
            # Simplificar razón
            razon_simple = razon
            if "prev_day" in razon.lower() or "pdc" in razon.lower():
                razon_simple = "día anterior muy fuerte — score bajo"
            elif "zona_muerta" in razon.lower():
                razon_simple = "zona neutra sin confirmación"
            elif "GAP_DOWN ≥5%" in razon:
                razon_simple = "gap bajista extremo con dirección alcista"
            parts.append(f"  {ticker}: {razon_simple}\n")
        for ticker, razon in zona_muerta_sin_1v3:
            parts.append(f"  {ticker}: zona neutra — confirmar al abrir\n")

    parts.append(f"\n{'━'*50}\n")

    path = Path("outputs") / f"ai_dashboard_prompt_{date_str}_{modo}.txt"
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(parts))
