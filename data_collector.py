# pip install alpaca-py finnhub-python websockets yfinance pandas numpy python-dotenv pytz
import os
import argparse
import asyncio
import re
import time
from datetime import datetime, timedelta, date
from pathlib import Path
import pytz
import pandas as pd
import numpy as np
import json
import finnhub  # noqa: F401 — disponible para fallback WS; el fallback usa websockets directamente
from dotenv import load_dotenv
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed
from alpaca.data.live import StockDataStream

from backtest_engine import (
    build_tfs_rth,
    caution_tf_key_label_pairs,
    compute_caution_cambio_1v3,
    compute_caution_real_930,
    mid_tf_key,
    rth_mask_vectorized,
)

import logging

# ── Logging setup ──────────────────────────────────────────────────────────
_LOG_DIR = Path(__file__).parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)
_log_filename = _LOG_DIR / f"dc_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
log_level = logging.DEBUG if os.getenv("DC_LOG_DEBUG") == "1" else logging.INFO
logging.basicConfig(
    level=log_level,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(_log_filename, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)
logger.info(f"data_collector iniciado — log: {_log_filename}")

# Configuración inicial
load_dotenv(dotenv_path=Path(__file__).parent / ".env")
# Si no hay claves Alpaca en bb_retroceso/.env, intenta el .env del repo raíz.
if not os.getenv("ALPACA_API_KEY") or not os.getenv("ALPACA_SECRET_KEY"):
    load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

# CONSTANTES
# QBTS excluido — sin playbook verificado
TICKERS = ["NVDA", "QQQ", "SPY", "IWM", "DIA", "GLD", "TLT", "TSLA", "AMD", "AAPL", "META", "MSFT", "GOOGL", "AMZN", "COIN", "PLTR", "AVGO"]
TICKERS_MACRO = ["QQQ", "SPY", "DIA"]

# ─── CLASIFICACIÓN DE TICKERS ───────────────────────────────────────────
TIER1 = ["GLD", "QQQ", "META", "NVDA", "TSLA", "COIN", "PLTR"]
TIER2 = ["AMD", "SPY", "MSFT", "AAPL", "AMZN", "DIA", "TLT", "IWM", "AVGO"]
CONFIRMADORES = ["GOOGL"]
MACRO_SEMAFORO = ["TLT", "GLD", "IWM", "QQQ", "DIA"]

# Familias de cruces (Hallazgo Maestro v21)
FAMILIAS = {
    "SEMIS": ["NVDA", "AMD"],
    "ADS": ["META", "GOOGL"],
    "CLOUD": ["MSFT", "AMZN"],
    "BETA": ["TSLA", "IWM"],
    "INDICES": ["QQQ", "DIA"],
    "SEMIS_NEW": ["AVGO", "PLTR"],   # nuevo — semis/tech de alto edge
    "CRYPTO_EQ": ["COIN"],           # nuevo — proxy crypto equity (referencia BTC)
}
TIMEZONE = pytz.timezone("America/New_York")
TF_LIST = ["1m", "5m", "15m", "30m", "1h"]
# Barras para caution_real_930 / 1v3 en modo open_930 (alineado con backtesting)
OPEN_930_TF_LIST = ["1m", "5m", "15m", "30m"]
UTC = pytz.UTC
N_VELAS_BB = 20

# Umbrales rebote post-INT ($) — calibrado con datos reales (MAX por rango de ticker)
REBOTE_UMBRAL: dict[str, dict[str, float]] = {
    "TLT":  {"ALC": 0.50,  "BAJ": 0.50},
    "GLD":  {"ALC": 1.00,  "BAJ": 1.00},
    "IWM":  {"ALC": 1.00,  "BAJ": 1.50},
    "AAPL": {"ALC": 1.50,  "BAJ": 1.50},
    "DIA":  {"ALC": 1.00,  "BAJ": 1.50},
    "NVDA": {"ALC": 1.50,  "BAJ": 1.50},
    "AMD":  {"ALC": 1.50,  "BAJ": 1.50},
    "QQQ":  {"ALC": 1.50,  "BAJ": 1.50},
    "SPY":  {"ALC": 1.50,  "BAJ": 1.50},
    "MSFT": {"ALC": 1.50,  "BAJ": 1.50},
    "GOOGL":{"ALC": 1.50,  "BAJ": 1.00},
    "AMZN": {"ALC": 1.50,  "BAJ": 1.50},
    "AVGO": {"ALC": 1.00,  "BAJ": 1.50},
    "META": {"ALC": 99.0,  "BAJ": 99.0},
    "COIN": {"ALC": 99.0,  "BAJ": 99.0},
    "TSLA": {"ALC": 5.00,  "BAJ": 5.00},
    "NFLX": {"ALC": 1.50,  "BAJ": 1.50},
    "PLTR": {"ALC": 1.50,  "BAJ": 1.50},
}

# Reglas prev_day → size hint (calibrado con datos reales v3, 14 tickers)
# Patrón universal: 2%-5% ALC = SIZE x2 / <-3% BAJ = SIZE x2
# Patrón universal: <-3% ALC = SKIP / positivo BAJ = SKIP
# Excepciones por ticker documentadas abajo.
PREV_DAY_RULES: dict[str, dict[str, float | str]] = {
    # rango_alc_skip_bajo: prev_day < este valor → SKIP ALC
    # rango_baj_skip_positivo: prev_day > este valor → SKIP BAJ
    # rango_alc_size2_bajo: prev_day > este valor → SIZE x2 ALC
    # rango_baj_size2_alto: prev_day < este valor → SIZE x2 BAJ
    "DEFAULT": {
        "alc_skip_bajo": -3.0,       # <-3% ALC = SKIP (todos los tickers)
        "alc_reducir_bajo": -3.0,    # -3% a 0% ALC = x0.5 (todos)
        "alc_size2_bajo": 2.0,       # 2%-5% ALC = SIZE x2 (todos)
        "baj_skip_positivo": 0.0,    # >0% BAJ = SKIP (todos)
        "baj_size2_alto": -3.0,      # <-3% BAJ = SIZE x2 (todos)
        "baj_reducir_positivo": 0.0, # 0%-2% BAJ = x0.5 (todos)
    },
    # Excepciones — solo se listan las diferencias respecto a DEFAULT
    "META": {
        # ALC <-3% = x0.5 (no SKIP) — META alcista resiste prev_day negativo
        "alc_skip_bajo": -999.0,     # nunca SKIP ALC por prev_day en META
        "alc_reducir_bajo": -3.0,    # <-3% ALC = reducir x0.5
        "alc_size2_bajo": 2.0,
        "baj_skip_positivo": 0.0,
        "baj_size2_alto": -3.0,
        "baj_reducir_positivo": 0.0,
    },
    "COIN": {
        # ALC -3%-0% = x0.5 operable (no skip como otros)
        "alc_skip_bajo": -3.0,
        "alc_reducir_bajo": -3.0,    # -3%-0% = x0.5 (igual DEFAULT pero explícito)
        "alc_size2_bajo": 5.0,       # >5% ALC = SIZE x2 (threshold más alto)
        "baj_skip_positivo": 0.0,
        "baj_size2_alto": -3.0,
        "baj_reducir_positivo": 0.0,
    },
    "PLTR": {
        # ALC -3%-0% = SKIP absoluto (46.5% INT — el peor del sistema)
        "alc_skip_bajo": 0.0,        # cualquier prev_day negativo = SKIP ALC
        "alc_reducir_bajo": 0.0,
        "alc_size2_bajo": 5.0,       # >5% ALC = SIZE x2
        "baj_skip_positivo": 2.0,    # BAJ solo SKIP si >2% positivo
        "baj_size2_alto": -3.0,
        "baj_reducir_positivo": 0.0,
    },
    "TLT": {
        # TLT tiene pocos rangos válidos — muy limpio
        "alc_skip_bajo": -3.0,
        "alc_reducir_bajo": -3.0,    # -3%-0% ALC = solo INT (77.8% INT / 23.8% MAX)
        "alc_size2_bajo": 0.0,       # 0%-2% ALC = SIZE x2 (98.6% INT / 81.7% MAX)
        "baj_skip_positivo": 0.0,
        "baj_size2_alto": -3.0,      # <-3% BAJ = SIZE x2
        "baj_reducir_positivo": 0.0,
    },
    "AVGO": {
        # 0%-2% ALC es premium en AVGO (90.8% INT / 60.5% MAX)
        "alc_skip_bajo": -3.0,
        "alc_reducir_bajo": -3.0,
        "alc_size2_bajo": 0.0,       # 0%-2% ya es SIZE x1/x2 en AVGO
        "baj_skip_positivo": 2.0,    # BAJ SKIP desde 2% positivo
        "baj_size2_alto": -3.0,
        "baj_reducir_positivo": 0.0,
    },
}


# Función helper para interpretar prev_day
def _get_prev_day_hint(ticker: str, prev_day_pct: float | None, direction: str) -> str:
    """
    Retorna hint operativo basado en prev_day_change.
    direction: 'ALCISTA' | 'BAJISTA' | 'ZONA_MUERTA'
    Retorna: 'SIZE_x2' | 'SIZE_x1' | 'REDUCIR_x0.5' | 'SKIP' | 'neutral'
    """
    if prev_day_pct is None:
        return "neutral"

    rules = PREV_DAY_RULES.get(ticker, PREV_DAY_RULES["DEFAULT"])

    if direction == "ALCISTA":
        if prev_day_pct < rules.get("alc_skip_bajo", -3.0):
            return "SKIP"
        if prev_day_pct < 0:
            return "REDUCIR_x0.5"
        if prev_day_pct >= rules.get("alc_size2_bajo", 2.0):
            return "SIZE_x2"
        return "SIZE_x1"

    if direction == "BAJISTA":
        if prev_day_pct > rules.get("baj_skip_positivo", 0.0):
            return "SKIP"
        if prev_day_pct < rules.get("baj_size2_alto", -3.0):
            return "SIZE_x2"
        if prev_day_pct < 0:
            return "SIZE_x1"
        return "REDUCIR_x0.5"

    # ZONA_MUERTA — prev_day no determina dirección pero sí confirma régimen
    if prev_day_pct >= 2.0:
        return "regime_alcista"
    if prev_day_pct <= -3.0:
        return "regime_bajista"
    return "neutral"


# Reglas cerro_bp → acción por dirección (calibrado con datos reales v3)
# cerro_bp=True significa que el precio cerró el gap del BP (volvió al BP)
# Valores: 'SKIP' | 'solo_int' | 'reducir' | 'operable' | 'n/a'
CERRO_BP_REGLA: dict[str, dict[str, str]] = {
    "AMZN": {
        "ALC": "SKIP",      # 0% MAX — único del sistema
        "BAJ": "solo_int",  # 81.2% INT pero 25% MAX
        "DD":  "reducir",   # 78.9% INT / 31.6% MAX
        "nota": "AMZN ALC cerro_bp = SKIP ABSOLUTO (0% MAX — único sistema)",
    },
    "MSFT": {
        "ALC": "solo_int",  # 64.3% INT / 14.3% MAX
        "BAJ": "solo_int",  # 69.2% INT / 23.1% MAX
        "DD":  "reducir",   # 100% INT / 46.2% MAX
        "nota": "MSFT cerro_bp = EXIT en INT ambas dirs",
    },
    "AVGO": {
        "ALC": "SKIP",      # 57.7% INT / 7.7% MAX — INT también débil
        "BAJ": "solo_int",  # 81% INT / 23.8% MAX
        "DD":  "solo_int",  # 71.4% INT / 14.3% MAX
        "nota": "AVGO ALC cerro_bp = SKIP (INT débil 57.7% + MAX 7.7%)",
    },
    "GOOGL": {
        "ALC": "solo_int",  # 64.7% INT / 17.6% MAX
        "BAJ": "solo_int",  # 69.6% INT / 17.4% MAX
        "DD":  "reducir",   # 83.3% INT / 20.8% MAX
        "nota": "GOOGL cerro_bp simétrico = EXIT en INT ambas dirs",
    },
    "PLTR": {
        "ALC": "solo_int",  # 46.7% INT / 13.3% MAX
        "BAJ": "reducir",   # 62.5% INT / 25% MAX
        "DD":  "solo_int",  # 84.3% INT / 21.9% MAX — 242 días = frecuente
        "nota": "PLTR DD cerro_bp afecta 19% de días — verificar siempre en ZM",
    },
    "TLT": {
        "ALC": "solo_int",  # 76.2% INT / 23.8% MAX
        "BAJ": "solo_int",  # 84% INT / 20% MAX
        "DD":  "solo_int",  # 82.2% INT / 16.2% MAX
        "nota": "TLT cerro_bp = siempre EXIT en INT, no buscar MAX",
    },
    "IWM": {
        "ALC": "SKIP",      # 32% INT / 4% MAX — SKIP ALC
        "BAJ": "reducir",   # 60% INT / 20% MAX
        "DD":  "reducir",   # 84.8% INT / 33.3% MAX
        "nota": "IWM ALC cerro_bp = SKIP (32% INT — el INT también colapsa)",
    },
    "AMD": {
        "ALC": "reducir",   # 68.4% INT / 26.3% MAX
        "BAJ": "solo_int",  # 76.9% INT / 7.7% MAX
        "DD":  "reducir",   # 92.9% INT / 21.4% MAX
        "nota": "AMD BAJ cerro_bp = EXIT en INT (7.7% MAX)",
    },
    "AAPL": {
        "ALC": "reducir",   # 81% INT / 38.1% MAX — INT fuerte pero MAX baja
        "BAJ": "reducir",   # 63.6% INT / 18.2% MAX
        "DD":  "reducir",   # 81.5% INT / 18.5% MAX
        "nota": "AAPL cerro_bp = operar solo INT en todas las dirs",
    },
    "NVDA": {
        "ALC": "solo_int",  # 75% INT / 16.7% MAX
        "BAJ": "SKIP",      # 16.7% INT — SKIP absoluto BAJ
        "DD":  "solo_int",  # 84.9% INT / 19.5% MAX
        "nota": "NVDA BAJ cerro_bp = SKIP (16.7% INT — prácticamente 0)",
    },
    "META": {
        "ALC": "n/a",       # 0 días con cerro_bp ALC en META — fenómeno inexistente
        "BAJ": "reducir",   # 100% INT / 33.3% MAX (n=6 pequeño)
        "DD":  "solo_int",  # 91.1% INT / 34.4% MAX
        "nota": "META ALC cerro_bp = no existe (0 días en 1266). DD = solo INT",
    },
    "COIN": {
        "ALC": "operable",  # 81.8% INT / 54.5% MAX — MEJOR que base (único sistema)
        "BAJ": "SKIP",      # 33.3% INT — SKIP absoluto BAJ
        "DD":  "n/a",       # datos insuficientes
        "nota": "COIN: ALC cerro_bp = OPERABLE (único sistema). BAJ = SKIP absoluto",
    },
    "AMZN_ZM": {  # alias para recordar el caso ZM de AMZN
        "DD":  "reducir",
        "nota": "AMZN ZM cerro_bp = 78.9% INT / 31.6% MAX — reducir target MAX",
    },
}


# Función helper para interpretar cerro_bp
def _get_cerro_bp_hint(ticker: str, cerro_bp: bool, direction: str) -> str:
    """
    Retorna hint operativo basado en cerro_bp.
    cerro_bp: True si el precio cerró el gap del BP
    direction: 'ALCISTA' | 'BAJISTA' | 'ZONA_MUERTA'
    Retorna: 'SKIP' | 'solo_int' | 'reducir' | 'operable' | 'normal'
    """
    if not cerro_bp:
        return "normal"

    reglas = CERRO_BP_REGLA.get(ticker)
    if reglas is None:
        # Sin regla específica → comportamiento genérico conservador
        if direction == "ALCISTA":
            return "solo_int"
        if direction == "BAJISTA":
            return "solo_int"
        return "reducir"

    dir_key = {"ALCISTA": "ALC", "BAJISTA": "BAJ", "ZONA_MUERTA": "DD"}.get(direction, "ALC")
    return reglas.get(dir_key, "reducir")


# ZM + escalo — % MAX aproximado (playbook v2.0)
ZM_ESCALO_PREMIUM_TICKERS: dict[str, float] = {
    "GLD": 85.7,
    "COIN": 85.7,
    "AVGO": 62.5,
    "AMZN": 68.8,
}
ZM_ESCALO_SKIP_TICKERS: dict[str, float] = {"PLTR": 29.7}

# Reglas >30min al INT (True = cerrar en INT si trade >30min)
VELOCIDAD_REGLA: dict[str, tuple[bool, bool, str]] = {
    "QQQ":  (False, False, "QQQ: resistente tardío — no cerrar"),
    "SPY":  (False, False, "SPY ZM tardío 61% — no cerrar"),
    "DIA":  (True,  True,  "DIA >30min = cerrar en INT"),
    "IWM":  (True,  False, "IWM: ALC >30min = 28.2% cerrar / BAJ >30min = 50% NO cerrar"),
    "NVDA": (True,  True,  "NVDA >30min = 36.7% — cerrar en INT"),
    "AMD":  (True,  True,  "AMD >30min = 33.3% — cerrar en INT"),
    "TSLA": (True,  True,  "TSLA >30min = 39.4% — cerrar en INT"),
    "AAPL": (True,  False, "AAPL: ALC fuera ZM >30min = 38.8% cerrar / ZM >30min = 55.2% NO cerrar"),
    "META": (True,  True,  "META >30min = 44.4% ALC / 37.5% BAJ — cerrar"),
    "MSFT": (True,  False, "MSFT: ALC fuera ZM >30min = 27.8% cerrar / BAJ >30min = 35% reducir"),
    "GOOGL":(False, False, "GOOGL >30min = 39.6% ALC reducir / BAJ 34.1% reducir — no cerrar obligatorio"),
    "AMZN": (True,  False, "AMZN: ALC >30min = 33.3% cerrar / BAJ >30min = 46.7% NO cerrar"),
    "GLD":  (True,  True,  "GLD >30min = cerrar en INT"),
    "TLT":  (True,  False, "TLT: ALC >30min = 33.3% cerrar / BAJ >30min = 42.9% reducir"),
    "COIN": (False, False, "COIN ZM >30min = 72.5% MAX — NO cerrar en ninguna dir"),
    "PLTR": (True,  True,  "PLTR >30min = 34.6% — cerrar en INT"),
    "AVGO": (True,  False, "AVGO: ALC fuera ZM >30min = 32.9% cerrar / BAJ = 44.4% reducir"),
}

# 2º toque INT (True = exit forzado en esa dirección) — calibrado con datos reales
SEGUNDO_TOQUE_REGLA: dict[str, tuple[bool, bool, str]] = {
    # MANTENER/REDUCIR — no exit obligatorio
    "TSLA": (False, False, "TSLA: 2do toque ALC=70.3% BAJ=70.8% — MANTENER"),
    "META": (False, False, "META: 2do toque ALC=59.6% BAJ=51.7% — REDUCIR no salir"),
    "COIN": (False, False, "COIN: 2do toque ALC=50.8% BAJ=42.3% — REDUCIR"),
    "MSFT": (False, False, "MSFT: 2do toque ALC=47.7% BAJ=53.2% — REDUCIR"),
    "QQQ":  (False, False, "QQQ: 2do toque ~60% — reducir no salir"),
    "SPY":  (False, False, "SPY: 2do toque BAJ=75.5% — mantener BAJ / reducir ALC"),
    "DIA":  (False, False, "DIA: 2do toque BAJ=67.3% — mantener BAJ"),
    "AVGO": (False, False, "AVGO: 2do toque ALC=41.5% BAJ=44.3% — REDUCIR"),
    "GLD":  (False, False, "GLD: 2do toque ~54% — reducir"),
    # EXIT ASIMÉTRICO
    "AMZN": (True,  False, "AMZN: 2do toque ALC=31.4% SALIR / BAJ=42.6% REDUCIR"),
    "NVDA": (True,  True,  "NVDA: 2do toque ALC=30.6% BAJ=38% — SALIR ambos"),
    "AMD":  (False, True,  "AMD: 2do toque ALC=43.9% REDUCIR / BAJ=33.9% SALIR"),
    "PLTR": (False, True,  "PLTR: 2do toque ALC=44.6% REDUCIR / BAJ=40.4% SALIR"),
    "GOOGL":(True,  True,  "GOOGL: 2do toque ALC=34.7% BAJ=33% — SALIR ambos"),
    "TLT":  (True,  True,  "TLT: 2do toque ALC=38.9% BAJ=29.9% — SALIR ambos"),
    # EXIT AMBAS
    "IWM":  (True,  True,  "IWM: 2do toque ALC=20.7% BAJ=28.5% — SALIR"),
    "AAPL": (True,  True,  "AAPL: 2do toque ALC=36.3% BAJ=34.9% — SALIR"),
}

# SKIP por open_zone + 1v3 + contexto (playbook v2.0)
_OPEN_ZONE_SKIP_RULES: list[tuple[list[str], list[str], str, str | None, str]] = [
    (
        ["AAPL"],
        ["normal_t1"],
        "cualquier",
        "BAJISTA+GAP_DOWN",
        "BAJ GAP DOWN t1 AAPL — playbook: Contra>52% — SKIP",
    ),
    (
        ["META"],
        ["normal_t1"],
        "escalo",
        "BAJISTA",
        "escalo BAJ t1 META = 0% INT — SKIP absoluto",
    ),
    (
        ["MSFT", "AMZN", "AVGO", "PLTR"],
        ["normal_t1"],
        "cualquier",
        "BAJISTA+GAP_DOWN",
        "BAJ GAP DOWN t1 — Contra>50% — SKIP",
    ),
]

# Avisos (no SKIP) por zona
_OPEN_ZONE_WARN_RULES: list[tuple[list[str], list[str], str, str | None, str]] = [
    (
        ["GOOGL"],
        ["normal_t3"],
        "igual",
        None,
        "igual en t3 GOOGL = ~25% MAX — target solo INT",
    ),
    (
        ["TLT"],
        ["normal_t2"],
        "cualquier",
        "BAJISTA+GAP_DOWN",
        "BAJ GAP DOWN t2 TLT — solo INT (MAX ~21%)",
    ),
]

ZONE_SIZE_HINT: dict[tuple[str, str | None], str] = {
    ("doble_dir_zone", None): "ZM — esperar INT, SIZE x2",
    ("normal_t1", "No"): "t1 sin bp — SIZE x0.5-x1",
    ("normal_t1", "Si"): "t1 tocó BP — SKIP/reducir",
    ("normal_t2", "No"): "t2 sin bp — SIZE x1-x2",
    ("normal_t2", "Si"): "t2 tocó BP — SKIP/reducir",
    ("normal_t3", "No"): "t3 sin bp — SIZE x2",
    ("normal_t3", "Si"): "t3 tocó BP — SKIP",
    ("ext_int_max", None): "Abrió pasado INT — SIZE x1-x2, target MAX",
}

ALC_T3_SOLO_INT_TICKERS = frozenset({"AVGO", "AMZN"})


def _bars_list_to_mi_dataframe(bars: list) -> pd.DataFrame:
    """Convierte barras Alpaca (dict con t,o,h,l,c,v) al formato esperado por mi_backtesting_nvda."""
    if not bars:
        return pd.DataFrame()
    df = pd.DataFrame(
        [
            {
                "timestamp": b["t"],
                "open": b["o"],
                "high": b["h"],
                "low": b["l"],
                "close": b["c"],
                "volume": b["v"],
            }
            for b in bars
        ]
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    if df["timestamp"].dt.tz is None:
        df["timestamp"] = df["timestamp"].dt.tz_localize(TIMEZONE)
    else:
        df["timestamp"] = df["timestamp"].dt.tz_convert(TIMEZONE)
    df["date_et"] = df["timestamp"].dt.date
    return df


def _get_igual_baj_rate(ticker: str) -> str:
    """INT rate de igual BAJ por ticker para warnings."""
    rates = {"SPY": "65.8", "MSFT": "65.7", "AMZN": "67.1", "GLD": "63.2"}
    return rates.get(ticker, "~66")


def _get_accion_1v3(
    ticker: str,
    direction: str,
    estado_1v3: str,
    ec_size: float,
    n_caution: int = 0,
) -> str:
    """Instrucción operativa 1v3 para dashboard (open_930)."""
    if estado_1v3 == "escalo" and direction == "ALCISTA":
        _escalo_alc_skip = {
            "QQQ", "SPY", "IWM", "DIA", "NVDA", "AMD", "PLTR", "COIN", "AAPL"
        }
        if ticker in _escalo_alc_skip:
            return f"⛔ SKIP — escalo ALC en {ticker} confirmado (42-66% INT, skip obligatorio)"
        # GLD/TLT = premium ambas dirs / TSLA/MSFT/GOOGL/AMZN/META/AVGO = neutro simétrico
        return "🟢 operable — escalo ALC (activo refugio o ticker simétrico)"
    if estado_1v3 == "desaparecio" and direction == "BAJISTA" and ticker == "NFLX":
        return "⛔ SKIP — desaparecio BAJ NFLX único"
    if direction == "ZONA_MUERTA" and estado_1v3 in ("igual", "nuevo"):
        return "✅ SIZE x2 → MAX (DD premium)"
    if estado_1v3 == "escalo" and direction == "BAJISTA" and ticker in {
        "META",
        "AMZN",
        "GLD",
        "TLT",
    }:
        return "✅ SIZE x2 → MAX (escalo BAJ premium)"
    if (
        estado_1v3 in ("escalo", "cambio_tipo")
        and direction == "BAJISTA"
        and n_caution >= 4
    ):
        return (
            "✅ SIZE x2 → MAX (n_caut=4 BAJ universal)"
            if ec_size >= 2
            else "🟡 SIZE x1 → INT"
        )
    if estado_1v3 == "igual" and direction == "BAJISTA" and ticker in {
        "SPY",
        "MSFT",
        "AMZN",
        "GLD",
    }:
        return "⚠️ REDUCIR x0.5 — igual BAJ débil en este ticker"
    if estado_1v3 == "nuevo" and direction == "BAJISTA" and ticker in {
        "GOOGL",
        "MSFT",
        "AMD",
        "AVGO",
    }:
        return "⚠️ REDUCIR — nuevo BAJ débil"
    if direction == "ZONA_MUERTA" and estado_1v3 == "desaparecio" and ticker == "IWM":
        return "🟡 Solo INT — desaparecio DD IWM (26.3% MAX)"
    if estado_1v3 == "desaparecio":
        return "🟢 SIZE x1 → MAX (desaparecio = fuerza universal)"
    return f"🟢 SIZE x1 → operar normal ({estado_1v3})"


# =============================================================================
# CAUTION (formato Milton) — port desde bb_retroceso/mi_backtesting_nvda.py
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


def _filter_bars_before(bars: list, cutoff_time: datetime) -> list:
    """Filtra barras con ts estrictamente menor a cutoff_time."""
    out = []
    for b in bars:
        ts = b["t"]
        if ts.tzinfo is None:
            ts = pd.Timestamp(ts).tz_localize(TIMEZONE)
        if ts < cutoff_time:
            out.append(b)
    out.sort(key=lambda x: x["t"])
    return out


def _filter_bars_upto_inclusive(bars: list, cutoff_time: datetime) -> list:
    """Filtra barras con ts <= cutoff_time (incluye el corte 16:00)."""
    out = []
    for b in bars:
        ts = b["t"]
        if ts.tzinfo is None:
            ts = pd.Timestamp(ts).tz_localize(TIMEZONE)
        if ts <= cutoff_time:
            out.append(b)
    out.sort(key=lambda x: x["t"])
    return out


def _test_cutoff_from_1m(bars_dict: dict) -> tuple[datetime, str]:
    """
    Modo test: cutoff = última barra 1m disponible (no 09:28 fijo).
    _filter_bars_before usa ts < cutoff; devolvemos cutoff = última_ts + 1s
    para incluir esa barra. Retorna (cutoff_para_bp, etiqueta legible ET).
    """
    bars_1m = bars_dict.get("1m", [])
    if not bars_1m:
        now = datetime.now(TIMEZONE).replace(second=0, microsecond=0)
        return now + timedelta(seconds=1), now.strftime("%Y-%m-%d %H:%M ET")
    sorted_bars = sorted(bars_1m, key=lambda x: x["t"])
    ts = pd.Timestamp(sorted_bars[-1]["t"])
    if ts.tzinfo is None:
        ts = ts.tz_localize(TIMEZONE)
    else:
        ts = ts.tz_convert(TIMEZONE)
    ts = ts.replace(second=0, microsecond=0)
    cutoff_bp = (ts + pd.Timedelta(seconds=1)).to_pydatetime()
    label = ts.strftime("%Y-%m-%d %H:%M ET")
    return cutoff_bp, label


def _validate_premarket_bars(
    ticker: str,
    bars_1m: list,
    cutoff_et: datetime,
    min_bars: int = 5,
) -> bool:
    """
    Verifica que haya al menos min_bars velas de 1m
    antes de cutoff_et. Si no, loguea advertencia y
    retorna False para que el ticker sea skipped.
    """
    filtered = _filter_bars_before(bars_1m, cutoff_et)
    if len(filtered) < min_bars:
        print(
            f"⚠️  {ticker}: solo {len(filtered)} barras 1m "
            f"premarket disponibles (mín {min_bars}). "
            f"BP de este ticker puede ser impreciso."
        )
        return len(filtered) >= 2  # mínimo absoluto para BP
    return True


def _compute_bb_from_closes(closes: list) -> tuple[float | None, float | None]:
    """Devuelve (bbt, bbb) con EMA-BB(20,2) — media y std exponenciales (ewm, adjust=False)."""
    if len(closes) < N_VELAS_BB:
        return None, None
    close = pd.Series([float(x) for x in closes[-N_VELAS_BB:]], dtype=float)
    mid = float(close.ewm(span=N_VELAS_BB, adjust=False).mean().iloc[-1])
    std = float(close.ewm(span=N_VELAS_BB, adjust=False).std().iloc[-1])
    if np.isnan(mid) or np.isnan(std):
        return None, None
    return mid + 2 * std, mid - 2 * std


def compute_badge_long(bars_dict: dict, cutoff_time: datetime) -> float:
    """% TF donde EMA3 > EMA9 (slice ts < cutoff_time)."""
    up = 0
    total = 0
    for tf, bars in bars_dict.items():
        if not bars:
            continue
        filtered = _filter_bars_before(bars, cutoff_time)
        if len(filtered) < 3:
            continue
        closes = pd.Series([float(b["c"]) for b in filtered], dtype=float)
        ema3 = float(closes.ewm(span=3, adjust=False).mean().iloc[-1])
        ema9 = float(closes.ewm(span=9, adjust=False).mean().iloc[-1])
        total += 1
        if ema3 > ema9:
            up += 1
    return (up / total) * 100.0 if total else 0.0


def compute_signals_3_9(bars_dict: dict, cutoff_time: datetime) -> str:
    """Port de compute_signals_3_9 (mi_backtesting_nvda.py) usando barras dict."""
    parts = []
    for tf in ["15m", "30m", "1h"]:
        if tf not in bars_dict or not bars_dict[tf]:
            continue
        filtered = _filter_bars_before(bars_dict[tf], cutoff_time)
        if len(filtered) < 9:
            continue
        tail20 = filtered[-N_VELAS_BB:]
        closes = pd.Series([float(b["c"]) for b in tail20], dtype=float)
        ema3 = float(closes.ewm(span=3, adjust=False).mean().iloc[-1])
        ema9 = float(closes.ewm(span=9, adjust=False).mean().iloc[-1])
        signal = "up" if ema3 > ema9 else "down"
        parts.append(f"3/9 {signal} {tf}")
    return ", ".join(parts) if parts else ""


def compute_caution_note(
    bars_dict: dict,
    precio_premarket: float,
    cutoff_prev_1600: datetime,
    ticker: str = "",
) -> str:
    """Port de compute_caution (mi_backtesting_nvda.py) para stocks.
    Compara precio_premarket vs BB(20,2) construido con barras hasta 16:00 prev.
    TFs fijos 1m–1d (caution_tf_key_label_pairs en mi_backtesting_nvda).
    """
    cautions = []
    tf_pairs = (
        caution_tf_key_label_pairs(ticker)
        if ticker
        else [
            ("1m", "1M"),
            ("5m", "5M"),
            ("15m", "15M"),
            ("30m", "30M"),
            ("1h", "1H"),
            ("1d", "1D"),
        ]
    )
    for tf, label in tf_pairs:
        if tf not in bars_dict or not bars_dict[tf]:
            continue
        filtered_prev = _filter_bars_upto_inclusive(bars_dict[tf], cutoff_prev_1600)
        closes_prev = [b["c"] for b in filtered_prev]
        bbt, bbb = _compute_bb_from_closes(closes_prev)
        if bbt is not None and precio_premarket > bbt:
            key = f"BBT {label}"
            cautions.append(CAUTION_TEXTS.get(key, key))
        if bbb is not None and precio_premarket < bbb:
            key = f"BBB {label}"
            cautions.append(CAUTION_TEXTS.get(key, key))
    return " ".join(cautions) if cautions else "Sin caution"


def ema_alignment(
    open_p: float,
    e20: float,
    e50: float,
    e200: float,
    pct: float = 0.005,
) -> str:
    """
    Clasifica la posición del precio respecto a EMA20/50/200.
    Retorna: 'sobre_3' | 'bajo_3' | 'entre' | 'at_any'
    pct = umbral de tolerancia AT (0.5% del precio)
    """
    def zone(o, e):
        if not e or e == 0:
            return "unknown"
        d = (o - e) / e
        return "above" if d > pct else ("below" if d < -pct else "at")

    zones = [zone(open_p, e20), zone(open_p, e50), zone(open_p, e200)]
    if "at" in zones:
        return "at_any"
    if all(z == "above" for z in zones):
        return "sobre_3"
    if all(z == "below" for z in zones):
        return "bajo_3"
    return "entre"


def conviction_score(
    dir_ph: str,
    gap_tipo: str,
    gap_mag: float,
    pdc: float,
    ema_align: str,
    bb_expande: bool | None = None,
    n_cautiones: int = 0,
    caution_1v3: str | None = None,
    ticker: str = "",
    open_930: float | None = None,
    int_pos: float | None = None,
    int_neg: float | None = None,
) -> float:
    """
    Score de convicción EC v2: 0-10.
    Base 5.0 + EMA(±2) + GAP(±2) + PrevDay(±2) + bb_expande(+0.5)
    + Milton cautiones(±1.5) + 1v3(±4) cuando disponible.

    score ≥7 → SIZE x2 (PREMIUM)
    5-6    → SIZE x1 (operable)
    ≤4     → SKIP

    Retorna -1.0 si el precio ya abrió más allá del INT (BLOQUEADO).
    """
    score = 5.0

    # ── EMA alignment (±2.0 pts) ──────────────────────────────────
    if dir_ph == "BAJISTA":
        if ema_align == "bajo_3":              score += 2.0
        elif ema_align == "sobre_3":           score -= 1.0
        elif ema_align in ("entre", "at_any"): score -= 0.5
    elif dir_ph == "ALCISTA":
        if ema_align == "sobre_3":             score += 2.0
        elif ema_align == "bajo_3":            score -= 1.0
        elif ema_align in ("entre", "at_any"): score -= 0.5

    # ── GAP tipo y magnitud (±2.0 pts) ────────────────────────────
    mag = abs(float(gap_mag or 0))
    if dir_ph == "BAJISTA":
        if gap_tipo == "GAP_DOWN":
            score += 2.0 if mag > 3 else (1.5 if mag > 1 else 0.5)
        elif gap_tipo == "GAP_UP":
            score += 1.5 if mag > 3 else (-0.5 if mag > 1 else 0.0)
    elif dir_ph == "ALCISTA":
        if gap_tipo == "GAP_UP":
            score += 1.5 if mag > 3 else (1.0 if mag > 1 else 0.5)
        elif gap_tipo == "GAP_DOWN":
            score += -1.5 if mag > 3 else (-0.5 if mag > 1 else 0.5)

    # ── Prev day change (±2.0 pts) ────────────────────────────────
    pdc_f = float(pdc or 0)
    if dir_ph == "BAJISTA":
        if pdc_f < -3:    score += 1.5
        elif pdc_f < -1:  score += 2.0
        elif pdc_f < 0:   score += 1.0
        elif pdc_f < 1:   score += 0.5
        elif pdc_f < 3:   score -= 0.5
        else:             score -= 1.5
    elif dir_ph == "ALCISTA":
        if pdc_f > 5:     score += 2.0   # muy fuerte = MAX casi garantizado
        elif pdc_f > 2:   score += 1.5   # fuerte = excelente
        elif pdc_f > 0:   score += 0.5   # tranquilo = normal
        elif pdc_f > -1:  score += 0.0   # neutro
        elif pdc_f > -3:  score -= 0.5   # caída moderada = precaución
        else:             score -= 1.5   # caída fuerte = precaución máxima

    # ── bb_expande bonus (+0.5) ───────────────────────────────────
    if bb_expande is True:
        score += 0.5

    # ── NUEVO: Cautiones de Milton 9:28 (±1.5 pts) ───────────────
    # n_cautiones = número de bandas BB tocadas premarket (0-4)
    if dir_ph in ("ALCISTA", "BAJISTA"):
        if n_cautiones == 0:
            # Sin cautiones = precio limpio sin sobreextensión
            score += 1.5
        elif n_cautiones == 4 and dir_ph == "BAJISTA":
            # Cuádruple bajista = momentum confirmado en todas las bandas
            score += 1.5
        elif n_cautiones == 4 and dir_ph == "ALCISTA":
            # Cuádruple alcista = sobreextendido
            score -= 0.5
        elif n_cautiones == 1:
            # Una caución — verificar si contradice la dirección
            # BBT siendo bajista o BBB siendo alcista = contradice
            score += 0.0  # neutral, se ajusta abajo si contradice

    # ── NUEVO: 1v3 estado al abrir 9:30 (±4.0 pts) ───────────────
    # Solo disponible en modo open_930
    if caution_1v3 is not None and dir_ph != "ZONA_MUERTA":

        # escalo ALC: neutro (0 pts). igual BAJ: sin penalización por ticker (setup premium).

        if caution_1v3 == "escalo" and dir_ph == "BAJISTA" and ticker == "NVDA":
            score -= 4.0  # NVDA único: escalo BAJ también es SKIP

        # PREMIUM — sube el score
        elif caution_1v3 == "escalo" and dir_ph == "BAJISTA" and ticker in {"META", "AMZN", "GLD", "TLT"}:
            score += 2.0  # Escalo BAJ premium en estos tickers

        elif n_cautiones == 4 and dir_ph == "BAJISTA" and caution_1v3 in ("escalo", "cambio_tipo"):
            score += 1.0  # n_caut=4 BAJ universal

    # ── NUEVO: DD + 1v3 ───────────────────────────────────────────
    # Si dir_ph es ZONA_MUERTA y 1v3=igual/nuevo → +3 (regla universal)
    if dir_ph == "ZONA_MUERTA" and caution_1v3 in ("igual", "nuevo"):
        score += 3.0

    # ── NUEVO: Bloqueo por precio más allá del INT ─────────────────
    # Si el precio ya pasó el objetivo al abrir → BLOQUEADO (-1 especial)
    if open_930 is not None:
        if dir_ph == "ALCISTA" and int_pos is not None and open_930 > int_pos:
            return -1.0  # BLOQUEADO — abrió más allá del INT
        if dir_ph == "BAJISTA" and int_neg is not None and open_930 < int_neg:
            return -1.0  # BLOQUEADO — abrió más allá del INT
        # Recorrido mínimo $0.30
        if dir_ph == "ALCISTA" and int_pos is not None:
            if (int_pos - open_930) < 0.30:
                return -1.0  # BLOQUEADO — sin recorrido
        if dir_ph == "BAJISTA" and int_neg is not None:
            if (open_930 - int_neg) < 0.30:
                return -1.0  # BLOQUEADO — sin recorrido

    return round(max(0.0, min(10.0, score)), 1)


def direction_from_dist(dist: float) -> str:
    """Misma regla que open_930: ±0.25 respecto a BP."""
    if dist > 0.25:
        return "ALCISTA"
    if dist < -0.25:
        return "BAJISTA"
    return "ZONA_MUERTA"


def compute_open_zone(
    open_930: float,
    bp: float,
    int_pos: float | None,
    int_neg: float | None,
    direction: str,
    threshold: float = 0.25,
) -> tuple[str | None, float | None]:
    """
    open_zone y open_pct_en_rango al abrir (9:30). Sin INT válidos → (None, None).
    pct > 1.0 → ext_int_max (live simplificado, sin sub-tercios INT→MAX).
    """
    dist_abs = abs(open_930 - bp)
    if dist_abs < threshold:
        return "doble_dir_zone", 0.0

    if direction == "ALCISTA":
        if int_pos is None:
            return None, None
        rango = int_pos - bp
        if rango <= 0:
            return "doble_dir_zone", 0.0
        pct = (open_930 - bp) / rango
    elif direction == "BAJISTA":
        if int_neg is None:
            return None, None
        rango = bp - int_neg
        if rango <= 0:
            return "doble_dir_zone", 0.0
        pct = (bp - open_930) / rango
    else:
        return "doble_dir_zone", 0.0

    if pct <= 0:
        return "doble_dir_zone", round(pct, 4)
    if pct <= 0.333:
        zone = "normal_t1"
    elif pct <= 0.667:
        zone = "normal_t2"
    elif pct <= 1.0:
        zone = "normal_t3"
    else:
        zone = "ext_int_max"
    return zone, round(pct, 4)


def get_rebote_umbral(ticker: str, direction: str) -> float:
    """Umbral rebote post-INT ($) para ticker y dirección."""
    umbrales = REBOTE_UMBRAL.get(ticker, {"ALC": 1.50, "BAJ": 1.50})
    if direction == "ALCISTA":
        return float(umbrales["ALC"])
    if direction == "BAJISTA":
        return float(umbrales["BAJ"])
    return float(max(umbrales["ALC"], umbrales["BAJ"]))


def _match_open_zone_dir_spec(spec: str | None, direction: str, gap_type: str) -> bool:
    if spec is None:
        return True
    if spec == "BAJISTA+GAP_DOWN":
        return direction == "BAJISTA" and gap_type == "GAP_DOWN"
    return direction == spec


def _open_zone_skip_match(
    ticker: str,
    open_zone: str | None,
    caution_1v3: str | None,
    direction: str,
    gap_type: str,
    rule: tuple[list[str], list[str], str, str | None, str],
) -> bool:
    tickers_r, zones_r, c1_r, dir_r, _motivo = rule
    if ticker not in tickers_r or not open_zone or open_zone not in zones_r:
        return False
    if c1_r != "cualquier":
        if caution_1v3 != c1_r:
            return False
    if not _match_open_zone_dir_spec(dir_r, direction, gap_type):
        return False
    return True


def _gestion_live_fragments(ticker: str, direction: str) -> list[str]:
    """Textos no estándar para dashboard (velocidad / 2º toque)."""
    out: list[str] = []
    std_vel_alc, std_vel_baj = True, True
    std_2t_alc, std_2t_baj = True, True

    vr = VELOCIDAD_REGLA.get(ticker)
    if vr:
        alc_c, baj_c, nota = vr
        if direction == "ALCISTA" and alc_c != std_vel_alc:
            out.append(f">30min→{'cerrar_INT' if alc_c else 'NO_cerrar'} ({nota})")
        elif direction == "BAJISTA" and baj_c != std_vel_baj:
            out.append(f">30min→{'cerrar_INT' if baj_c else 'NO_cerrar'} ({nota})")

    sr = SEGUNDO_TOQUE_REGLA.get(ticker)
    if sr:
        e_alc, e_baj, nota = sr
        if (e_alc, e_baj) != (std_2t_alc, std_2t_baj):
            parts = []
            if e_alc != std_2t_alc:
                parts.append(f"2do_toque→{'exit_ALC' if e_alc else 'no_exit_ALC'}")
            if e_baj != std_2t_baj:
                parts.append(f"2do_toque→{'exit_BAJ' if e_baj else 'no_exit_BAJ'}")
            if parts:
                out.append(" | ".join(parts) + f" ({nota})")
    return out


def _gestion_open_930_line(ticker: str, td: dict, direction: str) -> str | None:
    """Línea GESTIÓN en context_compressed: solo si hay algo no estándar."""
    chunks: list[str] = []
    ru = td.get("rebote_umbral")
    if ru is not None and abs(float(ru) - 1.50) > 0.001:
        chunks.append(f"umbral_rebote=${float(ru):.2f}")
    chunks.extend(_gestion_live_fragments(ticker, direction))
    for w in td.get("evaluation", {}).get("warnings", []):
        if (
            "Zona+1v3:" in w
            or "escalo en t1/t2" in w
            or "escalo ALC — operable" in w
        ):
            chunks.append(w)
    if not chunks:
        return None
    return " | ".join(chunks)


def compute_cruces(tickers_data: dict) -> dict:
    """Lecturas de par por familias (Hallazgo Maestro v21)."""
    cruces = {}
    for familia, members in FAMILIAS.items():
        dirs = {}
        for t in members:
            if t in tickers_data:
                dirs[t] = tickers_data[t].get("direction", "ZONA_MUERTA")
        if len(dirs) < 2:
            cruces[familia] = "SIN_DATOS"
            continue
        vals = list(dirs.values())
        tickers_str = list(dirs.keys())
        if all(v == "ALCISTA" for v in vals):
            cruces[familia] = (
                f"AMBOS_ALC — {' + '.join(tickers_str)} alineados alcistas"
            )
        elif all(v == "BAJISTA" for v in vals):
            cruces[familia] = (
                f"AMBOS_BAJ — {' + '.join(tickers_str)} alineados bajistas"
            )
        elif all(v == "ZONA_MUERTA" for v in vals):
            cruces[familia] = "AMBOS_ZM — sector indeciso"
        else:
            resumen = " | ".join([f"{t}={d}" for t, d in dirs.items()])
            cruces[familia] = f"DIVERGENCIA — {resumen}"

    tlt_dir = tickers_data.get("TLT", {}).get("direction", "ZONA_MUERTA")
    gld_dir = tickers_data.get("GLD", {}).get("direction", "ZONA_MUERTA")
    if tlt_dir == "ALCISTA" and gld_dir == "ALCISTA":
        cruces["MACRO_REFUGIOS"] = (
            "⚠️ PANIC SIGNAL — TLT+GLD ambos ALC = SKIP alcistas"
        )
    elif tlt_dir == "BAJISTA" and gld_dir == "BAJISTA":
        cruces["MACRO_REFUGIOS"] = (
            "✅ RISK-ON MÁXIMO — TLT+GLD ambos BAJ = full size alcistas"
        )
    elif tlt_dir == "BAJISTA" and gld_dir == "ALCISTA":
        cruces["MACRO_REFUGIOS"] = (
            "⚠️ INFLACIÓN/USD DÉBIL — GLD ALC con máxima convicción"
        )
    elif tlt_dir == "ALCISTA" and gld_dir == "BAJISTA":
        cruces["MACRO_REFUGIOS"] = "⚠️ HUIDA A USD — bajistas USD-sensibles"
    else:
        cruces["MACRO_REFUGIOS"] = (
            "NEUTRAL — refugios en ZM, contexto indeciso"
        )
    return cruces


def finalize_dashboard_output(output_data: dict) -> None:
    """skips SkipFilter, PremiumDetector, elimina tier SKIP, cruces."""
    output_data.setdefault("skips", {})
    tickers = output_data.setdefault("tickers", {})
    for t in list(tickers.keys()):
        td = tickers[t]
        if td.get("skip"):
            output_data["skips"][t] = td.get("skip_reason", "Skip")
    for t in list(tickers.keys()):
        td = tickers[t]
        if td.get("skip"):
            continue
        ev = PremiumDetector.evaluate(t, td)
        tickers[t]["evaluation"] = ev
        if ev.get("tier") == "SKIP":
            output_data["skips"][t] = ev.get("reason", "SKIP")
            del tickers[t]
    output_data["cruces"] = compute_cruces(output_data.get("tickers", {}))


class PremiumDetector:
    """
    Evalúa setup premium por playbook (Hallazgo Maestro v21).
    """

    @staticmethod
    def _signals_str(ticker_data: dict) -> str:
        return ticker_data.get("signals_3_9") or ticker_data.get("signals", "")

    @staticmethod
    def is_directo(ticker_data: dict) -> bool:
        badge = ticker_data.get("badge_long", 50)
        direction = ticker_data.get("direction", "ZONA_MUERTA")
        if direction == "ALCISTA" and badge >= 62:
            return True
        if direction == "BAJISTA" and badge <= 38:
            return True
        return False

    @staticmethod
    def get_triple(ticker_data: dict) -> str:
        signals = PremiumDetector._signals_str(ticker_data)
        ups = signals.count("up")
        downs = signals.count("down")
        if ups >= 3:
            return "TRIPLE_UP"
        if downs >= 3:
            return "TRIPLE_DOWN"
        if ups == 0 and downs >= 2:
            return "DOUBLE_DOWN"
        if downs == 0 and ups >= 2:
            return "DOUBLE_UP"
        return "NEUTRO"

    @staticmethod
    def get_n_caution(ticker_data: dict) -> int:
        caution = ticker_data.get("caution_note", "Sin caution")
        if caution in ("Sin caution", "PENDIENTE"):
            return 0
        return caution.count("BBT") + caution.count("BBB")

    @staticmethod
    def detect_escalo(ticker_data: dict) -> bool:
        """
        DEPRECATED — La detección de escalo se hace directamente en evaluate()
        usando caution_cambio_1v3 == 'escalo' cuando está disponible (modo open_930).
        Este método se mantiene por compatibilidad con código externo pero no se llama
        internamente. Ver línea ~583: escalo = caution_1v3 == 'escalo' si caution_1v3.
        """
        return False

    @staticmethod
    def evaluate(ticker: str, ticker_data: dict) -> dict:
        direction = ticker_data.get("direction", "ZONA_MUERTA")
        dist = abs(ticker_data.get("dist", 0))
        badge = ticker_data.get("badge_long", 50)
        n_caution = PremiumDetector.get_n_caution(ticker_data)
        triple = PremiumDetector.get_triple(ticker_data)
        directo = PremiumDetector.is_directo(ticker_data)
        caution_1v3 = ticker_data.get("caution_cambio_1v3")
        escalo = caution_1v3 == "escalo" if caution_1v3 is not None else False
        gap_type = ticker_data.get("gap_type", "FLAT")
        prev_day = ticker_data.get("prev_day_change", 0)

        warnings: list = []
        reason = ""
        stat_base = ""
        is_premium = False
        zm_1v3_unlock = False

        if prev_day > 9.5 and gap_type == "GAP_DOWN":
            return {
                "is_premium": False,
                "tier": "SKIP",
                "reason": "prev_day>9.5% + GAP_DOWN = capitulación",
                "warnings": [],
                "stat_base": "",
            }

        if (
            gap_type == "GAP_DOWN"
            and ticker_data.get("gap_pct", 0) <= -5
            and direction == "ALCISTA"
        ):
            return {
                "is_premium": False,
                "tier": "SKIP",
                "reason": "GAP_DOWN ≥5% con alcista",
                "warnings": [],
                "stat_base": "",
            }

        open_zone_ev = ticker_data.get("open_zone")
        if open_zone_ev:
            for rule in _OPEN_ZONE_SKIP_RULES:
                if _open_zone_skip_match(
                    ticker, open_zone_ev, caution_1v3, direction, gap_type, rule
                ):
                    motivo = rule[4]
                    return {
                        "is_premium": False,
                        "tier": "SKIP",
                        "reason": f"open_zone: {motivo}",
                        "warnings": [f"Zona+1v3: {motivo}"],
                        "stat_base": "",
                        "caution_1v3": caution_1v3,
                    }

        # ── REGLAS 1v3 — Solo con caution_cambio_1v3 (modo open_930) ─────────
        if caution_1v3 is not None:
            if direction == "ZONA_MUERTA" and caution_1v3 == "igual":
                is_premium = True
                reason = (
                    "DD + igual 1v3 → SIZE x2 universal (92-100% toco_int en todos los tickers)"
                )
                stat_base = "Regla universal 1v3 confirmada 14/14 tickers"
                zm_1v3_unlock = True
            elif direction == "ZONA_MUERTA" and caution_1v3 == "nuevo":
                is_premium = True
                reason = (
                    "DD + nuevo 1v3 → SIZE x2 (100% INT NFLX/TLT, premium sistema)"
                )
                stat_base = "DD nuevo: 100% INT NFLX(n=62), TLT(n=19)"
                zm_1v3_unlock = True
            elif caution_1v3 == "escalo" and direction == "ALCISTA":
                warnings.append(
                    "1v3: escalo ALC — operable (83-100% INT confirmado en todos los tickers)"
                )
            elif (
                caution_1v3 == "desaparecio"
                and direction == "BAJISTA"
                and ticker == "NFLX"
            ):
                return {
                    "is_premium": False,
                    "tier": "SKIP",
                    "reason": (
                        "1v3: desaparecio BAJ NFLX = SKIP (57.1% INT — único ticker donde aplica)"
                    ),
                    "warnings": ["Presión bajista premarket se resolvió — sin momentum"],
                    "stat_base": (
                        "NFLX desaparecio BAJ: 57.1% INT (n=21), "
                        "único SKIP del sistema en este estado"
                    ),
                    "caution_1v3": caution_1v3,
                }
            elif caution_1v3 == "igual" and direction == "BAJISTA" and ticker in {
                "SPY",
                "MSFT",
                "AMZN",
                "GLD",
            }:
                warnings.append(
                    f"1v3: igual BAJ en {ticker} = débil "
                    f"({_get_igual_baj_rate(ticker)}% INT) — reducir size"
                )
            elif caution_1v3 == "escalo" and direction == "BAJISTA" and ticker in {
                "META",
                "AMZN",
                "GLD",
                "TLT",
            }:
                is_premium = True
                rates = {
                    "META": "85.7%",
                    "AMZN": "83.3%",
                    "GLD": "89.3%",
                    "TLT": "84.9%",
                }
                reason = (
                    f"1v3: escalo BAJ {ticker} = PREMIUM ({rates.get(ticker, '85%+')} INT)"
                )
                stat_base = f"{ticker} escalo BAJ: top-3 sistema. SIZE x2 directo."
                if ticker == "AMZN":
                    stat_base = (
                        "AMZN escalo BAJ v2.0: 83.3% INT, MAX 16.7% "
                        "(sesgo ZM — no comparar con META/GLD al pie de la letra)"
                    )
            elif (
                caution_1v3 in ("escalo", "cambio_tipo")
                and direction == "BAJISTA"
                and n_caution >= 4
            ):
                is_premium = True
                reason = f"1v3: n_caut=4 BAJ + {caution_1v3} → SIZE x2 universal"
                stat_base = (
                    "n_caut=4 BAJ premium: 75-93% INT todos tickers, +12.5pp vs n_caut=3"
                )

        # ── ZM + escalo — PREMIUM (GLD/COIN/AVGO/AMZN), SKIP PLTR, neutro resto (v2.0)
        if caution_1v3 == "escalo" and direction == "ZONA_MUERTA":
            if ticker in ZM_ESCALO_SKIP_TICKERS:
                rate = ZM_ESCALO_SKIP_TICKERS[ticker]
                return {
                    "is_premium": False,
                    "tier": "SKIP",
                    "reason": (
                        f"ZM + escalo = SKIP en {ticker} ({rate}% MAX — peor ZM del sistema)"
                    ),
                    "warnings": [
                        f"{ticker} escalo en ZM es destructivo — contrasta con GLD/COIN/AVGO/AMZN"
                    ],
                    "stat_base": (
                        f"{ticker} ZM escalo: MAX~{rate}%, playbook v2.0"
                    ),
                    "caution_1v3": caution_1v3,
                }
            if ticker in ZM_ESCALO_PREMIUM_TICKERS:
                rate = ZM_ESCALO_PREMIUM_TICKERS[ticker]
                is_premium = True
                reason = (
                    f"ZM + escalo = PREMIUM en {ticker} ({rate}% MAX — semi-defensivo/crypto)"
                )
                stat_base = f"Playbook v2.0: {ticker} ZM escalo MAX~{rate}%"
                zm_1v3_unlock = True
            else:
                zm_1v3_unlock = True
                if not stat_base:
                    stat_base = f"{ticker} ZM escalo neutro (~50% MAX) — playbook v2.0"

        if direction == "ZONA_MUERTA" and not zm_1v3_unlock:
            return {
                "is_premium": False,
                "tier": "SKIP",
                "reason": "ZONA_MUERTA — esperar open_930",
                "warnings": [],
                "stat_base": "",
            }

        if ticker == "GLD":
            if directo and dist >= 0.50:
                if triple in ["TRIPLE_UP", "TRIPLE_DOWN"]:
                    is_premium = True
                    reason = f"directo + triple + dist ${dist:.2f}"
                    stat_base = "triple DOWN/UP GLD = 87.6%/85.9%"
                elif dist >= 1.00:
                    is_premium = True
                    reason = f"directo + dist ${dist:.2f} zona óptima"
                    stat_base = "dist $1-2 GLD = 93-100%"
            if escalo:
                is_premium = True
                reason += " + escalo detectado"
                stat_base = "escalo GLD = 92.6% ALC / 89.3% BAJ — RECORD"
            if direction == "ALCISTA" and date.today().month == 6:
                warnings.append("JUNIO ALCISTA GLD = SKIP (57.1%)")

        elif ticker == "QQQ":
            if directo:
                if direction == "BAJISTA" and n_caution == 0:
                    is_premium = True
                    reason = "BAJ directo + 0 caution"
                    stat_base = "BAJ directo 99.5% RECORD | 0 caution BAJ 85.1%"
                elif direction == "BAJISTA" and triple == "TRIPLE_DOWN":
                    is_premium = True
                    reason = "BAJ directo + triple DOWN"
                    stat_base = "Triple DOWN BAJ QQQ = 83.5%"
                elif direction == "ALCISTA" and n_caution == 0:
                    is_premium = True
                    reason = "ALC directo + 0 caution"
                    stat_base = "ALC directo 95.1% | 0 caution ALC 81.0%"
                elif direction == "ALCISTA" and triple == "TRIPLE_UP":
                    is_premium = True
                    reason = "ALC directo + triple UP"
                    stat_base = "Triple UP ALC QQQ = 75.2%"

        elif ticker == "META":
            mes = date.today().month
            if direction == "BAJISTA" and mes in (6, 5):
                return {
                    "is_premium": False,
                    "tier": "SKIP",
                    "reason": (
                        f"{'Junio' if mes == 6 else 'Mayo'} bajista META = SKIP (44-52%)"
                    ),
                    "warnings": [],
                    "stat_base": "",
                }
            if directo and dist >= 1.00:
                if triple == "TRIPLE_UP" and direction == "ALCISTA":
                    is_premium = True
                    reason = "ALC directo + triple UP + dist>$1"
                    stat_base = "Triple UP ALC META = 91.4% RECORD"
                elif triple == "TRIPLE_DOWN" and direction == "BAJISTA":
                    is_premium = True
                    reason = "BAJ directo + triple DOWN + dist>$1"
                    stat_base = "BAJ directo META = 89.7%"
                if n_caution == 3 and direction == "BAJISTA":
                    is_premium = True
                    reason = "BAJ directo + 3 caution"
                    stat_base = "3 CAUTION BAJ META = 84.4% premium"
            if escalo:
                is_premium = True
                reason += " + escalo"
                stat_base = "escalo META = 84.6% ALC / 85.7% BAJ"

        elif ticker == "NVDA":
            if direction == "BAJISTA":
                if dist < 0.50:
                    return {
                        "is_premium": False,
                        "tier": "SKIP",
                        "reason": f"BAJ dist ${dist:.2f} < $0.50 — sin edge (54.1%)",
                        "warnings": [],
                        "stat_base": "",
                    }
                if directo and dist >= 0.50:
                    is_premium = True
                    reason = f"BAJ directo + dist ${dist:.2f}"
                    if dist >= 1.00:
                        stat_base = "dist $1-2 BAJ NVDA = 84.4%"
                    else:
                        stat_base = "BAJ directo NVDA = 94.2%"
                    if n_caution == 1:
                        reason += " + 1 caution BAJ"
                        stat_base = "1 CAUTION BAJ NVDA = 84.6% premium"
            elif direction == "ALCISTA":
                if directo and dist >= 0.50:
                    is_premium = True
                    reason = f"ALC directo + dist ${dist:.2f}"
                    stat_base = "ALC directo NVDA = 87.0%"
                    if badge >= 80:
                        reason += " + badge alto"

        elif ticker == "TSLA":
            mes = date.today().month
            if direction == "BAJISTA" and mes == 6:
                return {
                    "is_premium": False,
                    "tier": "SKIP",
                    "reason": "Junio bajista TSLA = SKIP (50%)",
                    "warnings": [],
                    "stat_base": "",
                }
            if direction == "ALCISTA" and prev_day < -5:
                return {
                    "is_premium": False,
                    "tier": "SKIP",
                    "reason": "prev_day < -5% + alcista TSLA = SKIP (33.3%)",
                    "warnings": [],
                    "stat_base": "",
                }
            if directo:
                if dist >= 2.00:
                    is_premium = True
                    reason = f"directo + dist ${dist:.2f} zona óptima"
                    stat_base = "dist $2-5 TSLA = 86-90%"
                elif dist >= 1.00 and triple in ["TRIPLE_UP", "TRIPLE_DOWN"]:
                    is_premium = True
                    reason = f"directo + triple + dist ${dist:.2f}"
                    stat_base = "Triple TSLA = 85.4%/86.8%"
                elif dist >= 0.50:
                    reason = f"directo + dist ${dist:.2f} — tamaño normal"
                    stat_base = "TSLA directo = 98.3%/98.8%"

        elif ticker == "AMD":
            dia = date.today().weekday()
            if direction == "ALCISTA" and dia == 0:
                warnings.append(
                    "LUNES AMD ALCISTA = dia debil (66.2%) — reducir size"
                )
            if direction == "BAJISTA" and directo:
                if n_caution == 0:
                    is_premium = True
                    reason = "BAJ directo + 0 caution"
                    stat_base = "0 CAUTION BAJ AMD = 85.3% PREMIUM"
                elif escalo:
                    # escalo BAJ AMD: 66.3% INT — no es premium según 1v3
                    # Solo marcar si n_caution=4 (regla universal ya lo captura arriba)
                    reason = "BAJ directo + escalo AMD — solo INT"
                    stat_base = "escalo BAJ AMD = 66.3% INT (1v3) — target solo INT"
                elif triple == "TRIPLE_DOWN":
                    is_premium = True
                    reason = "BAJ directo + triple DOWN"
                    stat_base = "Triple DOWN BAJ AMD = 84.3%"

        elif ticker == "SPY":
            if direction == "BAJISTA" and directo:
                is_premium = True
                reason = "BAJ directo SPY"
                stat_base = "BAJ directo SPY = 97.7% | rebote<=1.50 = 100%"
                if n_caution == 0:
                    reason += " + 0 caution"
                    stat_base = "0 caution ALC SPY = 77.1%"
                elif escalo:
                    reason += " + escalo BAJ"
                    stat_base = (
                        "escalo BAJ SPY = 71.4% INT / 55.2% MAX — "
                        "mejor toco_max BAJ escalo del sistema"
                    )
            elif direction == "ALCISTA" and directo:
                if n_caution == 0:
                    is_premium = True
                    reason = "ALC directo + 0 caution"
                    stat_base = "0 caution ALC SPY = 77.1%"
                elif escalo:
                    # escalo ALC = operable en datos; premarket sin 1v3 sigue siendo contexto débil
                    reason = "ALC directo + escalo SPY (premarket)"
                    stat_base = "escalo ALC SPY = 62.4% INT — reducir size en premarket"
                if gap_type == "GAP_DOWN":
                    is_premium = True
                    reason += " + GAP_DOWN alcista"
                    stat_base = "GAP_DOWN ALC SPY = 79.7%"

        elif ticker == "MSFT":
            if direction == "BAJISTA" and directo:
                if n_caution == 4:
                    is_premium = True
                    reason = "BAJ directo + 4 CAUTION"
                    stat_base = "4 CAUTION BAJ MSFT = 85.5% PREMIUM"
                elif escalo:
                    # escalo BAJ MSFT = 65.7% INT según 1v3 — operable pero no premium
                    reason = "BAJ directo + escalo MSFT — solo INT"
                    stat_base = (
                        "escalo BAJ MSFT = 65.7% INT (1v3) — target INT, regla 30min estricta"
                    )
                elif dist >= 1.00 and triple == "TRIPLE_DOWN":
                    is_premium = True
                    reason = f"BAJ directo + triple DOWN + dist ${dist:.2f}"
                    stat_base = "BAJ directo MSFT = 97.2%"

        elif ticker == "AAPL":
            mes = date.today().month
            if direction == "BAJISTA" and mes in (2, 4, 6, 12):
                return {
                    "is_premium": False,
                    "tier": "SKIP",
                    "reason": f"Mes {mes} bajista AAPL = SKIP (52-55%)",
                    "warnings": [],
                    "stat_base": "",
                }
            if direction == "BAJISTA":
                if triple != "TRIPLE_DOWN":
                    return {
                        "is_premium": False,
                        "tier": "SKIP",
                        "reason": "BAJ AAPL requiere TRIPLE DOWN (2 TFs = 35.6% SKIP)",
                        "warnings": [],
                        "stat_base": "",
                    }
                if dist < 0.50:
                    return {
                        "is_premium": False,
                        "tier": "SKIP",
                        "reason": f"BAJ dist ${dist:.2f} < $0.50 AAPL = SKIP (53.1%)",
                        "warnings": [],
                        "stat_base": "",
                    }
            if direction == "ALCISTA" and directo:
                if triple == "TRIPLE_UP":
                    is_premium = True
                    reason = "ALC directo + triple UP"
                    stat_base = "Triple UP ALC AAPL = 84.2%"
                elif n_caution == 0:
                    is_premium = True
                    reason = "ALC directo + 0 caution"
                    stat_base = "0 caution ALC AAPL = 81.4%"
            elif direction == "BAJISTA" and directo and n_caution == 1:
                is_premium = True
                reason = "BAJ + 1 caution (premium AAPL único)"
                stat_base = "1 CAUTION BAJ AAPL = 79.6% (vs 59% TSLA/SPY)"

        elif ticker == "AMZN":
            if direction == "BAJISTA":
                if triple == "TRIPLE_DOWN":
                    if escalo:
                        is_premium = True
                        reason = "BAJ directo + triple DOWN + escalo"
                        stat_base = "escalo BAJ AMZN = 88.7% | triple DOWN = 87.9%"
                    else:
                        is_premium = True
                        reason = "BAJ directo + triple DOWN"
                        stat_base = "Triple DOWN BAJ AMZN = 87.9%"

        elif ticker == "DIA":
            if direction == "BAJISTA" and directo:
                if n_caution == 4:
                    is_premium = True
                    reason = "BAJ directo + 4 CAUTION cuadruple"
                    stat_base = "4 CAUTION BAJ DIA = 83.5% PREMIUM"
                elif dist >= 1.00:
                    is_premium = True
                    reason = f"BAJ directo + dist ${dist:.2f}"
                    stat_base = "BAJ directo DIA = 98.7% | dist $1-2 = 88.6%"
            if escalo:
                warnings.append(
                    "escalo DIA = REDUCIR size — 1v3: escalo BAJ DIA operable (64.7% INT), no premium"
                )

        elif ticker == "TLT":
            if directo:
                if escalo and direction == "ALCISTA":
                    is_premium = True
                    reason = "ALC sale ZM + escalo"
                    stat_base = "escalo ALC TLT = 93.0%"
                elif direction == "ALCISTA" and n_caution >= 3:
                    is_premium = True
                    reason = f"ALC sale ZM + {n_caution} caution"
                    stat_base = "3-4 CAUTION ALC TLT = 89-94%"
                elif direction == "BAJISTA" and directo:
                    is_premium = True
                    reason = "BAJ directo TLT sale ZM"
                    stat_base = "BAJ directo TLT = 93.8%"

        elif ticker == "IWM":
            mes = date.today().month
            if direction == "ALCISTA" and mes == 9:
                return {
                    "is_premium": False,
                    "tier": "SKIP",
                    "reason": "Septiembre alcista IWM = SKIP ABSOLUTO (39.1%)",
                    "warnings": [],
                    "stat_base": "",
                }
            if direction == "BAJISTA":
                if dist < 1.00:
                    return {
                        "is_premium": False,
                        "tier": "SKIP",
                        "reason": f"IWM dist ${dist:.2f} < $1.00 — sin edge (57-59%)",
                        "warnings": [],
                        "stat_base": "",
                    }
                if escalo:
                    is_premium = True
                    reason = "BAJ directo + escalo"
                    stat_base = "escalo BAJ IWM = 80.6% NUEVO v20"
                elif n_caution == 4:
                    is_premium = True
                    reason = "BAJ directo + 4 CAUTION"
                    stat_base = "4 CAUTION BAJ IWM = 78.6%"
            if direction == "ALCISTA":
                warnings.append(
                    "IWM alcista INT 62.4% — el más bajo del sistema"
                )

        elif ticker == "GOOGL":
            mes = date.today().month
            if direction == "ALCISTA" and mes == 2:
                return {
                    "is_premium": False,
                    "tier": "SKIP",
                    "reason": "FEBRERO ALCISTA GOOGL = SKIP ABSOLUTO (33.3%)",
                    "warnings": [],
                    "stat_base": "",
                }
            return {
                "is_premium": False,
                "tier": "CONF",
                "reason": "GOOGL = solo confirmador (BAJ INT 63.9% más bajo sistema)",
                "warnings": warnings,
                "stat_base": "",
            }

        elif ticker == "COIN":
            mes = date.today().month

            # Abril ALC COIN = 92% INT — operable, mes premium
            if direction == "ALCISTA" and mes == 4:
                is_premium = True
                reason = "Abril ALC COIN — mes premium (92% INT)"
                stat_base = "Abril ALC COIN = 92% INT / 74% MAX — operar normal"

            # WARNING: Octubre bajista (94% INT — mes más débil BAJ)
            if direction == "BAJISTA" and mes == 10:
                warnings.append("Octubre BAJ COIN = 94% INT — reducir size x0.5")

            # PREMIUM ALC
            if direction == "ALCISTA":
                if n_caution == 0:
                    is_premium = True
                    reason = "ALC + 0 caution"
                    stat_base = "0 caution ALC COIN = 100% INT (n=59)"
                elif n_caution == 3:
                    is_premium = True
                    reason = "ALC + 3 caution premium"
                    stat_base = "3 caution ALC COIN = 100% INT (n=42)"
                elif directo and dist >= 0.25:
                    is_premium = True
                    reason = f"ALC directo + dist ${dist:.2f}"
                    if dist >= 1.00:
                        stat_base = "dist >$1 ALC COIN = 99% INT, 79% MAX"
                    elif dist >= 0.50:
                        stat_base = "dist $0.50-1 ALC COIN = 100% INT, 73% MAX"
                    else:
                        stat_base = "dist corta ALC COIN = 100% INT, 76% MAX"
                if gap_type == "GAP_UP" and is_premium:
                    reason += " + GAP_UP"
                    stat_base = "GAP_UP ALC COIN = 99% INT, 77% MAX"

            # PREMIUM BAJ
            elif direction == "BAJISTA":
                if n_caution == 0:
                    is_premium = True
                    reason = "BAJ + 0 caution"
                    stat_base = "0 caution BAJ COIN = 100% INT (n=87)"
                elif n_caution >= 3:
                    is_premium = True
                    reason = f"BAJ + {n_caution} caution"
                    stat_base = f"{'3' if n_caution == 3 else '4'} caution BAJ COIN = 100%/99% INT"
                elif directo and dist >= 0.50:
                    is_premium = True
                    reason = f"BAJ directo + dist ${dist:.2f}"
                    stat_base = "dist $0.50+ BAJ COIN = 98% INT, 66-69% MAX"
                elif directo:
                    is_premium = True
                    reason = f"BAJ directo + dist ${dist:.2f}"
                    stat_base = "BAJ COIN = 97% INT, 57% MAX"

        elif ticker == "PLTR":
            mes = date.today().month
            dia = date.today().weekday()  # 0=Lun, 1=Mar, 2=Mié, 3=Jue, 4=Vie

            # SKIP: Junio alcista (89% INT — mes más débil ALC)
            if direction == "ALCISTA" and mes == 6:
                return {
                    "is_premium": False,
                    "tier": "SKIP",
                    "reason": "Junio alcista PLTR = 89% INT — SKIP",
                    "warnings": [],
                    "stat_base": "",
                }

            # WARNING: Agosto ambas dirs (92% INT)
            if mes == 8:
                warnings.append("Agosto PLTR = 92% INT ambas dirs — reducir size")

            # WARNING: Miércoles ALC (92% INT — día más débil ALC)
            if direction == "ALCISTA" and dia == 2:
                warnings.append("Miércoles ALC PLTR = 92% INT — día más débil")

            # PREMIUM BAJ: el más confiable del sistema en acciones individuales
            if direction == "BAJISTA":
                if dist >= 0.50:
                    is_premium = True
                    reason = f"BAJ directo + dist ${dist:.2f}"
                    if dist >= 1.00:
                        stat_base = "dist >$1 BAJ PLTR = 100% INT, 68% MAX"
                    else:
                        stat_base = "dist $0.50-1 BAJ PLTR = 100% INT, 77% MAX — RECORD"
                    if dia in (0, 1, 3):  # Lun, Mar, Jue
                        reason += " + día premium BAJ"
                        stat_base += " | Lun/Mar/Jue BAJ = 100%"
                    if n_caution in (0, 1, 2):
                        reason += f" + {n_caution} caution"
                        stat_base = f"{n_caution} caution BAJ PLTR = 100% INT"
                    if gap_type == "GAP_UP":
                        reason += " + GAP_UP BAJ (trampa)"
                        stat_base = "GAP_UP BAJ PLTR = 100% INT — trampa perfecta"
                elif directo:
                    is_premium = True
                    reason = f"BAJ directo + dist ${dist:.2f}"
                    stat_base = "dist corta BAJ PLTR = 96% INT, 69% MAX"

            # PREMIUM ALC
            elif direction == "ALCISTA":
                if n_caution == 0:
                    is_premium = True
                    reason = "ALC + 0 caution PERFECTO"
                    stat_base = "0 caution ALC PLTR = 100% INT (n=38) — RECORD acciones"
                elif dist >= 1.00:
                    is_premium = True
                    reason = f"ALC directo + dist ${dist:.2f}"
                    stat_base = "dist >$1 ALC PLTR = 98% INT, 75% MAX"
                elif directo:
                    is_premium = True
                    reason = f"ALC directo + dist ${dist:.2f}"
                    stat_base = "dist $0.25-1 ALC PLTR = 94-95% INT"
                if dia == 1 and is_premium:  # Martes
                    reason += " + Martes ALC"
                    stat_base += " | Martes ALC = 98%"
                if mes in (4, 5, 7) and is_premium:
                    reason += " + mes premium"
                    stat_base += f" | {'Abril' if mes == 4 else 'Mayo' if mes == 5 else 'Julio'} ALC = 100%"

        elif ticker == "AVGO":
            mes = date.today().month
            dia = date.today().weekday()  # 0=Lun, 1=Mar, 2=Mié

            # SKIP: Octubre bajista — único mes SKIP de AVGO (83% INT)
            if direction == "BAJISTA" and mes == 10:
                return {
                    "is_premium": False,
                    "tier": "SKIP",
                    "reason": "Octubre bajista AVGO = 83% INT — único SKIP",
                    "warnings": [],
                    "stat_base": "",
                }

            # WARNING: Lunes — día más débil (94% ALC / 96% BAJ)
            if dia == 0:
                warnings.append("Lunes AVGO = día más débil — reducir size x0.5")

            # WARNING: Noviembre ALC (93% INT)
            if direction == "ALCISTA" and mes == 11:
                warnings.append("Noviembre ALC AVGO = 93% INT — reducir size")

            # PREMIUM ALC
            if direction == "ALCISTA":
                if escalo:
                    is_premium = True
                    reason = "ALC + escalo = 100% RECORD sistema"
                    stat_base = "escalo ALC AVGO = 100% INT (n=74) — RECORD nuevos tickers"
                elif dist >= 1.00:
                    is_premium = True
                    reason = f"ALC directo + dist ${dist:.2f}"
                    stat_base = "dist >$1 ALC AVGO = 100% INT, 79% MAX"
                elif directo:
                    is_premium = True
                    reason = f"ALC directo + dist ${dist:.2f}"
                    stat_base = "ALC AVGO = 98% INT, 59-64% MAX"
                if n_caution == 4 and is_premium:
                    reason += " + 4 caution"
                    stat_base = "4 caut ALC AVGO = 98% INT uniforme"
                if dia == 1 and is_premium:  # Martes
                    reason += " + Martes"
                    stat_base += " | Martes ALC = 100%"
                if mes in (2, 4, 5, 7, 8, 9) and is_premium:
                    reason += " + mes premium"
                    stat_base += " | mes 100% ALC"

            # PREMIUM BAJ
            elif direction == "BAJISTA":
                if n_caution == 4:
                    is_premium = True
                    reason = "BAJ + 4 caution cuadruple"
                    stat_base = "4 caut BAJ AVGO = 100% INT (n=77)"
                elif dist >= 1.00:
                    is_premium = True
                    reason = f"BAJ directo + dist ${dist:.2f}"
                    stat_base = "dist >$1 BAJ AVGO = 100% INT, 80% MAX"
                elif directo:
                    is_premium = True
                    reason = f"BAJ directo + dist ${dist:.2f}"
                    stat_base = "BAJ AVGO = 97.6% INT, 59-66% MAX"
                if gap_type == "GAP_UP" and is_premium:
                    reason += " + GAP_UP BAJ (trampa alcista)"
                    stat_base = "GAP_UP BAJ AVGO = 98% INT, 75% MAX"
                if dia in (1, 2) and is_premium:  # Mar, Mié
                    reason += " + día premium"
                    stat_base += " | Mar/Mié BAJ = 100%"
                if mes in (2, 3, 6, 7, 8, 9, 11) and is_premium:
                    reason += " + mes premium BAJ"
                    stat_base += " | mes 100% BAJ"

        open_zone_u = ticker_data.get("open_zone")
        bp_antes_u = ticker_data.get("bp_antes_estimado")
        if open_zone_u and open_zone_u != "doble_dir_zone":
            zone_hint = ZONE_SIZE_HINT.get((open_zone_u, bp_antes_u))
            if not zone_hint:
                zone_hint = ZONE_SIZE_HINT.get((open_zone_u, None), "")
            if zone_hint:
                warnings.append(f"Zona: {zone_hint}")
        for w_rule in _OPEN_ZONE_WARN_RULES:
            if _open_zone_skip_match(
                ticker, open_zone_u, caution_1v3, direction, gap_type, w_rule
            ):
                warnings.append(f"⚠ {w_rule[4]}")
        if (
            open_zone_u == "normal_t3"
            and direction == "ALCISTA"
            and ticker in ALC_T3_SOLO_INT_TICKERS
        ):
            warnings.append(
                f"⚠ {ticker} ALC t3: MAX anómalo (~25-29%) — target SOLO INT_POS, no MAX"
            )

        if ticker in TIER1:
            tier = "T1"
        elif ticker in TIER2:
            tier = "T2"
        else:
            tier = "CONF"

        return {
            "is_premium": is_premium,
            "tier": tier,
            "reason": reason,
            "warnings": warnings,
            "stat_base": stat_base,
        }


def _score_label(score: float, ec_accion: str) -> str:
    """Convierte score numérico a etiqueta legible para el trader."""
    if score == -1.0 or ec_accion == "BLOQUEADO":
        return "BLOQUEADO"
    if ec_accion == "ZONA_MUERTA":
        return "zona neutra"
    if score >= 9.0:
        return "★★ MÁXIMA CONVICCIÓN"
    if score >= 7.0:
        return "★ PREMIUM"
    if score >= 5.0:
        return "operable"
    return "no operar"


def _dir_label(direction: str) -> str:
    """Convierte dirección técnica a etiqueta operativa."""
    if direction == "ALCISTA":
        return "LONG (comprar)"
    if direction == "BAJISTA":
        return "SHORT (vender)"
    if direction == "ZONA_MUERTA":
        return "ZONA NEUTRA — esperar primer tick"
    return direction


def _score_detail(
    ema_align: str,
    gap_type: str,
    gap_pct: float,
    prev_day: float,
    n_caut: int,
    caution_1v3: str | None,
    direction: str,
    ticker: str,
) -> str:
    """Genera línea de desglose del score en español simple."""
    parts = []

    # EMA
    if ema_align == "bajo_3" and direction == "BAJISTA":
        parts.append("tendencia a favor +2.0")
    elif ema_align == "sobre_3" and direction == "ALCISTA":
        parts.append("tendencia a favor +2.0")
    elif ema_align in ("entre", "at_any"):
        parts.append("tendencia neutral -0.5")

    # PrevDay
    if abs(prev_day) < 1.0:
        parts.append(f"día anterior tranquilo ({prev_day:+.1f}%) +1.5")
    elif prev_day > 3.0 and direction == "ALCISTA":
        parts.append(f"día anterior muy fuerte ({prev_day:+.1f}%) -2.0")

    # Milton
    if n_caut == 0:
        parts.append("0 cautiones Milton +1.5")
    elif n_caut == 4 and direction == "BAJISTA":
        parts.append("4 cautiones bajistas +1.5")

    # 1v3
    if caution_1v3 == "igual" and direction == "ZONA_MUERTA":
        parts.append("1v3 confirmó zona neutra +3.0")
    elif caution_1v3 == "escalo" and direction == "ALCISTA":
        parts.append("1v3 escalo ALC — operable (83-100% INT, score neutro)")
    elif caution_1v3 in ("igual", "nuevo") and direction != "ZONA_MUERTA":
        parts.append(f"1v3={caution_1v3}")

    return " | ".join(parts) if parts else ""


def _caution_note_short(full_note: str, max_len: int = 40) -> str:
    """Etiquetas compactas tipo BBT 1H BBB 5M para tablas."""
    if not full_note or full_note.strip() == "Sin caution":
        return "Sin caution"
    found = re.findall(r"(BBT|BBB)\s+(\d+[MH])", full_note)
    if found:
        return " ".join(f"{a} {b}" for a, b in found)
    one = full_note.replace("\n", " ").strip()
    return one if len(one) <= max_len else one[: max_len - 3] + "..."


DATA_DIR = Path(__file__).parent / "data" / "processed"


def _load_parquet_as_bars(ticker: str, tf: str) -> list:
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


def run_verify_mode(ticker: str) -> None:
    """Últimos 30 días hábiles: BP, targets; open 9:30 + caution_real_930 + 1v3 (parquets)."""
    from pandas.tseries.offsets import BDay

    end_ts = pd.Timestamp.now(tz=TIMEZONE).normalize()
    bdays_idx = pd.bdate_range(end=end_ts, periods=30, freq="B")
    trading_days: list[date] = [t.date() for t in bdays_idx]

    all_tf: dict[str, list] = {}
    for tf in TF_LIST:
        all_tf[tf] = _load_parquet_as_bars(ticker, tf)
        if not all_tf[tf]:
            print(f"Parquet no encontrado: {ticker}_{tf}.parquet")
            print(f"Buscando en: {DATA_DIR}")
            return

    # Para daily usar también el parquet 1d
    daily_bars = _load_parquet_as_bars(ticker, "1d")
    if not daily_bars:
        # Fallback: derivar daily desde 1h si existe
        bars_1h = _load_parquet_as_bars(ticker, "1h")
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
    all_tf_open[mid] = _load_parquet_as_bars(ticker, mid)
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
        print(
            f"[verify] Sin parquet mid ({mid}) o 1d: caution_real_930 / 1v3 omitidos — {DATA_DIR}"
        )
    elif tfs_rth_once is None:
        print("[verify] build_tfs_rth None — 1v3 omitido")

    rows_ok = []
    ec_verify_rows: list[tuple] = []
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
        badge_long = compute_badge_long(bars_dict, cutoff_et)
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
                print(f"  [verify 1v3] {D}: {exc}")

        # ── EMA 20 / 50 / 200 — barras 1H premarket (régimen real) ─
        bars_1m_for_ema = _filter_bars_before(
            bars_dict.get("1m", []), cutoff_et
        )
        bars_1h_for_ema = _filter_bars_before(
            all_tf["1h"], cutoff_et
        )
        closes_ema = pd.Series(
            [float(b["c"]) for b in bars_1h_for_ema], dtype=float
        )
        ema20_pre  = None
        ema50_pre  = None
        ema200_pre = None
        if len(closes_ema) >= 20:
            ema20_pre = round(
                float(closes_ema.ewm(span=20,  adjust=False).mean().iloc[-1]), 4
            )
        if len(closes_ema) >= 50:
            ema50_pre = round(
                float(closes_ema.ewm(span=50,  adjust=False).mean().iloc[-1]), 4
            )
        if len(closes_ema) >= 200:
            ema200_pre = round(
                float(closes_ema.ewm(span=200, adjust=False).mean().iloc[-1]), 4
            )
        # NOTA: bars_1m_for_ema sigue siendo necesario para bb_expande_premarket

        # ── bb_expande_premarket ────────────────────────────────
        bb_expande_premarket = None
        bars_1m_prev = _filter_bars_upto_inclusive(
            bars_dict.get("1m", []), cutoff_prev_1600
        )
        if len(bars_1m_for_ema) >= N_VELAS_BB and \
           len(bars_1m_prev)    >= N_VELAS_BB:
            bbt_now,  bbb_now  = _compute_bb_from_closes(
                [b["c"] for b in bars_1m_for_ema]
            )
            bbt_prev, bbb_prev = _compute_bb_from_closes(
                [b["c"] for b in bars_1m_prev]
            )
            if all(v is not None
                   for v in [bbt_now, bbb_now, bbt_prev, bbb_prev]):
                bb_expande_premarket = (
                    (bbt_now - bbb_now) > (bbt_prev - bbb_prev)
                )

        # ── Conviction Score EC ─────────────────────────────────
        ema_align = "unknown"
        ec_score  = None
        ec_accion = "SIN_DATOS"
        ec_size   = 1.0

        precio_ref = open_price if open_price is not None else precio_premarket

        # Cuando hay 1v3 disponible, usar siempre la dirección real al abrir
        # no la dirección premarket. Esto evita el bug donde direction_ec
        # puede ser premarket cuando el precio cambió de lado al abrir.
        _dir_for_score = (
            direction_from_dist(open_price - bp)
            if open_price is not None and c1v3 is not None
            else direction_ec
        )

        if all(v is not None for v in [ema20_pre, ema50_pre, ema200_pre]):
            ema_align = ema_alignment(
                precio_ref, ema20_pre, ema50_pre, ema200_pre
            )
            _n_caut_ec = (
                caution_full.count("BBT") + caution_full.count("BBB")
                if caution_full
                else 0
            )
            ec_score = conviction_score(
                _dir_for_score,
                gap_type,
                abs(gap_pct),
                prev_day_change_pct,
                ema_align,
                bb_expande_premarket,
                n_cautiones=_n_caut_ec,
                caution_1v3=c1v3,
                ticker=ticker,
                open_930=open_price,
                int_pos=targets["int_pos"],
                int_neg=targets["int_neg"],
            )
            if ec_score == -1.0:
                ec_accion = "BLOQUEADO"
                ec_size   = 0.0
            elif _dir_for_score == "ZONA_MUERTA" and c1v3 not in ("igual", "nuevo"):
                ec_accion = "ZONA_MUERTA"
                ec_size   = 0.0
            else:
                sc_int = round(ec_score)
                if sc_int <= 4:
                    ec_accion = "SKIP"
                    ec_size   = 0.0
                elif sc_int >= 7:
                    ec_accion = "SIZE_x2"
                    ec_size   = 2.0
                else:
                    ec_accion = "SIZE_x1"
                    ec_size   = 1.0

        if ec_score is None and _dir_for_score == "ZONA_MUERTA":
            ec_accion = "ZONA_MUERTA"
            ec_size   = 0.0

        ec_verify_rows.append(
            (D.strftime("%Y-%m-%d"), ema_align, ec_score, ec_accion)
        )

        rows_ok.append(
            (
                D.strftime("%Y-%m-%d"),
                bp,
                signals_str,
                caution_disp,
                badge_long,
                targets["int_pos"],
                targets["int_neg"],
                targets["max_pos"],
                targets["max_neg"],
                open_price,
                _short_caution_cell(cr930, 26),
                _short_caution_cell(c1v3, 14),
            )
        )

    # Tabla
    hdr = (
        f"{'fecha':<12} | {'BP':>8} | {'signals_3_9':<32} | {'caution_nota':<28} | "
        f"{'badge_long':>9} | {'INT_POS':>8} | {'INT_NEG':>8} | {'MAX_POS':>8} | {'MAX_NEG':>8}"
    )
    sep = "-" * 12 + "-|-" + "-" * 8 + "-|-" + "-" * 32 + "-|-" + "-" * 28 + "-|-" + "-" * 9 + "-|-" + "-" * 8 + "-|-" + "-" * 8 + "-|-" + "-" * 8 + "-|-" + "-" * 8
    print(hdr)
    print(sep)
    for row in rows_ok:
        (
            fdt,
            bp,
            sig,
            caut,
            badge,
            ip,
            ine,
            mp,
            mn,
            op930,
            cr_s,
            c13_s,
        ) = row
        sig_s = (sig[:29] + "..") if len(sig) > 31 else sig
        print(
            f"{fdt:<12} | {bp:8.2f} | {sig_s:<32} | {caut:<28} | "
            f"{badge:8.1f}% | {ip:8.2f} | {ine:8.2f} | {mp:8.2f} | {mn:8.2f}"
        )

    print()
    print("--- OPEN 9:30 + CAUTION (tipo3) + 1v3 — parquets (mismo pipeline que open_930) ---")
    hdr2 = (
        f"{'fecha':<12} | {'open_930':>9} | {'caution_real_930':<28} | {'caution_cambio_1v3':<16}"
    )
    sep2 = "-" * 12 + "-|-" + "-" * 9 + "-|-" + "-" * 28 + "-|-" + "-" * 16
    print(hdr2)
    print(sep2)
    for row in rows_ok:
        fdt = row[0]
        op930 = row[9]
        cr_s = row[10]
        c13_s = row[11]
        op_s = f"{op930:9.2f}" if op930 is not None else f"{'—':>9}"
        print(f"{fdt:<12} | {op_s} | {cr_s:<28} | {c13_s:<16}")

    print()
    print(f"Total días procesados: {len(trading_days)}")
    print(f"Días con caution activo: {caution_days}")
    print(f"Días sin datos (skip): {skipped}")

    print()
    print("--- SCORE DE CONVICCIÓN EC ---")
    print()
    print("Reglas: <=4=SKIP | 5-6=SIZE x1 | >=7=SIZE x2")
    print("Fuente: EMA20/50/200 + GAP + PrevDay + bb_expande")
    print()
    if ec_verify_rows:
        for fdt, ema_a, ecs, eca in ec_verify_rows:
            if ecs is not None:
                print(
                    f"{fdt} | ema_align={ema_a} | "
                    f"ec_score={ecs} | ec_accion={eca}"
                )
            else:
                print(
                    f"{fdt} | ema_align={ema_a} | "
                    f"ec_score=(sin datos) | ec_accion={eca}"
                )
    else:
        print(
            "(sin filas: ningún día con BP/barras 1m — "
            "misma causa que tabla vacía / skips)"
        )


# Cliente Alpaca (histórico)
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
    raise ValueError("ALPACA_API_KEY o ALPACA_SECRET_KEY no encontradas en .env")
alpaca_historical = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)

# Directorios de salida
os.makedirs("outputs", exist_ok=True)
os.makedirs("logs", exist_ok=True)

def log_exec_time(start_time, step_name):
    """Registra el tiempo de ejecución de un paso."""
    elapsed = time.time() - start_time
    with open(f"logs/execution_{datetime.now().date()}.log", "a") as f:
        f.write(f"{datetime.now()} - {step_name} completed in {elapsed:.2f}s\n")

class AlpacaHistoricalClient:
    """Cliente para obtener datos históricos via alpaca-py."""

    TF_MAP = {
        "1m": TimeFrame(1, TimeFrameUnit.Minute),
        "3m": TimeFrame(3, TimeFrameUnit.Minute),
        "5m": TimeFrame(5, TimeFrameUnit.Minute),
        "15m": TimeFrame(15, TimeFrameUnit.Minute),
        "30m": TimeFrame(30, TimeFrameUnit.Minute),
        "1h": TimeFrame(1, TimeFrameUnit.Hour),
        "1d": TimeFrame(1, TimeFrameUnit.Day),
    }

    @staticmethod
    def _to_utc(dt_val) -> datetime:
        if isinstance(dt_val, datetime):
            dt = dt_val
        else:
            # Expecta "YYYY-MM-DD"
            dt = datetime.fromisoformat(str(dt_val))
        if dt.tzinfo is None:
            dt = TIMEZONE.localize(dt)
        return dt.astimezone(UTC)

    @staticmethod
    def fetch_bars(ticker: str, timeframe: str, start_date, end_date):
        """Obtiene barras históricas y retorna lista de dicts normalizados."""
        start_time = time.time()
        if timeframe not in AlpacaHistoricalClient.TF_MAP:
            raise ValueError(f"Timeframe no soportado: {timeframe}")

        start_dt = AlpacaHistoricalClient._to_utc(start_date)
        end_dt = AlpacaHistoricalClient._to_utc(end_date)

        request = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=AlpacaHistoricalClient.TF_MAP[timeframe],
            start=start_dt,
            end=end_dt,
            feed=DataFeed.IEX,
            adjustment="raw",
        )
        resp = alpaca_historical.get_stock_bars(request)

        try:
            bars = resp[ticker]
        except (KeyError, TypeError):
            bars = []
        time.sleep(0.1)  # rate-limiting conservador para free plan
        log_exec_time(start_time, f"Fetch {ticker} {timeframe} bars (alpaca)")

        out = []
        for bar in bars:
            ts = bar.timestamp
            if ts is None:
                continue
            ts = pd.Timestamp(ts)
            if ts.tzinfo is None:
                ts = ts.tz_localize(UTC)
            ts_et = ts.tz_convert(TIMEZONE)
            out.append(
                {
                    "t": ts_et,
                    "o": float(bar.open),
                    "h": float(bar.high),
                    "l": float(bar.low),
                    "c": float(bar.close),
                    "v": float(bar.volume),
                }
            )
        return out

    @staticmethod
    def get_prev_day_change(ticker: str) -> float:
        """Cambio porcentual close-to-close del día anterior."""
        start_time = time.time()
        end_date = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
        start_date = (datetime.now(TIMEZONE) - timedelta(days=10)).strftime("%Y-%m-%d")
        bars_1d = AlpacaHistoricalClient.fetch_bars(ticker, "1d", start_date, end_date)
        if len(bars_1d) < 2:
            return 0.0
        c_last = bars_1d[-1]["c"]
        c_prev = bars_1d[-2]["c"]
        change = ((c_last - c_prev) / c_prev * 100.0) if c_prev else 0.0
        log_exec_time(start_time, f"Prev day change {ticker} (alpaca)")
        return float(change)

    @staticmethod
    def get_macro_prices() -> dict:
        """Macroeconomics solo ETFs: QQQ, SPY, DIA.
        Retorna {ticker: {price: float, change: float}}."""
        out: dict = {}
        # Descargamos 2 daily bars para close-to-close.
        end_date = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
        start_date = (datetime.now(TIMEZONE) - timedelta(days=10)).strftime("%Y-%m-%d")

        for sym in TICKERS_MACRO:
            try:
                bars = AlpacaHistoricalClient.fetch_bars(sym, "1d", start_date, end_date)
                if len(bars) >= 2:
                    c_last = float(bars[-1]["c"])
                    c_prev = float(bars[-2]["c"])
                    change = ((c_last - c_prev) / c_prev * 100.0) if c_prev else 0.0
                    out[sym] = {"price": round(c_last, 2), "change": round(change, 2)}
                elif len(bars) == 1:
                    c_last = float(bars[-1]["c"])
                    out[sym] = {"price": round(c_last, 2), "change": 0.0}
                else:
                    out[sym] = {"price": 0.0, "change": 0.0}
            except Exception as e:
                out[sym] = {"price": 0.0, "change": 0.0, "error": str(e)}
        return out

class BPCalculator:
    """Calcula el Break Point (BP) usando EMAs multi-timeframe."""

    @staticmethod
    def calculate_ema(df: pd.DataFrame, span: int) -> pd.Series:
        """Calcula EMA sobre un DataFrame con columna 'c'."""
        return df["c"].ewm(span=span, adjust=False).mean()

    @staticmethod
    def calculate_bp(bars_dict, cutoff_time: pd.Timestamp | None = None):
        """Calcula BP como promedio de EMA3/EMA9 por timeframe.

        bars_dict: {"1m":[{"t":TimestampET,"c":float},...], ...}
        cutoff_time: filtra barras estrictamente anteriores (ts < cutoff_time).
        """
        start_time = time.time()
        ema3_list = []
        ema9_list = []

        for tf, bars in bars_dict.items():
            if not bars:
                continue

            if cutoff_time is not None:
                filtered = []
                for b in bars:
                    ts = b["t"]
                    if ts.tzinfo is None:
                        ts = pd.Timestamp(ts).tz_localize(TIMEZONE)
                    # cutoff es estricto: no-inclusivo
                    if ts < cutoff_time:
                        filtered.append(b)
                bars_filtered = filtered
            else:
                bars_filtered = bars

            if len(bars_filtered) < 3:
                continue

            closes = pd.Series([float(b["c"]) for b in bars_filtered], dtype=float)
            ema3 = closes.ewm(span=3, adjust=False).mean().iloc[-1]
            ema9 = closes.ewm(span=9, adjust=False).mean().iloc[-1]
            ema3_list.append(float(ema3))
            ema9_list.append(float(ema9))

        if not ema3_list:
            return None

        avg_ema3 = np.mean(ema3_list)
        avg_ema9 = np.mean(ema9_list)
        bp = (avg_ema3 + avg_ema9) / 2

        log_exec_time(start_time, "Calculate BP (alpaca)")
        return bp

class TargetsCalculator:
    """Calcula targets INT/MAX y aplica reglas de asimetría."""

    @staticmethod
    def calculate_range_3d(ticker, daily_bars):
        """Calcula el rango promedio de high-low de 3 días."""
        if not daily_bars:
            return 0.0
        last3 = daily_bars[-3:]
        avg_range = np.mean([float(b["h"]) - float(b["l"]) for b in last3])

        # Ajuste del rango usado
        return avg_range * 0.7 if avg_range > 5 else avg_range

    @staticmethod
    def calculate_targets(
        bp,
        range_3d,
        signals=None,
        *,
        caution_note=None,
        gap_type=None,
    ):
        """Calcula targets INT y MAX con asimetría opcional."""
        base_int = 0.33 * range_3d
        base_max = 0.66 * range_3d

        dist_int_pos = base_int
        dist_int_neg = base_int
        dist_max_pos = base_max
        dist_max_neg = base_max

        if caution_note is not None or gap_type is not None:
            parts = []
            if caution_note is not None:
                parts.append(str(caution_note))
            if gap_type is not None:
                parts.append(str(gap_type))
            sig_txt = " ".join(parts)
        else:
            # signals puede venir como string o lista; unificamos a str para substring.
            sig_txt = " ".join(signals) if isinstance(signals, list) else str(signals or "")

        # Asimetría sobre DISTANCIAS (no sobre precio absoluto).
        if "BBT" in sig_txt and "GAP_UP" in sig_txt:
            dist_int_pos *= 1.25
            dist_max_pos *= 1.25
        elif "BBB" in sig_txt and "GAP_DOWN" in sig_txt:
            dist_int_neg *= 1.25
            dist_max_neg *= 1.25

        return {
            "int_pos": bp + dist_int_pos,
            "int_neg": bp - dist_int_neg,
            "max_pos": bp + dist_max_pos,
            "max_neg": bp - dist_max_neg,
            "int_dist": base_int,
            "max_dist": base_max,
        }

class AlpacaWSCollector:
    """Colector en tiempo real via alpaca-py WebSocket (trades → 1m OHLCV).
    Si Alpaca falla (p. ej. HTTP 429), fallback a Finnhub WebSocket 9:13–9:28 ET."""

    def __init__(self):
        self.bars_1m = {ticker: [] for ticker in TICKERS}

    def _aggregate_bars_from_raw(self, raw_trades: dict) -> None:
        """Agrupa trades en barras 1m OHLCV (misma lógica que el acumulador Alpaca)."""
        self.bars_1m = {ticker: [] for ticker in TICKERS}
        for ticker, trades in raw_trades.items():
            if not trades:
                continue
            current_bar = None
            for t in sorted(trades, key=lambda x: x["t"]):
                minute_key = t["t"].floor("min")
                p = t["p"]
                v = t["v"]
                if current_bar is None or current_bar["t"] != minute_key:
                    if current_bar is not None:
                        self.bars_1m[ticker].append(current_bar)
                    current_bar = {
                        "t": minute_key,
                        "o": p,
                        "h": p,
                        "l": p,
                        "c": p,
                        "v": v,
                    }
                else:
                    current_bar["h"] = max(current_bar["h"], p)
                    current_bar["l"] = min(current_bar["l"], p)
                    current_bar["c"] = p
                    current_bar["v"] += v
            if current_bar is not None:
                self.bars_1m[ticker].append(current_bar)

    async def _collect_finnhub_fallback(self, start_time_et: datetime, end_time_et: datetime) -> None:
        import websockets as _ws
        import json as _json

        uri = f"wss://ws.finnhub.io?token={os.getenv('FINNHUB_API_KEY')}"
        START_MINUTE = 9 * 60 + 13
        raw_trades = {ticker: [] for ticker in TICKERS}

        for attempt in range(3):
            try:
                async with _ws.connect(uri) as ws:
                    for ticker in TICKERS:
                        await ws.send(
                            _json.dumps({"type": "subscribe", "symbol": ticker})
                        )
                    while True:
                        if datetime.now(TIMEZONE) >= end_time_et:
                            break
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=5)
                        except asyncio.TimeoutError:
                            continue
                        data = _json.loads(msg)
                        if data.get("type") == "trade":
                            for trade in data.get("data", []):
                                ts = (
                                    pd.to_datetime(trade["t"], unit="ms")
                                    .tz_localize("UTC")
                                    .tz_convert(TIMEZONE)
                                )
                                trade_min = ts.hour * 60 + ts.minute
                                if trade_min < START_MINUTE:
                                    continue
                                if ts < start_time_et or ts >= end_time_et:
                                    continue
                                ticker_sym = trade["s"]
                                if ticker_sym not in raw_trades:
                                    continue
                                raw_trades[ticker_sym].append(
                                    {
                                        "t": ts,
                                        "p": float(trade["p"]),
                                        "v": float(trade.get("v", 0)),
                                    }
                                )
                break
            except Exception as e:
                print(f"Finnhub WS intento {attempt + 1}/3 falló: {e}")
                await asyncio.sleep(2)

        self._aggregate_bars_from_raw(raw_trades)

    async def _collect_alpaca_ws(self, start_time_et: datetime, end_time_et: datetime) -> None:
        # Nota: StockDataStream.run() es bloqueante, así que lo corremos en un executor.
        stream = StockDataStream(
            ALPACA_API_KEY,
            ALPACA_SECRET_KEY,
            raw_data=True,
            feed=DataFeed.IEX,
        )

        current = {ticker: None for ticker in TICKERS}

        async def on_trade(trade):
            try:
                symbol = trade.get("symbol")
                if symbol not in self.bars_1m:
                    return
                ts = pd.Timestamp(trade.get("timestamp"))
                if ts.tzinfo is None:
                    ts = ts.tz_localize(UTC).tz_convert(TIMEZONE)
                else:
                    ts = ts.tz_convert(TIMEZONE)

                if ts < start_time_et or ts >= end_time_et:
                    return

                price = float(trade.get("price", 0.0))
                size = float(trade.get("size", 0.0) or 0.0)

                minute_key = ts.floor("min")
                bar = current[symbol]
                if bar is None or bar["t"] != minute_key:
                    if bar is not None:
                        self.bars_1m[symbol].append(bar)
                    bar = {
                        "t": minute_key,
                        "o": price,
                        "h": price,
                        "l": price,
                        "c": price,
                        "v": size,
                    }
                    current[symbol] = bar
                else:
                    bar["h"] = max(bar["h"], price)
                    bar["l"] = min(bar["l"], price)
                    bar["c"] = price
                    bar["v"] += size
            except Exception:
                return

        stream.subscribe_trades(on_trade, *TICKERS)

        async def stop_later():
            max_wait_sec = float(os.getenv("WS_MAX_WAIT_SEC", "0") or "0")
            start_wait = datetime.now(TIMEZONE)
            while datetime.now(TIMEZONE) < end_time_et:
                if max_wait_sec > 0:
                    elapsed = (datetime.now(TIMEZONE) - start_wait).total_seconds()
                    if elapsed >= max_wait_sec:
                        break
                await asyncio.sleep(0.5)
            await stream.stop_ws()

        loop = asyncio.get_running_loop()
        try:
            await asyncio.gather(
                loop.run_in_executor(None, stream.run),
                stop_later(),
            )
        finally:
            for symbol, bar in current.items():
                if bar is not None:
                    self.bars_1m[symbol].append(bar)

    async def collect_bars(self, start_time_et: datetime, end_time_et: datetime):
        try:
            await self._collect_alpaca_ws(start_time_et, end_time_et)
        except Exception as e:
            print(f"Alpaca WS falló ({e}), usando Finnhub fallback...")
            await self._collect_finnhub_fallback(start_time_et, end_time_et)

    def get_accumulated_bars(self):
        return self.bars_1m

class AlpacaSnapshotClient:
    """Obtiene precio de apertura (open 9:30) via barras minuto (feed=iex)."""

    @staticmethod
    def get_open_price(ticker: str) -> float | None:
        # Pedimos una ventana corta alrededor de 09:30 ET y tomamos el open de la primera vela.
        # En el resto del sistema, la decisión usa open_930 exacto del start de sesión regular.
        start_et = datetime.now(TIMEZONE).replace(hour=9, minute=30, second=0, microsecond=0)
        end_et = start_et + timedelta(minutes=2)
        start_utc = start_et.astimezone(UTC)
        end_utc = end_et.astimezone(UTC)

        request = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame(1, TimeFrameUnit.Minute),
            start=start_utc,
            end=end_utc,
            feed=DataFeed.IEX,
            adjustment="raw",
        )
        resp = alpaca_historical.get_stock_bars(request)
        try:
            bars = resp[ticker]
        except (KeyError, TypeError):
            bars = []
        if not bars:
            return None
        return float(bars[0].open)

class SkipFilter:
    """Aplica filtros de SKIP universales y específicos."""

    @staticmethod
    def apply_skip_filters(ticker, prev_day_change, gap_pct, signals):
        """Determina si un ticker debe ser omitido."""
        # Filtro universal 1
        if prev_day_change > 9.5 and "GAP_DOWN" in signals:
            return True

        # Filtro universal 3
        if gap_pct <= -5 and "GAP_DOWN" in signals:
            return True

        # Filtro específico para NVDA
        if ticker == "NVDA" and "BAJ" in signals and "BP" in signals:
            return True

        return False

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
    parts.append(
        " | ".join(
            f"{ticker}={data['macro'][ticker]['price']} ({data['macro'][ticker]['change']:.2f}%)"
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
                f"caution={_caution_note_short(td['caution_note'])}"
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
                    parts.append(tail + "\n")
                elif direction == "ALCISTA" and int_pos is not None and open_p is not None:
                    rec_i = abs(float(int_pos) - float(open_p))
                    tail = f"   → INT_POS={float(int_pos):.2f} (recorrido: ${rec_i:.2f})"
                    if max_pos is not None:
                        tail += f" | MAX_POS={float(max_pos):.2f}"
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
    return "".join(parts)


def write_json(data: dict, filename: str) -> None:
    _ensure_outputs_dir()
    with open(Path("outputs") / filename, "w", encoding="utf-8") as f:
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
            ec_accion  = td.get("ec_accion", "") or ""
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
                if 0 < (int_pos - open_930) < 0.60:
                    parts.append(f"  ⚠️  Recorrido corto al objetivo — solo ${int_pos-open_930:.2f}\n")
            elif direction == "BAJISTA" and open_930 and int_neg:
                if 0 < (open_930 - int_neg) < 0.60:
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

async def main():
    """Función principal que orquesta los 3 modos de ejecución."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--modo",
        required=True,
        choices=["premarket_9_15", "premarket_9_28", "open_930", "verify", "test"],
    )
    parser.add_argument(
        "--ticker",
        default=None,
        help="Obligatorio si --modo verify. Opcional: un solo ticker en modos live/test",
    )
    args = parser.parse_args()

    if args.modo == "verify":
        if not args.ticker:
            parser.error("--modo verify requiere --ticker (ej. NVDA)")
        run_verify_mode(args.ticker.strip().upper())
        return

    if args.ticker:
        t = args.ticker.strip().upper()
        if t not in TICKERS:
            parser.error(f"--ticker {t} no esta en TICKERS")
        tickers_to_run = [t]
    else:
        tickers_to_run = list(TICKERS)

    output_data = {
        "macro": {},
        "tickers": {},
        "skips": {},
        "tickers_order": tickers_to_run,
    }
    date_str = str(datetime.now(TIMEZONE).date())

    # Obtener datos macro
    output_data["macro"] = AlpacaHistoricalClient.get_macro_prices()

    # Modo 1: premarket_9_15
    if args.modo == "premarket_9_15":
        logger.info(f"=== MODO: {args.modo} | tickers: {len(tickers_to_run)} ===")
        for ticker in tickers_to_run:
            try:
                open_price: float | None = None  # en open_930 se asigna el open RTH
                # Obtener barras multi-timeframe
                bars_dict = {}
                _end_hist = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
                for tf in TF_LIST:
                    days_back = 365 if tf == "1h" else 2
                    bars = AlpacaHistoricalClient.fetch_bars(
                        ticker,
                        tf,
                        (
                            datetime.now(TIMEZONE) - timedelta(days=days_back)
                        ).strftime("%Y-%m-%d"),
                        _end_hist,
                    )
                    bars_dict[tf] = bars

                # ── Logging técnico: velas por TF ─────────────────────────────
                for tf, bars in bars_dict.items():
                    n = len(bars)
                    if n > 0:
                        first_ts = str(bars[0]["t"])[:16]
                        last_ts = str(bars[-1]["t"])[:16]
                    else:
                        first_ts = last_ts = "sin datos"
                    logger.debug(f"[{ticker}] {tf}: {n} velas | {first_ts} → {last_ts}")

                # Obtener barras diarias
                daily_bars = AlpacaHistoricalClient.fetch_bars(
                    ticker,
                    "1d",
                    (datetime.now(TIMEZONE) - timedelta(days=10)).strftime("%Y-%m-%d"),
                    datetime.now(TIMEZONE).strftime("%Y-%m-%d"),
                )

                # Calcular BP
                cutoff_et = datetime.now(TIMEZONE).replace(hour=9, minute=0, second=0, microsecond=0)
                bp = BPCalculator.calculate_bp(bars_dict, cutoff_time=pd.Timestamp(cutoff_et))
                if bp is not None:
                    logger.info(
                        f"[{ticker}] BP={round(bp,4)} | "
                        f"cutoff={cutoff_et.strftime('%H:%M:%S')}"
                    )
                else:
                    logger.warning(f"[{ticker}] BP=None — bars_dict vacío o insuficiente")
                    continue

                bars_dict["1d"] = _filter_bars_before(daily_bars, cutoff_et)

                today_date_et = datetime.now(TIMEZONE).date()
                prev_date_et = today_date_et - timedelta(days=1)

                # Daily bars SOLO de días previos (evita el “día de hoy” incompleto)
                daily_prev = [b for b in daily_bars if b["t"].date() < today_date_et]
                daily_prev.sort(key=lambda x: x["t"])

                prev_close = float(daily_prev[-1]["c"]) if daily_prev else 0.0
                if len(daily_prev) >= 2:
                    c_last = float(daily_prev[-1]["c"])
                    c_prev = float(daily_prev[-2]["c"])
                    prev_day_change_pct = ((c_last - c_prev) / c_prev * 100.0) if c_prev else 0.0
                else:
                    prev_day_change_pct = 0.0

                # Precio premarket en el corte (estricto < 09:00)
                bars_1m_pre = _filter_bars_before(bars_dict.get("1m", []), cutoff_et)
                if not bars_1m_pre:
                    continue
                precio_premarket = float(bars_1m_pre[-1]["c"])
                precio_pm_actual = precio_premarket

                # GAP (vs prev_close) preliminar
                if prev_close > 0:
                    gap_pct = ((precio_premarket - prev_close) / prev_close) * 100.0
                    gap_type = "GAP_UP" if gap_pct > 0 else ("GAP_DOWN" if gap_pct < 0 else "FLAT")
                else:
                    gap_pct = 0.0
                    gap_type = "FLAT"

                cutoff_prev_1600 = datetime(prev_date_et.year, prev_date_et.month, prev_date_et.day, 16, 0, 0, tzinfo=TIMEZONE)

                # Port full: signals_3/9, badge_long, caution_nota
                signals_str = compute_signals_3_9(bars_dict, cutoff_et)
                badge_long = compute_badge_long(bars_dict, cutoff_et)
                caution_note = compute_caution_note(
                    bars_dict, precio_premarket, cutoff_prev_1600, ticker=ticker
                )

                # Targets con asimetría basada en caution_nota + gap_type
                range_3d = TargetsCalculator.calculate_range_3d(ticker, daily_prev)
                targets = TargetsCalculator.calculate_targets(bp, range_3d, [caution_note, gap_type])

                # Aplicar filtros SKIP
                skip_reason = None
                if SkipFilter.apply_skip_filters(ticker, prev_day_change_pct, gap_pct, [gap_type]):
                    skip_reason = "Filtro SKIP activado"

                dist_pm = precio_premarket - bp
                direction_pm = direction_from_dist(dist_pm)

                logger.info(
                    f"[{ticker}] precio_pm={round(precio_pm_actual,4)} | "
                    f"dist_bp={round(dist_pm,4)} | "
                    f"dir={direction_pm} | gap={gap_type} {round(gap_pct,3)}%"
                )
                _pm_int_tgt = (
                    targets["int_pos"]
                    if direction_pm == "ALCISTA"
                    else targets["int_neg"]
                )
                _pm_recorrido = (
                    round(abs(precio_pm_actual - _pm_int_tgt), 4)
                    if _pm_int_tgt is not None
                    else None
                )
                logger.info(
                    f"[{ticker}] BP={round(bp, 4)} | "
                    f"INT_POS={round(targets['int_pos'], 4)} | "
                    f"INT_NEG={round(targets['int_neg'], 4)} | "
                    f"MAX_POS={round(targets['max_pos'], 4)} | "
                    f"MAX_NEG={round(targets['max_neg'], 4)} | "
                    f"recorrido_int={_pm_recorrido if _pm_recorrido is not None else 'N/A'}"
                )

                # ── EMA 20 / 50 / 200 — barras 1H premarket (régimen real) ─
                bars_1m_for_ema = _filter_bars_before(
                    bars_dict.get("1m", []), cutoff_et
                )
                bars_1h_for_ema = _filter_bars_before(
                    bars_dict.get("1h", []), cutoff_et
                )
                closes_ema = pd.Series(
                    [float(b["c"]) for b in bars_1h_for_ema], dtype=float
                )
                ema20_pre  = None
                ema50_pre  = None
                ema200_pre = None
                if len(closes_ema) >= 20:
                    ema20_pre = round(
                        float(closes_ema.ewm(span=20,  adjust=False).mean().iloc[-1]), 4
                    )
                if len(closes_ema) >= 50:
                    ema50_pre = round(
                        float(closes_ema.ewm(span=50,  adjust=False).mean().iloc[-1]), 4
                    )
                if len(closes_ema) >= 200:
                    ema200_pre = round(
                        float(closes_ema.ewm(span=200, adjust=False).mean().iloc[-1]), 4
                    )

                precio_ref_for_log = (
                    open_price if (open_price is not None) else precio_premarket
                )
                ema_align_for_log = (
                    ema_alignment(precio_ref_for_log, ema20_pre, ema50_pre, ema200_pre)
                    if all(v is not None for v in [ema20_pre, ema50_pre, ema200_pre])
                    else "unknown"
                )
                logger.info(
                    f"[{ticker}] EMAs 1h: velas={len(closes_ema)} | "
                    f"EMA20={ema20_pre} | EMA50={ema50_pre} | EMA200={ema200_pre} | "
                    f"align={ema_align_for_log}"
                )
                if ema200_pre is None:
                    logger.warning(
                        f"[{ticker}] EMA200 null — {len(closes_ema)} velas 1h "
                        f"(necesita ≥200) — score EC no se calculará"
                    )
                # NOTA: bars_1m_for_ema sigue siendo necesario para bb_expande_premarket

                # ── bb_expande_premarket ────────────────────────────────
                bb_expande_premarket = None
                bars_1m_prev = _filter_bars_upto_inclusive(
                    bars_dict.get("1m", []), cutoff_prev_1600
                )
                if len(bars_1m_for_ema) >= N_VELAS_BB and \
                   len(bars_1m_prev)    >= N_VELAS_BB:
                    bbt_now,  bbb_now  = _compute_bb_from_closes(
                        [b["c"] for b in bars_1m_for_ema]
                    )
                    bbt_prev, bbb_prev = _compute_bb_from_closes(
                        [b["c"] for b in bars_1m_prev]
                    )
                    if all(v is not None
                           for v in [bbt_now, bbb_now, bbt_prev, bbb_prev]):
                        bb_expande_premarket = (
                            (bbt_now - bbb_now) > (bbt_prev - bbb_prev)
                        )

                logger.info(
                    f"[{ticker}] caution='{caution_note}' | "
                    f"signals='{signals_str}' | "
                    f"badge={round(badge_long,1)}% | "
                    f"bb_expande={bb_expande_premarket}"
                )

                # ── Conviction Score EC ─────────────────────────────────
                ema_align = "unknown"
                ec_score  = None
                ec_accion = "SIN_DATOS"
                ec_size   = 1.0

                # precio de referencia: open_price en open_930,
                # precio_premarket en los otros dos modos
                precio_ref = (
                    open_price if (open_price is not None) else precio_premarket
                )

                if all(v is not None for v in [ema20_pre, ema50_pre, ema200_pre]):
                    ema_align = ema_alignment(
                        precio_ref, ema20_pre, ema50_pre, ema200_pre
                    )
                    _n_caut_ec = (
                        caution_note.count("BBT") + caution_note.count("BBB")
                        if caution_note
                        else 0
                    )
                    caution_1v3_ec = None  # 1v3 solo en open_930 / verify
                    ec_score = conviction_score(
                        direction_pm,
                        gap_type,
                        abs(gap_pct),
                        prev_day_change_pct,
                        ema_align,
                        bb_expande_premarket,
                        n_cautiones=_n_caut_ec,
                        caution_1v3=caution_1v3_ec,
                        ticker=ticker,
                        open_930=None,
                        int_pos=None,
                        int_neg=None,
                    )
                    if ec_score == -1.0:
                        ec_accion = "BLOQUEADO"
                        ec_size   = 0.0
                    elif direction_pm == "ZONA_MUERTA" and caution_1v3_ec not in ("igual", "nuevo"):
                        ec_accion = "ZONA_MUERTA"
                        ec_size   = 0.0
                    else:
                        sc_int = round(ec_score)
                        if sc_int <= 4:
                            ec_accion = "SKIP"
                            ec_size   = 0.0
                        elif sc_int >= 7:
                            ec_accion = "SIZE_x2"
                            ec_size   = 2.0
                        else:
                            ec_accion = "SIZE_x1"
                            ec_size   = 1.0

                if ec_score is None and direction_pm == "ZONA_MUERTA":
                    ec_accion = "ZONA_MUERTA"
                    ec_size   = 0.0

                if ec_score is None and ec_accion == "SIN_DATOS":
                    logger.warning(
                        f"[{ticker}] score EC = SIN_DATOS | "
                        f"ema20={ema20_pre} ema50={ema50_pre} ema200={ema200_pre}"
                    )
                else:
                    accion_label = {
                        "SIZE_x2": "★★ SIZE x2",
                        "SIZE_x1": "★ SIZE x1",
                        "SKIP": "✗ SKIP",
                        "ZONA_MUERTA": "~ ZM",
                        "BLOQUEADO": "⛔ BLOQUEADO",
                    }.get(ec_accion, ec_accion)
                    logger.info(
                        f"[{ticker}] score={ec_score} | {accion_label} | "
                        f"align={ema_align} | dir={direction_pm} | "
                        f"prev_day={round(prev_day_change_pct,2)}% | "
                        f"n_caut={_n_caut_ec if '_n_caut_ec' in dir() else '?'}"
                    )

                logger.debug(
                    f"[{ticker}] targets: "
                    f"INT+={round(targets['int_pos'],2)} INT-={round(targets['int_neg'],2)} | "
                    f"MAX+={round(targets['max_pos'],2)} MAX-={round(targets['max_neg'],2)} | "
                    f"range_3d={round(range_3d,3)}"
                )

                # Guardar datos
                output_data["tickers"][ticker] = {
                    "bp": bp,
                    "int_pos": targets["int_pos"],
                    "int_neg": targets["int_neg"],
                    "max_pos": targets["max_pos"],
                    "max_neg": targets["max_neg"],
                    "int_dist": targets["int_dist"],
                    "max_dist": targets["max_dist"],
                    "gap_type": gap_type,
                    "gap_pct": gap_pct,
                    "prev_day_change": prev_day_change_pct,
                    "signals": signals_str,
                    "signals_3_9": signals_str,
                    "badge_long": round(badge_long, 2),
                    "caution_note": caution_note,
                    "skip": skip_reason is not None,
                    "skip_reason": skip_reason,
                    "dist": dist_pm,
                    "direction": direction_pm,
                    "prev_day_hint": _get_prev_day_hint(
                        ticker,
                        float(prev_day_change_pct)
                        if prev_day_change_pct not in (None, "", "nan")
                        else None,
                        direction_pm,
                    ),
                    "cerro_bp_hint": _get_cerro_bp_hint(ticker, False, direction_pm),
                    "ema20_pre":            ema20_pre,
                    "ema50_pre":            ema50_pre,
                    "ema200_pre":           ema200_pre,
                    "ema_align":            ema_align,
                    "bb_expande_premarket": bb_expande_premarket,
                    "ec_score":             ec_score,
                    "ec_score_v2":          ec_score,
                    "ec_accion":            ec_accion,
                    "ec_size":              ec_size,
                }

            except Exception as e:
                logger.error(f"[{ticker}] Error procesando: {e}", exc_info=True)
                continue

        tickers_ok = output_data.get("tickers", {})
        skips_n = sum(1 for t in tickers_ok.values() if t.get("skip"))
        zm_n = sum(1 for t in tickers_ok.values() if t.get("ec_accion") == "ZONA_MUERTA")
        x2_n = sum(1 for t in tickers_ok.values() if t.get("ec_accion") == "SIZE_x2")
        x1_n = sum(1 for t in tickers_ok.values() if t.get("ec_accion") == "SIZE_x1")
        logger.info(
            f"=== MODO {args.modo} completado | "
            f"OK={len(tickers_ok)} | SKIP={skips_n} | ZM={zm_n} | "
            f"SIZE_x2={x2_n} | SIZE_x1={x1_n} ==="
        )
        finalize_dashboard_output(output_data)
        write_json(output_data, f"premarket_9_15_{date_str}.json")
        write_context_compressed(output_data, date_str, "premarket_9_15")
        write_ai_dashboard(output_data, date_str, "premarket_9_15")

    # Modo 2: premarket_9_28
    elif args.modo == "premarket_9_28":
        logger.info(f"=== MODO: {args.modo} | tickers: {len(tickers_to_run)} ===")
        # ── Barras premarket via histórico IEX (sin WebSocket) ──
        # IEX histórico entrega barras premarket del día en curso
        # sin restricción. Más confiable que WS en free tier.
        bars_by_ticker = {}
        for ticker in tickers_to_run:
            bars_by_ticker[ticker] = {}
            _end_hist = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
            for tf in TF_LIST:
                days_back = 365 if tf == "1h" else 2
                bars_by_ticker[ticker][tf] = AlpacaHistoricalClient.fetch_bars(
                    ticker,
                    tf,
                    (
                        datetime.now(TIMEZONE) - timedelta(days=days_back)
                    ).strftime("%Y-%m-%d"),
                    _end_hist,
                )

        for ticker in tickers_to_run:
            try:
                open_price: float | None = None  # en open_930 se asigna el open RTH
                bars_dict = bars_by_ticker[ticker]
                cutoff_et = datetime.now(TIMEZONE).replace(
                    hour=9, minute=28, second=0, microsecond=0
                )

                # ── Logging técnico: velas por TF (previo a filtros/cutoff) ──
                for tf, bars in bars_dict.items():
                    n = len(bars)
                    if n > 0:
                        first_ts = str(bars[0]["t"])[:16]
                        last_ts = str(bars[-1]["t"])[:16]
                    else:
                        first_ts = last_ts = "sin datos"
                    logger.debug(f"[{ticker}] {tf}: {n} velas | {first_ts} → {last_ts}")

                bars_1m_pre = _filter_bars_before(bars_dict.get("1m", []), cutoff_et)
                if not bars_1m_pre:
                    continue
                if not _validate_premarket_bars(
                    ticker, bars_dict.get("1m", []), cutoff_et
                ):
                    # Log pero no skip — BP con datos parciales
                    # es mejor que no tener BP
                    pass

                bp = BPCalculator.calculate_bp(
                    bars_dict, cutoff_time=pd.Timestamp(cutoff_et)
                )
                if bp is None:
                    logger.warning(f"[{ticker}] BP=None — bars_dict vacío o insuficiente")
                    continue

                today_date_et = datetime.now(TIMEZONE).date()
                prev_date_et = today_date_et - timedelta(days=1)

                # Daily bars previos (excluye el “día de hoy” incompleto)
                daily_bars = AlpacaHistoricalClient.fetch_bars(
                    ticker,
                    "1d",
                    (datetime.now(TIMEZONE) - timedelta(days=10)).strftime("%Y-%m-%d"),
                    datetime.now(TIMEZONE).strftime("%Y-%m-%d"),
                )
                bars_dict["1d"] = _filter_bars_before(daily_bars, cutoff_et)
                daily_prev = [b for b in daily_bars if b["t"].date() < today_date_et]
                daily_prev.sort(key=lambda x: x["t"])

                prev_close = float(daily_prev[-1]["c"]) if daily_prev else 0.0
                if len(daily_prev) >= 2:
                    c_last = float(daily_prev[-1]["c"])
                    c_prev = float(daily_prev[-2]["c"])
                    prev_day_change_pct = ((c_last - c_prev) / c_prev * 100.0) if c_prev else 0.0
                else:
                    prev_day_change_pct = 0.0

                precio_premarket = float(bars_1m_pre[-1]["c"])
                precio_pm_actual = precio_premarket
                logger.info(
                    f"[{ticker}] BP={round(bp,4)} | "
                    f"precio_pm={round(precio_pm_actual,4)} | "
                    f"cutoff={cutoff_et.strftime('%H:%M:%S')}"
                )

                # GAP (vs prev_close)
                if prev_close > 0:
                    gap_pct = ((precio_premarket - prev_close) / prev_close) * 100.0
                    gap_type = "GAP_UP" if gap_pct > 0 else ("GAP_DOWN" if gap_pct < 0 else "FLAT")
                else:
                    gap_pct = 0.0
                    gap_type = "FLAT"

                cutoff_prev_1600 = datetime(
                    prev_date_et.year,
                    prev_date_et.month,
                    prev_date_et.day,
                    16,
                    0,
                    0,
                    tzinfo=TIMEZONE,
                )

                # Port full: signals_3/9, badge_long y caution_nota
                signals_str = compute_signals_3_9(bars_dict, cutoff_et)
                badge_long = compute_badge_long(bars_dict, cutoff_et)
                caution_note = compute_caution_note(
                    bars_dict, precio_premarket, cutoff_prev_1600, ticker=ticker
                )

                # Targets con asimetría por caution_nota + gap_type
                range_3d = TargetsCalculator.calculate_range_3d(ticker, daily_prev)
                targets = TargetsCalculator.calculate_targets(bp, range_3d, [caution_note, gap_type])

                logger.debug(
                    f"[{ticker}] targets: "
                    f"INT+={round(targets['int_pos'],2)} INT-={round(targets['int_neg'],2)} | "
                    f"MAX+={round(targets['max_pos'],2)} MAX-={round(targets['max_neg'],2)} | "
                    f"range_3d={round(range_3d,3)}"
                )

                # Aplicar filtros SKIP
                skip_reason = None
                if SkipFilter.apply_skip_filters(ticker, prev_day_change_pct, gap_pct, [gap_type]):
                    skip_reason = "Filtro SKIP activado"

                dist_pm = precio_premarket - bp
                direction_pm = direction_from_dist(dist_pm)

                logger.info(
                    f"[{ticker}] precio_pm={round(precio_pm_actual,4)} | "
                    f"dist_bp={round(precio_pm_actual - bp, 4)} | "
                    f"dir={direction_pm} | gap={gap_type} {round(gap_pct,3)}%"
                )
                _pm28_int_tgt = (
                    targets["int_pos"]
                    if direction_pm == "ALCISTA"
                    else targets["int_neg"]
                )
                _pm28_recorrido = (
                    round(abs(precio_pm_actual - _pm28_int_tgt), 4)
                    if _pm28_int_tgt is not None
                    else None
                )
                logger.info(
                    f"[{ticker}] BP={round(bp, 4)} | "
                    f"INT_POS={round(targets['int_pos'], 4)} | "
                    f"INT_NEG={round(targets['int_neg'], 4)} | "
                    f"MAX_POS={round(targets['max_pos'], 4)} | "
                    f"MAX_NEG={round(targets['max_neg'], 4)} | "
                    f"recorrido_int={_pm28_recorrido if _pm28_recorrido is not None else 'N/A'}"
                )

                # ── EMA 20 / 50 / 200 — barras 1H premarket (régimen real) ─
                bars_1m_for_ema = _filter_bars_before(
                    bars_dict.get("1m", []), cutoff_et
                )
                bars_1h_for_ema = _filter_bars_before(
                    bars_dict.get("1h", []), cutoff_et
                )
                closes_ema = pd.Series(
                    [float(b["c"]) for b in bars_1h_for_ema], dtype=float
                )
                ema20_pre  = None
                ema50_pre  = None
                ema200_pre = None
                if len(closes_ema) >= 20:
                    ema20_pre = round(
                        float(closes_ema.ewm(span=20,  adjust=False).mean().iloc[-1]), 4
                    )
                if len(closes_ema) >= 50:
                    ema50_pre = round(
                        float(closes_ema.ewm(span=50,  adjust=False).mean().iloc[-1]), 4
                    )
                if len(closes_ema) >= 200:
                    ema200_pre = round(
                        float(closes_ema.ewm(span=200, adjust=False).mean().iloc[-1]), 4
                    )

                precio_ref_for_log = (
                    open_price if (open_price is not None) else precio_pm_actual
                )
                ema_align_for_log = (
                    ema_alignment(precio_ref_for_log, ema20_pre, ema50_pre, ema200_pre)
                    if all(v is not None for v in [ema20_pre, ema50_pre, ema200_pre])
                    else "unknown"
                )
                logger.info(
                    f"[{ticker}] EMAs 1h: velas={len(closes_ema)} | "
                    f"EMA20={ema20_pre} | EMA50={ema50_pre} | EMA200={ema200_pre} | "
                    f"align={ema_align_for_log}"
                )
                if ema200_pre is None:
                    logger.warning(
                        f"[{ticker}] EMA200 null — {len(closes_ema)} velas 1h "
                        f"(necesita ≥200) — score EC no se calculará"
                    )
                # NOTA: bars_1m_for_ema sigue siendo necesario para bb_expande_premarket

                # ── bb_expande_premarket ────────────────────────────────
                bb_expande_premarket = None
                bars_1m_prev = _filter_bars_upto_inclusive(
                    bars_dict.get("1m", []), cutoff_prev_1600
                )
                if len(bars_1m_for_ema) >= N_VELAS_BB and \
                   len(bars_1m_prev)    >= N_VELAS_BB:
                    bbt_now,  bbb_now  = _compute_bb_from_closes(
                        [b["c"] for b in bars_1m_for_ema]
                    )
                    bbt_prev, bbb_prev = _compute_bb_from_closes(
                        [b["c"] for b in bars_1m_prev]
                    )
                    if all(v is not None
                           for v in [bbt_now, bbb_now, bbt_prev, bbb_prev]):
                        bb_expande_premarket = (
                            (bbt_now - bbb_now) > (bbt_prev - bbb_prev)
                        )

                logger.info(
                    f"[{ticker}] caution='{caution_note}' | "
                    f"signals='{signals_str}' | "
                    f"badge={round(badge_long,1)}% | "
                    f"bb_expande={bb_expande_premarket}"
                )

                # ── Conviction Score EC ─────────────────────────────────
                ema_align = "unknown"
                ec_score  = None
                ec_accion = "SIN_DATOS"
                ec_size   = 1.0

                # precio de referencia: open_price en open_930,
                # precio_premarket en los otros dos modos
                precio_ref = (
                    open_price if (open_price is not None) else precio_premarket
                )

                if all(v is not None for v in [ema20_pre, ema50_pre, ema200_pre]):
                    ema_align = ema_alignment(
                        precio_ref, ema20_pre, ema50_pre, ema200_pre
                    )
                    _n_caut_ec = (
                        caution_note.count("BBT") + caution_note.count("BBB")
                        if caution_note
                        else 0
                    )
                    caution_1v3_ec = None  # 1v3 solo en open_930 / verify
                    ec_score = conviction_score(
                        direction_pm,
                        gap_type,
                        abs(gap_pct),
                        prev_day_change_pct,
                        ema_align,
                        bb_expande_premarket,
                        n_cautiones=_n_caut_ec,
                        caution_1v3=caution_1v3_ec,
                        ticker=ticker,
                        open_930=None,
                        int_pos=None,
                        int_neg=None,
                    )
                    if ec_score == -1.0:
                        ec_accion = "BLOQUEADO"
                        ec_size   = 0.0
                    elif direction_pm == "ZONA_MUERTA" and caution_1v3_ec not in ("igual", "nuevo"):
                        ec_accion = "ZONA_MUERTA"
                        ec_size   = 0.0
                    else:
                        sc_int = round(ec_score)
                        if sc_int <= 4:
                            ec_accion = "SKIP"
                            ec_size   = 0.0
                        elif sc_int >= 7:
                            ec_accion = "SIZE_x2"
                            ec_size   = 2.0
                        else:
                            ec_accion = "SIZE_x1"
                            ec_size   = 1.0

                if ec_score is None and direction_pm == "ZONA_MUERTA":
                    ec_accion = "ZONA_MUERTA"
                    ec_size   = 0.0

                if ec_score is None and ec_accion == "SIN_DATOS":
                    logger.warning(
                        f"[{ticker}] score EC = SIN_DATOS | "
                        f"ema20={ema20_pre} ema50={ema50_pre} ema200={ema200_pre}"
                    )
                else:
                    accion_label = {
                        "SIZE_x2": "★★ SIZE x2",
                        "SIZE_x1": "★ SIZE x1",
                        "SKIP": "✗ SKIP",
                        "ZONA_MUERTA": "~ ZM",
                        "BLOQUEADO": "⛔ BLOQUEADO",
                    }.get(ec_accion, ec_accion)
                    logger.info(
                        f"[{ticker}] score={ec_score} | {accion_label} | "
                        f"align={ema_align} | dir={direction_pm} | "
                        f"prev_day={round(prev_day_change_pct,2)}% | "
                        f"n_caut={_n_caut_ec if '_n_caut_ec' in dir() else '?'}"
                    )

                # Guardar datos
                output_data["tickers"][ticker] = {
                    "bp": bp,
                    "int_pos": targets["int_pos"],
                    "int_neg": targets["int_neg"],
                    "max_pos": targets["max_pos"],
                    "max_neg": targets["max_neg"],
                    "int_dist": targets["int_dist"],
                    "max_dist": targets["max_dist"],
                    "gap_pct": gap_pct,
                    "gap_type": gap_type,
                    "prev_day_change": prev_day_change_pct,
                    "signals": signals_str,
                    "signals_3_9": signals_str,
                    "badge_long": round(badge_long, 2),
                    "caution_note": caution_note,
                    "skip": skip_reason is not None,
                    "skip_reason": skip_reason,
                    "dist": dist_pm,
                    "direction": direction_pm,
                    "prev_day_hint": _get_prev_day_hint(
                        ticker,
                        float(prev_day_change_pct)
                        if prev_day_change_pct not in (None, "", "nan")
                        else None,
                        direction_pm,
                    ),
                    "cerro_bp_hint": _get_cerro_bp_hint(ticker, False, direction_pm),
                    "ema20_pre":            ema20_pre,
                    "ema50_pre":            ema50_pre,
                    "ema200_pre":           ema200_pre,
                    "ema_align":            ema_align,
                    "bb_expande_premarket": bb_expande_premarket,
                    "ec_score":             ec_score,
                    "ec_score_v2":          ec_score,
                    "ec_accion":            ec_accion,
                    "ec_size":              ec_size,
                }

            except Exception as e:
                logger.error(f"[{ticker}] Error procesando: {e}", exc_info=True)
                continue

        tickers_ok = output_data.get("tickers", {})
        skips_n = sum(1 for t in tickers_ok.values() if t.get("skip"))
        zm_n = sum(1 for t in tickers_ok.values() if t.get("ec_accion") == "ZONA_MUERTA")
        x2_n = sum(1 for t in tickers_ok.values() if t.get("ec_accion") == "SIZE_x2")
        x1_n = sum(1 for t in tickers_ok.values() if t.get("ec_accion") == "SIZE_x1")
        logger.info(
            f"=== MODO {args.modo} completado | "
            f"OK={len(tickers_ok)} | SKIP={skips_n} | ZM={zm_n} | "
            f"SIZE_x2={x2_n} | SIZE_x1={x1_n} ==="
        )
        finalize_dashboard_output(output_data)
        write_json(output_data, f"premarket_9_28_{date_str}.json")
        write_context_compressed(output_data, date_str, "premarket_9_28")
        write_ai_dashboard(output_data, date_str, "premarket_9_28")

    # Modo test: mismo pipeline que premarket_9_28, cutoff = ultima barra 1m
    elif args.modo == "test":
        bars_by_ticker = {}
        alpaca_1m_ok = 0
        for ticker in tickers_to_run:
            bars_by_ticker[ticker] = {}
            for tf in TF_LIST:
                try:
                    bars_by_ticker[ticker][tf] = AlpacaHistoricalClient.fetch_bars(
                        ticker,
                        tf,
                        (datetime.now(TIMEZONE) - timedelta(days=3)).strftime(
                            "%Y-%m-%d"
                        ),
                        datetime.now(TIMEZONE).strftime("%Y-%m-%d"),
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"Advertencia: {ticker} {tf} fetch: {exc}")
                    bars_by_ticker[ticker][tf] = []
            if bars_by_ticker[ticker].get("1m"):
                alpaca_1m_ok += 1
            else:
                print(f"Advertencia: {ticker}: sin barras 1m desde Alpaca")

        banner_cutoff = None
        for ticker in tickers_to_run:
            try:
                open_price: float | None = None  # en open_930 se asigna el open RTH
                bars_dict = bars_by_ticker[ticker]
                cutoff_et, cutoff_label = _test_cutoff_from_1m(bars_dict)
                if banner_cutoff is None:
                    banner_cutoff = cutoff_label

                bars_1m_pre = _filter_bars_before(
                    bars_dict.get("1m", []), cutoff_et
                )
                if not bars_1m_pre:
                    print(
                        f"Advertencia: {ticker}: sin barras 1m antes del cutoff test"
                    )
                    continue

                if not _validate_premarket_bars(
                    ticker, bars_dict.get("1m", []), cutoff_et
                ):
                    pass

                bp = BPCalculator.calculate_bp(
                    bars_dict, cutoff_time=pd.Timestamp(cutoff_et)
                )
                if bp is None:
                    print(f"Advertencia: {ticker}: BP no calculable")
                    continue

                today_date_et = datetime.now(TIMEZONE).date()
                prev_date_et = today_date_et - timedelta(days=1)

                daily_bars = AlpacaHistoricalClient.fetch_bars(
                    ticker,
                    "1d",
                    (datetime.now(TIMEZONE) - timedelta(days=10)).strftime(
                        "%Y-%m-%d"
                    ),
                    datetime.now(TIMEZONE).strftime("%Y-%m-%d"),
                )
                bars_dict["1d"] = _filter_bars_before(daily_bars, cutoff_et)
                daily_prev = [b for b in daily_bars if b["t"].date() < today_date_et]
                daily_prev.sort(key=lambda x: x["t"])

                prev_close = float(daily_prev[-1]["c"]) if daily_prev else 0.0
                if len(daily_prev) >= 2:
                    c_last = float(daily_prev[-1]["c"])
                    c_prev = float(daily_prev[-2]["c"])
                    prev_day_change_pct = (
                        ((c_last - c_prev) / c_prev * 100.0) if c_prev else 0.0
                    )
                else:
                    prev_day_change_pct = 0.0

                precio_premarket = float(bars_1m_pre[-1]["c"])

                if prev_close > 0:
                    gap_pct = ((precio_premarket - prev_close) / prev_close) * 100.0
                    gap_type = (
                        "GAP_UP"
                        if gap_pct > 0
                        else ("GAP_DOWN" if gap_pct < 0 else "FLAT")
                    )
                else:
                    gap_pct = 0.0
                    gap_type = "FLAT"

                cutoff_prev_1600 = datetime(
                    prev_date_et.year,
                    prev_date_et.month,
                    prev_date_et.day,
                    16,
                    0,
                    0,
                    tzinfo=TIMEZONE,
                )

                signals_str = compute_signals_3_9(bars_dict, cutoff_et)
                badge_long = compute_badge_long(bars_dict, cutoff_et)
                caution_note = compute_caution_note(
                    bars_dict,
                    precio_premarket,
                    cutoff_prev_1600,
                    ticker=ticker,
                )

                range_3d = TargetsCalculator.calculate_range_3d(ticker, daily_prev)
                targets = TargetsCalculator.calculate_targets(
                    bp, range_3d, [caution_note, gap_type]
                )

                skip_reason = None
                if SkipFilter.apply_skip_filters(
                    ticker, prev_day_change_pct, gap_pct, [gap_type]
                ):
                    skip_reason = "Filtro SKIP activado"

                dist_pm = precio_premarket - bp
                direction_pm = direction_from_dist(dist_pm)

                # ── EMA 20 / 50 / 200 — barras 1H premarket (régimen real) ─
                bars_1m_for_ema = _filter_bars_before(
                    bars_dict.get("1m", []), cutoff_et
                )
                bars_1h_for_ema = _filter_bars_before(
                    bars_dict.get("1h", []), cutoff_et
                )
                closes_ema = pd.Series(
                    [float(b["c"]) for b in bars_1h_for_ema], dtype=float
                )
                ema20_pre = None
                ema50_pre = None
                ema200_pre = None
                if len(closes_ema) >= 20:
                    ema20_pre = round(
                        float(
                            closes_ema.ewm(span=20, adjust=False).mean().iloc[-1]
                        ),
                        4,
                    )
                if len(closes_ema) >= 50:
                    ema50_pre = round(
                        float(
                            closes_ema.ewm(span=50, adjust=False).mean().iloc[-1]
                        ),
                        4,
                    )
                if len(closes_ema) >= 200:
                    ema200_pre = round(
                        float(
                            closes_ema.ewm(span=200, adjust=False).mean().iloc[-1]
                        ),
                        4,
                    )
                # NOTA: bars_1m_for_ema sigue siendo necesario para bb_expande_premarket

                bb_expande_premarket = None
                bars_1m_prev = _filter_bars_upto_inclusive(
                    bars_dict.get("1m", []), cutoff_prev_1600
                )
                if len(bars_1m_for_ema) >= N_VELAS_BB and len(bars_1m_prev) >= N_VELAS_BB:
                    bbt_now, bbb_now = _compute_bb_from_closes(
                        [b["c"] for b in bars_1m_for_ema]
                    )
                    bbt_prev, bbb_prev = _compute_bb_from_closes(
                        [b["c"] for b in bars_1m_prev]
                    )
                    if all(
                        v is not None
                        for v in [bbt_now, bbb_now, bbt_prev, bbb_prev]
                    ):
                        bb_expande_premarket = (bbt_now - bbb_now) > (
                            bbt_prev - bbb_prev
                        )

                ema_align = "unknown"
                ec_score = None
                ec_accion = "SIN_DATOS"
                ec_size = 1.0

                precio_ref = (
                    open_price if (open_price is not None) else precio_premarket
                )

                if all(
                    v is not None for v in [ema20_pre, ema50_pre, ema200_pre]
                ):
                    ema_align = ema_alignment(
                        precio_ref, ema20_pre, ema50_pre, ema200_pre
                    )
                    _n_caut_ec = (
                        caution_note.count("BBT") + caution_note.count("BBB")
                        if caution_note
                        else 0
                    )
                    caution_1v3_ec = None  # 1v3 solo en open_930 / verify
                    ec_score = conviction_score(
                        direction_pm,
                        gap_type,
                        abs(gap_pct),
                        prev_day_change_pct,
                        ema_align,
                        bb_expande_premarket,
                        n_cautiones=_n_caut_ec,
                        caution_1v3=caution_1v3_ec,
                        ticker=ticker,
                        open_930=None,
                        int_pos=None,
                        int_neg=None,
                    )
                    if ec_score == -1.0:
                        ec_accion = "BLOQUEADO"
                        ec_size = 0.0
                    elif direction_pm == "ZONA_MUERTA" and caution_1v3_ec not in ("igual", "nuevo"):
                        ec_accion = "ZONA_MUERTA"
                        ec_size = 0.0
                    else:
                        sc_int = round(ec_score)
                        if sc_int <= 4:
                            ec_accion = "SKIP"
                            ec_size = 0.0
                        elif sc_int >= 7:
                            ec_accion = "SIZE_x2"
                            ec_size = 2.0
                        else:
                            ec_accion = "SIZE_x1"
                            ec_size = 1.0

                if ec_score is None and direction_pm == "ZONA_MUERTA":
                    ec_accion = "ZONA_MUERTA"
                    ec_size = 0.0

                output_data["tickers"][ticker] = {
                    "bp": bp,
                    "int_pos": targets["int_pos"],
                    "int_neg": targets["int_neg"],
                    "max_pos": targets["max_pos"],
                    "max_neg": targets["max_neg"],
                    "int_dist": targets["int_dist"],
                    "max_dist": targets["max_dist"],
                    "gap_pct": gap_pct,
                    "gap_type": gap_type,
                    "prev_day_change": prev_day_change_pct,
                    "signals": signals_str,
                    "signals_3_9": signals_str,
                    "badge_long": round(badge_long, 2),
                    "caution_note": caution_note,
                    "skip": skip_reason is not None,
                    "skip_reason": skip_reason,
                    "dist": dist_pm,
                    "direction": direction_pm,
                    "prev_day_hint": _get_prev_day_hint(
                        ticker,
                        float(prev_day_change_pct)
                        if prev_day_change_pct not in (None, "", "nan")
                        else None,
                        direction_pm,
                    ),
                    "cerro_bp_hint": _get_cerro_bp_hint(ticker, False, direction_pm),
                    "ema20_pre": ema20_pre,
                    "ema50_pre": ema50_pre,
                    "ema200_pre": ema200_pre,
                    "ema_align": ema_align,
                    "bb_expande_premarket": bb_expande_premarket,
                    "ec_score": ec_score,
                    "ec_score_v2": ec_score,
                    "ec_accion": ec_accion,
                    "ec_size": ec_size,
                    "test_cutoff_last_bar": cutoff_label,
                }

            except Exception as e:
                print(f"Error procesando {ticker} (test): {str(e)}")
                continue

        cutoffs_map = {
            t: output_data["tickers"][t].get("test_cutoff_last_bar")
            for t in output_data["tickers"]
            if output_data["tickers"][t].get("test_cutoff_last_bar")
        }
        output_data["test_meta"] = {
            "banner_line": (
                f"MODO TEST — cutoff (ultima barra 1m): {banner_cutoff or 'N/A'}"
            ),
            "cutoffs_by_ticker": cutoffs_map,
        }

        n_run = len(tickers_to_run)
        td_pre = output_data.get("tickers", {})
        bp_n = sum(1 for t in td_pre if td_pre[t].get("bp") is not None)
        ec_n = sum(1 for t in td_pre if td_pre[t].get("ec_score") is not None)

        finalize_dashboard_output(output_data)
        write_json(output_data, f"test_{date_str}.json")
        write_context_compressed(output_data, date_str, "test")
        write_ai_dashboard(output_data, date_str, "test")

        dash_path = Path("outputs") / f"ai_dashboard_prompt_{date_str}_test.txt"
        print()
        print("[!] MODO TEST — " + (banner_cutoff or "sin cutoff"))
        print("-" * 40)
        print(f"Alpaca OK (1m): {alpaca_1m_ok}/{n_run} tickers con datos")
        print(f"BP calculados: {bp_n}/{n_run}")
        print(f"Scores EC: {ec_n}/{n_run}")
        print(f"Archivo generado: {dash_path}")
        print(
            "Nota: caution_cambio_1v3 solo se calcula en --modo open_930 "
            "(requiere premarket_9_28 + open RTH)."
        )
        print("-" * 40)

    # Modo 3: open_930
    elif args.modo == "open_930":
        logger.info(f"=== MODO: {args.modo} | tickers: {len(tickers_to_run)} ===")
        # Leer datos premarket_9_28
        try:
            with open(f"outputs/premarket_9_28_{date_str}.json", "r", encoding="utf-8") as f:
                premarket_data = json.load(f)
        except FileNotFoundError:
            print("Error: No se encontraron datos premarket_9_28")
            return

        # Obtener precios de apertura
        premarket_data["tickers_order"] = tickers_to_run
        for ticker in tickers_to_run:
            try:
                open_price: float | None = None
                if ticker not in premarket_data["tickers"]:
                    logger.warning(
                        f"[{ticker}] sin fila en premarket_9_28.json — omitido open_930"
                    )
                    continue
                td = premarket_data["tickers"][ticker]
                open_price = AlpacaSnapshotClient.get_open_price(ticker)
                if open_price is None:
                    print(f"  {ticker}: sin open_930 (Alpaca)")
                    continue
                bp = td["bp"]
                dist = open_price - bp
                direction = direction_from_dist(dist)

                td["open_930"] = open_price
                td["dist"] = dist
                td["direction"] = direction

                oz, opct = compute_open_zone(
                    open_price,
                    bp,
                    td.get("int_pos"),
                    td.get("int_neg"),
                    direction,
                )
                td["open_zone"] = oz
                td["open_pct_en_rango"] = opct
                td["rebote_umbral"] = get_rebote_umbral(ticker, direction)

                logger.info(
                    f"[{ticker}] open_930={open_price} | "
                    f"open_zone={oz} | "
                    f"dist_open={round(open_price - bp, 4)}"
                )
                _int_tgt = (
                    td.get("int_pos")
                    if direction == "ALCISTA"
                    else td.get("int_neg")
                )
                _recorrido_int = (
                    round(abs(open_price - _int_tgt), 4)
                    if _int_tgt is not None
                    else None
                )
                logger.info(
                    f"[{ticker}] BP={round(bp, 4)} | "
                    f"INT_POS={round(td.get('int_pos') or 0, 4)} | "
                    f"INT_NEG={round(td.get('int_neg') or 0, 4)} | "
                    f"MAX_POS={round(td.get('max_pos') or 0, 4)} | "
                    f"MAX_NEG={round(td.get('max_neg') or 0, 4)} | "
                    f"recorrido_int={_recorrido_int if _recorrido_int is not None else 'N/A'}"
                )

                direction_pm = direction
                gap_type = td["gap_type"]
                gap_pct = td["gap_pct"]
                prev_day_change_pct = td["prev_day_change"]

                prev_date_et = datetime.now(TIMEZONE).date() - timedelta(days=1)
                cutoff_et = datetime.now(TIMEZONE).replace(
                    hour=9, minute=28, second=0, microsecond=0
                )
                cutoff_prev_1600 = datetime(
                    prev_date_et.year,
                    prev_date_et.month,
                    prev_date_et.day,
                    16,
                    0,
                    0,
                    tzinfo=TIMEZONE,
                )

                precio_premarket = None
                end_fetch = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
                bars_dict: dict[str, list] = {}
                for tf in OPEN_930_TF_LIST:
                    start_fetch = (
                        datetime.now(TIMEZONE) - timedelta(days=2)
                    ).strftime("%Y-%m-%d")
                    try:
                        bars_dict[tf] = AlpacaHistoricalClient.fetch_bars(
                            ticker, tf, start_fetch, end_fetch
                        )
                    except Exception as _fetch_exc:
                        logger.warning(
                            f"[{ticker}] fetch {tf} falló: {_fetch_exc} — usando lista vacía"
                        )
                        bars_dict[tf] = []

                # ── Logging técnico: velas por TF ─────────────────────────
                for tf, bars in bars_dict.items():
                    n = len(bars)
                    if n > 0:
                        first_ts = str(bars[0]["t"])[:16]
                        last_ts = str(bars[-1]["t"])[:16]
                    else:
                        first_ts = last_ts = "sin datos"
                    logger.debug(f"[{ticker}] {tf}: {n} velas | {first_ts} → {last_ts}")

                td["caution_real_930"] = None
                td["caution_cambio_1v3"] = None
                tfs_live = {
                    tf: _bars_list_to_mi_dataframe(bars_dict.get(tf, []))
                    for tf in OPEN_930_TF_LIST
                }
                try:
                    fecha_today = datetime.now(TIMEZONE).date()
                    ts_930 = pd.Timestamp(
                        datetime.now(TIMEZONE).replace(
                            hour=9, minute=30, second=0, microsecond=0
                        )
                    )
                    _inyecto_vela_930 = False
                    for tf in list(tfs_live.keys()):
                        df_tf = tfs_live[tf]
                        if df_tf is None or df_tf.empty:
                            continue
                        if "date_et" not in df_tf.columns:
                            continue
                        mask_hoy = df_tf["date_et"] == fecha_today
                        ya_rth_hoy = False
                        if mask_hoy.any():
                            sub_hoy = df_tf.loc[mask_hoy]
                            ya_rth_hoy = bool(rth_mask_vectorized(sub_hoy).any())
                        if ya_rth_hoy:
                            continue
                        fila = pd.DataFrame(
                            [
                                {
                                    "timestamp": ts_930,
                                    "open": float(open_price),
                                    "high": float(open_price),
                                    "low": float(open_price),
                                    "close": float(open_price),
                                    "volume": 0.0,
                                    "date_et": fecha_today,
                                }
                            ]
                        )
                        tfs_live[tf] = (
                            pd.concat([df_tf, fila], ignore_index=True)
                            .sort_values("timestamp")
                            .reset_index(drop=True)
                        )
                        _inyecto_vela_930 = True
                    if _inyecto_vela_930:
                        logger.info(
                            f"[{ticker}] vela RTH inyectada: open_930={open_price} | ts={ts_930}"
                        )
                    tfs_rth_l = build_tfs_rth(tfs_live, ticker)
                    cr930 = compute_caution_real_930(
                        tfs_live,
                        fecha_today,
                        open_price,
                        ticker,
                        tfs_rth=tfs_rth_l,
                    )
                    td["caution_real_930"] = cr930
                    td["caution_cambio_1v3"] = compute_caution_cambio_1v3(
                        td.get("caution_note"),
                        cr930,
                        ticker,
                        tfs_live,
                    )
                    logger.info(
                        f"[{ticker}] caution_real_930='{cr930}' | "
                        f"1v3='{td.get('caution_cambio_1v3')}'"
                    )
                    if cr930 == "Sin datos BB":
                        logger.warning(
                            f"[{ticker}] caution_real_930='Sin datos BB' — "
                            f"parquets RTH sin datos suficientes"
                        )
                except Exception as _caut_exc:
                    print(f"  [caution 1v3] {ticker}: {_caut_exc}")

                # ── EMAs: reutilizar premarket 9:28 (sin re-fetch 1h) ───────
                bars_1m_for_ema = _filter_bars_before(
                    bars_dict.get("1m", []), cutoff_et
                )
                ema20_pre = td.get("ema20_pre")
                ema50_pre = td.get("ema50_pre")
                ema200_pre = td.get("ema200_pre")
                logger.info(
                    f"[{ticker}] EMAs 1h (desde 9:28): "
                    f"EMA20={ema20_pre} | EMA50={ema50_pre} | EMA200={ema200_pre} | "
                    f"align={td.get('ema_align')}"
                )
                if ema200_pre is None:
                    logger.warning(
                        f"[{ticker}] EMA200 null (JSON 9:28) — score EC no se calculará"
                    )
                # NOTA: bars_1m_for_ema sigue siendo necesario para bb_expande_premarket

                # ── bb_expande_premarket ────────────────────────────────
                bb_expande_premarket = None
                bars_1m_prev = _filter_bars_upto_inclusive(
                    bars_dict.get("1m", []), cutoff_prev_1600
                )
                if len(bars_1m_for_ema) >= N_VELAS_BB and \
                   len(bars_1m_prev)    >= N_VELAS_BB:
                    bbt_now,  bbb_now  = _compute_bb_from_closes(
                        [b["c"] for b in bars_1m_for_ema]
                    )
                    bbt_prev, bbb_prev = _compute_bb_from_closes(
                        [b["c"] for b in bars_1m_prev]
                    )
                    if all(v is not None
                           for v in [bbt_now, bbb_now, bbt_prev, bbb_prev]):
                        bb_expande_premarket = (
                            (bbt_now - bbb_now) > (bbt_prev - bbb_prev)
                        )

                # ── Conviction Score EC ─────────────────────────────────
                ema_align = "unknown"
                ec_score  = None
                ec_accion = "SIN_DATOS"
                ec_size   = 1.0

                # precio de referencia: open_price en open_930,
                # precio_premarket en los otros dos modos
                precio_ref = (
                    open_price
                    if (open_price is not None)
                    else precio_premarket
                )

                _caution_1v3_val = td.get("caution_cambio_1v3")
                _n_caut_ec = (
                    (td.get("caution_note", "") or "").count("BBT")
                    + (td.get("caution_note", "") or "").count("BBB")
                )
                # Dirección real al abrir para el score cuando hay 1v3
                _dir_for_score = (
                    direction_from_dist(open_price - bp)
                    if open_price is not None and _caution_1v3_val is not None
                    else direction_pm
                )

                if all(v is not None for v in [ema20_pre, ema50_pre, ema200_pre]):
                    ema_align = ema_alignment(
                        precio_ref, ema20_pre, ema50_pre, ema200_pre
                    )
                    ec_score = conviction_score(
                        _dir_for_score,
                        gap_type,
                        abs(gap_pct),
                        prev_day_change_pct,
                        ema_align,
                        bb_expande_premarket,
                        n_cautiones=_n_caut_ec,
                        caution_1v3=_caution_1v3_val,
                        ticker=ticker,
                        open_930=open_price,
                        int_pos=td.get("int_pos"),
                        int_neg=td.get("int_neg"),
                    )
                    if ec_score == -1.0:
                        ec_accion = "BLOQUEADO"
                        ec_size   = 0.0
                    elif _dir_for_score == "ZONA_MUERTA" and _caution_1v3_val not in ("igual", "nuevo"):
                        ec_accion = "ZONA_MUERTA"
                        ec_size   = 0.0
                    else:
                        sc_int = round(ec_score)
                        if sc_int <= 4:
                            ec_accion = "SKIP"
                            ec_size   = 0.0
                        elif sc_int >= 7:
                            ec_accion = "SIZE_x2"
                            ec_size   = 2.0
                        else:
                            ec_accion = "SIZE_x1"
                            ec_size   = 1.0

                if ec_score is None and _dir_for_score == "ZONA_MUERTA":
                    ec_accion = "ZONA_MUERTA"
                    ec_size   = 0.0

                logger.info(
                    f"[{ticker}] caution='{td.get('caution_note')}' | "
                    f"signals='{td.get('signals_3_9')}' | "
                    f"badge={round(td.get('badge_long', 0.0),1)}% | "
                    f"bb_expande={bb_expande_premarket}"
                )

                if ec_score is None and ec_accion == "SIN_DATOS":
                    logger.warning(
                        f"[{ticker}] score EC = SIN_DATOS | "
                        f"ema20={ema20_pre} ema50={ema50_pre} ema200={ema200_pre}"
                    )
                else:
                    accion_label = {
                        "SIZE_x2": "★★ SIZE x2",
                        "SIZE_x1": "★ SIZE x1",
                        "SKIP": "✗ SKIP",
                        "ZONA_MUERTA": "~ ZM",
                        "BLOQUEADO": "⛔ BLOQUEADO",
                    }.get(ec_accion, ec_accion)
                    logger.info(
                        f"[{ticker}] score={ec_score} | {accion_label} | "
                        f"align={ema_align} | dir={_dir_for_score} | "
                        f"prev_day={round(prev_day_change_pct,2)}% | "
                        f"n_caut={_n_caut_ec if '_n_caut_ec' in dir() else '?'}"
                    )

                td["ema20_pre"] = ema20_pre
                td["ema50_pre"] = ema50_pre
                td["ema200_pre"] = ema200_pre
                td["ema_align"] = ema_align
                td["bb_expande_premarket"] = bb_expande_premarket
                td["ec_score"] = ec_score
                td["ec_score_v2"] = ec_score
                td["ec_accion"] = ec_accion
                td["ec_size"] = ec_size

                # Re-evaluar con open_zone + 1v3 ya cargados (coherente antes de finalize)
                td["evaluation"] = PremiumDetector.evaluate(ticker, td)

                _pdc_oz = td.get("prev_day_change")
                _prev_f_oz = (
                    float(_pdc_oz)
                    if _pdc_oz not in (None, "", "nan")
                    else None
                )
                td["prev_day_hint"] = _get_prev_day_hint(
                    ticker, _prev_f_oz, td.get("direction", "ZONA_MUERTA")
                )
                _cb_oz = td.get("cerro_bp")
                _cerro_bp_bool_oz = str(_cb_oz).strip().upper() in (
                    "SÍ",
                    "SI",
                    "S",
                    "TRUE",
                    "1",
                )
                td["cerro_bp_hint"] = _get_cerro_bp_hint(
                    ticker, _cerro_bp_bool_oz, td.get("direction", "ZONA_MUERTA")
                )

                logger.info(
                    f"[{ticker}] open_930 resultado: ec_accion={ec_accion} | "
                    f"ec_score={ec_score}"
                )

            except Exception as e:
                logger.error(f"[{ticker}] Error procesando: {e}", exc_info=True)
                continue

        premarket_data.setdefault("skips", {})
        td_map_930 = premarket_data.get("tickers", {})
        n_univ_930 = len(tickers_to_run)
        ec_skip_n = sum(
            1
            for t in tickers_to_run
            if td_map_930.get(t, {}).get("ec_accion") == "SKIP"
        )
        zm_n = sum(
            1
            for t in tickers_to_run
            if td_map_930.get(t, {}).get("ec_accion") == "ZONA_MUERTA"
        )
        x2_n = sum(
            1
            for t in tickers_to_run
            if td_map_930.get(t, {}).get("ec_accion") == "SIZE_x2"
        )
        x1_n = sum(
            1
            for t in tickers_to_run
            if td_map_930.get(t, {}).get("ec_accion") == "SIZE_x1"
        )
        logger.info(
            f"=== MODO {args.modo} completado | "
            f"OK={n_univ_930} | SKIP={ec_skip_n} | ZM={zm_n} | "
            f"SIZE_x2={x2_n} | SIZE_x1={x1_n} ==="
        )
        finalize_dashboard_output(premarket_data)
        write_json(premarket_data, f"open_930_{date_str}.json")
        write_context_compressed(premarket_data, date_str, "open_930")
        write_ai_dashboard(premarket_data, date_str, "open_930")

if __name__ == "__main__":
    asyncio.run(main())