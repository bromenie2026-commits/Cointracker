"""
monitor.py — positie-monitor (plan §8.3).

Bewaakt de munten die je ECHT gekocht hebt tegen de regels die je zelf hebt
opgeschreven in `risk_config.yaml`, en mailt zodra een niveau geraakt wordt.

Dit is geen trading-bot. Hij koopt en verkoopt niets en kan dat ook niet. Hij
tikt je op de schouder met wat je zelf besloot toen je nog rustig keek — het
moment waarop dat het moeilijkst is, is precies het moment waarop je een
positie open hebt staan.

Je vult `posities.yaml` handmatig. Dat is bewust: het systeem hoort niet te
weten wat je koopt tenzij jij het vertelt.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

import config
import data_sources
import state_store

log = logging.getLogger(__name__)


@dataclass
class Position:
    mint: str
    symbol: str = ""
    entry_price_usd: Optional[float] = None
    entry_marketcap_eur: Optional[float] = None
    inleg_eur: Optional[float] = None
    gekocht_op: str = ""


@dataclass
class Trigger:
    position: Position
    level: str          # "stop_loss" of "tp_100" enzovoort
    pct_change: float
    action: str
    current_price_usd: Optional[float]
    current_mc_eur: Optional[float]


def load_positions(path=None) -> list[Position]:
    """Leest posities.yaml. Ontbreekt het bestand, dan zijn er geen posities."""
    path = path or config.POSITIONS_PATH
    try:
        import yaml

        if not path.exists():
            return []
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception as exc:  # noqa: BLE001 — mag nooit de scan blokkeren
        log.warning("posities.yaml niet leesbaar: %s", exc)
        return []

    entries = data.get("posities") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []

    out: list[Position] = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("mint"):
            continue
        out.append(
            Position(
                mint=str(entry["mint"]).strip(),
                symbol=str(entry.get("symbol", "")).strip(),
                entry_price_usd=_f(entry.get("entry_price_usd")),
                entry_marketcap_eur=_f(entry.get("entry_marketcap_eur")),
                inleg_eur=_f(entry.get("inleg_eur")),
                gekocht_op=str(entry.get("gekocht_op", "")),
            )
        )
    return out


def _f(value: Any) -> Optional[float]:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def current_change_pct(position: Position, pair) -> Optional[float]:
    """Verandering sinds instap, via prijs of anders via marketcap."""
    if pair is None:
        return -100.0  # geen markt meer
    if position.entry_price_usd and pair.price_usd:
        return (pair.price_usd / position.entry_price_usd - 1.0) * 100.0
    if position.entry_marketcap_eur:
        mc_usd = pair.market_cap_usd if pair.market_cap_usd is not None else pair.fdv_usd
        mc_eur = data_sources.usd_to_eur(mc_usd)
        if mc_eur:
            return (mc_eur / position.entry_marketcap_eur - 1.0) * 100.0
    return None


def levels_from_risk_config(risk: dict[str, Any]) -> list[tuple[str, float, str]]:
    """Zet risk_config.yaml om in een lijst van (naam, drempel%, actie)."""
    per_trade = risk.get("per_trade") or {}
    levels: list[tuple[str, float, str]] = []

    stop = _f(per_trade.get("stop_loss_pct"))
    if stop is not None:
        levels.append(("stop_loss", stop, f"Je stop-loss van {stop:g}% is geraakt."))

    for step in per_trade.get("take_profit_ladder") or []:
        if not isinstance(step, dict):
            continue
        at = _f(step.get("at_pct"))
        sell = _f(step.get("sell_pct"))
        if at is None:
            continue
        actie = f"Jouw regel zegt: verkoop {sell:g}% van de positie." if sell else "Jouw regel zegt: neem winst."
        if at >= 100 and sell and sell >= 50:
            actie += " Daarmee haal je je inleg eruit."
        levels.append((f"tp_{at:g}", at, actie))

    return levels


def check_positions(risk: dict[str, Any], now_state: Optional[dict] = None) -> list[Trigger]:
    """Kijkt welke niveaus NIEUW geraakt zijn. Geeft alleen die terug."""
    if not config.POSITION_MONITOR_ENABLED:
        return []

    positions = load_positions()
    if not positions:
        return []

    state = now_state if now_state is not None else state_store.load(config.POSITION_STATE_PATH)
    levels = levels_from_risk_config(risk)
    triggers: list[Trigger] = []

    for position in positions:
        pairs, status = data_sources.fetch_pairs_for_token(position.mint)
        if status == "error":
            log.warning("Positie %s: API-fout, deze ronde overgeslagen", position.mint)
            continue
        pair = data_sources.best_pair(pairs)
        change = current_change_pct(position, pair)
        if change is None:
            continue

        gemeld = set((state.get(position.mint) or {}).get("gemeld", []))
        mc_usd = None
        if pair is not None:
            mc_usd = pair.market_cap_usd if pair.market_cap_usd is not None else pair.fdv_usd

        for naam, drempel, actie in levels:
            geraakt = change <= drempel if drempel < 0 else change >= drempel
            if geraakt and naam not in gemeld:
                triggers.append(
                    Trigger(
                        position=position,
                        level=naam,
                        pct_change=change,
                        action=actie,
                        current_price_usd=(pair.price_usd if pair else None),
                        current_mc_eur=data_sources.usd_to_eur(mc_usd),
                    )
                )
                gemeld.add(naam)

        state[position.mint] = {
            "gemeld": sorted(gemeld),
            "laatste_pct": round(change, 1),
            "symbol": position.symbol,
        }

    if now_state is None:
        state_store.save(config.POSITION_STATE_PATH, state)
    return triggers


def format_trigger(trigger: Trigger) -> tuple[str, str]:
    """Onderwerp en tekst voor de mail."""
    naam = trigger.position.symbol or trigger.position.mint[:8]
    richting = "+" if trigger.pct_change >= 0 else ""
    onderwerp = f"POSITIE {naam} staat op {richting}{trigger.pct_change:.0f}%"

    regels = [
        f"{naam} staat op {richting}{trigger.pct_change:.1f}% sinds je instap.",
        "",
        trigger.action,
        "",
    ]
    if trigger.position.inleg_eur:
        waarde = trigger.position.inleg_eur * (1 + trigger.pct_change / 100.0)
        regels.append(
            f"Inleg EUR {trigger.position.inleg_eur:,.2f} -> nu ongeveer EUR {waarde:,.2f}"
        )
    if trigger.current_mc_eur:
        regels.append(f"Marketcap nu: EUR {trigger.current_mc_eur:,.0f}")
    regels += [
        "",
        f"DexScreener: https://dexscreener.com/solana/{trigger.position.mint}",
        "",
        "Dit systeem handelt niet en kan niet handelen. Verkopen doe je zelf.",
        "Dit is geen financieel advies.",
    ]
    return onderwerp, "\n".join(regels)
