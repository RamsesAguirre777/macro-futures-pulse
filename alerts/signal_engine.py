from __future__ import annotations

"""
Signal engine for macro_futures alerts.

Signals evaluated each poll (every POLL_INTERVAL_SEC seconds):
  1. EMA3/9 cross on 1H  — from signals_3_9 field
  2. EMA3 riding BB bands on 1H — requires fresh 1H bar fetch
  3. Open outside BB 1H at session open — fires once per session
  4. Price crosses BP / INT▲▼ / MAX▲▼ between polls
  5. ZB+GC regime change (Pattern V)

User price alerts are also checked here (user_alerts.json).
Anti-spam is handled via state.json — a signal fires only on state change.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytz

from alerts.telegram_sender import send_message

logger = logging.getLogger(__name__)

_ET = pytz.timezone("America/New_York")

_STATE_PATH = Path(__file__).resolve().parent / "state.json"
_USER_ALERTS_PATH = Path(__file__).resolve().parent / "user_alerts.json"

# ─── Symbols that have gap logic (BTC excluded) ───────────────────────────────
_REGIME_SYMBOLS = ("ZB", "GC")
_ALL_SYMBOLS = ("ES", "NQ", "YM", "BTC", "ZB", "GC")

# ─── Level keys tracked for signal 4 ─────────────────────────────────────────
_LEVEL_KEYS = ("bp", "int_pos", "int_neg", "max_pos", "max_neg")


# ─── State persistence ────────────────────────────────────────────────────────

def _load_state() -> dict[str, Any]:
    try:
        return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict[str, Any]) -> None:
    try:
        _STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        logger.error("Failed to save state.json: %s", exc)


def _load_user_alerts() -> list[dict[str, Any]]:
    try:
        return json.loads(_USER_ALERTS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_user_alerts(alerts: list[dict[str, Any]]) -> None:
    try:
        _USER_ALERTS_PATH.write_text(
            json.dumps(alerts, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        logger.error("Failed to save user_alerts.json: %s", exc)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _parse_1h_cross(signals_3_9: str) -> str | None:
    """
    Parse '3/9 up 1h, 3/9 down 15m' → 'up' or 'down' if 1h present, else None.
    """
    if not signals_3_9:
        return None
    for chunk in signals_3_9.split(","):
        parts = chunk.strip().split()
        if len(parts) >= 3 and parts[-1] == "1h":
            direction = parts[1].lower()
            if direction in ("up", "down"):
                return direction
    return None


def _get_regime(zb_dir: str, gc_dir: str) -> str:
    """
    Pattern V regime table from CLAUDE.md.
    zb_dir / gc_dir are the 'direction' field values from the data dict.
    Normalise to 'ALCISTA' / 'BAJISTA' / 'ZM'.
    """
    zb = zb_dir.upper()
    gc = gc_dir.upper()

    zb_alc = "ALCI" in zb or zb == "UP"
    zb_baj = "BAJI" in zb or zb == "DOWN"
    gc_alc = "ALCI" in gc or gc == "UP"
    gc_baj = "BAJI" in gc or gc == "DOWN"

    if zb_alc and gc_alc:
        return "PANIC"
    if zb_baj and gc_baj:
        return "RISK-ON"
    if zb_alc and gc_baj:
        return "USD"
    if zb_baj and gc_alc:
        return "INFLACION"
    return "INDECISO"


def _side(price: float, level: float) -> str:
    """'above' if price >= level, else 'below'."""
    return "above" if price >= level else "below"


# ─── 1H bar fetch (for signals 2 & 3) ────────────────────────────────────────

def _fetch_1h_bars() -> dict[str, list[dict]]:
    """Returns {sym: [1h_bars]} for all FUTURES symbols."""
    try:
        from dc2.macro_futures.data_client import FuturesDataClient

        all_bars = FuturesDataClient().fetch_all_bars(lookback_days=2)
        return {sym: all_bars.get(sym, {}).get("1h", []) for sym in _ALL_SYMBOLS}
    except Exception as exc:
        logger.warning("_fetch_1h_bars failed: %s", exc)
        return {}


# ─── Signal 1: EMA3/9 cross on 1H ────────────────────────────────────────────

def _signal1_ema_cross(futures: dict[str, Any], state: dict[str, Any]) -> None:
    prev: dict[str, str | None] = state.get("ema_cross_1h", {})
    current: dict[str, str | None] = {}

    for sym, row in futures.items():
        cross = _parse_1h_cross(row.get("signals_3_9") or "")
        current[sym] = cross

        prev_cross = prev.get(sym)
        if cross is not None and cross != prev_cross:
            direction_label = "alcista ▲" if cross == "up" else "bajista ▼"
            send_message(f"⚠️ {sym} — cruce {direction_label} EMA3/9 — 1H")
            logger.info("Signal1 fired: %s cross %s", sym, cross)

    state["ema_cross_1h"] = current


# ─── Signal 2: EMA3 riding BB bands on 1H ────────────────────────────────────

def _signal2_ema3_bb(
    futures: dict[str, Any],
    bars_1h: dict[str, list[dict]],
    state: dict[str, Any],
) -> None:
    from dc2.utils import _compute_bb_from_closes

    prev: dict[str, str | None] = state.get("ema3_bb_1h", {})
    current: dict[str, str | None] = {}

    for sym, row in futures.items():
        sym_bars = bars_1h.get(sym, [])
        if len(sym_bars) < 3:
            current[sym] = prev.get(sym)
            continue

        closes = [float(b["c"]) for b in sym_bars]
        bbt, bbb = _compute_bb_from_closes(closes)
        if bbt is None or bbb is None:
            current[sym] = prev.get(sym)
            continue

        # EMA3 from closes
        ema3 = float(
            pd.Series(closes, dtype=float).ewm(span=3, adjust=False).mean().iloc[-1]
        )

        if ema3 >= bbt:
            zone: str | None = "UPPER"
        elif ema3 <= bbb:
            zone = "LOWER"
        else:
            zone = None

        current[sym] = zone
        prev_zone = prev.get(sym)

        if zone is not None and zone != prev_zone:
            side_label = "superior 🔴" if zone == "UPPER" else "inferior 🟢"
            send_message(
                f"🚨 {sym} — EMA3 en BB {side_label} — presión extrema (1H)"
            )
            logger.info("Signal2 fired: %s EMA3 at BB %s", sym, zone)

    state["ema3_bb_1h"] = current


# ─── Signal 3: Open outside BB at 9:30 ET (once per session) ─────────────────

def _signal3_open_bb(
    futures: dict[str, Any],
    bars_1h: dict[str, list[dict]],
    state: dict[str, Any],
) -> None:
    from dc2.utils import _compute_bb_from_closes

    now_et = datetime.now(_ET)
    session_date = now_et.date().isoformat()

    # Only fires between 9:28 and 9:40 ET
    is_open_window = (now_et.hour == 9 and 28 <= now_et.minute <= 40) or (
        now_et.hour == 9 and now_et.minute > 40
    )
    if not (now_et.hour == 9 and 28 <= now_et.minute <= 40):
        return

    fired_today: dict[str, str] = state.get("open_bb_session", {})

    for sym, row in futures.items():
        if fired_today.get(sym) == session_date:
            continue  # already fired today for this symbol

        sym_bars = bars_1h.get(sym, [])
        if len(sym_bars) < 3:
            continue

        closes = [float(b["c"]) for b in sym_bars[:-1]]  # exclude current forming bar
        bbt, bbb = _compute_bb_from_closes(closes)
        if bbt is None or bbb is None:
            continue

        price = row.get("price")
        if price is None:
            continue
        price = float(price)

        if price > bbt:
            side_label = "UPPER 🔴"
        elif price < bbb:
            side_label = "LOWER 🟢"
        else:
            continue

        send_message(f"🔴 {sym} — abrió fuera BB {side_label} (1H 9:30 ET)")
        logger.info("Signal3 fired: %s opened outside BB %s", sym, side_label)
        fired_today[sym] = session_date

    state["open_bb_session"] = fired_today


# ─── Signal 4: Price crosses BP / INT▲▼ / MAX▲▼ ──────────────────────────────

_LEVEL_LABELS: dict[str, str] = {
    "bp": "BP",
    "int_pos": "INT▲",
    "int_neg": "INT▼",
    "max_pos": "MAX▲",
    "max_neg": "MAX▼",
}


def _signal4_level_touch(futures: dict[str, Any], state: dict[str, Any]) -> None:
    prev_sides: dict[str, dict[str, str]] = state.get("level_sides", {})
    new_sides: dict[str, dict[str, str]] = {}

    for sym, row in futures.items():
        price = row.get("price")
        if price is None:
            new_sides[sym] = prev_sides.get(sym, {})
            continue
        price = float(price)

        sym_prev = prev_sides.get(sym, {})
        sym_new: dict[str, str] = {}

        for key in _LEVEL_KEYS:
            level = row.get(key)
            if level is None:
                sym_new[key] = sym_prev.get(key, "above")
                continue
            level = float(level)
            current_side = _side(price, level)
            sym_new[key] = current_side

            prev_side = sym_prev.get(key)
            if prev_side is not None and current_side != prev_side:
                label = _LEVEL_LABELS.get(key, key)
                send_message(
                    f"🎯 {sym} — tocó {label} {level:,.4f} "
                    f"(PRC {price:,.4f})"
                )
                logger.info("Signal4 fired: %s crossed %s at %s", sym, label, level)

        new_sides[sym] = sym_new

    state["level_sides"] = new_sides


# ─── Signal 5: ZB+GC regime change ───────────────────────────────────────────

def _signal5_regime(futures: dict[str, Any], state: dict[str, Any]) -> None:
    zb_row = futures.get("ZB", {})
    gc_row = futures.get("GC", {})
    zb_dir = str(zb_row.get("direction", "N/D"))
    gc_dir = str(gc_row.get("direction", "N/D"))

    regime = _get_regime(zb_dir, gc_dir)
    prev_regime = state.get("regime")

    state["regime"] = regime

    if prev_regime is not None and regime != prev_regime:
        send_message(
            f"🚨 RÉGIMEN CAMBIÓ — {prev_regime} → {regime}\n"
            f"ZB: {zb_dir}  |  GC: {gc_dir}"
        )
        logger.info("Signal5 fired: regime %s → %s", prev_regime, regime)


# ─── User price alerts ────────────────────────────────────────────────────────

def _check_user_alerts(futures: dict[str, Any], state: dict[str, Any]) -> None:
    alerts = _load_user_alerts()
    if not alerts:
        return

    prev_prices: dict[str, float] = state.get("prev_prices", {})
    remaining: list[dict[str, Any]] = []
    changed = False

    for alert in alerts:
        sym = alert.get("symbol", "")
        target = alert.get("target")
        if target is None:
            remaining.append(alert)
            continue

        target = float(target)
        row = futures.get(sym)
        if row is None:
            remaining.append(alert)
            continue

        price = row.get("price")
        if price is None:
            remaining.append(alert)
            continue
        price = float(price)

        prev = prev_prices.get(sym)
        if prev is None:
            # First poll — record price, don't fire yet
            prev_prices[sym] = price
            remaining.append(alert)
            continue

        # Fire on crossing
        crossed = (prev < target <= price) or (prev > target >= price)
        if crossed:
            direction = "▲" if price >= target else "▼"
            send_message(
                f"🔔 {sym} {direction} alerta: tocó {target:,.4f} "
                f"(PRC {price:,.4f})"
            )
            logger.info("User alert fired: %s target %s", sym, target)
            changed = True
            # alert consumed — not added to remaining
        else:
            remaining.append(alert)

    # Update prev_prices with current prices
    for sym, row in futures.items():
        p = row.get("price")
        if p is not None:
            prev_prices[sym] = float(p)
    state["prev_prices"] = prev_prices

    if changed:
        _save_user_alerts(remaining)


# ─── Public entrypoint ────────────────────────────────────────────────────────

def evaluate_signals(data: dict[str, Any]) -> None:
    """
    Called once per poll with the result of run_macro_analysis().

    data = {"futures": {sym: row}, "vix": {...}}
    """
    futures: dict[str, Any] = data.get("futures") or {}
    if not futures:
        logger.debug("evaluate_signals: empty futures dict — skipping")
        return

    state = _load_state()

    # Signals that only need the pre-computed data dict
    try:
        _signal1_ema_cross(futures, state)
    except Exception as exc:
        logger.error("Signal1 error: %s", exc)

    try:
        _signal4_level_touch(futures, state)
    except Exception as exc:
        logger.error("Signal4 error: %s", exc)

    try:
        _signal5_regime(futures, state)
    except Exception as exc:
        logger.error("Signal5 error: %s", exc)

    try:
        _check_user_alerts(futures, state)
    except Exception as exc:
        logger.error("User alerts error: %s", exc)

    # Signals 2 & 3 require a separate bar fetch — degrade gracefully
    try:
        bars_1h = _fetch_1h_bars()
        if bars_1h:
            try:
                _signal2_ema3_bb(futures, bars_1h, state)
            except Exception as exc:
                logger.error("Signal2 error: %s", exc)

            try:
                _signal3_open_bb(futures, bars_1h, state)
            except Exception as exc:
                logger.error("Signal3 error: %s", exc)
    except Exception as exc:
        logger.warning("Bar fetch for signals 2/3 failed — skipped: %s", exc)

    _save_state(state)
