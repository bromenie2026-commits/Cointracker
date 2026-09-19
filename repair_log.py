"""
repair_log.py — eenmalig: de nagekeken foute metingen wissen zodat ze
opnieuw gemeten worden.

AANLEIDING
----------

`fetch_pairs_for_token` viel terug op álle pairs als er geen enkele pair was
waarin onze munt de basis-token is. Dan lees je de koers van een ándere munt
af. Die fout is op 16-09 weggenomen; dit script ruimt op wat er al in het
logboek stond.

WAAROM EEN LIJST EN GEEN GRENS (herzien op 19-09)
------------------------------------------------

De eerste versie wiste alles boven de 2.000x. Dat was fout: hij zou ZCAT
hebben gewist, en die munt deed het écht — van USD 0,0000306 bij het alert
naar USD 0,128 op 19-09, een factor 4.190, met een hoogtepunt rond 5.405x.
De gebruiker zag dat zelf; ik had het voor een meetfout aangezien.

Een grens kan het onderscheid niet maken, want de fouten en de echte winnaars
liggen in dezelfde orde van grootte. Daarom is elke verdachte munt op 19-09
met de hand nagekeken tegen de huidige koers op DexScreener:

    munt     logboek zei   koers nu   oordeel
    ZCAT        5.405x      4.190x    echt — NIET aanraken
    KNOTS         297x        137x    echt — NIET aanraken
    BTC           489x        347x    echt — NIET aanraken
    STONK      27.110x          2x    fout
    GRAMS      17.819x          1x    fout
    PENIS      37.022x          6x    fout
    swSOL       4.847x          -     fout (gestakete SOL van USD 99; een
                                      koers van USD 479.339 is onmogelijk)

Alleen de munten die aantoonbaar fout zijn staan hieronder. IDIOT, WSOLP, fih
en EMBER hebben geen markt meer en zijn dus niet na te kijken; die blijven
bewust staan zoals ze zijn. Liever een twijfelgeval laten staan dan nog eens
een echte winnaar wissen.

Per munt worden alleen de metingen gewist die meer dan 50x de instapprijs
zijn. Hun echte koers ligt nu op 1 tot 6 keer, dus alles daarboven is de fout;
hun normale metingen blijven staan. Leeg betekent niet weg: de follow-up meet
een leeg meetmoment opnieuw, nu met de gerepareerde code.

Draaien:  python repair_log.py --dry-run     (laat zien wat er zou gebeuren)
          python repair_log.py               (voert het uit)
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

import csv_log

log = logging.getLogger(__name__)

#: Met de hand nagekeken op 19-09 tegen de koers op DexScreener.
AANTOONBAAR_FOUT: dict[str, str] = {
    "6GmAFSYs4gk3FDao5FzzySQpPZaWsa4rUJHacpMpUNgx": "STONK",
    "G1jonmoSEbMJSwEg1AmgDAJq2utmuBSZct8oqttBf9rT": "GRAMS",
    "JE3HT7SbCgXDQWV6xp3oiiAisDzq4HyZ8wyEVBDCs45Z": "PENIS",
    "swso1x7A8Dy36znxtcstSVLNseeCQzNV3wVAfa5GGLu": "swSOL",
}

#: Echte koers van deze munten ligt nu op 1-6x de instap; alles boven deze
#: factor is dus de fout, alles eronder een gewone meting.
FOUT_BOVEN_FACTOR = 50.0


def _f(waarde: str) -> Optional[float]:
    try:
        return float(waarde)
    except (TypeError, ValueError):
        return None


def is_besmet(row: dict[str, str], interval: str) -> bool:
    """Is deze ene meting de bekende fout?"""
    if row.get("token_address", "") not in AANTOONBAAR_FOUT:
        return False
    instap = _f(row.get("price_usd", ""))
    prijs = _f(row.get(f"price_{interval}", ""))
    if instap is None or instap <= 0 or prijs is None or prijs <= 0:
        return False
    return prijs / instap > FOUT_BOVEN_FACTOR


def repareer_rij(row: dict[str, str]) -> list[str]:
    """Maakt de foute metingen leeg. Geeft terug welke intervallen geraakt zijn."""
    geraakt = []
    for interval in csv_log.FOLLOWUP_INTERVALS:
        if not is_besmet(row, interval):
            continue
        row[f"price_{interval}"] = ""
        row[f"mc_eur_{interval}"] = ""
        row[f"followup_{interval}_at"] = ""  # leeg = opnieuw meten
        geraakt.append(interval)

    # De hoogste stand kan ook op zo'n foute meting gebaseerd zijn.
    instap = _f(row.get("price_usd", ""))
    hoogste = _f(row.get("max_price_seen", ""))
    fout_hoogste = (
        row.get("token_address", "") in AANTOONBAAR_FOUT
        and instap
        and hoogste
        and hoogste / instap > FOUT_BOVEN_FACTOR
    )
    if geraakt or fout_hoogste:
        row["max_price_seen"] = ""
        row["max_gain_pct"] = ""
        row["max_price_at"] = ""
        row["followup_note"] = "foute meting gewist (bugfix 16-09), wordt hermeten"
        if not geraakt:
            geraakt.append("hoogste stand")
    return geraakt


def run(dry_run: bool = False) -> dict[str, int]:
    rows = csv_log.read_rows()
    if not rows:
        log.info("Leeg logboek — niets te doen.")
        return {"regels": 0, "gerepareerd": 0, "metingen": 0}

    gerepareerd = 0
    metingen = 0
    munten: set[str] = set()
    for row in rows:
        geraakt = repareer_rij(row)
        if geraakt:
            gerepareerd += 1
            metingen += len(geraakt)
            munten.add(row.get("token_address", ""))
            log.info(
                "  %s: %s gewist",
                AANTOONBAAR_FOUT.get(row.get("token_address", ""), row.get("symbol", "?")),
                ", ".join(geraakt),
            )

    log.info(
        "%d regels met een foute meting, %d metingen in totaal, %d munten "
        "(van de %d nagekeken foute munten).",
        gerepareerd,
        metingen,
        len(munten),
        len(AANTOONBAAR_FOUT),
    )
    if dry_run:
        log.info("Dry-run: er is niets weggeschreven.")
    elif gerepareerd:
        csv_log.rewrite_rows(rows)
        log.info("Logboek herschreven. De follow-up meet deze regels opnieuw.")
    return {"regels": len(rows), "gerepareerd": gerepareerd, "metingen": metingen}


def main() -> None:
    parser = argparse.ArgumentParser(description="Nagekeken foute metingen wissen")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)-7s %(message)s", stream=sys.stdout
    )
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
