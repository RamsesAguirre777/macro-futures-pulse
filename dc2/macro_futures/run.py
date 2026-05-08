from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
import pytz

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dc2.macro_futures.config import OUTPUT_DIR, POLL_INTERVAL_SEC  # noqa: E402
from dc2.macro_futures.mode_macro import run_macro_analysis  # noqa: E402

_ET = pytz.timezone("America/New_York")


def _setup_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
        force=True,
    )


def _fmt_dist_pct(p: object) -> str:
    if p is None:
        return ""
    try:
        x = float(p)
    except (TypeError, ValueError):
        return str(p)
    if x > 0:
        return f"+{x:.3f}%"
    return f"{x:.3f}%"


def _fmt_signals_compact(s: str) -> str:
    """'3/9 up 15m, 3/9 down 30m' → '15m▲ 30m▼'."""
    if not s:
        return ""
    out: list[str] = []
    for chunk in s.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split()
        if len(parts) < 3:
            continue
        direction = parts[1].lower()
        tf = parts[-1]
        if direction == "up":
            arrow = "▲"
        elif direction == "down":
            arrow = "▼"
        else:
            arrow = "?"
        out.append(f"{tf}{arrow}")
    return " ".join(out)


def _fmt_card_rango(precio: object, hi: object, lo: object) -> str:
    """DAY/PRV: ▲ % to high, ▼ % to low (▲- if no data or price already above high)."""
    if precio is None:
        return "▲-  ▼-"
    try:
        pa = float(precio)
    except (TypeError, ValueError):
        return "▲-  ▼-"
    if pa == 0:
        return "▲-  ▼-"

    if hi is None:
        up_str = "▲-"
    else:
        u = (float(hi) - pa) / pa * 100.0
        up_str = "▲-" if u < 0 else f"▲{u:.2f}%"

    if lo is None:
        dn_str = "▼-"
    else:
        d = (pa - float(lo)) / pa * 100.0
        dn_str = "▼-" if d < 0 else f"▼{d:.2f}%"

    return f"{up_str} {dn_str}"


def _fmt_gap_line1(gap_tipo: object, gap_mag: object) -> str:
    if gap_tipo is None:
        return ""
    if gap_tipo == "GAP_UP":
        try:
            g = float(gap_mag)
        except (TypeError, ValueError):
            return ""
        return f"  ▲GAP +{g:.2f}%"
    if gap_tipo == "GAP_DOWN":
        try:
            g = float(gap_mag)
        except (TypeError, ValueError):
            return ""
        return f"  ▼GAP -{g:.2f}%"
    if gap_tipo == "FLAT":
        return "  GAP ~0"
    return ""


def _print_table(data: dict) -> None:
    w = 54
    now = datetime.now(_ET)
    title = now.strftime("MACRO FUTURES — %H:%M ET  %Y-%m-%d")

    print("\n" + "═" * w)
    print(title)
    print("Session opens: ES/NQ/YM=18:00 ET  |  BTC=00:00 ET")
    print("═" * w)

    cards: list[tuple[str, dict]] = []
    for sym in ("ES", "NQ", "YM", "BTC"):
        r = data.get(sym)
        if r:
            cards.append((sym, r))

    for idx, (sym, r) in enumerate(cards):
        direction = str(r.get("direction", ""))
        zona = str(r.get("zona", ""))
        d_pct = _fmt_dist_pct(r.get("dist_overnight_pct"))
        if not d_pct:
            d_pct = "—"
        bd = r.get("badge")
        bdg_str = f"{float(bd):.1f}" if bd is not None else "N/D"

        line1 = (
            f"  {sym:>5}    {direction:<9}  {zona:>4}    D% {d_pct}    BDG {bdg_str}"
            f"{_fmt_gap_line1(r.get('gap_tipo'), r.get('gap_mag'))}"
        )

        pr = r.get("price")
        try:
            pr_f = float(pr) if pr is not None else None
        except (TypeError, ValueError):
            pr_f = None
        prc_part = f"{pr_f:>10,.2f}" if pr_f is not None else f"{'N/D':>10}"
        line2 = f"  PRC {prc_part}    BP "
        if r.get("bp") is None:
            line2 += " N/D"
        else:
            line2 += f"{float(r['bp']):>10,.4f}"

        if r.get("bp") is None:
            line3 = "  INT  N/D    MAX  N/D"
        else:
            ip, inn = float(r["int_pos"]), float(r["int_neg"])
            mp, mn = float(r["max_pos"]), float(r["max_neg"])
            if sym == "BTC":
                line3 = (
                    f"  INT  ▲{ip:,.0f}  ▼{inn:,.0f}    "
                    f"MAX  ▲{mp:,.0f}  ▼{mn:,.0f}"
                )
            else:
                line3 = (
                    f"  INT  ▲{ip:,.2f}  ▼{inn:,.2f}    "
                    f"MAX  ▲{mp:,.2f}  ▼{mn:,.2f}"
                )

        sig = _fmt_signals_compact(r.get("signals_3_9") or "")
        dia_s = _fmt_card_rango(
            r.get("price"), r.get("session_high"), r.get("session_low")
        )
        prv_s = _fmt_card_rango(
            r.get("price"), r.get("prev_session_high"), r.get("prev_session_low")
        )
        line4 = f"  SIG  {sig}    DAY {dia_s}   PRV {prv_s}"

        print(line1)
        print(line2)
        print(line3)
        print(line4)

        if idx < len(cards) - 1:
            print("─" * w)

    print("═" * w + "\n")


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    out_dir = Path(OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "macro.log"
    _setup_logging(log_path)
    log = logging.getLogger("macro_futures.run")

    print(
        "⚠️  yfinance: ~10-15min delay en futuros ES/NQ/YM. "
        "BTC Binance = real-time."
    )
    log.info("macro_futures run started (poll=%ss)", POLL_INTERVAL_SEC)

    try:
        while True:
            try:
                result = run_macro_analysis(lookback_days=5)
                _print_table(result)
                fname = datetime.now(_ET).strftime("macro_%Y-%m-%d_%H-%M.json")
                jpath = out_dir / fname
                with open(jpath, "w", encoding="utf-8") as f:
                    json.dump(result, f, indent=2, ensure_ascii=False, default=str)
                log.info("JSON saved: %s", jpath)
            except Exception as e:
                log.exception("Error en ciclo: %s", e)

            time.sleep(POLL_INTERVAL_SEC)
    except KeyboardInterrupt:
        log.info("Stopped by user (KeyboardInterrupt)")
        print("\nStopped.")


if __name__ == "__main__":
    main()
