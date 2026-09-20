"""
vroege_kopers.py — wie kocht er als eerste, en zitten dezelfde wallets steeds
vroeg in de winnaars?

WAAROM
------

Ruim dertig kenmerken van de munt zelf zijn getoetst — prijs, volume,
liquiditeit, marketcap, leeftijd, aantal transacties — en geen enkele scheidt
de grote winnaars van de rest. Wat we nooit hebben bekeken is wíé er koopt.
Op Solana is elke aankoop openbaar. Als dezelfde wallets steeds vroeg in
munten zitten die later ontploffen, is dat een signaal dat in geen enkele
koers of volume zichtbaar is.

WAT HET DOET
------------

1. Verzamelen. Voor elke munt waarover een alert is verstuurd: blader in de
   pool die we bij het alert zagen terug naar de allereerste transacties,
   open de oudste 40, en noteer welke wallets daarin de munt kochten.
2. Analyseren. Welke wallets komen bij meerdere munten terug, en zitten die
   vaker in winnaars dan in verliezers?

De analyse is bewust streng. De verleidelijke fout is: "wallet X zat vroeg in
ZCAT, ZCAT won, dus wallet X voorspelt winnaars." Dat is altijd waar en zegt
niets. Daarom tellen we voor elke munt alleen wallets die vroeg in ÁNDERE
winnaars zaten, en in de strengste variant alleen in winnaars die vóór deze
munt al bekend waren. Dat laatste is het enige wat de bot in het echt had
kunnen weten.

Wallets die in vrijwel alles zitten (snipers, bots) worden apart gezet: die
kopen elke lancering en zeggen dus niets.

KOSTEN
------

Alleen leesverzoeken op je eigen Helius-sleutel. Ongeveer 30.000 aanroepen
voor ~600 munten, ruim binnen het gratis maandbudget van een miljoen.
Hervatbaar: munten die al gedaan zijn worden overgeslagen.

Dit script leest alleen. Het handelt niet en kan niet handelen.

Draaien:  python vroege_kopers.py --limit 5        (eerst proberen)
          python vroege_kopers.py                  (alles verzamelen + rapport)
          python vroege_kopers.py --alleen-analyse (alleen het rapport)
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import config
import csv_log
import data_sources
import repair_log

log = logging.getLogger(__name__)

UITVOER = Path(config.LOG_DIR) / "vroege_kopers.csv"
RAPPORT = Path(config.LOG_DIR) / "vroege_kopers_rapport.txt"
KOLOMMEN = ["token_address", "symbol", "alert_ts", "bron", "wallet", "rang", "status"]

#: Hoeveel van de oudste transacties per munt we openen.
VROEGSTE_N = 40
#: Hoe ver we maximaal terugbladeren (x 1000 handtekeningen). Een munt die zo
#: druk is dat het begin daarbuiten ligt, slaan we over en melden we.
MAX_PAGINAS = 60
#: Vanaf deze stijging telt een munt als winnaar.
WINNAAR_FACTOR = 10.0
#: Alleen alerts vanaf de holder-fix; daarvoor is het logboek onbetrouwbaar.
VANAF = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
#: Een wallet die in meer dan dit deel van alle munten vroeg zit, koopt alles.
BOT_DREMPEL = 0.15
#: ...en in minstens zoveel munten, anders telt een kleine proef niet.
BOT_MIN_MUNTEN = 3


# --------------------------------------------------------------------------- #
# Verzamelen
# --------------------------------------------------------------------------- #


def oudste_handtekeningen(
    adres: str, n: int = VROEGSTE_N, max_paginas: int = MAX_PAGINAS
) -> tuple[list[str], str]:
    """De n oudste geslaagde transacties van een adres, oudste eerst.

    De RPC geeft de nieuwste eerst, dus we bladeren terug tot een pagina niet
    meer vol is: dan zijn we bij het begin.
    """
    vorige: list[dict[str, Any]] = []
    staart: list[dict[str, Any]] = []
    voor: Optional[str] = None
    for _ in range(max_paginas):
        opties: dict[str, Any] = {"limit": 1000}
        if voor:
            opties["before"] = voor
        resp = data_sources.rpc_call("getSignaturesForAddress", [adres, opties])
        if not resp.ok:
            return [], f"fout: {resp.error}"
        pagina = (resp.data or {}).get("result") or []
        if len(pagina) < 1000:
            # Niet vol = het begin. De vorige pagina houden we erbij voor het
            # geval deze laatste er minder dan n bevat. Beide staan nieuw->oud.
            staart = vorige + pagina
            break
        vorige = pagina
        voor = pagina[-1].get("signature")
    else:
        return [], "begin niet bereikt"

    geslaagd = [s for s in staart if not s.get("err") and s.get("signature")]
    if not geslaagd:
        return [], "geen transacties"
    oudste = list(reversed(geslaagd))[:n]
    return [s["signature"] for s in oudste], "ok"


def _pubkey(sleutel: Any) -> Optional[str]:
    if isinstance(sleutel, dict):
        return sleutel.get("pubkey")
    return sleutel if isinstance(sleutel, str) else None


def koper_uit_transactie(tx: Optional[dict[str, Any]], mint: str) -> Optional[str]:
    """De wallet die in deze transactie de munt kocht, of None.

    "Kopen" = de betaler van de transactie heeft na afloop meer van deze munt
    dan ervoor. Pool aanmaken, liquiditeit toevoegen en verkopen tellen dus
    niet mee.
    """
    if not tx:
        return None
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return None
    sleutels = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    if not sleutels:
        return None
    betaler = _pubkey(sleutels[0])
    if not betaler:
        return None

    def saldo(lijst: list[dict[str, Any]]) -> float:
        totaal = 0.0
        for b in lijst or []:
            if b.get("mint") != mint or b.get("owner") != betaler:
                continue
            bedrag = ((b.get("uiTokenAmount") or {}).get("uiAmount")) or 0.0
            totaal += float(bedrag)
        return totaal

    voor = saldo(meta.get("preTokenBalances"))
    na = saldo(meta.get("postTokenBalances"))
    return betaler if na > voor else None


def _al_gedaan() -> set[str]:
    if not UITVOER.exists():
        return set()
    with UITVOER.open(newline="", encoding="utf-8") as fh:
        return {r["token_address"] for r in csv.DictReader(fh)}


def _schrijf(regels: list[dict[str, Any]]) -> None:
    nieuw = not UITVOER.exists()
    UITVOER.parent.mkdir(parents=True, exist_ok=True)
    with UITVOER.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=KOLOMMEN)
        if nieuw:
            w.writeheader()
        for r in regels:
            w.writerow({k: r.get(k, "") for k in KOLOMMEN})


def _ts(waarde: str) -> Optional[datetime]:
    try:
        t = datetime.fromisoformat(waarde)
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def alerts() -> list[dict[str, str]]:
    """Eerste alert per munt, vanaf de holder-fix, oudste eerst."""
    gezien: dict[str, dict[str, str]] = {}
    for r in csv_log.read_rows():
        if str(r.get("alerted", "")).lower() not in ("true", "1"):
            continue
        t = _ts(r.get("timestamp_utc", ""))
        if t is None or t < VANAF:
            continue
        adres = r.get("token_address", "")
        if adres and adres not in gezien:
            gezien[adres] = r
    return sorted(gezien.values(), key=lambda r: r.get("timestamp_utc", ""))


def verzamel_munt(r: dict[str, str]) -> list[dict[str, Any]]:
    mint = r["token_address"]
    basis = {"token_address": mint, "symbol": r.get("symbol", ""), "alert_ts": r.get("timestamp_utc", "")}

    # Eerst de pool die we bij het alert zagen; lukt dat niet, dan de munt zelf.
    laatste_status = "geen adres"
    for bron, adres in (("pool", r.get("pair_address", "")), ("munt", mint)):
        if not adres:
            continue
        handtekeningen, status = oudste_handtekeningen(adres)
        if status != "ok":
            laatste_status = status
            continue
        kopers: list[str] = []
        for sig in handtekeningen:
            wallet = koper_uit_transactie(data_sources.get_transaction(sig), mint)
            if wallet and wallet not in kopers:
                kopers.append(wallet)
        if kopers:
            return [
                {**basis, "bron": bron, "wallet": w, "rang": i + 1, "status": "ok"}
                for i, w in enumerate(kopers)
            ]
        laatste_status = "geen kopers gevonden"
    return [{**basis, "bron": "", "wallet": "", "rang": "", "status": laatste_status}]


def verzamel(limit: Optional[int] = None) -> int:
    gedaan = _al_gedaan()
    todo = [r for r in alerts() if r["token_address"] not in gedaan]
    if limit:
        todo = todo[:limit]
    log.info("%d munten te doen (%d al gedaan).", len(todo), len(gedaan))
    for i, r in enumerate(todo, start=1):
        regels = verzamel_munt(r)
        _schrijf(regels)
        ok = [x for x in regels if x["status"] == "ok"]
        log.info(
            "[%d/%d] %s: %s",
            i,
            len(todo),
            r.get("symbol") or r["token_address"][:8],
            f"{len(ok)} vroege kopers" if ok else regels[0]["status"],
        )
    return len(todo)


# --------------------------------------------------------------------------- #
# Analyseren
# --------------------------------------------------------------------------- #


def uitkomsten() -> dict[str, dict[str, Any]]:
    """Per gealerteerde munt: alerttijd en of hij een winnaar werd."""
    uit: dict[str, dict[str, Any]] = {}
    for r in alerts():
        mint = r["token_address"]
        try:
            piek = float(r.get("max_gain_pct") or "nan")
        except ValueError:
            piek = float("nan")
        # Van de nagekeken foute munten is de hoogste stand onbetrouwbaar.
        if mint in repair_log.AANTOONBAAR_FOUT and piek > (repair_log.FOUT_BOVEN_FACTOR - 1) * 100:
            piek = float("nan")
        uit[mint] = {
            "symbol": r.get("symbol", ""),
            "ts": r.get("timestamp_utc", ""),
            "piek": piek,
            "winnaar": piek >= (WINNAAR_FACTOR - 1) * 100 if piek == piek else None,
        }
    return uit


def _fisher(a: int, b: int, c: int, d: int) -> float:
    try:
        from scipy.stats import fisher_exact

        return float(fisher_exact([[a, b], [c, d]])[1])
    except Exception:  # noqa: BLE001 — scipy is optioneel
        return float("nan")


def analyseer() -> str:
    if not UITVOER.exists():
        return "Nog geen vroege-kopersdata. Draai eerst het verzamelen."
    with UITVOER.open(newline="", encoding="utf-8") as fh:
        regels = list(csv.DictReader(fh))
    uit = uitkomsten()

    kopers: dict[str, set[str]] = defaultdict(set)
    status: dict[str, str] = {}
    for r in regels:
        status[r["token_address"]] = r["status"]
        if r["status"] == "ok" and r["wallet"]:
            kopers[r["token_address"]].add(r["wallet"])

    munten = [m for m in kopers if m in uit and uit[m]["winnaar"] is not None]
    n = len(munten)
    winnaars = [m for m in munten if uit[m]["winnaar"]]

    per_wallet: dict[str, set[str]] = defaultdict(set)
    for m in munten:
        for w in kopers[m]:
            per_wallet[w].add(m)

    # Minstens drie munten, anders is bij een kleine proef elke wallet een
    # "bot" (bij 5 munten zit één aankoop al op 20%). Zo stond het in de
    # eerste proef van 19-09: 88 van de 88 wallets als bot aangemerkt.
    bots = {
        w for w, ms in per_wallet.items()
        if n and len(ms) >= BOT_MIN_MUNTEN and len(ms) / n > BOT_DREMPEL
    }

    R: list[str] = []
    R.append("VROEGE KOPERS — zitten dezelfde wallets steeds in de winnaars?")
    R.append("=" * 66)
    R.append(f"Gemaakt       : {datetime.now(timezone.utc):%d-%m-%Y %H:%M} UTC")
    R.append(f"Munten        : {len(status)} bekeken, {n} met kopers én een uitkomst")
    R.append(f"Winnaars      : {len(winnaars)} (ooit {WINNAAR_FACTOR:.0f}x of meer)")
    tel = defaultdict(int)
    for s in status.values():
        tel[s if not s.startswith("fout") else "fout"] += 1
    R.append("Dekking       : " + ", ".join(f"{k} {v}" for k, v in sorted(tel.items())))
    R.append(f"Wallets       : {len(per_wallet)} verschillende vroege kopers")
    R.append(f"Bots/snipers  : {len(bots)} (zitten in meer dan {BOT_DREMPEL:.0%} van alle munten; tellen niet mee)")
    R.append("")

    herhalers = {w: ms for w, ms in per_wallet.items() if len(ms) >= 2 and w not in bots}
    R.append(f"Wallets die in 2 of meer munten vroeg zaten: {len(herhalers)}")
    R.append("")
    R.append("Meest opvallend (in de meeste winnaars):")
    top = sorted(
        herhalers.items(),
        key=lambda kv: (sum(1 for m in kv[1] if uit[m]["winnaar"]), -len(kv[1])),
        reverse=True,
    )[:15]
    for w, ms in top:
        nw = sum(1 for m in ms if uit[m]["winnaar"])
        namen = ", ".join(uit[m]["symbol"] for m in ms if uit[m]["winnaar"])[:60]
        R.append(f"  {w[:8]}…  {nw} winnaars / {len(ms)} munten   {namen}")
    R.append("")

    def score(m: str, alleen_eerder: bool) -> int:
        """Hoeveel vroege kopers van m zaten ook vroeg in een ÁNDERE winnaar?"""
        t = 0
        for w in kopers[m] - bots:
            for ander in per_wallet[w]:
                if ander == m or not uit[ander]["winnaar"]:
                    continue
                if alleen_eerder and uit[ander]["ts"] >= uit[m]["ts"]:
                    continue
                t += 1
                break
        return t

    for naam, streng in (("Zonder de munt zelf (optimistisch)", False),
                         ("Alleen eerdere winnaars (wat de bot echt had kunnen weten)", True)):
        s = {m: score(m, streng) for m in munten}
        a = sum(1 for m in winnaars if s[m] > 0)
        b = len(winnaars) - a
        c = sum(1 for m in munten if not uit[m]["winnaar"] and s[m] > 0)
        d = (n - len(winnaars)) - c
        met = a + c
        R.append(naam)
        R.append("-" * 66)
        R.append(f"  Munten met minstens één 'winnaarswallet' vroeg erin : {met}")
        if met:
            R.append(f"    daarvan werd winnaar                         : {a} ({a / met:.0%})")
        zonder = b + d
        if zonder:
            R.append(f"  Munten zonder zo'n wallet                         : {zonder}")
            R.append(f"    daarvan werd winnaar                         : {b} ({b / zonder:.0%})")
        R.append(f"  Toevalskans (Fisher)                              : {_fisher(a, b, c, d):.3f}")
        R.append("")

    R.append("Lezen: het tweede blok is het enige dat telt. Scoren munten met een")
    R.append("'winnaarswallet' daar duidelijk hoger dan zonder, en is de toevalskans")
    R.append("klein (onder 0,05), dan zit er iets. Zo niet, dan niet — hoe mooi de")
    R.append("lijst met opvallende wallets hierboven er ook uitziet.")
    R.append("")
    R.append("Dit script leest alleen. Het handelt niet en kan niet handelen.")
    tekst = "\n".join(R)
    RAPPORT.write_text(tekst + "\n", encoding="utf-8")
    return tekst


def main() -> None:
    parser = argparse.ArgumentParser(description="Vroege kopers verzamelen en analyseren")
    parser.add_argument("--limit", type=int, default=None, help="Maximaal zoveel munten")
    parser.add_argument("--alleen-analyse", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        stream=sys.stdout,
    )
    if not args.alleen_analyse:
        verzamel(limit=args.limit)
    print()
    print(analyseer())


if __name__ == "__main__":
    main()
