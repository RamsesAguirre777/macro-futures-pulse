from __future__ import annotations
import os
import asyncio
import time
import logging
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
import pytz
import finnhub  # noqa: F401
import websockets

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed
from alpaca.data.live import StockDataStream

from dc2.constants import TICKERS, TICKERS_MACRO, TIMEZONE, UTC, N_VELAS_BB
from dc2.utils import (
    _bars_list_to_mi_dataframe,
    _filter_bars_before,
    _compute_bb_from_closes,
)

# Cliente Alpaca (histórico) — mismas variables que data_collector; cliente creado bajo demanda
# para permitir `import dc2.data_clients` sin .env (tests que no llaman a Alpaca).
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
_alpaca_historical_singleton: StockHistoricalDataClient | None = None


def _alpaca_historical() -> StockHistoricalDataClient:
    global _alpaca_historical_singleton
    if _alpaca_historical_singleton is None:
        if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
            raise ValueError(
                "ALPACA_API_KEY o ALPACA_SECRET_KEY no encontradas en .env"
            )
        _alpaca_historical_singleton = StockHistoricalDataClient(
            ALPACA_API_KEY, ALPACA_SECRET_KEY
        )
    return _alpaca_historical_singleton

os.makedirs("outputs", exist_ok=True)
os.makedirs("logs", exist_ok=True)

logger = logging.getLogger(__name__)


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
        resp = _alpaca_historical().get_stock_bars(request)

        try:
            bars = resp[ticker]
        except (KeyError, TypeError):
            bars = []
        time.sleep(0.1)  # rate-limiting conservador para free plan
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
                if isinstance(bars, pd.DataFrame):
                    cutoff = pd.Timestamp(cutoff_time)
                    if cutoff.tzinfo is None:
                        cutoff = cutoff.tz_localize(TIMEZONE)
                    else:
                        cutoff = cutoff.tz_convert(TIMEZONE)
                    # cutoff estricto: ts < cutoff
                    df_slice = bars.loc[: cutoff - pd.Timedelta(nanoseconds=1)]
                    bars_filtered = df_slice.reset_index()[["t", "c"]].to_dict("records")
                else:
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
                if isinstance(bars, pd.DataFrame):
                    bars_filtered = bars.reset_index()[["t", "c"]].to_dict("records")
                else:
                    bars_filtered = bars

            if len(bars_filtered) < 3:
                continue

            closes = pd.Series([float(b["c"]) for b in bars_filtered], dtype=float)
            ema3 = closes.ewm(span=3, adjust=False).mean().iloc[-1]
            ema9 = closes.ewm(span=9, adjust=False).mean().iloc[-1]
            logger.debug(
                "[BP] tf=%s bars=%d ema3=%.4f ema9=%.4f",
                tf,
                len(bars_filtered),
                float(ema3),
                float(ema9),
            )
            ema3_list.append(float(ema3))
            ema9_list.append(float(ema9))

        if not ema3_list:
            logger.warning("[BP] NINGÚN TF con >=3 barras — BP=None")
            return None

        avg_ema3 = np.mean(ema3_list)
        avg_ema9 = np.mean(ema9_list)
        bp = (avg_ema3 + avg_ema9) / 2
        logger.info(
            "[BP] tfs_usados=%d avg_ema3=%.4f avg_ema9=%.4f BP=%.4f",
            len(ema3_list),
            avg_ema3,
            avg_ema9,
            bp,
        )

        return bp

class TargetsCalculator:
    """Calcula targets INT/MAX y aplica reglas de asimetría."""

    @staticmethod
    def calculate_range_3d(
        ticker,
        daily_bars,
        pm_high=None,
        pm_low=None,
        bars_1h=None,
        cutoff_time=None,
    ):
        """Rango para targets: ancho BB(20,2) EWM en 1H pre-cutoff, o fallback diario.

        Si `bars_1h` y `cutoff_time`: filtra 1H con t < cutoff, últimos 20 cierres,
        BBT−BBB = range_3d. Si no hay datos o BB nulo: avg(H−L) últimos 3 días en
        `daily_bars`. pm_high/pm_low se aceptan por compatibilidad pero no se usan.
        """
        attempted_1h = bool(bars_1h) and cutoff_time is not None
        if attempted_1h:
            filtered = _filter_bars_before(bars_1h, cutoff_time)
            if len(filtered) >= N_VELAS_BB:
                closes = [float(b["c"]) for b in filtered[-N_VELAS_BB:]]
                bbt, bbb = _compute_bb_from_closes(closes)
                if bbt is not None and bbb is not None:
                    range_3d = float(bbt) - float(bbb)
                    if range_3d > 0:
                        logger.info(
                            "[RANGE3D] %s: BB_width_1H=%.4f (bbt=%.4f bbb=%.4f)",
                            ticker,
                            range_3d,
                            float(bbt),
                            float(bbb),
                        )
                        return range_3d
            logger.info(
                "[RANGE3D] %s: fallback avg H-L 3d diarios "
                "(1H: n=%d filtradas; requiere >=%d y BB válido con ancho>0)",
                ticker,
                len(filtered),
                N_VELAS_BB,
            )
        if not daily_bars:
            return 0.0
        last3 = daily_bars[-3:]
        ranges = [
            float(b["h"]) - float(b["l"])
            for b in last3
            if float(b["h"]) > float(b["l"])
        ]
        if not ranges:
            return 0.0
        avg_hl = sum(ranges) / len(ranges)
        if not attempted_1h:
            logger.debug(
                "[RANGE3D] %s: d_ranges=%s avg=%.4f (sin 1H/cutoff)",
                ticker,
                [round(r, 4) for r in ranges],
                avg_hl,
            )
        else:
            logger.debug(
                "[RANGE3D] %s: d_ranges=%s avg=%.4f",
                ticker,
                [round(r, 4) for r in ranges],
                avg_hl,
            )
        return avg_hl

    @staticmethod
    def calculate_targets(
        bp,
        range_3d,
        signals=None,
        *,
        caution_note=None,
        gap_type=None,
    ):
        """Calcula targets INT y MAX simétricos — alineado con herramienta Milton."""
        base_int = 0.33 * range_3d
        base_max = 0.66 * range_3d

        result = {
            "int_pos": bp + base_int,
            "int_neg": bp - base_int,
            "max_pos": bp + base_max,
            "max_neg": bp - base_max,
            "int_dist": base_int,
            "max_dist": base_max,
        }
        logger.info(
            "[TARGETS] BP=%.4f range3d=%.4f | int_pos=%.4f int_neg=%.4f max_pos=%.4f max_neg=%.4f",
            bp,
            range_3d,
            result["int_pos"],
            result["int_neg"],
            result["max_pos"],
            result["max_neg"],
        )
        return result

class AlpacaWSCollector:
    """Colector en tiempo real via alpaca-py WebSocket (trades → 1m OHLCV).
    Si Alpaca falla (p. ej. HTTP 429), fallback a Finnhub WebSocket 9:13–9:28 ET."""

    def __init__(self):
        self.bars_1m = {ticker: [] for ticker in TICKERS}

    def _prefetch_premarket_bars(self, start_4am: datetime, end_913: datetime) -> None:
        """Carga 1m histórico 4:00–antes del WS (fin exclusivo ~9:13) como base para BP estable."""
        for ticker in TICKERS:
            try:
                bars = AlpacaHistoricalClient.fetch_bars(
                    ticker, "1m", start_4am, end_913
                )
                self.bars_1m[ticker] = bars
                if len(bars) < 50:
                    logger.warning(
                        "[PREFETCH] %s: solo %d barras — BP puede ser impreciso (esperado >=200)",
                        ticker,
                        len(bars),
                    )
                logger.info(
                    "[PREFETCH] %s: %d barras 1m (%s → %s)",
                    ticker,
                    len(bars),
                    start_4am.strftime("%H:%M"),
                    end_913.strftime("%H:%M"),
                )
            except Exception as e:
                logger.warning("[%s] prefetch pre-market falló: %s — continúa solo WS", ticker, e)
                self.bars_1m[ticker] = []

    def _aggregate_bars_from_raw(self, raw_trades: dict) -> None:
        """Agrupa trades en barras 1m OHLCV (misma lógica que el acumulador Alpaca).
        No borra self.bars_1m (prefetch); concatena y ordena por tiempo."""
        aggregated: dict[str, list] = {ticker: [] for ticker in TICKERS}
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
                        aggregated[ticker].append(current_bar)
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
                aggregated[ticker].append(current_bar)

        for ticker in TICKERS:
            prefetch_count = len(self.bars_1m[ticker])
            ws_count = len(aggregated[ticker])
            merged = self.bars_1m[ticker] + aggregated[ticker]
            merged.sort(key=lambda x: x["t"])
            self.bars_1m[ticker] = merged
            logger.info(
                "[BARS_MERGE] %s: prefetch=%d WS=%d total=%d",
                ticker,
                prefetch_count,
                ws_count,
                len(merged),
            )

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
        start_4am = start_time_et.replace(hour=4, minute=0, second=0, microsecond=0)
        end_913 = start_time_et
        self._prefetch_premarket_bars(start_4am, end_913)
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
        resp = _alpaca_historical().get_stock_bars(request)
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
