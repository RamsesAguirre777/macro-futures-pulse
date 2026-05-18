"""Salida legible para CEREBRO / Claude Desktop."""
from __future__ import annotations

import json
from datetime import date
from typing import Any

from dc2.crypto_screener.compute import CryptoRow
from dc2.crypto_screener.regime_gate import RegimeResult


def format_scan_markdown(
    fecha: date,
    regime_in: str,
    rr: RegimeResult,
    *,
    db_path: str,
) -> str:
    lines = [
        "## Crypto screener Binance (BB Retroceso)",
        "",
        f"- **Fecha UTC**: {fecha}",
        f"- **Régimen entrada**: {regime_in}",
        f"- **Régimen normalizado**: aplicado en persistencia",
        f"- **size_hint**: {rr.size_hint}",
        f"- **Nota**: {rr.note}",
        f"- **DuckDB**: `{db_path}`",
        "",
        "### LONG",
    ]
    if not rr.longs:
        lines.append("_(vacío)_")
    else:
        for r in rr.longs:
            lines.append(
                f"- **{r.symbol}** close={r.close_1d:.4f} res20d={r.resistencia_1d:.4f} "
                f"vol24h/avg20={r.volume_usdt_24h:.0f}/{r.avg_volume_usdt_20d:.0f} "
                f"1h>{r.ema20_1h:.4f} 15m EMA9/20={r.ema9_15m:.4f}/{r.ema20_15m:.4f}"
            )
    lines.extend(["", "### SHORT"])
    if not rr.shorts:
        lines.append("_(vacío)_")
    else:
        for r in rr.shorts:
            lines.append(
                f"- **{r.symbol}** close={r.close_1d:.4f} sop20d={r.soporte_1d:.4f} "
                f"vol24h/avg20={r.volume_usdt_24h:.0f}/{r.avg_volume_usdt_20d:.0f} "
                f"1h<{r.ema20_1h:.4f} 15m EMA9/20={r.ema9_15m:.4f}/{r.ema20_15m:.4f}"
            )
    lines.append("")
    return "\n".join(lines)


def format_open_signals_json(records: list[dict[str, Any]]) -> str:
    return json.dumps(records, indent=2, default=str)


def rows_to_table_dicts(longs: list[CryptoRow], shorts: list[CryptoRow]) -> dict[str, Any]:
    return {
        "long": [_row_dict(r) for r in longs],
        "short": [_row_dict(r) for r in shorts],
    }


def _row_dict(r: CryptoRow) -> dict[str, Any]:
    return {
        "symbol": r.symbol,
        "close_1d": r.close_1d,
        "resistencia_1d": r.resistencia_1d,
        "soporte_1d": r.soporte_1d,
        "ema3_1d": r.ema3_1d,
        "ema9_1d": r.ema9_1d,
        "ema3_m": r.ema3_monthly,
        "ema9_m": r.ema9_monthly,
        "ema20_m": r.ema20_monthly,
        "volume_usdt_24h": r.volume_usdt_24h,
        "avg_volume_usdt_20d": r.avg_volume_usdt_20d,
        "close_1h": r.close_1h,
        "ema20_1h": r.ema20_1h,
        "ema9_15m": r.ema9_15m,
        "ema20_15m": r.ema20_15m,
    }
