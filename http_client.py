"""
http_client.py — gedeelde HTTP-laag met exponential backoff, retry en
per-host throttling.

Ontwerpregel: deze module gooit NOOIT een exception naar boven. Hij geeft
altijd een ApiResponse terug met `ok` en `error`. De filters beslissen zelf
wat ze met ontbrekende data doen (fail-closed), zodat een API-storing nooit
de hele run laat crashen.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

import config

log = logging.getLogger(__name__)

#: Teller van rate-limit-hits per host. main.py logt dit aan het eind van een
#: run, zodat je kunt zien of de scanfrequentie omlaag moet (plan §7).
RATE_LIMIT_HITS: dict[str, int] = {}
REQUEST_COUNTS: dict[str, int] = {}

_lock = threading.Lock()
_last_request_at: dict[str, float] = {}
_session: Optional[requests.Session] = None


@dataclass
class ApiResponse:
    ok: bool
    status_code: Optional[int] = None
    data: Any = None
    error: str = ""
    attempts: int = 0
    host: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def json_or(self, default: Any) -> Any:
        return self.data if self.ok and self.data is not None else default


def reset_counters() -> None:
    RATE_LIMIT_HITS.clear()
    REQUEST_COUNTS.clear()


def get_session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update(
            {
                "User-Agent": config.HTTP_USER_AGENT,
                "Accept": "application/json",
            }
        )
        _session = s
    return _session


def _throttle(host: str, min_interval: float) -> None:
    """Simpele per-host throttle zodat we onder de rate limit blijven."""
    if min_interval <= 0:
        return
    with _lock:
        now = time.monotonic()
        last = _last_request_at.get(host)
        if last is not None:
            wait = min_interval - (now - last)
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
        _last_request_at[host] = now


def _backoff_seconds(attempt: int, retry_after: Optional[float] = None) -> float:
    if retry_after is not None and retry_after > 0:
        return min(retry_after, config.HTTP_BACKOFF_MAX_SECONDS)
    base = config.HTTP_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
    jitter = random.uniform(0, config.HTTP_BACKOFF_BASE_SECONDS * 0.5)
    return min(base + jitter, config.HTTP_BACKOFF_MAX_SECONDS)


def request_json(
    method: str,
    url: str,
    *,
    host_key: str,
    min_interval: float = 0.0,
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
    timeout: Optional[float] = None,
    max_attempts: Optional[int] = None,
    treat_404_as_empty: bool = True,
) -> ApiResponse:
    """Doet een HTTP-call met retry/backoff en geeft altijd een ApiResponse.

    `host_key` groepeert throttling en rate-limit-tellers per databron
    ("dexscreener", "rugcheck", "solana-rpc").
    """
    attempts_allowed = max_attempts or config.HTTP_MAX_ATTEMPTS
    timeout_s = timeout or config.HTTP_TIMEOUT_SECONDS
    session = get_session()
    last_error = "onbekende fout"
    status: Optional[int] = None

    for attempt in range(1, attempts_allowed + 1):
        _throttle(host_key, min_interval)
        REQUEST_COUNTS[host_key] = REQUEST_COUNTS.get(host_key, 0) + 1
        try:
            resp = session.request(
                method.upper(),
                url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=timeout_s,
            )
            status = resp.status_code

            if status == 429 or status in (502, 503, 504):
                if status == 429:
                    RATE_LIMIT_HITS[host_key] = RATE_LIMIT_HITS.get(host_key, 0) + 1
                    log.warning("Rate limit (%s) op %s", status, host_key)
                retry_after = None
                raw_ra = resp.headers.get("Retry-After")
                if raw_ra:
                    try:
                        retry_after = float(raw_ra)
                    except ValueError:
                        retry_after = None
                last_error = f"HTTP {status}"
                if attempt < attempts_allowed:
                    time.sleep(_backoff_seconds(attempt, retry_after))
                    continue
                return ApiResponse(
                    ok=False,
                    status_code=status,
                    error=last_error,
                    attempts=attempt,
                    host=host_key,
                )

            if status == 404 and treat_404_as_empty:
                return ApiResponse(
                    ok=True,
                    status_code=status,
                    data=None,
                    attempts=attempt,
                    host=host_key,
                    extra={"not_found": True},
                )

            if status >= 400:
                # 4xx (behalve 429/404) is niet retry-waardig.
                return ApiResponse(
                    ok=False,
                    status_code=status,
                    error=f"HTTP {status}: {resp.text[:200]}",
                    attempts=attempt,
                    host=host_key,
                )

            try:
                payload = resp.json()
            except ValueError:
                last_error = "ongeldige JSON in respons"
                if attempt < attempts_allowed:
                    time.sleep(_backoff_seconds(attempt))
                    continue
                return ApiResponse(
                    ok=False,
                    status_code=status,
                    error=last_error,
                    attempts=attempt,
                    host=host_key,
                )

            return ApiResponse(
                ok=True,
                status_code=status,
                data=payload,
                attempts=attempt,
                host=host_key,
            )

        except requests.Timeout:
            last_error = f"timeout na {timeout_s}s"
        except requests.RequestException as exc:  # netwerk/DNS/TLS
            last_error = f"netwerkfout: {type(exc).__name__}: {exc}"[:200]

        if attempt < attempts_allowed:
            time.sleep(_backoff_seconds(attempt))

    return ApiResponse(
        ok=False,
        status_code=status,
        error=last_error,
        attempts=attempts_allowed,
        host=host_key,
    )


def get_json(url: str, **kwargs: Any) -> ApiResponse:
    return request_json("GET", url, **kwargs)


def post_json(url: str, **kwargs: Any) -> ApiResponse:
    return request_json("POST", url, **kwargs)


def rate_limit_summary() -> str:
    if not REQUEST_COUNTS:
        return "geen HTTP-calls gedaan"
    parts = []
    for host, count in sorted(REQUEST_COUNTS.items()):
        hits = RATE_LIMIT_HITS.get(host, 0)
        parts.append(f"{host}: {count} calls, {hits}x rate-limited")
    return " | ".join(parts)
