"""Tests mínimos: filtros, régimen, clasificación 00:01."""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from dc2.crypto_screener.compute import CryptoRow, compute_crypto_row
from dc2.crypto_screener.filters import filter_long, filter_short
from dc2.crypto_screener.regime_gate import apply_regime, normalize_regime
from dc2.crypto_screener.run import classify_open_0001


def _make_dfs(*, n_days: int = 800) -> dict[str, pd.DataFrame]:
    idx = pd.date_range("2022-01-01", periods=n_days, freq="D", tz="UTC")
    t = np.linspace(0, 1, n_days)
    # Tendencia alcista + ruido; últimos 20d high por debajo del close final
    price = 100 + 80 * t + np.random.default_rng(42).normal(0, 0.5, n_days)
    high = price * 1.015
    low = price * 0.985
    vol = np.full(n_days, 5e7) * (1.0 + 0.1 * t)
    d1 = pd.DataFrame(
        {"open": price, "high": high, "low": low, "close": price, "volume": vol},
        index=idx,
    )
    # Intradía alcista al final
    nh = 200
    ph = np.linspace(float(price[-1]) * 0.99, float(price[-1]) * 1.03, nh)
    idx_h = pd.date_range(end=idx[-1], periods=nh, freq="h", tz="UTC")
    h1 = pd.DataFrame(
        {
            "open": ph,
            "high": ph * 1.002,
            "low": ph * 0.998,
            "close": ph,
            "volume": np.full(nh, 1e6),
        },
        index=idx_h,
    )
    nm = 300
    pm = np.linspace(float(price[-1]) * 0.99, float(price[-1]) * 1.02, nm)
    idx_m = pd.date_range(end=idx[-1], periods=nm, freq="15min", tz="UTC")
    m15 = pd.DataFrame(
        {
            "open": pm,
            "high": pm * 1.002,
            "low": pm * 0.998,
            "close": pm,
            "volume": np.full(nm, 1e5),
        },
        index=idx_m,
    )
    return {"1d": d1, "1h": h1, "15m": m15}


class TestNormalizeRegime(unittest.TestCase):
    def test_aliases(self) -> None:
        self.assertEqual(normalize_regime("riskon"), "RISK-ON")
        self.assertEqual(normalize_regime("INFLACIÓN"), "INFLACION")
        self.assertEqual(normalize_regime("panic mode"), "PANIC")


class TestApplyRegime(unittest.TestCase):
    def _row(self, symbol: str = "X/USDT") -> CryptoRow:
        return CryptoRow(
            symbol=symbol,
            close_1d=100.0,
            ema3_1d=99.0,
            ema9_1d=98.0,
            ema3_monthly=95.0,
            ema9_monthly=94.0,
            ema20_monthly=90.0,
            monthly_ok=True,
            resistencia_1d=95.0,
            soporte_1d=105.0,
            bp_long=95.0,
            bp_short=105.0,
            volume_usdt_24h=2e8,
            avg_volume_usdt_20d=1e8,
            close_1h=101.0,
            ema20_1h=99.0,
            ema9_15m=101.0,
            ema20_15m=99.0,
            prev_close_1d=99.0,
        )

    def test_panic_clears(self) -> None:
        rr = apply_regime([self._row()], [self._row("Y/USDT")], "PANIC")
        self.assertEqual(len(rr.longs), 0)
        self.assertEqual(len(rr.shorts), 0)

    def test_risk_on_only_long(self) -> None:
        rr = apply_regime([self._row()], [self._row("Y/USDT")], "RISK-ON")
        self.assertEqual(len(rr.longs), 1)
        self.assertEqual(len(rr.shorts), 0)


class TestClassifyOpen(unittest.TestCase):
    def test_long_breakout(self) -> None:
        bp = 100.0
        sig, _ = classify_open_0001(100.0, bp * 1.003, 99.0, bp, "LONG")
        self.assertEqual(sig, "LONG")

    def test_dead_zone(self) -> None:
        bp = 100.0
        sig, note = classify_open_0001(100.0, 100.0, 99.0, bp, "LONG")
        self.assertEqual(sig, "SKIP")
        self.assertIsNotNone(note)
        self.assertIn("zona", (note or "").lower())


class TestComputeAndFilters(unittest.TestCase):
    def test_compute_returns_row(self) -> None:
        dfs = _make_dfs()
        r = compute_crypto_row("TEST/USDT", dfs)
        self.assertIsNotNone(r)
        assert r is not None
        self.assertTrue(r.monthly_ok)
        self.assertGreater(r.close_1d, 0)

    def test_filter_long_solo_1d_bypasses_intraday(self) -> None:
        dfs = {"1d": _make_dfs()["1d"]}
        r = compute_crypto_row("TEST/USDT", dfs, solo_1d=True)
        self.assertIsNotNone(r)
        assert r is not None
        # Con datos sintéticos 1D puede o no pasar; solo comprobamos que no crashea
        _ = filter_long(r, solo_1d=True)


if __name__ == "__main__":
    unittest.main()
