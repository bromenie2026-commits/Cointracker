"""
deployer_reputation.py — historie van de deployer-wallet.

Vraag die we beantwoorden: heeft deze wallet eerder tokens gelanceerd, en
wat is daarvan geworden? Een wallet met vijf eerdere launches die allemaal
naar nul gingen is een sterk negatief signaal.

Aanpak:
1. Bepaal de deployer/creator (rugcheck `creator`, anders de mint-authority
   uit de transactie die de mint aanmaakte).
2. Loop een begrensd aantal transacties van die wallet door en zoek
   `initializeMint` / `initializeMint2` instructies -> eerdere mints.
3. Vraag voor die mints in één batch de huidige marketcap op bij DexScreener.
4. Tel hoeveel er onder DEPLOYER_DEAD_MC_EUR zitten (of helemaal geen markt
   meer hebben) = "naar nul gegaan".

Alles is begrensd door config (DEPLOYER_MAX_SIGNATURES / MAX_TX_FETCH) zodat
één scan-run nooit de RPC-rate-limit opblaast.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import config
import data_sources
from models import DeployerReputation

log = logging.getLogger(__name__)

TOKEN_PROGRAM_IDS = {
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",  # Token-2022
}
INIT_MINT_TYPES = {"initializeMint", "initializeMint2"}


def _iter_instructions(tx: dict[str, Any]) -> list[dict[str, Any]]:
    """Alle top-level + inner instructies van een jsonParsed transactie."""
    out: list[dict[str, Any]] = []
    message = ((tx or {}).get("transaction") or {}).get("message") or {}
    for ins in message.get("instructions") or []:
        if isinstance(ins, dict):
            out.append(ins)
    meta = (tx or {}).get("meta") or {}
    for inner in meta.get("innerInstructions") or []:
        for ins in (inner or {}).get("instructions") or []:
            if isinstance(ins, dict):
                out.append(ins)
    return out


def extract_created_mints(tx: dict[str, Any]) -> list[str]:
    """Mints die in deze transactie zijn geïnitialiseerd."""
    mints: list[str] = []
    for ins in _iter_instructions(tx):
        if ins.get("programId") not in TOKEN_PROGRAM_IDS:
            continue
        parsed = ins.get("parsed")
        if not isinstance(parsed, dict):
            continue
        if parsed.get("type") not in INIT_MINT_TYPES:
            continue
        info = parsed.get("info") or {}
        mint = info.get("mint")
        if mint:
            mints.append(str(mint))
    return mints


def find_creator_from_chain(mint: str) -> Optional[str]:
    """Zoekt de wallet die de mint aanmaakte via de oudste transactie."""
    signatures = data_sources.get_signatures_for_address(mint, limit=1000)
    if not signatures:
        return None
    # De oudste signature staat achteraan.
    oldest = signatures[-1]
    sig = oldest.get("signature")
    if not sig:
        return None
    tx = data_sources.get_transaction(str(sig))
    if not tx:
        return None
    accounts = (((tx.get("transaction") or {}).get("message") or {}).get("accountKeys")) or []
    for account in accounts:
        if isinstance(account, dict) and account.get("signer") and account.get("writable"):
            return str(account.get("pubkey"))
    for account in accounts:
        if isinstance(account, dict) and account.get("signer"):
            return str(account.get("pubkey"))
    return None


def previous_mints_of(wallet: str, exclude_mint: str) -> tuple[list[str], str]:
    """Eerdere mints van deze wallet (begrensd)."""
    signatures = data_sources.get_signatures_for_address(
        wallet, limit=config.DEPLOYER_MAX_SIGNATURES
    )
    if not signatures:
        return [], "geen transactiegeschiedenis opgehaald"

    mints: list[str] = []
    fetched = 0
    for entry in signatures:
        if fetched >= config.DEPLOYER_MAX_TX_FETCH:
            break
        sig = entry.get("signature")
        if not sig:
            continue
        if entry.get("err"):
            continue
        tx = data_sources.get_transaction(str(sig))
        fetched += 1
        if not tx:
            continue
        for mint in extract_created_mints(tx):
            if mint != exclude_mint and mint not in mints:
                mints.append(mint)

    truncated = fetched >= config.DEPLOYER_MAX_TX_FETCH and len(signatures) > fetched
    note = f"{fetched} transacties bekeken" + (" (afgekapt)" if truncated else "")
    return mints, note


def classify_mints(mints: list[str]) -> tuple[int, list[str]]:
    """Hoeveel van deze mints zijn effectief naar nul gegaan?"""
    if not mints:
        return 0, []
    dead_threshold_usd = data_sources.eur_to_usd(config.DEPLOYER_DEAD_MC_EUR) or 0.0
    pairs_by_token = data_sources.get_pairs_for_tokens(mints)

    dead: list[str] = []
    for mint in mints:
        pair = data_sources.best_pair(pairs_by_token.get(mint, []))
        if pair is None:
            dead.append(mint)  # geen markt meer = dood
            continue
        mc = pair.market_cap_usd if pair.market_cap_usd is not None else pair.fdv_usd
        if mc is None or mc < dead_threshold_usd:
            dead.append(mint)
    return len(dead), dead


def get_reputation(mint: str, known_creator: Optional[str] = None) -> DeployerReputation:
    """Publieke ingang. Geeft altijd een DeployerReputation terug."""
    wallet = known_creator or find_creator_from_chain(mint)
    if not wallet:
        return DeployerReputation(
            wallet=None, available=False, error="deployer-wallet niet vast te stellen"
        )

    mints, note = previous_mints_of(wallet, exclude_mint=mint)
    if not mints:
        # Geen eerdere deploys gevonden. Dat is een geldige uitkomst
        # (eerste launch van deze wallet), maar alleen als we de historie
        # daadwerkelijk konden lezen.
        if note.startswith("geen transactiegeschiedenis"):
            return DeployerReputation(wallet=wallet, available=False, error=note)
        return DeployerReputation(
            wallet=wallet,
            available=True,
            previous_deploys=0,
            dead_deploys=0,
            dead_ratio=0.0,
            error="",
            inspected_mints=[],
        )

    dead_count, dead_mints = classify_mints(mints)
    return DeployerReputation(
        wallet=wallet,
        available=True,
        previous_deploys=len(mints),
        dead_deploys=dead_count,
        dead_ratio=dead_count / len(mints) if mints else 0.0,
        error=note if "afgekapt" in note else "",
        inspected_mints=dead_mints[:10],
    )
