"""
state_store.py — kleine, atomaire JSON-state op schijf.

Gebruikt door dedup.py (cooldown) en door de holder-groei-heuristiek (die
observaties over meerdere runs nodig heeft). Bewust een plat JSON-bestand:
geen database, makkelijk te committen in een GitHub Action, makkelijk te
inspecteren als er iets raars gebeurt.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def load(path: Path) -> dict[str, Any]:
    try:
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("State %s onleesbaar (%s) — begin met lege state", path, exc)
        return {}


def save(path: Path, data: dict[str, Any]) -> None:
    """Atomair wegschrijven zodat een afgebroken run geen corrupte state laat."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp_name, path)
    except OSError as exc:
        log.error("Kon state niet wegschrijven naar %s: %s", path, exc)
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def prune(data: dict[str, Any], key: str, max_age_seconds: float) -> dict[str, Any]:
    """Verwijdert entries waarvan `key` ouder is dan max_age_seconds."""
    cutoff = time.time() - max_age_seconds
    out = {}
    for token, entry in data.items():
        if not isinstance(entry, dict):
            continue
        ts = entry.get(key)
        if isinstance(ts, (int, float)) and ts >= cutoff:
            out[token] = entry
    return out
