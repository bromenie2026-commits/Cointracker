"""
repair_log.py — eenmalig: besmette follow-up-metingen wissen zodat ze
opnieuw gemeten worden.

AANLEIDING (16-09-2026)
-----------------------

`fetch_pairs_for_token` viel terug op álle pairs als er geen enkele pair was
waarin onze munt de basis-token is. Dan lees je de koers van de tégenpartij
af. Bij JUPCAT leverde dat de prijs van JUP op — EUR 0,66 in plaats van
EUR 0,000046 — oftewel een gemeten winst van 1.455.499%, en een marketcap van
honderden miljoenen. Negen munten raakten zo besmet.

Op de raakkansen maakte het bijna niets uit. Op elk *gemiddelde* rendement
maakte het alles uit: één regel van +3.432.489% verwoest een hele reeks.

De oorzaak is weggenomen. Dit script ruimt op wat er al in het logboek staat.
Leeg betekent niet weg: de follow-up beschouwt een leeg meetmoment als "nog
te doen" en meet het opnieuw, nu met de gerepareerde code. We gooien dus geen
data weg, we laten hem hermeten — en was een waarde tóch echt, dan komt hij
gewoon terug.

Draaien:  python repair_log.py --dry-run     (laat zien wat er zou gebeuren)
          python repair_log.py               (voert het uit)
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

import config
import csv_log

log = logging.getLogger(__name__)

#: Boven dit bedrag én deze factor geloven we een marketcap niet meer.
MIN_ABSURDE_MC_EUR = 10_000_000.0
MIN_ABSURDE_FACTOR = 100.0

#: En los daarvan: een prijs die meer dan zoveel keer de instapprijs is.
#: swSOL kreeg zo een koers van USD 479.339 (instap USD 98,89) zonder dat de
#: marketcap opviel. Ruim boven de 575x van ZCAT, dus echte uitschieters
#: blijven staan.
MIN_ABSURDE_PRIJSFACTOR = 2_000.0


def _f(waarde: str) -> Optional[float]:
    try:
        return float(waarde)
    except (TypeError, ValueError):
        return None


def is_besmet(row: dict[str, str], interval: str) -> bool:
    """Is deze ene meting onmogelijk, gegeven het alert?

    Twee onafhankelijke controles, want de besmetting ziet er niet altijd
    hetzelfde uit: bij JUPCAT sprong de marketcap naar honderden miljoenen,
    bij swSOL bleef die normaal maar werd de koers duizenden keren te hoog.
    """
    basis_mc = _f(row.get("market_cap_eur", ""))
    mc = _f(row.get(f"mc_eur_{interval}", ""))
    if (
        mc is not None
        and basis_mc is not None
        and basis_mc > 0
        and mc > MIN_ABSURDE_MC_EUR
        and mc > basis_mc * MIN_ABSURDE_FACTOR
    ):
        return True

    instap = _f(row.get("price_usd", ""))
    prijs = _f(row.get(f"price_{interval}", ""))
    if instap is not None and instap > 0 and prijs is not None and prijs > 0:
        if prijs / instap > MIN_ABSURDE_PRIJSFACTOR:
            return True
    return False


def repareer_rij(row: dict[str, str]) -> list[str]:
    """Maakt besmette metingen leeg. Geeft terug welke intervallen geraakt zijn."""
    geraakt = []
    for interval in csv_log.FOLLOWUP_INTERVALS:
        if not is_besmet(row, interval):
            continue
        row[f"price_{interval}"] = ""
        row[f"mc_eur_{interval}"] = ""
        row[f"followup_{interval}_at"] = ""  # leeg = opnieuw meten
        geraakt.append(interval)

    if geraakt:
        # Deze drie zijn afgeleid van de metingen hierboven en dus ook fout.
        # De follow-up bouwt ze vanzelf opnieuw op.
        row["max_price_seen"] = ""
        row["max_gain_pct"] = ""
        row["max_price_at"] = ""
        row["followup_note"] = "besmette meting gewist (bugfix 16-09), wordt hermeten"
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
                "  %s (%s): %s gewist",
                row.get("symbol", "?"),
                row.get("token_address", "")[:8],
                ", ".join(geraakt),
            )

    log.info(
        "%d regels met een besmette meting, %d metingen in totaal, %d unieke munten.",
        gerepareerd,
        metingen,
        len(munten),
    )
    if dry_run:
        log.info("Dry-run: er is niets weggeschreven.")
    elif gerepareerd:
        csv_log.rewrite_rows(rows)
        log.info("Logboek herschreven. De follow-up meet deze regels opnieuw.")
    return {"regels": len(rows), "gerepareerd": gerepareerd, "metingen": metingen}


def main() -> None:
    parser = argparse.ArgumentParser(description="Besmette metingen wissen")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)-7s %(message)s", stream=sys.stdout
    )
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
