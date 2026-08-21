"""
csv_log.py — append-only scan-log met RUWE waardes (plan §5).

Dit bestand is de kern van het systeem. Elke gescande coin krijgt één regel,
doorgelaten of niet, met per filter zowel de uitkomst als de daadwerkelijk
gemeten waarde. Dat maakt drempel-tuning achteraf mogelijk zonder opnieuw
te scannen ("wat als de bot-score-grens 40 was in plaats van 60?").

De follow-up-job (followup.py) schrijft later in DEZELFDE regel de
kolommen price_24h / price_72h / price_7d bij.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import config
import data_sources
import filters
from models import Evaluation

log = logging.getLogger(__name__)

BASE_COLUMNS = [
    "row_id",
    "timestamp_utc",
    "scan_id",
    "token_address",
    "pair_address",
    "symbol",
    "name",
    "dex_id",
    "pair_url",
    "pair_created_at_utc",
    "pair_age_minutes",
    "price_usd",
    "market_cap_eur",
    "liquidity_eur",
    "liq_mc_ratio",
    "volume_h1_eur",
    "volume_h24_eur",
    "buys_h1",
    "sells_h1",
    "buys_h24",
    "sells_h24",
]

RESULT_COLUMNS = [
    "hard_pass",
    "soft_score",
    "alerted",
    "alert_suppressed_reason",
    "blocking_reasons",
    "data_unavailable_filters",
    "rugcheck_source",
    "rugcheck_score",
    "rugcheck_error",
    "deployer_wallet",
    "narrative_verdict",
]

#: Meetmomenten na het alert. 1/4/12 uur zijn toegevoegd omdat de levensloop
#: van een memecoin vaak korter is dan 24 uur (plan §7.3).
FOLLOWUP_INTERVALS = ["1h", "4h", "12h", "24h", "72h", "7d"]

FOLLOWUP_COLUMNS = []
for _iv in FOLLOWUP_INTERVALS:
    FOLLOWUP_COLUMNS += [f"price_{_iv}", f"mc_eur_{_iv}", f"followup_{_iv}_at"]
FOLLOWUP_COLUMNS += [
    # De hoogste prijs die we bij een meetmoment zagen. Zonder dit kun je niet
    # onderscheiden tussen "er gebeurde niets" en "er gebeurde iets en je was
    # te laat" (plan §7.3).
    "max_price_seen",
    "max_gain_pct",
    "max_price_at",
    "followup_note",
]

SHADOW_COLUMNS = [f"shadow_{s}_alert" for s in ("A", "B", "C", "D")] + ["active_set"]

#: Verandering sinds de vorige waarneming van dezelfde munt (plan §7.2).
DELTA_COLUMNS = ["minutes_since_prev"] + [
    f"{veld}_delta_pct" for veld in filters.DELTA_FIELDS
]


def filter_columns() -> list[str]:
    cols: list[str] = []
    for name in filters.FILTER_NAMES:
        cols.append(f"{name}__outcome")
        cols.append(f"{name}__raw")
    return cols


def header() -> list[str]:
    return (
        BASE_COLUMNS
        + filter_columns()
        + RESULT_COLUMNS
        + SHADOW_COLUMNS
        + DELTA_COLUMNS
        + FOLLOWUP_COLUMNS
    )


def _iso(ts_ms: Optional[int]) -> str:
    if not ts_ms:
        return ""
    try:
        return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _num(value: Optional[float], digits: int = 6) -> str:
    if value is None:
        return ""
    return f"{round(value, digits)}"


def _raw_to_cell(value: Any) -> str:
    """Ruwe waarde als string. Dicts worden compacte JSON zodat je ze later
    programmatisch terug kunt lezen zonder de CSV te breken."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, str)):
        return str(value)
    try:
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def build_row(evaluation: Evaluation, scan_id: str) -> dict[str, str]:
    pair = evaluation.pair
    report = evaluation.rugcheck

    mc_usd = None
    if pair:
        mc_usd = pair.market_cap_usd if pair.market_cap_usd is not None else pair.fdv_usd
    liq_mc = None
    if pair and pair.liquidity_usd is not None and mc_usd:
        liq_mc = pair.liquidity_usd / mc_usd

    row: dict[str, str] = {c: "" for c in header()}
    row.update(
        {
            "row_id": uuid.uuid4().hex[:16],
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "scan_id": scan_id,
            "token_address": evaluation.token_address,
            "pair_address": pair.pair_address if pair else "",
            "symbol": evaluation.symbol,
            "name": evaluation.name,
            "dex_id": pair.dex_id if pair else "",
            "pair_url": evaluation.dexscreener_url,
            "pair_created_at_utc": _iso(pair.pair_created_at_ms) if pair else "",
            "pair_age_minutes": _num(pair.age_minutes, 1) if pair else "",
            "price_usd": _num(pair.price_usd, 12) if pair else "",
            "market_cap_eur": _num(data_sources.usd_to_eur(mc_usd), 2),
            "liquidity_eur": _num(
                data_sources.usd_to_eur(pair.liquidity_usd) if pair else None, 2
            ),
            "liq_mc_ratio": _num(liq_mc, 4),
            "volume_h1_eur": _num(
                data_sources.usd_to_eur(pair.volume_h1_usd) if pair else None, 2
            ),
            "volume_h24_eur": _num(
                data_sources.usd_to_eur(pair.volume_h24_usd) if pair else None, 2
            ),
            "buys_h1": str(pair.buys_h1) if pair and pair.buys_h1 is not None else "",
            "sells_h1": str(pair.sells_h1) if pair and pair.sells_h1 is not None else "",
            "buys_h24": str(pair.buys_h24) if pair and pair.buys_h24 is not None else "",
            "sells_h24": str(pair.sells_h24) if pair and pair.sells_h24 is not None else "",
        }
    )

    unavailable = []
    for result in evaluation.results:
        row[f"{result.name}__outcome"] = result.outcome.value
        row[f"{result.name}__raw"] = _raw_to_cell(result.raw_value)
        if result.outcome.value == "data_unavailable":
            unavailable.append(f"{result.name}:{result.detail or 'geen reden'}")

    row.update(
        {
            "hard_pass": "true" if evaluation.hard_pass else "false",
            "soft_score": _num(evaluation.soft_score, 1),
            "alerted": "true" if evaluation.alerted else "false",
            "alert_suppressed_reason": evaluation.alert_suppressed_reason,
            "blocking_reasons": "; ".join(evaluation.blocking_reasons),
            "data_unavailable_filters": "; ".join(unavailable),
            "rugcheck_source": report.source if report else "",
            "rugcheck_score": str(report.score) if report and report.score is not None else "",
            "rugcheck_error": (report.error or "")[:200] if report else "",
            "deployer_wallet": (evaluation.deployer.wallet or "") if evaluation.deployer else "",
            "narrative_verdict": (
                (evaluation.narrative.verdict or "")[:200] if evaluation.narrative else ""
            ),
            "active_set": config.ACTIVE_SET,
        }
    )

    for set_name, would in (evaluation.shadow_sets or {}).items():
        row[f"shadow_{set_name}_alert"] = "true" if would else "false"

    for sleutel, waarde in (evaluation.deltas or {}).items():
        if sleutel in row:
            row[sleutel] = _raw_to_cell(waarde)

    return row


# --------------------------------------------------------------------------- #
# Lezen / schrijven
# --------------------------------------------------------------------------- #


def ensure_file(path: Optional[Path] = None) -> Path:
    path = Path(path or config.SCAN_LOG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.stat().st_size == 0:
        with path.open("w", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=header()).writeheader()
    return path


def migrate_if_needed(path: Optional[Path] = None) -> list[str]:
    """Voegt nieuwe kolommen toe aan een bestaand logbestand.

    Zonder dit zou `append_rows` het bestaande (oude) kopregel gebruiken en
    alle nieuwe kolommen stilletjes weggooien — dan log je de nieuwe signalen
    wel, maar komen ze nergens terecht. Bestaande regels blijven ongemoeid;
    de nieuwe kolommen blijven daar leeg.
    """
    path = ensure_file(path)
    bestaand = read_header(path)
    ontbrekend = [c for c in header() if c not in bestaand]
    if not ontbrekend:
        return bestaand
    log.info("Logschema uitgebreid met %d kolommen: %s", len(ontbrekend), ", ".join(ontbrekend))
    rewrite_rows(read_rows(path), path)
    return read_header(path)


def append_rows(rows: Iterable[dict[str, str]], path: Optional[Path] = None) -> int:
    rows = list(rows)
    if not rows:
        return 0
    path = ensure_file(path)
    cols = migrate_if_needed(path)
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in cols})
    return len(rows)


def read_header(path: Optional[Path] = None) -> list[str]:
    path = Path(path or config.SCAN_LOG_PATH)
    if not path.exists():
        return header()
    with path.open("r", newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        try:
            existing = next(reader)
        except StopIteration:
            return header()
    return existing or header()


def read_rows(path: Optional[Path] = None) -> list[dict[str, str]]:
    path = Path(path or config.SCAN_LOG_PATH)
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as fh:
        return [dict(row) for row in csv.DictReader(fh)]


def rewrite_rows(rows: list[dict[str, str]], path: Optional[Path] = None) -> None:
    """Volledige, atomaire herschrijving — gebruikt door followup.py.

    We migreren meteen naar het actuele schema: nieuwe kolommen die in een
    oud logbestand ontbraken worden toegevoegd, bestaande blijven staan.
    """
    path = Path(path or config.SCAN_LOG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)

    cols = list(read_header(path))
    for col in header():
        if col not in cols:
            cols.append(col)

    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in cols})
    os.replace(tmp_name, path)
