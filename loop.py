"""
loop.py — één doorlopende taak die de scan, de follow-up en de volglijst
om beurten uitvoert, in plaats van drie losse taken die GitHub moet wekken.

WAAROM DIT BESTAAT
------------------

Twee gemeten problemen met de oude opzet:

1. **GitHub houdt zich niet aan het schema.** Gevraagd werd elke 20 minuten
   een scan en elke 10 minuten een volglijst-meting. Werkelijk gemeten over
   116 scans: mediaan 97 minuten. De volglijst: mediaan 101 minuten. En op
   26-08 om 14:29 UTC hield GitHub er zonder melding 18 uur helemaal mee op.
   Op zo'n wekker kun je geen meting bouwen.

2. **De drie taken botsten op hetzelfde bestand.** De scan schreef om 14:29
   het logboek weg; de follow-up, die om 14:29 met een oudere versie was
   begonnen, kon zijn eigen versie daarna niet meer kwijt en viel om met
   exit code 1. Twee schrijvers, één bestand — dat gaat een keer mis.

Beide verdwijnen als er nog maar één taak is die lang blijft draaien en zijn
eigen klok bijhoudt. Binnen deze lus loopt alles netjes achter elkaar, dus
er is per definitie maar één schrijver.

HOE HET WERKT
-------------

De lus draait ongeveer vijf uur en doet in die tijd:

* elke 10 minuten : de volglijst meten (goedkoop, één call per munt)
* elke 20 minuten : een scan
* elke 60 minuten : de follow-up

Na elke ronde wordt er gecommit en gepusht, zodat je nooit meer dan één
ronde kwijt kunt raken als de runner halverwege wordt afgebroken.

De workflow start elk uur opnieuw. Draait er al een lus, dan blijft de
nieuwe wachten en neemt hij het over zodra de vorige klaar is. Zo is er
vrijwel altijd één lus actief, ook als GitHub een paar starts overslaat.

Dit systeem handelt niet en kan niet handelen.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

import config

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Git — het wegschrijven van de meting
# --------------------------------------------------------------------------- #


def _git(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(config.BASE_DIR),
        capture_output=True,
        text=True,
        check=check,
        timeout=180,
    )


def git_available() -> bool:
    """Buiten een checkout (bijvoorbeeld op je eigen laptop) slaan we dit over."""
    if not (config.BASE_DIR / ".git").exists():
        return False
    return shutil.which("git") is not None


def pull_first() -> None:
    """Eerst ophalen, dan pas werken. Voorkomt dat je achterloopt bij het pushen."""
    if not git_available():
        return
    result = _git("pull", "--rebase", "--autostash")
    if result.returncode != 0:
        log.warning("Ophalen mislukte: %s", (result.stderr or "").strip()[:300])
        _git("rebase", "--abort")


def _bewaar_kopie(reden: str) -> None:
    """Zet het logboek veilig in raw/ voordat we iets weggooien.

    raw/ gaat als artifact naar buiten en niet de git-geschiedenis in, dus
    zelfs als we lokaal moeten terugvallen op de versie van GitHub is de
    meting niet echt weg.
    """
    doel = Path(config.RAW_ARCHIVE_DIR) / "noodkopie"
    try:
        doel.mkdir(parents=True, exist_ok=True)
        for pad in config.LOG_DIR.glob("*.csv"):
            shutil.copy2(pad, doel / f"{int(time.time())}-{pad.name}")
        log.warning("Noodkopie van het logboek gemaakt in raw/noodkopie (%s)", reden)
    except OSError as exc:
        log.error("Noodkopie mislukte: %s", exc)


def commit_and_push(bericht: str, mislukt_op_rij: int = 0) -> tuple[bool, int]:
    """Commit logs/ en state/ en push. Geeft (gelukt, aantal mislukt op rij)."""
    if not git_available():
        return True, 0

    _git("config", "user.name", "memecoin-alert-bot")
    _git("config", "user.email", "actions@github.com")
    _git("add", "-A", "logs", "state")

    if _git("diff", "--cached", "--quiet").returncode == 0:
        return True, 0  # niets veranderd, dat is geen fout

    _git("commit", "-m", f"{bericht} [skip ci]")

    for poging in (1, 2, 3):
        pull = _git("pull", "--rebase", "--autostash")
        if pull.returncode != 0:
            log.warning("Rebase mislukte (poging %d)", poging)
            _git("rebase", "--abort")
            time.sleep(5)
            continue
        if _git("push").returncode == 0:
            return True, 0
        log.warning("Push mislukte (poging %d)", poging)
        time.sleep(5)

    mislukt_op_rij += 1
    log.error("Kon niet pushen (%d keer op rij mislukt).", mislukt_op_rij)

    # Blijft het hangen, dan is er een echt conflict met wat er op GitHub
    # staat. Doorgaan zou betekenen dat we voor altijd vastzitten. We zetten
    # het logboek dus veilig in raw/ en beginnen opnieuw vanaf GitHub.
    if mislukt_op_rij >= 3:
        _bewaar_kopie("drie mislukte pushes op rij")
        _git("fetch", "origin")
        if _git("reset", "--hard", "origin/main").returncode == 0:
            log.warning("Teruggezet naar de versie op GitHub; de lus gaat verder.")
            return False, 0
    return False, mislukt_op_rij


# --------------------------------------------------------------------------- #
# De taken
# --------------------------------------------------------------------------- #


def _veilig(naam: str, functie: Callable[[], object]) -> bool:
    """Voert één taak uit. Een fout stopt nooit de hele lus."""
    start = time.time()
    try:
        uitkomst = functie()
    except Exception as exc:  # noqa: BLE001 — de lus moet blijven leven
        log.exception("%s faalde: %s", naam, exc)
        return False
    log.info("%s klaar in %.0fs (%s)", naam, time.time() - start, uitkomst)
    return True


def taak_volglijst(dry_run: bool) -> bool:
    import watchlist

    return _veilig("volglijst", lambda: watchlist.run(dry_run=dry_run))


def taak_scan(dry_run: bool) -> bool:
    import main as scan_module

    return _veilig("scan", lambda: scan_module.run(dry_run=dry_run))


def taak_followup(dry_run: bool) -> bool:
    import followup

    return _veilig("follow-up", lambda: followup.run(dry_run=dry_run))


# --------------------------------------------------------------------------- #
# De lus
# --------------------------------------------------------------------------- #


def run(
    duur_minuten: Optional[float] = None,
    dry_run: bool = False,
    tijdbron: Callable[[], float] = time.time,
    slaap: Callable[[float], None] = time.sleep,
) -> dict[str, int]:
    """Draait tot de tijd op is. Geeft terug hoe vaak elke taak liep."""
    duur = duur_minuten if duur_minuten is not None else config.LOOP_MINUTES
    start = tijdbron()
    einde = start + duur * 60.0

    # Wanneer elke taak voor het laatst liep. None = nog nooit, dus meteen.
    laatst: dict[str, Optional[float]] = {"volglijst": None, "scan": None, "followup": None}
    intervallen = {
        "volglijst": config.LOOP_WATCHLIST_MINUTES * 60.0,
        "scan": config.LOOP_SCAN_MINUTES * 60.0,
        "followup": config.LOOP_FOLLOWUP_MINUTES * 60.0,
    }
    taken = {"volglijst": taak_volglijst, "scan": taak_scan, "followup": taak_followup}
    tellers = {naam: 0 for naam in taken}

    log.info(
        "Lus gestart voor %.0f minuten — volglijst/%.0f scan/%.0f followup/%.0f min",
        duur,
        config.LOOP_WATCHLIST_MINUTES,
        config.LOOP_SCAN_MINUTES,
        config.LOOP_FOLLOWUP_MINUTES,
    )

    if not dry_run:
        pull_first()

    mislukt = 0
    while tijdbron() < einde:
        nu = tijdbron()
        gedaan = []

        # Volgorde is bewust: eerst de goedkope volglijst, dan de scan (die
        # nieuwe munten op de volglijst kan zetten), dan de follow-up.
        for naam in ("volglijst", "scan", "followup"):
            vorige = laatst[naam]
            if vorige is not None and nu - vorige < intervallen[naam]:
                continue
            taken[naam](dry_run)
            laatst[naam] = tijdbron()
            tellers[naam] += 1
            gedaan.append(naam)

        if gedaan and not dry_run:
            _, mislukt = commit_and_push("lus: " + ", ".join(gedaan), mislukt)

        # Slapen tot de eerstvolgende taak aan de beurt is, maar niet voorbij
        # het einde van de lus.
        nu = tijdbron()
        volgende = min(
            (laatst[n] or nu) + intervallen[n] for n in taken
        )
        wachten = max(5.0, min(volgende, einde) - nu)
        if nu + wachten >= einde:
            break
        log.info("Wachten %.0f minuten tot de volgende taak.", wachten / 60.0)
        slaap(wachten)

    log.info(
        "Lus klaar: %d volglijst, %d scans, %d follow-ups.",
        tellers["volglijst"],
        tellers["scan"],
        tellers["followup"],
    )
    return tellers


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Doorlopende lus")
    parser.add_argument("--minutes", type=float, default=None, help="Hoe lang draaien")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    run(duur_minuten=args.minutes, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
