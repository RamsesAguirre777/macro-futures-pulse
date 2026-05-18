from __future__ import annotations
import re
import logging
from datetime import date

from dc2.constants import (
    TICKERS,
    ZONE_SIZE_HINT,
    ALC_T3_SOLO_INT_TICKERS,
    _OPEN_ZONE_SKIP_RULES,
    _OPEN_ZONE_WARN_RULES,
    TIER1,
    TIER2,
)
from dc2.utils import _get_igual_baj_rate

logger = logging.getLogger(__name__)


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
        # Import local (evita ciclo con dc2.indicators, que importa PremiumDetector arriba).
        from dc2.indicators import _open_zone_skip_match

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
                    stat_base = "0 caution BAJ SPY = 77.1%"
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
