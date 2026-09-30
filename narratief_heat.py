"""
narratief_heat.py — was de meme/naam van een munt "heet" op het moment van lancering?

WAAROM
------

De vorige narratief-test gebruikte Wikipedia als maatstaf voor "viral". Dat is
een slappe proxy: mensen kopen een memecoin niet omdat een artikel hoog op
Wikipedia staat, maar omdat iets rondgaat op TikTok/Insta/X. Google Trends
staat dichter bij die echte zoek- en aandachtsgolf: als een meme of naam
opkomt, zie je de zoekterm stijgen.

Dit script meet voor elke gemailde munt de Google Trends-belangstelling voor
het kernwoord uit z'n naam, rond de lanceerdag. Twee dingen:

  * heat     — hoe hoog stond de belangstelling op de lanceerdag (0-100)?
  * stijging — was die belangstelling aan het opkomen (dag vs de week ervoor)?

Daarna de eerlijke vraag: doen munten die op een HETE, STIJGENDE term lanceren
het beter dan de rest? Zo niet, dan heeft het geen zin om dit in de live-bot te
bouwen. Zo wel, dan pas.

EERLIJK OVER DE BEPERKINGEN
---------------------------

* Google Trends is RELATIEF (0-100 binnen de opgevraagde term/venster), dus
  "heat" is niet 1-op-1 vergelijkbaar tussen termen. De "stijging" (opkomend
  vs de week ervoor) is het betekenisvollere signaal.
* Trends loopt uren achter en heeft geen TikTok-geluid erin. Het is een proxy,
  geen glazen bol.
* De data is officieel maar de toegang is ongedocumenteerd; Google knijpt af
  (429). Daarom: langzaam, hervatbaar, en fouten worden gemeld, niet verzonnen.

Leest alleen openbare gegevens. Handelt niet en kan niet handelen.

Draaien:  python narratief_heat.py                 (alles, hervatbaar)
          python narratief_heat.py --limit 150     (eerst een steekproef)
          python narratief_heat.py --alleen-analyse (alleen rapport opnieuw)
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import config
from narratief_test import _fisher, munten, woorden

log = logging.getLogger(__name__)

RAPPORT = Path(config.LOG_DIR) / "narratief_heat_rapport.txt"
UITVOER = Path(config.LOG_DIR) / "narratief_heat.csv"

KOLOMMEN = ["token_address", "symbol", "kernwoord", "heat", "stijging",
            "status", "lanceerdag", "piek_pct", "hard_pass", "gemaild"]

#: Dagen vóór de lancering die we ophalen om de opkomst te kunnen zien.
VENSTER_DAGEN = 30
#: Basislijn: gemiddelde belangstelling in deze dagen vóór de lancering.
BASIS_DAGEN = 7
#: Vanaf welke Trends-waarde (0-100) noemen we een term "heet".
HEET_DREMPEL = 50
#: Hoeveel hoger dan de basislijn telt als "stijgend".
STIJG_DREMPEL = 10.0
#: Rustpauze tussen Trends-aanvragen (Google knijpt af bij te snel).
INTERVAL = float(os.getenv("TRENDS_MIN_INTERVAL_SECONDS", "3"))
#: Zoveel mislukte aanvragen op rij -> stoppen en bewaren (volgende run gaat verder).
MAX_FOUT_OP_RIJ = 6


# --------------------------------------------------------------------------- #
# Kernwoord
# --------------------------------------------------------------------------- #


def kernwoord(munt: dict[str, Any]) -> Optional[str]:
    """Het meest kenmerkende (langste) woord uit naam/symbool, of None."""
    kandidaten = woorden(munt["symbol"]) | woorden(munt["naam"])
    if not kandidaten:
        return None
    # Langste eerst; bij gelijke lengte alfabetisch, zodat het herhaalbaar is.
    return sorted(kandidaten, key=lambda w: (-len(w), w))[0]


# --------------------------------------------------------------------------- #
# Google Trends ophalen  (ongedocumenteerd; wordt in tests gemockt)
# --------------------------------------------------------------------------- #


def _pytrends_reeks(term: str, van: date, tot: date) -> Optional[dict[date, int]]:
    """Ruwe dag-belangstelling voor `term` via pytrends, of None bij een fout."""
    try:
        from pytrends.request import TrendReq
    except Exception as e:  # noqa: BLE001
        log.error("pytrends niet beschikbaar: %s", e)
        return None
    try:
        py = TrendReq(hl="en-US", tz=0, timeout=(10, 25))
        py.build_payload([term], timeframe=f"{van:%Y-%m-%d} {tot:%Y-%m-%d}", geo="")
        df = py.interest_over_time()
    except Exception as e:  # noqa: BLE001
        log.warning("Trends '%s': %s", term, e)
        return None
    if df is None or df.empty or term not in df.columns:
        return {}
    uit: dict[date, int] = {}
    for stempel, waarde in df[term].items():
        try:
            uit[stempel.date()] = int(waarde)
        except Exception:  # noqa: BLE001
            continue
    return uit


def interesse_reeks(term: str, van: date, tot: date) -> Optional[dict[date, int]]:
    """Dag-belangstelling (0-100) voor `term`. None = ophalen mislukt."""
    return _pytrends_reeks(term, van, tot)


def heat_en_stijging(reeks: dict[date, int], lanceerdag: date) -> Optional[tuple[int, float]]:
    """(heat op lanceerdag, stijging t.o.v. de week ervoor), of None zonder data."""
    if not reeks:
        return None
    # heat: waarde op de lanceerdag, of de dichtstbijzijnde eerdere dag met data.
    heat = None
    for delta in range(0, VENSTER_DAGEN + 1):
        d = lanceerdag - timedelta(days=delta)
        if d in reeks:
            heat = reeks[d]
            break
    if heat is None:
        return None
    basis_waarden = [reeks[lanceerdag - timedelta(days=k)]
                     for k in range(1, BASIS_DAGEN + 1) if (lanceerdag - timedelta(days=k)) in reeks]
    basis = sum(basis_waarden) / len(basis_waarden) if basis_waarden else 0.0
    return heat, round(heat - basis, 1)


# --------------------------------------------------------------------------- #
# Verzamelen (hervatbaar)
# --------------------------------------------------------------------------- #


def _gedaan() -> set[str]:
    if not UITVOER.exists():
        return set()
    with UITVOER.open(newline="", encoding="utf-8") as fh:
        return {r["token_address"] for r in csv.DictReader(fh) if r.get("token_address")}


def _schrijf_rij(rij: dict[str, Any]) -> None:
    nieuw = not UITVOER.exists()
    UITVOER.parent.mkdir(parents=True, exist_ok=True)
    with UITVOER.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=KOLOMMEN)
        if nieuw:
            w.writeheader()
        w.writerow({k: rij.get(k, "") for k in KOLOMMEN})


def verzamel(limit: Optional[int] = None) -> int:
    """Haal voor gemailde munten de Trends-heat op. Slaat al gedane munten over."""
    lijst = [m for m in munten() if m["gemaild"]]
    lijst.sort(key=lambda m: m["lanceerdag"])
    gedaan = _gedaan()
    todo = [m for m in lijst if m["adres"] not in gedaan]
    if limit:
        todo = todo[:limit]
    log.info("Te doen: %d munten (%d al gedaan)", len(todo), len(gedaan))

    fout_op_rij = 0
    verwerkt = 0
    for m in todo:
        term = kernwoord(m)
        basis = {"token_address": m["adres"], "symbol": m["symbol"],
                 "kernwoord": term or "", "lanceerdag": m["lanceerdag"].isoformat(),
                 "piek_pct": round(m["piek"], 1),
                 "hard_pass": "true" if m["hard_pass"] else "false",
                 "gemaild": "true" if m["gemaild"] else "false"}
        if not term:
            _schrijf_rij({**basis, "heat": "", "stijging": "", "status": "geen woord"})
            verwerkt += 1
            continue
        van = m["lanceerdag"] - timedelta(days=VENSTER_DAGEN)
        reeks = interesse_reeks(term, van, m["lanceerdag"])
        if reeks is None:
            fout_op_rij += 1
            log.warning("Ophalen mislukt (%d op rij) voor '%s'", fout_op_rij, term)
            if fout_op_rij >= MAX_FOUT_OP_RIJ:
                log.error("Te veel fouten op rij; stoppen en bewaren. Volgende run gaat verder.")
                break
            time.sleep(INTERVAL * 2)
            continue
        fout_op_rij = 0
        hs = heat_en_stijging(reeks, m["lanceerdag"])
        if hs is None:
            _schrijf_rij({**basis, "heat": "", "stijging": "", "status": "geen data"})
        else:
            heat, stijging = hs
            _schrijf_rij({**basis, "heat": heat, "stijging": stijging, "status": "ok"})
        verwerkt += 1
        time.sleep(INTERVAL)
    return verwerkt


# --------------------------------------------------------------------------- #
# Analyse
# --------------------------------------------------------------------------- #


def _rijen() -> list[dict[str, Any]]:
    if not UITVOER.exists():
        return []
    uit = []
    with UITVOER.open(newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r.get("status") != "ok":
                continue
            try:
                heat = int(r["heat"]); stijging = float(r["stijging"]); piek = float(r["piek_pct"])
            except (ValueError, KeyError):
                continue
            uit.append({
                "symbol": r.get("symbol", ""), "kernwoord": r.get("kernwoord", ""),
                "heat": heat, "stijging": stijging, "piek": piek,
                "heet": heat >= HEET_DREMPEL,
                "heet_stijgend": heat >= HEET_DREMPEL and stijging >= STIJG_DREMPEL,
                "hard_pass": str(r.get("hard_pass", "")).lower() in ("true", "1"),
                "gemaild": str(r.get("gemaild", "")).lower() in ("true", "1"),
            })
    return uit


def _vergelijk(lijst: list[dict[str, Any]], sleutel: str, drempel: float) -> str:
    ja = [m for m in lijst if m[sleutel]]
    nee = [m for m in lijst if not m[sleutel]]
    a = sum(1 for m in ja if m["piek"] >= drempel)
    c = sum(1 for m in nee if m["piek"] >= drempel)
    pj = a / len(ja) * 100 if ja else float("nan")
    pn = c / len(nee) * 100 if nee else float("nan")
    p = _fisher(a, len(ja) - a, c, len(nee) - c)
    return f"{len(ja):>5} heet: {pj:>5.1f}%  |  {len(nee):>5} niet: {pn:>5.1f}%  |  p = {p:.3f}"


def analyseer() -> str:
    rijen = _rijen()
    R = []
    R.append("NARRATIEF-HEAT — lanceerde de munt op een hete, opkomende zoekterm?")
    R.append("=" * 70)
    R.append(f"Gemaakt   : {datetime.now(timezone.utc):%d-%m-%Y %H:%M} UTC")
    if not rijen:
        R.append("Nog geen bruikbare metingen. Draai eerst zonder --alleen-analyse.")
        tekst = "\n".join(R)
        RAPPORT.parent.mkdir(parents=True, exist_ok=True)
        RAPPORT.write_text(tekst + "\n", encoding="utf-8")
        return tekst
    R.append(f"Metingen  : {len(rijen)} gemailde munten met Trends-data")
    R.append(f"Heet      : belangstelling >= {HEET_DREMPEL}/100 op de lanceerdag")
    R.append(f"Stijgend  : minstens {STIJG_DREMPEL:.0f} hoger dan de week ervoor")
    R.append(f"            {sum(m['heet'] for m in rijen)} heet, "
             f"{sum(m['heet_stijgend'] for m in rijen)} heet én stijgend")
    R.append("")

    groepen = [("Alle gemailde", rijen),
               ("Door de harde filters", [m for m in rijen if m["hard_pass"]])]
    for sleutel, uitleg in (("heet", "heet"), ("heet_stijgend", "heet én stijgend")):
        R.append(f"MUNTEN DIE {uitleg.upper()} LANCEERDEN")
        R.append("-" * 70)
        for naam, groep in groepen:
            R.append(f"{naam}  (n={len(groep)})")
            for label, drempel in (("ooit 2x ", 100.0), ("ooit 10x", 900.0)):
                R.append(f"  {label}: {_vergelijk(groep, sleutel, drempel)}")
        R.append("")

    R.append("VOORBEELDEN — hoogste stijgers, met hun heat op de lanceerdag")
    R.append("-" * 70)
    for m in sorted(rijen, key=lambda x: -x["piek"])[:25]:
        R.append(f"  {str(m['symbol'])[:12]:<13}{m['piek'] / 100 + 1:>8.1f}x  "
                 f"heat {m['heat']:>3}  stijging {m['stijging']:>+6.1f}  ('{m['kernwoord']}')")
    R.append("")
    R.append("Lezen: telt vooral 'Door de harde filters'. Scoren hete/stijgende")
    R.append("munten daar duidelijk hoger, met p onder 0,05, dan zit er een signaal")
    R.append("en is het de moeite waard om in de live-bot te bouwen. Zo niet, dan niet.")
    R.append("")
    R.append("Google Trends is relatief en loopt achter; zie het als proxy, geen bewijs.")
    R.append("Leest alleen openbare gegevens. Handelt niet en kan niet handelen.")
    tekst = "\n".join(R)
    RAPPORT.parent.mkdir(parents=True, exist_ok=True)
    RAPPORT.write_text(tekst + "\n", encoding="utf-8")
    return tekst


def run(limit: Optional[int] = None, alleen_analyse: bool = False) -> str:
    if not alleen_analyse:
        verzamel(limit)
    return analyseer()


def main() -> None:
    p = argparse.ArgumentParser(description="Narratief-heat via Google Trends")
    p.add_argument("--limit", type=int, default=None, help="Maximaal zoveel nieuwe munten")
    p.add_argument("--alleen-analyse", action="store_true", help="Niets ophalen, alleen het rapport")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", stream=sys.stdout)
    print(run(limit=args.limit, alleen_analyse=args.alleen_analyse))


if __name__ == "__main__":
    main()
