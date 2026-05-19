from __future__ import annotations

"""
Telegram bot for user-defined price alerts.

Commands:
  /alerta ES 5500        — add price alert for ES at 5500
  /mis_alertas           — list active alerts
  /borrar ES             — remove all alerts for ES
  /borrar_todas          — remove all alerts

Run standalone:
  python -m alerts.price_alerts
"""

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from alerts.bb_commands import cmd_bb, cmd_macro, cmd_screener
from alerts.config import TELEGRAM_TOKEN
from alerts.signal_engine import _load_user_alerts, _save_user_alerts

logger = logging.getLogger(__name__)

_VALID_SYMBOLS = {"ES", "NQ", "YM", "BTC", "ZB", "GC"}


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Devuelve el chat_id para pegarlo en alerts/config.py."""
    cid = update.effective_chat.id
    await update.message.reply_text(
        f"Tu chat_id es:\n<code>{cid}</code>\n\n"
        "Pégalo en <b>alerts/config.py</b> → TELEGRAM_CHAT_ID",
        parse_mode="HTML",
    )


async def cmd_alerta(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /alerta <SYMBOL> <PRICE>"""
    args = context.args or []
    if len(args) != 2:
        await update.message.reply_text(
            "Uso: /alerta <SÍMBOLO> <PRECIO>\nEjemplo: /alerta ES 5500"
        )
        return

    sym = args[0].upper()
    if sym not in _VALID_SYMBOLS:
        await update.message.reply_text(
            f"Símbolo inválido. Opciones: {', '.join(sorted(_VALID_SYMBOLS))}"
        )
        return

    try:
        target = float(args[1].replace(",", ""))
    except ValueError:
        await update.message.reply_text("Precio inválido. Usa un número, ej: 5500.25")
        return

    alerts = _load_user_alerts()
    alerts.append({"symbol": sym, "target": target})
    _save_user_alerts(alerts)

    await update.message.reply_text(
        f"✅ Alerta guardada: {sym} @ {target:,.4f}\n"
        f"Activas para {sym}: {sum(1 for a in alerts if a['symbol'] == sym)}"
    )
    logger.info("Alert added: %s @ %s", sym, target)


async def cmd_mis_alertas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    alerts = _load_user_alerts()
    if not alerts:
        await update.message.reply_text("No hay alertas activas.")
        return

    lines = ["📋 Alertas activas:"]
    by_sym: dict[str, list[float]] = {}
    for a in alerts:
        by_sym.setdefault(a["symbol"], []).append(float(a["target"]))

    for sym in sorted(by_sym):
        targets_str = "  |  ".join(f"{t:,.4f}" for t in sorted(by_sym[sym]))
        lines.append(f"  {sym}: {targets_str}")

    await update.message.reply_text("\n".join(lines))


async def cmd_borrar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /borrar <SYMBOL>"""
    args = context.args or []
    if len(args) != 1:
        await update.message.reply_text("Uso: /borrar <SÍMBOLO>\nEjemplo: /borrar ES")
        return

    sym = args[0].upper()
    alerts = _load_user_alerts()
    before = len(alerts)
    alerts = [a for a in alerts if a["symbol"] != sym]
    removed = before - len(alerts)
    _save_user_alerts(alerts)

    if removed:
        await update.message.reply_text(f"🗑 {removed} alerta(s) de {sym} eliminadas.")
    else:
        await update.message.reply_text(f"No había alertas para {sym}.")
    logger.info("Removed %d alerts for %s", removed, sym)


async def cmd_borrar_todas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    alerts = _load_user_alerts()
    count = len(alerts)
    _save_user_alerts([])
    await update.message.reply_text(f"🗑 {count} alerta(s) eliminadas.")
    logger.info("All %d user alerts removed", count)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if not TELEGRAM_TOKEN:
        logger.error(
            "TELEGRAM_TOKEN no configurado en alerts/config.py — bot no puede iniciar."
        )
        sys.exit(1)

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(CommandHandler("alerta", cmd_alerta))
    app.add_handler(CommandHandler("mis_alertas", cmd_mis_alertas))
    app.add_handler(CommandHandler("borrar", cmd_borrar))
    app.add_handler(CommandHandler("borrar_todas", cmd_borrar_todas))
    app.add_handler(CommandHandler("screener", cmd_screener))
    app.add_handler(CommandHandler("bb", cmd_bb))
    app.add_handler(CommandHandler("macro", cmd_macro))

    logger.info("Price-alerts bot iniciado. Ctrl+C para detener.")
    app.run_polling()


if __name__ == "__main__":
    main()
