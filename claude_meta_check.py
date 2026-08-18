"""
claude_meta_check.py — optionele narratief-/story-beoordeling (plan §4).

Wat dit WEL is: een zacht signaal over de kwaliteit van naam, ticker en
social-copy. Is het narratief origineel en pakkend, of is het de zoveelste
kopie van een bestaande coin met een cijfer erachter?

Wat dit NIET is: een sentimentanalyse en geen harde filter. De laatste
sentiment-check op X blijft handmatig — daarom gaat er altijd een
X-zoeklink mee in de mail.

Standaard staat deze laag AAN. Ontbreekt ANTHROPIC_API_KEY, dan stopt de run
met een duidelijke fout (zie ensure_configured) in plaats van stil door te
draaien zonder deze laag.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

import config
import http_client
from models import NarrativeCheck, PairData

log = logging.getLogger(__name__)

HOST = "anthropic"
API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

SYSTEM_PROMPT = """Je beoordeelt het NARRATIEF van een nieuwe Solana-memecoin.

Je krijgt alleen metadata: naam, ticker, website en social links. Je hebt
GEEN koersdata en je geeft GEEN koop- of verkoopadvies.

Beoordeel op een schaal van 0-100 hoe sterk en origineel het narratief is:
- 0-25: generiek, afgeleid, duidelijk een kopie ("PEPE2", "BONKINU"),
  spelfouten, of een naam die alleen op een trend meelift.
- 26-50: begrijpelijk maar zwak; weinig eigen invalshoek.
- 51-75: duidelijk concept met een eigen hoek en consistente branding.
- 76-100: sterk, memorabel, eigen wereld; naam/ticker/socials versterken
  elkaar.

Verlaag de score bij: impersonatie van bestaande projecten of personen,
beloftes over rendement, "guaranteed"/"100x"-taal, ontbrekende of net
aangemaakte socials, of een naam die op scam-patronen lijkt.

Antwoord UITSLUITEND met JSON:
{"score": <0-100 getal>, "verdict": "<max 20 woorden>", "reasoning": "<max 60 woorden>"}"""


class ConfigError(RuntimeError):
    """Narratief-check staat aan maar is niet bruikbaar geconfigureerd."""


def ensure_configured() -> None:
    """Faalt luid als de check aanstaat zonder API-key."""
    if not config.CLAUDE_META_CHECK_ENABLED:
        return
    if not config.ANTHROPIC_API_KEY:
        raise ConfigError(
            "CLAUDE_META_CHECK_ENABLED staat aan maar ANTHROPIC_API_KEY ontbreekt. "
            "Zet de secret ANTHROPIC_API_KEY, of zet CLAUDE_META_CHECK_ENABLED=false "
            "als je zonder de narratief-check wilt draaien."
        )


def build_prompt(pair: Optional[PairData], token_address: str) -> str:
    lines = [f"Contractadres: {token_address}"]
    if pair:
        lines.append(f"Naam: {pair.name or '(onbekend)'}")
        lines.append(f"Ticker: {pair.symbol or '(onbekend)'}")
        lines.append(f"DEX: {pair.dex_id or '(onbekend)'}")
        if pair.websites:
            lines.append("Websites: " + ", ".join(pair.websites[:5]))
        if pair.socials:
            socials = ", ".join(
                f"{s.get('type', '?')}: {s.get('url', '')}" for s in pair.socials[:5]
            )
            lines.append("Socials: " + socials)
        else:
            lines.append("Socials: (geen gekoppeld)")
        age = pair.age_minutes
        if age is not None:
            lines.append(f"Pair-leeftijd: {age/60:.1f} uur")
    return "\n".join(lines)


def _extract_json(text: str) -> Optional[dict[str, Any]]:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def check(pair: Optional[PairData], token_address: str) -> NarrativeCheck:
    """Geeft altijd een NarrativeCheck terug; nooit een exception."""
    if not config.CLAUDE_META_CHECK_ENABLED:
        return NarrativeCheck(available=False, error="uitgezet in config")
    if not config.ANTHROPIC_API_KEY:
        return NarrativeCheck(available=False, error="ANTHROPIC_API_KEY ontbreekt")

    resp = http_client.post_json(
        API_URL,
        host_key=HOST,
        min_interval=0.3,
        timeout=config.CLAUDE_TIMEOUT_SECONDS,
        headers={
            "x-api-key": config.ANTHROPIC_API_KEY,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        },
        json_body={
            "model": config.CLAUDE_MODEL,
            "max_tokens": config.CLAUDE_MAX_TOKENS,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": build_prompt(pair, token_address)}],
        },
        treat_404_as_empty=False,
    )

    if not resp.ok or not isinstance(resp.data, dict):
        return NarrativeCheck(available=False, error=resp.error or "lege respons")

    blocks = resp.data.get("content") or []
    text = ""
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            text += block.get("text", "")

    payload = _extract_json(text)
    if not payload:
        return NarrativeCheck(available=False, error=f"onparseerbaar antwoord: {text[:120]}")

    try:
        score = float(payload.get("score"))
    except (TypeError, ValueError):
        return NarrativeCheck(available=False, error="geen numerieke score in antwoord")

    return NarrativeCheck(
        available=True,
        score=max(0.0, min(100.0, score)),
        verdict=str(payload.get("verdict", ""))[:200],
        reasoning=str(payload.get("reasoning", ""))[:500],
    )
