"""
narratief_test.py — doen munten die inspelen op wat NU viral is het beter?

WAAROM
------

Een indeling van munten naar soort naam (dieren, memes, nieuws, AI) laat over
8.358 munten geen verschil zien dat groot genoeg is om toeval uit te sluiten.
Maar dat meet het soort naam, niet de timing. Het idee dat we hier toetsen is
scherper: een munt waarvan de naam past bij iets dat op de dag van lancering
viral gaat.

Als maatstaf voor "viral" gebruiken we de meest bekeken Wikipedia-artikelen
per dag. Als er iets gebeurt waar de wereld over praat, schiet het artikel
erover binnen een dag naar de top. Die lijst is gratis, officieel, en terug
te kijken — dus we kunnen het toetsen op munten waarvan we de uitkomst al
kennen, in plaats van te gokken.

HOE
---

1. Voor elke dag sinds 21-08 de top 1.000 meest bekeken artikelen ophalen,
   van de Engelse én de Nederlandse Wikipedia.
2. Voor elke munt in het logboek kijken of een woord uit zijn naam of symbool
   voorkomt in de titel van een artikel dat op de lanceerdag of de dag ervoor
   in die top stond.
3. Vergelijken: verdubbelen munten met zo'n match vaker dan munten zonder?

EERLIJK OVER DE BEPERKINGEN
---------------------------

Woorden matchen is grof. "CAT" matcht elk artikel met "cat" in de titel,
ook als dat niets met de munt te maken heeft. Daarom negeren we korte woorden
en een lange lijst algemene woorden, en zetten we voorbeelden van matches in
het rapport zodat je met eigen ogen kunt zien of ze kloppen. Veel foute
matches verdunnen het effect; ze maken het niet groter dan het is.

Leest alleen openbare gegevens. Handelt niet en kan niet handelen.

Draaien:  python narratief_test.py
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import config
import csv_log
import http_client
import repair_log

log = logging.getLogger(__name__)

RAPPORT = Path(config.LOG_DIR) / "narratief_rapport.txt"
MATCHES = Path(config.LOG_DIR) / "narratief_matches.csv"

WIKI_URL = "https://wikimedia.org/api/rest_v1/metrics/pageviews/top/{project}/all-access/{y:04d}/{m:02d}/{d:02d}"
PROJECTEN = ("en.wikipedia", "nl.wikipedia")
# Wikimedia vraagt om een herkenbare User-Agent met contactgegevens.
WIKI_HEADERS = {
    "User-Agent": "CointrackerResearch/1.0 (https://github.com/bromenie2026-commits/Cointracker)"
}

VANAF = date(2026, 8, 21)
#: Hoeveel dagen vóór de lancering een artikel nog als "actueel" telt.
DAGEN_ERVOOR = 1
#: Kortere woorden matchen te makkelijk op toeval.
MIN_WOORDLENGTE = 4
#: Strenge variant: alleen de echte uitschieters van die dag.
TOP_STRENG = 200

#: Woorden die zowel in muntnamen als in titels steeds voorkomen zonder dat
#: het iets betekent. Zonder deze lijst matcht bijna alles op alles.
STOPWOORDEN = {
    # Engels algemeen
    "the", "and", "for", "with", "from", "that", "this", "your", "have", "what",
    "when", "who", "why", "how", "just", "only", "very", "more", "most", "into",
    "over", "under", "about", "after", "before", "back", "down", "here", "there",
    "they", "them", "their", "will", "would", "could", "should", "been", "being",
    "first", "last", "next", "best", "good", "great", "real", "true", "king",
    "queen", "lord", "life", "love", "time", "world", "house", "home", "game",
    "games", "city", "land", "star", "stars", "black", "white", "blue", "green",
    "gold", "golden", "little", "baby", "super", "mega", "ultra", "grand",
    # Titels op Wikipedia die altijd hoog staan
    "list", "deaths", "death", "season", "series", "film", "episode", "episodes",
    "album", "song", "songs", "band", "united", "states", "kingdom", "national",
    "international", "league", "championship", "cup", "football", "club", "team",
    "election", "elections", "county", "district", "state", "university",
    "school", "church", "war", "battle", "history", "people", "family", "wife",
    "husband", "children", "tour", "show", "television", "character", "characters",
    "main", "page", "special", "wikipedia", "portal", "file", "search", "hoofdpagina",
    "speciaal", "lijst", "van", "het", "een", "voor", "met", "2025", "2026", "2027",
    # Crypto-woorden die in munten overal zitten
    "coin", "token", "crypto", "solana", "pump", "meme", "memes", "moon", "official",
    "doge", "inu", "chain", "swap", "finance", "protocol", "network", "labs", "dao",
}

#: Titels die geen onderwerp zijn maar navigatie.
META = re.compile(r"^(Main_Page|Special:|Wikipedia:|Portal:|File:|Help:|Talk:|Hoofdpagina|Speciaal:|Categorie:|Category:|-)")


# --------------------------------------------------------------------------- #
# Woorden
# --------------------------------------------------------------------------- #


def woorden(tekst: str) -> set[str]:
    """Betekenisvolle woorden uit een titel of muntnaam."""
    tekst = re.sub(r"\([^)]*\)", " ", str(tekst or ""))       # "(film)" eruit
    tekst = tekst.replace("_", " ").lower()
    stukken = re.split(r"[^a-z0-9àâäéèêëïîôöùûüç]+", tekst)
    return {
        w for w in stukken
        if len(w) >= MIN_WOORDLENGTE and w not in STOPWOORDEN and not w.isdigit()
    }


# --------------------------------------------------------------------------- #
# Wikipedia ophalen
# --------------------------------------------------------------------------- #


def top_artikelen(project: str, dag: date) -> list[tuple[str, int]]:
    """(titel, rang) van de meest bekeken artikelen op die dag. Leeg bij een fout."""
    url = WIKI_URL.format(project=project, y=dag.year, m=dag.month, d=dag.day)
    resp = http_client.get_json(url, host_key="wikimedia", min_interval=0.2, headers=WIKI_HEADERS)
    if not resp.ok:
        log.warning("Wikipedia %s %s: %s", project, dag, resp.error)
        return []
    items = (((resp.data or {}).get("items") or [{}])[0].get("articles")) or []
    uit = []
    for a in items:
        titel = a.get("article", "")
        if titel and not META.match(titel):
            uit.append((titel, int(a.get("rank") or 9999)))
    return uit


def haal_trends(van: date, tot: date) -> dict[date, list[tuple[str, int, str]]]:
    """Per dag: (titel, rang, project)."""
    per_dag: dict[date, list[tuple[str, int, str]]] = {}
    dag = van
    while dag <= tot:
        rij = []
        for p in PROJECTEN:
            rij += [(t, r, p) for t, r in top_artikelen(p, dag)]
        per_dag[dag] = rij
        log.info("%s: %d artikelen", dag, len(rij))
        dag += timedelta(days=1)
    return per_dag


# --------------------------------------------------------------------------- #
# Munten en matchen
# --------------------------------------------------------------------------- #


def _ts(waarde: str) -> Optional[datetime]:
    try:
        t = datetime.fromisoformat(str(waarde))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def munten() -> list[dict[str, Any]]:
    """Eerste waarneming per munt, met lanceerdag en uitkomst."""
    gezien: dict[str, dict[str, Any]] = {}
    for r in csv_log.read_rows():
        t = _ts(r.get("timestamp_utc", ""))
        if t is None or t.date() < VANAF:
            continue
        adres = r.get("token_address", "")
        if not adres or adres in gezien:
            continue
        try:
            piek = float(r.get("max_gain_pct") or "nan")
        except ValueError:
            piek = float("nan")
        if adres in repair_log.AANTOONBAAR_FOUT:
            continue
        lancering = _ts(r.get("pair_created_at_utc", "")) or t
        gezien[adres] = {
            "adres": adres,
            "symbol": r.get("symbol", ""),
            "naam": r.get("name", ""),
            "lanceerdag": lancering.date(),
            "piek": piek,
            "hard_pass": str(r.get("hard_pass", "")).lower() in ("true", "1"),
            "gemaild": str(r.get("alerted", "")).lower() in ("true", "1"),
        }
    # Alerts komen soms pas bij een latere waarneming; neem die mee.
    for r in csv_log.read_rows():
        if str(r.get("alerted", "")).lower() in ("true", "1") and r.get("token_address") in gezien:
            gezien[r["token_address"]]["gemaild"] = True
    return [m for m in gezien.values() if m["piek"] == m["piek"]]  # zonder NaN


_INDEX_CACHE: dict[tuple[int, int], dict[date, dict[str, tuple[int, str, str]]]] = {}


def index(trends: dict[date, list[tuple[str, int, str]]], max_rang: int) -> dict[date, dict[str, tuple[int, str, str]]]:
    """Per dag: woord -> (beste rang, titel, project). Eén keer opbouwen, niet per munt.

    Zonder deze index zou elke munt elke titel opnieuw in woorden knippen:
    8.000 munten x 4.000 titels = tientallen miljoenen keer hetzelfde werk.
    """
    sleutel = (id(trends), max_rang)
    if sleutel in _INDEX_CACHE:
        return _INDEX_CACHE[sleutel]
    uit: dict[date, dict[str, tuple[int, str, str]]] = {}
    for dag, lijst in trends.items():
        per_woord: dict[str, tuple[int, str, str]] = {}
        for titel, rang, project in lijst:
            if rang > max_rang:
                continue
            for w in woorden(titel):
                if w not in per_woord or rang < per_woord[w][0]:
                    per_woord[w] = (rang, titel, project)
        uit[dag] = per_woord
    _INDEX_CACHE[sleutel] = uit
    return uit


def match(munt: dict[str, Any], trends: dict[date, list[tuple[str, int, str]]],
          max_rang: int = 1000) -> Optional[tuple[str, str, int, str]]:
    """(woord, titel, rang, project) waarmee deze munt op het nieuws inspeelt, of None."""
    mw = woorden(munt["symbol"]) | woorden(munt["naam"])
    if not mw:
        return None
    idx = index(trends, max_rang)
    beste = None
    for delta in range(DAGEN_ERVOOR + 1):
        per_woord = idx.get(munt["lanceerdag"] - timedelta(days=delta), {})
        for w in mw:
            if w in per_woord:
                rang, titel, project = per_woord[w]
                if beste is None or rang < beste[2]:
                    beste = (w, titel, rang, project)
    return beste


# --------------------------------------------------------------------------- #
# Rapport
# --------------------------------------------------------------------------- #


def _fisher(a: int, b: int, c: int, d: int) -> float:
    try:
        from scipy.stats import fisher_exact

        return float(fisher_exact([[a, b], [c, d]])[1])
    except Exception:  # noqa: BLE001
        return float("nan")


def vergelijk(lijst: list[dict[str, Any]], sleutel: str, drempel: float) -> tuple[str, float]:
    ja = [m for m in lijst if m[sleutel]]
    nee = [m for m in lijst if not m[sleutel]]
    a = sum(1 for m in ja if m["piek"] >= drempel)
    c = sum(1 for m in nee if m["piek"] >= drempel)
    pj = a / len(ja) * 100 if ja else float("nan")
    pn = c / len(nee) * 100 if nee else float("nan")
    p = _fisher(a, len(ja) - a, c, len(nee) - c)
    return f"{len(ja):>6} met match: {pj:>5.1f}%  |  {len(nee):>6} zonder: {pn:>5.1f}%  |  p = {p:.3f}", p


def run() -> str:
    lijst = munten()
    if not lijst:
        return "Geen munten met een uitkomst in het logboek."
    van = min(m["lanceerdag"] for m in lijst) - timedelta(days=DAGEN_ERVOOR)
    tot = min(max(m["lanceerdag"] for m in lijst), datetime.now(timezone.utc).date() - timedelta(days=1))
    trends = haal_trends(van, tot)
    dagen_ok = sum(1 for v in trends.values() if v)

    regels = []
    for m in lijst:
        ruim = match(m, trends, 1000)
        streng = match(m, trends, TOP_STRENG)
        m["match"] = ruim is not None
        m["match_streng"] = streng is not None
        if ruim:
            regels.append({"symbol": m["symbol"], "naam": m["naam"], "lanceerdag": m["lanceerdag"],
                           "woord": ruim[0], "artikel": ruim[1], "rang": ruim[2],
                           "wikipedia": ruim[3], "piek_pct": round(m["piek"], 1),
                           "gemaild": m["gemaild"]})
    MATCHES.parent.mkdir(parents=True, exist_ok=True)
    with MATCHES.open("w", newline="", encoding="utf-8") as fh:
        velden = ["symbol", "naam", "lanceerdag", "woord", "artikel", "rang", "wikipedia", "piek_pct", "gemaild"]
        w = csv.DictWriter(fh, fieldnames=velden)
        w.writeheader()
        for r in sorted(regels, key=lambda x: -x["piek_pct"]):
            w.writerow(r)

    R = []
    R.append("NARRATIEF-TEST — doen munten die op het nieuws inspelen het beter?")
    R.append("=" * 70)
    R.append(f"Gemaakt   : {datetime.now(timezone.utc):%d-%m-%Y %H:%M} UTC")
    R.append(f"Munten    : {len(lijst)} met bekende uitkomst, sinds {VANAF:%d-%m}")
    R.append(f"Wikipedia : {dagen_ok} dagen opgehaald ({van:%d-%m} t/m {tot:%d-%m}), Engels + Nederlands")
    R.append(f"Match     : een woord uit naam/symbool staat in een top-artikel op de lanceerdag of de dag ervoor")
    R.append(f"            {sum(m['match'] for m in lijst)} munten matchen (top 1000), "
             f"{sum(m['match_streng'] for m in lijst)} in de strenge variant (top {TOP_STRENG})")
    R.append("")

    groepen = [
        ("Alle munten", lijst),
        ("Door de harde filters", [m for m in lijst if m["hard_pass"]]),
        ("Gemaild", [m for m in lijst if m["gemaild"]]),
    ]
    for sleutel, uitleg in (("match", "top 1000"), ("match_streng", f"top {TOP_STRENG}, alleen echte uitschieters")):
        R.append(f"MATCH MET WIKIPEDIA ({uitleg})")
        R.append("-" * 70)
        for naam, groep in groepen:
            R.append(f"{naam}  (n={len(groep)})")
            for label, drempel in (("ooit 2x ", 100.0), ("ooit 10x", 900.0)):
                tekst, _ = vergelijk(groep, sleutel, drempel)
                R.append(f"  {label}: {tekst}")
        R.append("")

    R.append("VOORBEELDEN — klopt de match? (hoogste stijgers met een match)")
    R.append("-" * 70)
    for r in sorted(regels, key=lambda x: -x["piek_pct"])[:25]:
        R.append(f"  {str(r['symbol'])[:12]:<13}{r['piek_pct'] / 100 + 1:>8.1f}x  "
                 f"'{r['woord']}' in {r['artikel'][:40]} (#{r['rang']}, {r['wikipedia'][:2]})")
    R.append("")
    R.append("Lezen: kijk eerst naar de voorbeelden. Zijn dat echte nieuwsmunten, of")
    R.append("toevallige woordovereenkomsten? Pas als de matches kloppen, zeggen de")
    R.append("percentages iets. En dan telt vooral de groep 'Door de harde filters':")
    R.append("de vraag is niet of nieuwsmunten bestaan, maar of ze het beter doen")
    R.append("dan de rest van wat onze filters doorlaten. p onder 0,05 = waarschijnlijk")
    R.append("geen toeval.")
    R.append("")
    R.append("Leest alleen openbare gegevens. Handelt niet en kan niet handelen.")
    tekst = "\n".join(R)
    RAPPORT.write_text(tekst + "\n", encoding="utf-8")
    return tekst


def main() -> None:
    argparse.ArgumentParser(description="Narratief-test met Wikipedia").parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", stream=sys.stdout)
    print(run())


if __name__ == "__main__":
    main()
