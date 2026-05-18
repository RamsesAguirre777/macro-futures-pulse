"""Orquestación: scan 23:55 UTC y open_signal 00:01 UTC."""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dc2.crypto_screener.compute import CryptoRow, compute_crypto_row
from dc2.crypto_screener.data_fetcher import (
    fetch_bundle_for_screener,
    fetch_ohlcv_df,
    make_binance_exchange,
    make_binance_futures_exchange,
    since_ms_for_last_n_days,
)
from dc2.crypto_screener.filters import filter_long, filter_short
from dc2.crypto_screener.output import format_open_signals_json, format_scan_markdown
from dc2.crypto_screener.regime_gate import apply_regime
from dc2.crypto_screener.storage import (
    CRYPTO_DB_PATH,
    get_crypto_conn,
    load_daily_rows_for_open,
    persist_daily_scan,
    persist_open_signals,
)
from dc2.crypto_screener.universe import (
    UniverseEntry,
    build_universe_top_mcap,
    universe_fallback_by_volume,
    universe_futures_by_volume,
)

logger = logging.getLogger(__name__)


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _parse_pairs(s: str | None) -> list[UniverseEntry] | None:
    if not s:
        return None
    out: list[UniverseEntry] = []
    for part in s.split(","):
        p = part.strip().upper()
        if not p:
            continue
        if "/" not in p:
            p = f"{p}/USDT"
        base = p.split("/")[0]
        out.append(
            UniverseEntry(
                symbol_ccxt=p,
                base=base,
                market_cap_usd=float("nan"),
                volume_24h_usd=float("nan"),
                coingecko_id="",
            )
        )
    return out or None


def classify_open_0001(
    first_open: float,
    first_close: float,
    prev_close: float,
    bp: float,
    scan_dir: str,
) -> tuple[str, str | None]:
    """
    Retorna (señal, nota). LONG/SHORT/SKIP; GAP_REVIEW si gap > +5% (alcista).
    """
    note: str | None = None
    if prev_close and prev_close > 0:
        gap = (first_open - prev_close) / prev_close * 100.0
        if gap > 5.0:
            note = f"GAP_REVIEW gap_open_vs_prev_close=+{gap:.2f}%"
    hi = bp * 1.0025
    lo = bp * 0.9975
    if scan_dir.upper() == "LONG":
        if first_close > hi:
            sig = "LONG"
        elif first_close < lo:
            sig = "SKIP"
            note = (note + "; " if note else "") + "breakdown vs BP (lista LONG)"
        else:
            sig = "SKIP"
            note = (note + "; " if note else "") + "zona_muerta"
    else:
        if first_close < lo:
            sig = "SHORT"
        elif first_close > hi:
            sig = "SKIP"
            note = (note + "; " if note else "") + "breakup vs BP (lista SHORT)"
        else:
            sig = "SKIP"
            note = (note + "; " if note else "") + "zona_muerta"
    return sig, note


def run_scan(
    fecha: date,
    regime: str,
    *,
    pairs_override: list[UniverseEntry] | None,
    solo_1d: bool,
    indeciso_top: int,
    use_volume_fallback: bool,
    binance_only: bool = False,
    futures: bool = False,
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ex = make_binance_futures_exchange() if futures else make_binance_exchange()
    if pairs_override:
        universe = pairs_override
        logger.info("Universo override: %d pares", len(universe))
    elif futures:
        universe = universe_futures_by_volume(ex)
    elif binance_only:
        universe = universe_fallback_by_volume(ex)
    else:
        try:
            universe = build_universe_top_mcap(ex)
        except Exception as e:
            logger.error("Universo CoinGecko falló: %s", e)
            universe = []
        if not universe and use_volume_fallback:
            universe = universe_fallback_by_volume(ex)
    logger.info("Screener sobre %d símbolos", len(universe))

    longs: list[CryptoRow] = []
    shorts: list[CryptoRow] = []
    for i, ent in enumerate(universe):
        sym = ent.symbol_ccxt
        if i % 20 == 0:
            logger.info("Progreso %d/%d %s", i, len(universe), sym)
        try:
            if solo_1d:
                since_d = since_ms_for_last_n_days(1100)
                d1 = fetch_ohlcv_df(ex, sym, "1d", since_ms=since_d)
                dfs = {"1d": d1}
            else:
                dfs = fetch_bundle_for_screener(ex, sym)
            row = compute_crypto_row(sym, dfs, solo_1d=solo_1d)
            if row is None:
                continue
            if filter_long(row, solo_1d=solo_1d):
                longs.append(row)
            if filter_short(row, solo_1d=solo_1d):
                shorts.append(row)
        except Exception as e:
            logger.warning("%s omitido: %s", sym, e)

    logger.info("Pre-régimen — LONG: %d SHORT: %d", len(longs), len(shorts))
    rr = apply_regime(longs, shorts, regime, indeciso_top_n=indeciso_top)
    logger.info(
        "Post-régimen — LONG: %d SHORT: %d | %s",
        len(rr.longs),
        len(rr.shorts),
        rr.note,
    )

    conn = get_crypto_conn()
    try:
        persist_daily_scan(
            conn,
            fecha,
            regime,
            rr.longs,
            rr.shorts,
            size_hint_default=rr.size_hint,
        )
    finally:
        conn.close()

    md = format_scan_markdown(
        fecha,
        regime,
        rr,
        db_path=str(CRYPTO_DB_PATH),
    )
    print(md)


def run_open_signal(
    fecha: date,
    *,
    scan_date: date | None = None,
) -> None:
    """Evalúa primera vela 1m UTC del día ``fecha`` vs BP guardado en ``scan_date``."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if scan_date is None:
        scan_date = fecha - timedelta(days=1)
    conn = get_crypto_conn()
    try:
        rows = load_daily_rows_for_open(conn, scan_date)
    finally:
        conn.close()
    if not rows:
        logger.warning("Sin filas en screener_crypto_daily para %s", scan_date)
        return

    ex = make_binance_exchange()
    start = datetime(fecha.year, fecha.month, fecha.day, 0, 0, 0, tzinfo=timezone.utc)
    since_ms = int(start.timestamp() * 1000)
    records: list[dict] = []

    for row in rows:
        sym = row["symbol"]
        dire = row["direccion"]
        bp = float(row["bp_long"] or 0) if dire == "LONG" else float(row["bp_short"] or 0)
        prev_close = float(row["close_1d"] or 0)
        try:
            bars = ex.fetch_ohlcv(sym, "1m", since=since_ms, limit=5)
        except Exception as e:
            logger.warning("%s: %s", sym, e)
            records.append(
                {
                    "symbol": sym,
                    "direccion_scan": dire,
                    "open_signal": "SKIP",
                    "first_1m_close": None,
                    "bp_used": bp,
                    "gap_pct": None,
                    "note": f"fetch error: {e}",
                }
            )
            continue
        if not bars:
            records.append(
                {
                    "symbol": sym,
                    "direccion_scan": dire,
                    "open_signal": "SKIP",
                    "first_1m_close": None,
                    "bp_used": bp,
                    "gap_pct": None,
                    "note": "sin velas 1m",
                }
            )
            continue
        o1, c1 = float(bars[0][1]), float(bars[0][4])
        gap_pct = (
            (o1 - prev_close) / prev_close * 100.0 if prev_close and prev_close > 0 else None
        )
        sig, note = classify_open_0001(o1, c1, prev_close, bp, dire)
        if gap_pct is not None and gap_pct > 5.0 and sig in ("LONG", "SHORT"):
            note = (note + "; " if note else "") + "GAP_REVIEW>5%"
        records.append(
            {
                "symbol": sym,
                "direccion_scan": dire,
                "open_signal": sig,
                "first_1m_close": c1,
                "bp_used": bp,
                "gap_pct": gap_pct,
                "note": note,
            }
        )

    conn = get_crypto_conn()
    try:
        persist_open_signals(conn, fecha, records)
    finally:
        conn.close()

    print(format_open_signals_json(records))


def main() -> None:
    p = argparse.ArgumentParser(description="Crypto screener Binance spot (USDT)")
    p.add_argument(
        "--mode",
        choices=("scan", "open_signal"),
        default="scan",
        help="scan (~23:55 UTC) u open_signal (00:01 UTC)",
    )
    p.add_argument("--fecha", type=str, default=None, help="YYYY-MM-DD (UTC)")
    p.add_argument(
        "--scan-date",
        type=str,
        default=None,
        help="open_signal: fecha DuckDB del scan previo (default: fecha-1 día)",
    )
    p.add_argument(
        "--regime",
        type=str,
        default="RISK-ON",
        help="RISK-ON | RISK-OFF | PANIC | INFLACION | INDECISO",
    )
    p.add_argument(
        "--pairs",
        type=str,
        default=None,
        help="Override: BTC/USDT,ETH/USDT",
    )
    p.add_argument(
        "--solo-1d",
        action="store_true",
        help="Solo datos 1D reales; intradía sintético y filtros 1h/15m omitidos",
    )
    p.add_argument(
        "--indeciso-top",
        type=int,
        default=3,
        help="INDECISO: máximo candidatos tras ranking",
    )
    p.add_argument(
        "--volume-fallback",
        action="store_true",
        help="Si CoinGecko falla, universo por volumen Binance",
    )
    p.add_argument(
        "--binance-only",
        action="store_true",
        help="Universo spot Binance por volumen 24h (sin CoinGecko)",
    )
    p.add_argument(
        "--futures",
        action="store_true",
        help="Usar Binance USDT-M perpetuos (~300 contratos, volumen 5-10x mayor)",
    )
    args = p.parse_args()

    fecha = date.fromisoformat(args.fecha) if args.fecha else _utc_today()
    scan_date = (
        date.fromisoformat(args.scan_date)
        if args.scan_date
        else (fecha - timedelta(days=1))
    )
    override = _parse_pairs(args.pairs)

    if args.mode == "scan":
        run_scan(
            fecha,
            args.regime,
            pairs_override=override,
            solo_1d=args.solo_1d,
            indeciso_top=args.indeciso_top,
            use_volume_fallback=args.volume_fallback,
            binance_only=args.binance_only,
            futures=args.futures,
        )
    else:
        run_open_signal(fecha, scan_date=scan_date)


if __name__ == "__main__":
    main()
