"""Compuerta de régimen macro (CLI); ranking INDECISO."""
from __future__ import annotations

from dataclasses import dataclass

from dc2.crypto_screener.compute import CryptoRow, strength_score_long, strength_score_short


@dataclass
class RegimeResult:
    longs: list[CryptoRow]
    shorts: list[CryptoRow]
    size_hint: str
    note: str


def normalize_regime(raw: str) -> str:
    s = (raw or "").strip().upper().replace("_", "-")
    s = (
        s.replace("Ó", "O")
        .replace("Á", "A")
        .replace("É", "E")
        .replace("Í", "I")
        .replace("Ú", "U")
    )
    aliases = {
        "RISKON": "RISK-ON",
        "RISK OFF": "RISK-OFF",
        "RISKOFF": "RISK-OFF",
        "PANIC MODE": "PANIC",
        "INDECIS": "INDECISO",
    }
    if s in aliases:
        return aliases[s]
    if s in ("RISK-ON", "RISK-OFF", "PANIC", "INFLACION", "INDECISO"):
        return s
    return s


def apply_regime(
    longs: list[CryptoRow],
    shorts: list[CryptoRow],
    regime_raw: str,
    *,
    indeciso_top_n: int = 3,
) -> RegimeResult:
    """
    Filtra listas según régimen. ``INDECISO`` deja los ``indeciso_top_n`` mejores
    por ``strength_score`` (LONG y SHORT mezclados, luego se reparten).
    """
    r = normalize_regime(regime_raw)
    if r not in ("RISK-ON", "RISK-OFF", "PANIC", "INFLACION", "INDECISO"):
        return RegimeResult(
            longs,
            shorts,
            size_hint="normal",
            note=f"Régimen desconocido '{regime_raw}' — sin filtrado de lista",
        )

    if r == "PANIC":
        return RegimeResult(
            [],
            [],
            size_hint="skip",
            note="PANIC — sin operativa LONG/SHORT",
        )

    if r == "RISK-ON":
        ranked = sorted(longs, key=strength_score_long, reverse=True)
        return RegimeResult(
            ranked,
            [],
            size_hint="full_long",
            note="RISK-ON — solo LONG",
        )

    if r == "RISK-OFF":
        ranked = sorted(shorts, key=strength_score_short, reverse=True)
        return RegimeResult(
            [],
            ranked,
            size_hint="full_short",
            note="RISK-OFF — solo SHORT",
        )

    if r == "INFLACION":
        ranked = sorted(longs, key=strength_score_long, reverse=True)
        return RegimeResult(
            ranked,
            [],
            size_hint="long_bias_reduce",
            note="INFLACIÓN — lista LONG (cripto/commodities sesgo)",
        )

    # INDECISO
    scored: list[tuple[str, CryptoRow, float]] = []
    for row in longs:
        scored.append(("LONG", row, strength_score_long(row)))
    for row in shorts:
        scored.append(("SHORT", row, strength_score_short(row)))
    scored.sort(key=lambda x: x[2], reverse=True)
    top = scored[: max(1, indeciso_top_n)]
    out_long = [row for side, row, _ in top if side == "LONG"]
    out_short = [row for side, row, _ in top if side == "SHORT"]
    return RegimeResult(
        out_long,
        out_short,
        size_hint="reduce_top_ranked",
        note=f"INDECISO — top {len(top)} por strength_score (mezcla L/S)",
    )
