"""
rugcheck.py — de vier harde rug-vectoren.

Primaire bron: rugcheck.xyz (/v1/tokens/{mint}/report en .../report/summary).
Fallback: Solana RPC voor mint/freeze authority als rugcheck niets levert.

Alles wat we niet betrouwbaar kunnen vaststellen blijft None. filters.py
vertaalt None naar DATA_UNAVAILABLE en dus (fail-closed) naar een afwijzing.
Dat is bewust: liever een gemiste kans dan een gemiste rug.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import config
import data_sources
import http_client
from models import RugcheckReport

log = logging.getLogger(__name__)

HOST = "rugcheck"

# Risk-namen die rugcheck teruggeeft en die wij als honeypot-indicatie lezen.
HONEYPOT_RISK_MARKERS = (
    "honeypot",
    "not transferable",
    "non-transferable",
    "transfer hook",
    "transfer fee",
    "permanent delegate",
    "cannot sell",
    "blacklist",
)

# Risk-namen die op een actieve authority wijzen (extra bevestiging).
MINT_RISK_MARKERS = ("mint authority", "mintable")
FREEZE_RISK_MARKERS = ("freeze authority", "freezable")


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if config.RUGCHECK_API_KEY:
        headers["X-API-KEY"] = config.RUGCHECK_API_KEY
    return headers


def _rc_get(path: str) -> http_client.ApiResponse:
    return http_client.get_json(
        f"{config.RUGCHECK_BASE}{path}",
        host_key=HOST,
        min_interval=config.RUGCHECK_MIN_INTERVAL_SECONDS,
        headers=_headers(),
    )


def fetch_report(mint: str) -> tuple[Optional[dict[str, Any]], str]:
    """Haalt het volledige rapport op, met de summary als terugvaloptie."""
    resp = _rc_get(f"/v1/tokens/{mint}/report")
    if resp.ok and isinstance(resp.data, dict):
        return resp.data, ""
    full_error = resp.error or "leeg rapport"

    resp = _rc_get(f"/v1/tokens/{mint}/report/summary")
    if resp.ok and isinstance(resp.data, dict):
        return resp.data, ""
    return None, f"report: {full_error}; summary: {resp.error or 'leeg'}"


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def _risk_names(payload: dict[str, Any]) -> list[str]:
    names = []
    for risk in payload.get("risks") or []:
        if isinstance(risk, dict):
            names.append(f"{risk.get('name', '')} {risk.get('description', '')}".lower())
    return names


def _any_marker(names: list[str], markers: tuple[str, ...]) -> bool:
    return any(any(m in n for m in markers) for n in names)


def _f(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        out = float(value)
        return None if out != out else out
    except (TypeError, ValueError):
        return None


def _authority_renounced(value: Any) -> Optional[bool]:
    """None/leeg/'null' = renounced. Een adres = NIET renounced."""
    if value is None:
        return True
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in {"", "null", "none", "11111111111111111111111111111111"}:
            return True
        return False
    return None


def _extract_lp(payload: dict[str, Any]) -> tuple[Optional[float], Optional[bool]]:
    """Bepaalt het vergrendelde/geburnde LP-percentage.

    rugcheck levert dit op meerdere plekken afhankelijk van endpoint en
    versie; we proberen ze op volgorde en nemen de hoogste betrouwbare waarde.
    """
    candidates: list[float] = []

    top_level = _f(payload.get("lpLockedPct"))
    if top_level is not None:
        candidates.append(top_level)

    for market in payload.get("markets") or []:
        if not isinstance(market, dict):
            continue
        lp = market.get("lp")
        if not isinstance(lp, dict):
            continue
        for key in ("lpLockedPct", "lpLockedPercentage"):
            val = _f(lp.get(key))
            if val is not None:
                candidates.append(val)
        # burned + locked apart aangeleverd
        burned = _f(lp.get("lpBurnPct"))
        if burned is not None:
            candidates.append(burned)

    if not candidates:
        return None, None

    pct = max(candidates)
    # rugcheck geeft soms een fractie (0-1) in plaats van een percentage.
    if 0.0 < pct <= 1.0 and all(c <= 1.0 for c in candidates):
        pct *= 100.0
    return pct, pct >= config.MIN_LP_LOCKED_PCT


def _extract_holders(payload: dict[str, Any]) -> tuple[Optional[float], Optional[float], Optional[int]]:
    holders = payload.get("topHolders")
    if not isinstance(holders, list) or not holders:
        return None, None, _int(payload.get("totalHolders"))

    pcts: list[float] = []
    for entry in holders:
        if not isinstance(entry, dict):
            continue
        owner = entry.get("owner") or entry.get("address")
        if owner in data_sources.BURN_ADDRESSES:
            continue
        pct = _f(entry.get("pct"))
        if pct is None:
            continue
        pcts.append(pct)

    if not pcts:
        return None, None, _int(payload.get("totalHolders"))

    pcts.sort(reverse=True)
    return sum(pcts[:10]), pcts[0], _int(payload.get("totalHolders"))


def _int(value: Any) -> Optional[int]:
    f = _f(value)
    return int(f) if f is not None else None


def parse_report(mint: str, payload: dict[str, Any]) -> RugcheckReport:
    """Zet een ruw rugcheck-rapport om in een RugcheckReport."""
    report = RugcheckReport(mint=mint, available=True, source="rugcheck", raw=payload)

    if payload.get("error"):
        report.error = str(payload["error"])[:200]

    token = payload.get("token") if isinstance(payload.get("token"), dict) else {}
    risks = payload.get("risks") if isinstance(payload.get("risks"), list) else []
    report.risks = [r for r in risks if isinstance(r, dict)]
    names = _risk_names(payload)

    # --- mint authority ---
    if "mintAuthority" in token:
        report.mint_authority_renounced = _authority_renounced(token.get("mintAuthority"))
    elif names:
        report.mint_authority_renounced = not _any_marker(names, MINT_RISK_MARKERS)

    # --- freeze authority ---
    if "freezeAuthority" in token:
        report.freeze_authority_renounced = _authority_renounced(token.get("freezeAuthority"))
    elif names:
        report.freeze_authority_renounced = not _any_marker(names, FREEZE_RISK_MARKERS)

    # --- LP lock/burn ---
    report.lp_locked_pct, report.lp_locked_or_burned = _extract_lp(payload)

    # --- honeypot ---
    # rugcheck doet zelf de verkoop-simulatie/transfer-analyse. Als we een
    # geldig rapport hebben en er staat geen honeypot-achtig risico in, dan
    # is de check geslaagd. Geen rapport => None => fail-closed.
    if payload.get("rugged") is True:
        report.honeypot_ok = False
    elif names or payload.get("score") is not None or payload.get("score_normalised") is not None:
        report.honeypot_ok = not _any_marker(names, HONEYPOT_RISK_MARKERS)

    report.rugged = payload.get("rugged") if isinstance(payload.get("rugged"), bool) else None
    report.score = _int(payload.get("score"))
    report.score_normalised = _int(payload.get("score_normalised"))
    report.top_holders_pct, report.largest_holder_pct, report.total_holders = _extract_holders(
        payload
    )
    creator = payload.get("creator") or (payload.get("tokenMeta") or {}).get("updateAuthority")
    report.creator = str(creator) if creator else None

    return report


def _apply_rpc_fallback(report: RugcheckReport) -> RugcheckReport:
    """Vult mint/freeze/holders aan via de Solana RPC als rugcheck ze mist."""
    needs_authority = (
        report.mint_authority_renounced is None or report.freeze_authority_renounced is None
    )
    needs_holders = report.top_holders_pct is None

    if not (needs_authority or needs_holders):
        return report

    if needs_authority:
        mint_info = data_sources.get_mint_info(report.mint)
        if mint_info.get("available"):
            if report.mint_authority_renounced is None:
                report.mint_authority_renounced = mint_info["mint_authority_renounced"]
            if report.freeze_authority_renounced is None:
                report.freeze_authority_renounced = mint_info["freeze_authority_renounced"]
            report.source = "mixed" if report.available else "rpc-fallback"
            report.raw.setdefault("_rpc_mint_info", mint_info)
        else:
            report.error = (report.error + " | " if report.error else "") + str(
                mint_info.get("error", "RPC mint-info onbeschikbaar")
            )[:160]

    if needs_holders:
        holders = data_sources.get_top_holders(report.mint)
        if holders.get("available"):
            report.top_holders_pct = holders["top10_pct"]
            report.largest_holder_pct = holders["largest_pct"]
            report.source = "mixed" if report.available else "rpc-fallback"
            report.raw.setdefault("_rpc_holders", {k: holders[k] for k in ("top10_pct", "largest_pct")})

    return report


def check_token(mint: str) -> RugcheckReport:
    """Publieke ingang: geeft altijd een RugcheckReport terug."""
    payload, error = fetch_report(mint)

    if payload is None:
        log.warning("rugcheck onbeschikbaar voor %s: %s", mint, error)
        report = RugcheckReport(mint=mint, available=False, source="rpc-fallback", error=error)
        return _apply_rpc_fallback(report)

    report = parse_report(mint, payload)
    return _apply_rpc_fallback(report)
