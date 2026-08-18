"""
dedup.py — cooldown per contractadres (plan §3.4).

Doel: coins die op de grens van een drempel fluctueren mogen niet elke 5
minuten opnieuw een mail triggeren. Loggen doen we altijd; mailen alleen als
het adres buiten de cooldown valt.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import config
import state_store

log = logging.getLogger(__name__)


class DedupStore:
    def __init__(self, path=None, cooldown_hours: Optional[float] = None) -> None:
        self.path = path or config.DEDUP_STATE_PATH
        self.cooldown_seconds = (
            cooldown_hours if cooldown_hours is not None else config.DEDUP_COOLDOWN_HOURS
        ) * 3600.0
        self.data: dict[str, Any] = state_store.load(self.path)

    # ------------------------------------------------------------------ #

    def last_alert_ts(self, token_address: str) -> Optional[float]:
        entry = self.data.get(token_address)
        if isinstance(entry, dict):
            ts = entry.get("last_alert_ts")
            if isinstance(ts, (int, float)):
                return float(ts)
        return None

    def should_alert(self, token_address: str, now: Optional[float] = None) -> tuple[bool, str]:
        """True als er gemaild mag worden, plus de reden bij False."""
        now = now if now is not None else time.time()
        last = self.last_alert_ts(token_address)
        if last is None:
            return True, ""
        elapsed = now - last
        if elapsed >= self.cooldown_seconds:
            return True, ""
        remaining_h = (self.cooldown_seconds - elapsed) / 3600.0
        return False, f"cooldown actief, nog {remaining_h:.1f}u (laatste alert {elapsed/3600:.1f}u geleden)"

    def record_alert(self, token_address: str, symbol: str = "", now: Optional[float] = None) -> None:
        now = now if now is not None else time.time()
        entry = self.data.get(token_address)
        count = 0
        if isinstance(entry, dict):
            count = int(entry.get("alert_count") or 0)
        self.data[token_address] = {
            "last_alert_ts": now,
            "symbol": symbol,
            "alert_count": count + 1,
        }

    def prune(self) -> None:
        self.data = state_store.prune(
            self.data, "last_alert_ts", config.DEDUP_RETENTION_DAYS * 86400.0
        )

    def save(self) -> None:
        self.prune()
        state_store.save(self.path, self.data)
