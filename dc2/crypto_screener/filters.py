"""Filtros LONG / SHORT del crypto screener."""
from __future__ import annotations

import numpy as np

from dc2.crypto_screener.compute import CryptoRow

# Liquidez mínima: promedio 20D debe superar este piso
MIN_AVG_VOLUME_USDT = 10_000_000


def filter_long(r: CryptoRow, *, solo_1d: bool = False) -> bool:
    # Alineación alcista completa en 1D
    if not (
        r.close_1d > r.ema3_1d
        and r.ema3_1d > r.ema9_1d
        and r.ema9_1d > r.ema20_1d
    ):
        return False
    # Cerró por encima de resistencia 20D
    if not (r.close_1d > r.resistencia_1d):
        return False
    # Volumen: piso mínimo + breakout vs promedio 20D
    if np.isnan(r.avg_volume_usdt_20d) or r.avg_volume_usdt_20d < MIN_AVG_VOLUME_USDT:
        return False
    if not (r.volume_usdt_24h > r.avg_volume_usdt_20d):
        return False
    if solo_1d:
        return True
    if not (r.close_1h > r.ema20_1h):
        return False
    if not (r.ema9_15m > r.ema20_15m):
        return False
    return True


def filter_short(r: CryptoRow, *, solo_1d: bool = False) -> bool:
    # Alineación bajista completa en 1D
    if not (
        r.close_1d < r.ema3_1d
        and r.ema3_1d < r.ema9_1d
        and r.ema9_1d < r.ema20_1d
    ):
        return False
    # Cerró por debajo de soporte 20D
    if not (r.close_1d < r.soporte_1d):
        return False
    # Volumen: piso mínimo + breakout vs promedio 20D
    if np.isnan(r.avg_volume_usdt_20d) or r.avg_volume_usdt_20d < MIN_AVG_VOLUME_USDT:
        return False
    if not (r.volume_usdt_24h > r.avg_volume_usdt_20d):
        return False
    if solo_1d:
        return True
    if not (r.close_1h < r.ema20_1h):
        return False
    if not (r.ema9_15m < r.ema20_15m):
        return False
    return True
