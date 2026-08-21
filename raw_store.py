"""
raw_store.py — archief van de volledige API-antwoorden (plan §7.5).

Waarom dit bestaat: uit elk DexScreener- en rugcheck-antwoord halen we een
vast setje velden en gooien we de rest weg. Daardoor konden we pas achteraf
ontdekken dat de gemiddelde tradegrootte een signaal is — en een veld dat we
volgende maand interessant vinden, is voor de data van vandaag dan al weg.

**De waardevolste analyse is bijna altijd degene die je nog niet had bedacht.**
Dit bestand is het enige wat die mogelijk houdt.

Belangrijk: dit archief gaat NIET de git-geschiedenis in. Het wordt per run
als GitHub Actions-artifact bewaard (90 dagen). Zou je het committen, dan
groeit je repo onbeperkt, want git vergeet nooit iets.
"""

from __future__ import annotations

import gzip
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

import config

log = logging.getLogger(__name__)


def _path_for(scan_id: str) -> Path:
    return Path(config.RAW_ARCHIVE_DIR) / f"scan-{scan_id}.jsonl.gz"


def save(
    scan_id: str,
    row_id: str,
    token_address: str,
    payloads: dict[str, Any],
    path: Optional[Path] = None,
) -> bool:
    """Schrijft één regel weg met alle ruwe antwoorden voor deze coin.

    `row_id` is dezelfde sleutel als in scan_log.csv, zodat je een logregel
    later aan zijn ruwe data kunt koppelen.
    """
    if not config.RAW_ARCHIVE_ENABLED:
        return False

    target = Path(path) if path else _path_for(scan_id)
    record = {
        "row_id": row_id,
        "scan_id": scan_id,
        "token_address": token_address,
        "ts": time.time(),
        "payloads": {k: v for k, v in payloads.items() if v},
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # gzip in append-modus: losse leden achter elkaar vormen samen een
        # geldig gzip-bestand, dus dit kan regel voor regel.
        with gzip.open(target, "at", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return True
    except (OSError, TypeError, ValueError) as exc:
        log.warning("Ruw archief wegschrijven mislukt voor %s: %s", token_address, exc)
        return False


def read(path: Path) -> list[dict[str, Any]]:
    """Leest een archiefbestand terug. Voor latere analyses."""
    out: list[dict[str, Any]] = []
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        log.warning("Ruw archief %s onleesbaar: %s", path, exc)
    return out


def prune(now: Optional[float] = None) -> int:
    """Ruimt archieven op die ouder zijn dan de bewaartermijn."""
    directory = Path(config.RAW_ARCHIVE_DIR)
    if not directory.exists():
        return 0
    now = now if now is not None else time.time()
    cutoff = now - config.RAW_ARCHIVE_RETENTION_DAYS * 86400.0
    verwijderd = 0
    for entry in directory.glob("scan-*.jsonl.gz"):
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
                verwijderd += 1
        except OSError:
            continue
    if verwijderd:
        log.info("%d oude archiefbestanden opgeruimd", verwijderd)
    return verwijderd


def archive_size_mb() -> float:
    directory = Path(config.RAW_ARCHIVE_DIR)
    if not directory.exists():
        return 0.0
    total = sum(p.stat().st_size for p in directory.glob("scan-*.jsonl.gz"))
    return total / 1_048_576.0
