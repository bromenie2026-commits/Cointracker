"""
followup.py — automatische follow-up op gelogde coins (plan §5.1).

Draait als losse job (elk uur). Voor elke logregel die 24u / 72u / 7d oud is
en nog geen follow-up-data heeft voor dat interval, wordt de huidige prijs en
marketcap opnieuw opgehaald en TERUGGESCHREVEN in dezelfde regel.

Dit gebeurt voor ALLE gelogde coins, ook de afgewezen — zodat je achteraf
kunt zien of je afwijzingen terecht waren. Dat was in het originele plan de
zwakste schakel: handmatig bijhouden gebeurt in de praktijk niet.

Draai:
    python followup.py
    python followup.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from typing import Optional

import config
import csv_log
import data_sources
import http_client

log = logging.getLogger("followup")

#: Volgorde van de meetmomenten. 1/4/12 uur zijn toegevoegd omdat een
#: memecoin vaak binnen een dag zijn hele levensloop doorloopt (plan §7.3).
INTERVALS = ("1h", "4h", "12h", "24h", "72h", "7d")


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def _parse_ts(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def age_hours(row: dict[str, str], now: Optional[datetime] = None) -> Optional[float]:
    now = now or datetime.now(timezone.utc)
    logged = _parse_ts(row.get("timestamp_utc", ""))
    if logged is None:
        return None
    return (now - logged).total_seconds() / 3600.0


def due_intervals(row: dict[str, str], now: Optional[datetime] = None) -> list[str]:
    """Welke intervallen zijn toe aan een follow-up voor deze regel?"""
    hours = age_hours(row, now)
    if hours is None:
        return []
    due = []
    for interval in INTERVALS:
        needed = config.FOLLOWUP_INTERVALS_HOURS.get(interval)
        if needed is None:
            continue
        if hours < needed:
            continue
        if (row.get(f"followup_{interval}_at") or "").strip():
            continue  # al ingevuld
        due.append(interval)
    return due


def _f(value: str) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def update_max_price(row: dict[str, str], price: Optional[float], now: datetime) -> None:
    """Houdt de hoogste prijs bij die we ooit bij een meetmoment zagen.

    Dit is een BENADERING: we kijken alleen op de meetmomenten (1, 4, 12, 24,
    72 uur en 7 dagen), niet continu. Een piek tussen twee metingen missen we.
    Maar het is het verschil tussen "er gebeurde niets" en "er gebeurde iets
    en ik was te laat", en dat onderscheid bestond tot nu toe helemaal niet.
    """
    if price is None or price <= 0:
        return
    huidige = _f(row.get("max_price_seen", ""))
    if huidige is not None and price <= huidige:
        return
    row["max_price_seen"] = f"{price:.12g}"
    row["max_price_at"] = now.isoformat()
    entry = _f(row.get("price_usd", ""))
    if entry and entry > 0:
        row["max_gain_pct"] = f"{(price / entry - 1) * 100:.1f}"


def apply_followup(row: dict[str, str], intervals: list[str], now: Optional[datetime] = None) -> bool:
    """Haalt actuele data op en schrijft die in de regel. True = gewijzigd."""
    now = now or datetime.now(timezone.utc)
    token = (row.get("token_address") or "").strip()
    if not token or not intervals:
        return False

    pairs, status = data_sources.fetch_pairs_for_token(token)
    pair = data_sources.best_pair(pairs)

    if status == "error":
        # BUGFIX 4.3: de API antwoordde niet. Dat is géén bewijs dat de munt
        # dood is. Niets invullen; bij de volgende run proberen we opnieuw.
        row["followup_note"] = f"API-fout bij {','.join(intervals)} — later opnieuw"
        return False

    if pair is None:
        # Nu wél zeker: de API antwoordde en er is geen markt meer.
        for interval in intervals:
            row[f"price_{interval}"] = "0"
            row[f"mc_eur_{interval}"] = "0"
            row[f"followup_{interval}_at"] = now.isoformat()
        row["followup_note"] = "geen pair meer gevonden (markt weg / naar nul)"
        return True

    mc_usd = pair.market_cap_usd if pair.market_cap_usd is not None else pair.fdv_usd
    mc_eur = data_sources.usd_to_eur(mc_usd)
    for interval in intervals:
        row[f"price_{interval}"] = "" if pair.price_usd is None else f"{pair.price_usd:.12g}"
        row[f"mc_eur_{interval}"] = "" if mc_eur is None else f"{mc_eur:.2f}"
        row[f"followup_{interval}_at"] = now.isoformat()

    update_max_price(row, pair.price_usd, now)
    if row.get("followup_note", "").startswith("API-fout"):
        row["followup_note"] = ""
    return True


def run(
    dry_run: bool = False,
    max_rows: Optional[int] = None,
    now: Optional[datetime] = None,
) -> int:
    http_client.reset_counters()
    max_rows = max_rows or config.FOLLOWUP_MAX_ROWS_PER_RUN

    rows = csv_log.read_rows()
    if not rows:
        log.info("Nog geen logregels — niets te doen.")
        return 0

    now = now or datetime.now(timezone.utc)
    pending = [(i, row, due_intervals(row, now)) for i, row in enumerate(rows)]
    pending = [(i, row, due) for i, row, due in pending if due]

    if not pending:
        log.info("Geen regels toe aan een follow-up (%d regels gecontroleerd).", len(rows))
        return 0

    # Oudste eerst — die dreigen anders steeds achteraan te blijven.
    pending.sort(key=lambda item: item[1].get("timestamp_utc", ""))
    batch = pending[:max_rows]
    log.info("%d regels toe aan follow-up, %d in deze run.", len(pending), len(batch))

    changed = 0
    for _, row, intervals in batch:
        token = row.get("token_address", "")
        try:
            if apply_followup(row, intervals, now):
                changed += 1
                log.info(
                    "  %s (%s): %s",
                    row.get("symbol") or token[:8],
                    ",".join(intervals),
                    row.get("followup_note") or f"mc={row.get('mc_eur_' + intervals[0])}",
                )
        except Exception as exc:  # noqa: BLE001
            log.exception("Follow-up faalde voor %s: %s", token, exc)

    if changed and not dry_run:
        csv_log.rewrite_rows(rows)
        log.info("%d regels bijgewerkt in %s", changed, config.SCAN_LOG_PATH)
    elif dry_run:
        log.info("dry-run: %d regels zouden zijn bijgewerkt", changed)

    log.info("Rate limits: %s", http_client.rate_limit_summary())
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description="Follow-up op gelogde coins")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-rows", type=int, default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    setup_logging(args.verbose)
    run(dry_run=args.dry_run, max_rows=args.max_rows)


if __name__ == "__main__":
    main()
