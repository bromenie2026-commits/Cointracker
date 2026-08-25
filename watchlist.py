"""
watchlist.py — fijnmazig volgen van munten waarover een alert is gestuurd.

De aanleiding staat in de meting van 24-08-2026: van alle momenten waarop een
munt boven +30% kwam, lag **87% in het eerste uur** na het alert. De scan
draait eens per ~100 minuten en de follow-up meet op 1, 4, 12 en 24 uur. Met
die korrel zie je de piek simpelweg niet.

Deze module houdt een klein lijstje bij — alleen de munten waarover je
daadwerkelijk een alert kreeg — en kijkt dat elke tien minuten na. Eén
API-call per munt, dus goedkoop.

Twee dingen worden vastgelegd:

1. `logs/watchlist.csv` — één regel per meting, met de tijd sinds het alert
   en het rendement op dat moment. Dat is de fijnmazige koersgeschiedenis die
   je nodig hebt om te beoordelen of een verkoopregel werkt.
2. De hoogste stand per munt, in de state, zodat je die later kunt vergelijken
   met de eindstand.

Mailen bij een niveau kán, maar staat standaard UIT (`WATCHLIST_NOTIFY_ENABLED`).
Eerst meten, dan pas beslissen of je erop wilt handelen.

Dit systeem handelt niet en kan niet handelen.
"""

from __future__ import annotations

import csv
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import config
import data_sources
import state_store

log = logging.getLogger(__name__)

LOG_COLUMNS = [
    "sample_ts_utc",
    "token_address",
    "symbol",
    "alert_ts_utc",
    "minutes_since_alert",
    "entry_price_usd",
    "price_usd",
    "pct_change",
    "max_pct_so_far",
    "market_cap_eur",
    "status",
]


@dataclass
class Crossing:
    """Een munt die voor het eerst boven een niveau uitkomt."""

    token_address: str
    symbol: str
    level: float
    pct_change: float
    minutes_since_alert: float
    price_usd: Optional[float]
    market_cap_eur: Optional[float]


# --------------------------------------------------------------------------- #
# Lijst beheren
# --------------------------------------------------------------------------- #


def load(path: Optional[Path] = None) -> dict[str, Any]:
    return state_store.load(Path(path or config.WATCHLIST_PATH))


def save(data: dict[str, Any], path: Optional[Path] = None) -> None:
    state_store.save(Path(path or config.WATCHLIST_PATH), data)


def add(
    data: dict[str, Any],
    token_address: str,
    symbol: str,
    entry_price_usd: Optional[float],
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Zet een gealerteerde munt op de lijst. Al aanwezig? Dan niets doen."""
    if not config.WATCHLIST_ENABLED or not token_address:
        return data
    if entry_price_usd is None or entry_price_usd <= 0:
        log.warning("Geen instapprijs voor %s; niet op de volglijst gezet", token_address)
        return data
    if token_address in data:
        return data
    if len(data) >= config.WATCHLIST_MAX_TOKENS:
        log.warning("Volglijst zit vol (%d); %s overgeslagen", len(data), token_address)
        return data

    now = now if now is not None else time.time()
    data[token_address] = {
        "symbol": symbol,
        "alert_ts": now,
        "entry_price_usd": float(entry_price_usd),
        "max_pct": 0.0,
        "max_pct_at": now,
        "samples": 0,
        "notified": [],
    }
    log.info("Volglijst: %s toegevoegd", symbol or token_address[:8])
    return data


def expired(entry: dict[str, Any], now: float) -> bool:
    alert_ts = entry.get("alert_ts")
    if not isinstance(alert_ts, (int, float)):
        return True
    return (now - alert_ts) / 3600.0 > config.WATCHLIST_TRACK_HOURS


def prune(data: dict[str, Any], now: Optional[float] = None) -> tuple[dict[str, Any], int]:
    """Haalt munten van de lijst die hun volgperiode voorbij zijn."""
    now = now if now is not None else time.time()
    blijft = {k: v for k, v in data.items() if isinstance(v, dict) and not expired(v, now)}
    return blijft, len(data) - len(blijft)


# --------------------------------------------------------------------------- #
# Meten
# --------------------------------------------------------------------------- #


def _append_log(rows: list[dict[str, Any]], path: Optional[Path] = None) -> None:
    if not rows:
        return
    target = Path(path or config.WATCHLIST_LOG_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    nieuw = not target.exists() or target.stat().st_size == 0
    try:
        with target.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=LOG_COLUMNS, extrasaction="ignore")
            if nieuw:
                writer.writeheader()
            for row in rows:
                writer.writerow({c: row.get(c, "") for c in LOG_COLUMNS})
    except OSError as exc:
        log.error("Volglijst-log wegschrijven mislukt: %s", exc)


def _iso(ts: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def check_all(
    data: dict[str, Any], now: Optional[float] = None
) -> tuple[dict[str, Any], list[Crossing]]:
    """Meet elke munt op de lijst één keer. Geeft nieuw geraakte niveaus terug."""
    now = now if now is not None else time.time()
    crossings: list[Crossing] = []
    rows: list[dict[str, Any]] = []

    for token, entry in list(data.items()):
        if not isinstance(entry, dict):
            continue
        instap = entry.get("entry_price_usd")
        if not isinstance(instap, (int, float)) or instap <= 0:
            continue

        pairs, status = data_sources.fetch_pairs_for_token(token)
        if status == "error":
            # Geen meting is beter dan een verzonnen meting (bugfix 4.3).
            log.warning("Volglijst: API-fout bij %s, overgeslagen", token[:8])
            continue

        minuten = (now - float(entry.get("alert_ts", now))) / 60.0
        pair = data_sources.best_pair(pairs)

        if pair is None or pair.price_usd is None:
            pct = -100.0
            prijs = 0.0
            mc_eur = 0.0
            toestand = "markt weg"
        else:
            prijs = pair.price_usd
            pct = (prijs / instap - 1.0) * 100.0
            mc_usd = pair.market_cap_usd if pair.market_cap_usd is not None else pair.fdv_usd
            mc_eur = data_sources.usd_to_eur(mc_usd) or 0.0
            toestand = "ok"

        if pct > float(entry.get("max_pct", 0.0)):
            entry["max_pct"] = round(pct, 2)
            entry["max_pct_at"] = now
        entry["samples"] = int(entry.get("samples", 0)) + 1
        entry["last_pct"] = round(pct, 2)
        entry["last_check"] = now

        rows.append(
            {
                "sample_ts_utc": _iso(now),
                "token_address": token,
                "symbol": entry.get("symbol", ""),
                "alert_ts_utc": _iso(float(entry.get("alert_ts", now))),
                "minutes_since_alert": round(minuten, 1),
                "entry_price_usd": f"{instap:.12g}",
                "price_usd": f"{prijs:.12g}",
                "pct_change": round(pct, 2),
                "max_pct_so_far": entry["max_pct"],
                "market_cap_eur": round(mc_eur, 2),
                "status": toestand,
            }
        )

        gemeld = set(entry.get("notified", []))
        for niveau in sorted(config.WATCHLIST_ALERT_LEVELS):
            if pct >= niveau and niveau not in gemeld:
                gemeld.add(niveau)
                crossings.append(
                    Crossing(
                        token_address=token,
                        symbol=str(entry.get("symbol", "")),
                        level=niveau,
                        pct_change=pct,
                        minutes_since_alert=minuten,
                        price_usd=prijs,
                        market_cap_eur=mc_eur,
                    )
                )
        entry["notified"] = sorted(gemeld)
        data[token] = entry

    _append_log(rows)
    return data, crossings


def format_crossing(c: Crossing) -> tuple[str, str]:
    """Onderwerp en tekst voor de mail. Puur informatief — geen advies."""
    naam = c.symbol or c.token_address[:8]
    onderwerp = f"{naam} staat op +{c.pct_change:.0f}% sinds het alert"
    tekst = "\n".join(
        [
            f"{naam} is boven de +{c.level:.0f}% gekomen.",
            "",
            f"Stand nu            : {c.pct_change:+.1f}%",
            f"Tijd sinds het alert: {c.minutes_since_alert:.0f} minuten",
            f"Marketcap nu        : EUR {c.market_cap_eur:,.0f}" if c.market_cap_eur else "",
            "",
            f"DexScreener: https://dexscreener.com/solana/{c.token_address}",
            "",
            "Dit is een meting, geen advies. Kijk in risk_config.yaml wat je",
            "zelf hebt afgesproken. Dit systeem handelt niet en kan niet handelen.",
        ]
    )
    return onderwerp, tekst


# --------------------------------------------------------------------------- #
# Losse job
# --------------------------------------------------------------------------- #


def run(dry_run: bool = False, now: Optional[float] = None) -> int:
    """Eén ronde: opruimen, meten, eventueel melden. Geeft aantal metingen."""
    if not config.WATCHLIST_ENABLED:
        log.info("Volglijst staat uit.")
        return 0

    data = load()
    if not data:
        log.info("Volglijst is leeg — nog geen alerts om te volgen.")
        return 0

    now = now if now is not None else time.time()
    data, verlopen = prune(data, now)
    if verlopen:
        log.info("%d munten van de lijst gehaald (volgperiode voorbij)", verlopen)

    aantal = len(data)
    data, crossings = check_all(data, now)
    log.info("%d munten gemeten, %d nieuwe niveaus geraakt", aantal, len(crossings))

    if crossings and config.WATCHLIST_NOTIFY_ENABLED and not dry_run:
        import notify

        for c in crossings:
            onderwerp, tekst = format_crossing(c)
            notify.send_run_summary(onderwerp, tekst)
    elif crossings:
        for c in crossings:
            log.info(
                "NIVEAU GERAAKT (mail staat uit): %s +%.0f%% na %.0f min",
                c.symbol or c.token_address[:8],
                c.pct_change,
                c.minutes_since_alert,
            )

    if not dry_run:
        save(data)
    return aantal


def main() -> None:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Volglijst bijwerken")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
