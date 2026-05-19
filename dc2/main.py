"""
dc2/main.py — Orquestador principal BB Retroceso.
Reemplaza la función main() de data_collector.py (L3157–4614).
Uso: python -m dc2.main --modo premarket_9_28 [--ticker QQQ]
"""
from __future__ import annotations
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")
if not os.getenv("ALPACA_API_KEY") or not os.getenv("ALPACA_SECRET_KEY"):
    load_dotenv(dotenv_path=Path(__file__).parent.parent.parent / ".env")

import argparse
import asyncio
import logging
from datetime import datetime, timedelta

from dc2.constants import TICKERS, TIMEZONE
from dc2.data_clients import AlpacaHistoricalClient
from dc2.modes.mode_verify import run_verify_mode, run_verify_today
from dc2.modes.mode_9_15 import run_mode_9_15
from dc2.modes.mode_9_28 import run_mode_9_28
from dc2.modes.mode_test import run_mode_test
from dc2.modes.mode_930 import run_mode_930
from dc2.modes.mode_backtest import run_mode_backtest
from dc2.modes.mode_live import run_mode_live

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

logger = logging.getLogger(__name__)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--modo",
        required=True,
        choices=[
            "premarket_9_15",
            "premarket_9_28",
            "open_930",
            "live",
            "verify",
            "test",
            "backtest",
        ],
    )
    parser.add_argument(
        "--ticker",
        default=None,
        help="Obligatorio si --modo verify. Opcional: un solo ticker en modos live/test",
    )
    parser.add_argument(
        "--today",
        action="store_true",
        help="Solo para --modo verify: corre output compacto del día de hoy",
    )
    parser.add_argument(
        "--desde", default=None, help="Fecha inicio backtest (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--hasta", default=None, help="Fecha fin backtest (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--excel", action="store_true", help="Exportar Excel además de DuckDB"
    )
    args = parser.parse_args()

    # ── Resolver lista de tickers ───────────────────────────────────
    if args.ticker:
        t = args.ticker.strip().upper()
        if t not in TICKERS:
            parser.error(f"--ticker {t} no esta en TICKERS")
        tickers_to_run = [t]
    else:
        tickers_to_run = list(TICKERS)

    # ── verify — despacho directo, sin output_data ─────────────────
    if args.modo == "verify":
        if args.today:
            await run_verify_today(tickers_to_run)
            return
        if not args.ticker:
            parser.error("--modo verify requiere --ticker (ej. NVDA)")
        run_verify_mode(args.ticker.strip().upper())
        return

    # ── live — yfinance, no necesita Alpaca ───────────────────────
    if args.modo == "live":
        if not args.ticker:
            parser.error("--modo live requiere --ticker (ej. NVDA)")
        run_mode_live(args.ticker.strip().upper())
        return

    # ── backtest no necesita Alpaca ni output_data ─────────────────
    if args.modo == "backtest":
        desde = args.desde or (
            (datetime.now(TIMEZONE) - timedelta(days=365)).strftime("%Y-%m-%d")
        )
        hasta = args.hasta or datetime.now(TIMEZONE).strftime("%Y-%m-%d")
        await run_mode_backtest(tickers_to_run, desde, hasta, excel=args.excel)
        return

    # ── output_data base (mismo dict que armaba main() en el original) ─
    output_data = {
        "macro": {},
        "tickers": {},
        "skips": {},
        "tickers_order": tickers_to_run,
    }
    date_str = str(datetime.now(TIMEZONE).date())

    # ── Macro (igual que L3195 del original) ───────────────────────
    output_data["macro"] = AlpacaHistoricalClient.get_macro_prices()

    # ── Despacho al modo ────────────────────────────────────────────
    if args.modo == "premarket_9_15":
        await run_mode_9_15(tickers_to_run, date_str, output_data)

    elif args.modo == "premarket_9_28":
        await run_mode_9_28(tickers_to_run, date_str, output_data)

    elif args.modo == "test":
        await run_mode_test(tickers_to_run, date_str, output_data)

    elif args.modo == "open_930":
        # open_930 lee su propio JSON premarket_9_28 internamente
        await run_mode_930(tickers_to_run, date_str)


if __name__ == "__main__":
    asyncio.run(main())
