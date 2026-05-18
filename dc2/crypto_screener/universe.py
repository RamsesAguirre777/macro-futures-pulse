"""Universo: top por market cap en Binance spot USDT + filtros de exclusión.

Market cap vía CoinGecko API pública (``/coins/markets``). Opcional: variable de
entorno ``COINGECKO_API_KEY`` para el plan Pro (cabecera ``x-cg-pro-api-key``).
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
COINGECKO_KEY = os.environ.get("COINGECKO_API_KEY", "").strip()

# Mínimos (USD)
MIN_MARKET_CAP_USD = 150_000_000
MIN_QUOTE_VOLUME_24H_USD = 10_000_000
TOP_N = 500  # universo amplio; los filtros técnicos reducen la lista final

STABLECOIN_BASES: frozenset[str] = frozenset(
    {
        "USDT",
        "USDC",
        "DAI",
        "FDUSD",
        "TUSD",
        "BUSD",
        "USDP",
        "PYUSD",
        "GUSD",
        "USDD",
        "USTC",
        "EUR",
        "GBP",
    }
)

WRAPPED_BASES: frozenset[str] = frozenset(
    {
        "WBTC",
        "WETH",
        "WBNB",
        "WMATIC",
        "WAVAX",
        "WSTETH",
        "WEETH",
        "WBETH",
    }
)

# Bases que no son spot “normales” aunque pasen otros filtros
EXTRA_EXCLUDED_BASES: frozenset[str] = frozenset()


def _is_leveraged_style_base(base: str) -> bool:
    b = base.upper()
    if re.search(r"\d+[LS]$", b):
        return True
    for suf in ("UP", "DOWN", "BULL", "BEAR"):
        if b.endswith(suf) and len(b) > len(suf):
            return True
    return False


def _should_exclude_base(base: str) -> bool:
    u = base.upper()
    if u in STABLECOIN_BASES or u in WRAPPED_BASES or u in EXTRA_EXCLUDED_BASES:
        return True
    if _is_leveraged_style_base(u):
        return True
    return False


def load_binance_usdt_spot_bases(exchange) -> set[str]:
    """Bases (ej. BTC) que tienen par spot activo USDT en Binance vía CCXT."""
    exchange.load_markets()
    bases: set[str] = set()
    for sym, m in exchange.markets.items():
        if not m.get("active", True):
            continue
        if m.get("type") != "spot":
            continue
        if m.get("quote") != "USDT":
            continue
        base = m.get("base")
        if not base:
            continue
        bases.add(str(base).upper())
    return bases


def _coingecko_headers() -> dict[str, str]:
    h = {"User-Agent": "bb-retroceso-crypto-screener/1.0"}
    if COINGECKO_KEY:
        h["x-cg-pro-api-key"] = COINGECKO_KEY
    return h


def _coingecko_markets_page(page: int, per_page: int = 250) -> list[dict]:
    url = f"{COINGECKO_BASE}/coins/markets"
    params = {
        "vs_currency": "usd",
        "order": "market_cap_desc",
        "per_page": per_page,
        "page": page,
        "sparkline": "false",
    }
    r = requests.get(url, params=params, headers=_coingecko_headers(), timeout=60)
    r.raise_for_status()
    return r.json()


@dataclass
class UniverseEntry:
    symbol_ccxt: str  # BTC/USDT
    base: str
    market_cap_usd: float
    volume_24h_usd: float
    coingecko_id: str


def build_universe_top_mcap(
    exchange,
    *,
    top_n: int = TOP_N,
    min_mcap: float = MIN_MARKET_CAP_USD,
    min_volume_usd: float = MIN_QUOTE_VOLUME_24H_USD,
) -> list[UniverseEntry]:
    """
    Top ``top_n`` pares Binance spot USDT ordenados por market cap (CoinGecko),
    filtrando por volumen 24h y exclusiones.
    """
    binance_bases = load_binance_usdt_spot_bases(exchange)
    out: list[UniverseEntry] = []
    seen_bases: set[str] = set()

    page = 1
    max_pages = 20
    while len(out) < top_n and page <= max_pages:
        try:
            rows = _coingecko_markets_page(page)
        except Exception as e:
            logger.error("CoinGecko page %s falló: %s", page, e)
            break
        if not rows:
            break
        for row in rows:
            if len(out) >= top_n:
                break
            sym = str(row.get("symbol", "")).upper()
            if not sym or sym in seen_bases:
                continue
            if sym not in binance_bases:
                continue
            if _should_exclude_base(sym):
                continue
            mcap = float(row.get("market_cap") or 0)
            vol = float(row.get("total_volume") or 0)
            if mcap < min_mcap or vol < min_volume_usd:
                continue
            cid = str(row.get("id", ""))
            ccxt_sym = f"{sym}/USDT"
            if ccxt_sym not in exchange.markets:
                continue
            seen_bases.add(sym)
            out.append(
                UniverseEntry(
                    symbol_ccxt=ccxt_sym,
                    base=sym,
                    market_cap_usd=mcap,
                    volume_24h_usd=vol,
                    coingecko_id=cid,
                )
            )
        page += 1

    if len(out) < top_n:
        logger.warning(
            "Universo incompleto: %d/%d (CoinGecko+Binance+filtros). "
            "Considera más páginas o relajar filtros.",
            len(out),
            top_n,
        )
    return out


def universe_fallback_by_volume(
    exchange,
    *,
    top_n: int = TOP_N,
    min_quote_vol: float = MIN_QUOTE_VOLUME_24H_USD,
) -> list[UniverseEntry]:
    """Fallback si CoinGecko falla: ordenar tickers Binance spot por quoteVolume 24h."""
    exchange.load_markets()
    tickers = exchange.fetch_tickers()
    rows: list[tuple[str, str, float]] = []
    for sym, t in tickers.items():
        m = exchange.markets.get(sym)
        if not m or m.get("type") != "spot" or m.get("quote") != "USDT":
            continue
        if not m.get("active", True):
            continue
        base = str(m.get("base", "")).upper()
        if _should_exclude_base(base):
            continue
        qv = float(t.get("quoteVolume") or 0)
        if qv < min_quote_vol:
            continue
        rows.append((sym, base, qv))
    rows.sort(key=lambda x: x[2], reverse=True)
    out: list[UniverseEntry] = []
    seen: set[str] = set()
    for sym, base, qv in rows:
        if base in seen:
            continue
        seen.add(base)
        out.append(
            UniverseEntry(
                symbol_ccxt=sym,
                base=base,
                market_cap_usd=float("nan"),
                volume_24h_usd=qv,
                coingecko_id="",
            )
        )
        if len(out) >= top_n:
            break
    logger.warning("Universo por volumen Binance spot: %d símbolos", len(out))
    return out


def universe_futures_by_volume(
    exchange,
    *,
    top_n: int = TOP_N,
    min_quote_vol: float = MIN_QUOTE_VOLUME_24H_USD,
) -> list[UniverseEntry]:
    """Binance USDT-M perpetuos ordenados por quoteVolume 24h."""
    exchange.load_markets()
    tickers = exchange.fetch_tickers()
    rows: list[tuple[str, str, float]] = []
    for sym, t in tickers.items():
        m = exchange.markets.get(sym)
        if not m:
            continue
        # Solo swaps (perpetuos) liquidados en USDT
        if m.get("type") != "swap":
            continue
        if m.get("settle") != "USDT":
            continue
        if not m.get("active", True):
            continue
        base = str(m.get("base", "")).upper()
        if _should_exclude_base(base):
            continue
        qv = float(t.get("quoteVolume") or 0)
        if qv < min_quote_vol:
            continue
        rows.append((sym, base, qv))
    rows.sort(key=lambda x: x[2], reverse=True)
    out: list[UniverseEntry] = []
    seen: set[str] = set()
    for sym, base, qv in rows:
        if base in seen:
            continue
        seen.add(base)
        out.append(
            UniverseEntry(
                symbol_ccxt=sym,
                base=base,
                market_cap_usd=float("nan"),
                volume_24h_usd=qv,
                coingecko_id="",
            )
        )
        if len(out) >= top_n:
            break
    logger.info("Universo Binance USDT-M futures: %d contratos", len(out))
    return out
