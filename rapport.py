"""
rapport.py — wekelijks overzicht per mail (plan §8.1).

Elke handmatige stap is een moment waarop dit project kan stranden: CSV
downloaden, in de goede map zetten, terminal openen. Over zes weken doe je dat
niet meer. Deze job draait wekelijks op GitHub, rekent alles uit en mailt de
uitkomst. Geen download, geen terminal.

Het grootste risico voor dit project is niet dat de filters niet werken. Het
is dat je stopt met kijken.

Draai:
    python rapport.py            # rekent en mailt
    python rapport.py --print    # alleen tonen, niet mailen
    python rapport.py --days 14  # andere periode
"""

from __future__ import annotations

import argparse
import logging
import statistics
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import config
import csv_log
import filters
import notify

log = logging.getLogger("rapport")

KOSTEN_PCT = 5.0
INLEG_EUR = 25.0


def _f(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _ts(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _dedup_per_token(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Dezelfde munt komt meerdere keren voorbij; tel hem één keer."""
    gezien: dict[str, dict[str, str]] = {}
    for row in sorted(rows, key=lambda r: r.get("timestamp_utc", "")):
        token = row.get("token_address", "")
        if token and token not in gezien:
            gezien[token] = row
    return list(gezien.values())


def _returns(rows: list[dict[str, str]], interval: str) -> list[float]:
    out = []
    for row in rows:
        instap = _f(row.get("price_usd"))
        uitstap = _f(row.get(f"price_{interval}"))
        if instap and uitstap is not None and instap > 0:
            out.append((uitstap / instap - 1.0) * 100.0)
    return out


def _paper(rendementen: list[float]) -> tuple[float, float]:
    """(inleg, eindwaarde) bij gelijke inzet per positie, na kosten."""
    inleg = INLEG_EUR * len(rendementen)
    eind = sum(INLEG_EUR * (1 + (r - KOSTEN_PCT) / 100.0) for r in rendementen)
    return inleg, eind


def build_report(rows: list[dict[str, str]], days: float = 7.0) -> str:
    nu = datetime.now(timezone.utc)
    grens = nu - timedelta(days=days)
    recent = [r for r in rows if (_ts(r.get("timestamp_utc", "")) or nu) >= grens]

    R: list[str] = []
    R.append(f"WEEKRAPPORT COINTRACKER — {nu:%d-%m-%Y}")
    R.append("=" * 62)
    R.append("")

    if not rows:
        return "\n".join(R + ["Nog geen logregels. Draait de scan wel?"])

    eerste = min((_ts(r.get("timestamp_utc", "")) for r in rows if _ts(r.get("timestamp_utc", ""))), default=nu)
    scans = len({r.get("scan_id", "") for r in recent})
    uren = max((nu - grens).total_seconds() / 3600.0, 1.0)

    R.append(f"Periode          : laatste {days:.0f} dagen")
    R.append(f"Logboek loopt al : sinds {eerste:%d-%m-%Y}, {len(rows)} regels totaal")
    R.append(f"Deze periode     : {len(recent)} regels, {len({r.get('token_address') for r in recent})} unieke munten")
    R.append(f"Scan-runs        : {scans}  (gemiddeld 1 per {uren*60/max(scans,1):.0f} minuten)")
    R.append(f"Actieve set      : {config.ACTIVE_SET} (die mailt; de rest loopt mee in het log)")
    R.append("")

    # ------------------------------------------------------------------ #
    # De hoofdvraag: welke schaduw-set doet het het beste?
    # ------------------------------------------------------------------ #
    R.append("WELKE DREMPELSET WINT?")
    R.append("-" * 62)
    R.append("Papieren handel: EUR 25 per alert, 5% kosten. GEEN echt geld.")
    R.append("")
    iets_gemeten = False
    for set_name in ("A", "B", "C", "D"):
        kolom = f"shadow_{set_name}_alert"
        gekozen = _dedup_per_token([r for r in recent if r.get(kolom) == "true"])
        regel = f"  Set {set_name}: {len(gekozen):4d} alerts"
        rendementen = _returns(gekozen, "24h")
        if rendementen:
            iets_gemeten = True
            inleg, eind = _paper(rendementen)
            plus30 = sum(1 for r in rendementen if r >= 30) / len(rendementen) * 100
            regel += (
                f" | n_meting={len(rendementen):3d}"
                f" | mediaan {statistics.median(rendementen):+7.1f}%"
                f" | >=+30%: {plus30:3.0f}%"
                f" | EUR {inleg:5,.0f} -> {eind:6,.0f} ({(eind/inleg-1)*100:+6.1f}%)"
            )
        else:
            regel += " | nog geen 24-uursdata"
        R.append(regel)
    R.append("")
    if not iets_gemeten:
        R.append("  Nog te vers om te vergelijken. Over een paar dagen staat hier iets.")
        R.append("")

    # ------------------------------------------------------------------ #
    # Piek versus eindstand
    # ------------------------------------------------------------------ #
    pieken = [_f(r.get("max_gain_pct")) for r in recent if r.get("alerted") == "true"]
    pieken = [p for p in pieken if p is not None]
    if pieken:
        R.append("PIEK VERSUS EINDSTAND (zat het geld in de selectie of in het verkopen?)")
        R.append("-" * 62)
        R.append(f"  Alerts met een piekmeting : {len(pieken)}")
        R.append(f"  Mediane hoogste stand     : {statistics.median(pieken):+.1f}%")
        R.append(f"  Stond ooit boven +30%     : {sum(1 for p in pieken if p >= 30)/len(pieken)*100:.0f}%")
        R.append(f"  Stond ooit boven +100%    : {sum(1 for p in pieken if p >= 100)/len(pieken)*100:.0f}%")
        eind24 = _returns([r for r in recent if r.get("alerted") == "true"], "24h")
        if eind24:
            R.append(f"  Mediane stand na 24 uur   : {statistics.median(eind24):+.1f}%")
            R.append("")
            R.append("  Ligt de piek veel hoger dan de eindstand, dan is je probleem niet")
            R.append("  het filter maar het uitstapmoment.")
        R.append("")

    # ------------------------------------------------------------------ #
    # Waar vielen coins af
    # ------------------------------------------------------------------ #
    R.append("WAAR VIELEN MUNTEN AF")
    R.append("-" * 62)
    uniek = _dedup_per_token(recent)
    for naam in filters.FILTER_NAMES:
        kolom = f"{naam}__outcome"
        fails = sum(1 for r in uniek if r.get(kolom) == "fail")
        geen = sum(1 for r in uniek if r.get(kolom) == "data_unavailable")
        if fails or geen:
            R.append(f"  {naam:26s} fail={fails:4d}  geen-data={geen:4d}")
    R.append("")

    # ------------------------------------------------------------------ #
    # Gezondheid
    # ------------------------------------------------------------------ #
    R.append("GEZONDHEID VAN DE BOT")
    R.append("-" * 62)
    harde = [f"{n}__outcome" for n in filters.HARD_FILTER_NAMES]
    totaal = len(uniek) * len(harde)
    onbekend = sum(1 for r in uniek for k in harde if r.get(k) == "data_unavailable")
    ratio = (onbekend / totaal * 100) if totaal else 0.0
    R.append(f"  Harde filters zonder data : {ratio:.0f}%")
    laatste = max((_ts(r.get("timestamp_utc", "")) for r in rows if _ts(r.get("timestamp_utc", ""))), default=None)
    if laatste:
        stil = (nu - laatste).total_seconds() / 3600.0
        R.append(f"  Laatste scan              : {stil:.1f} uur geleden")
        if stil > 6:
            R.append("  LET OP: er is al lang geen scan meer gedraaid. Check Actions.")
    R.append("")

    R.append("-" * 62)
    R.append("Dit is een meetinstrument, geen advies. De bot handelt niet en kan")
    R.append("niet handelen. Een set die voorloopt op een paar honderd metingen is")
    R.append("nog geen bewijs — kijk naar de trend over meerdere weken.")
    return "\n".join(R)


def run(days: float = 7.0, send: bool = True) -> str:
    rows = csv_log.read_rows()
    tekst = build_report(rows, days=days)
    if send:
        if notify.send_run_summary("Weekrapport", tekst):
            log.info("Weekrapport verstuurd.")
        else:
            log.error("Weekrapport kon niet verstuurd worden.")
    return tekst


def main() -> None:
    parser = argparse.ArgumentParser(description="Wekelijks rapport")
    parser.add_argument("--days", type=float, default=7.0)
    parser.add_argument("--print", dest="alleen_tonen", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", stream=sys.stdout)
    print(run(days=args.days, send=not args.alleen_tonen))


if __name__ == "__main__":
    main()
