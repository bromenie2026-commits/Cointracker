"""
data_sources.py — DexScreener + Solana RPC.

Bevat geen filterlogica. Alleen ophalen en normaliseren. Alles wat misgaat
komt terug als None / lege lijst met een reden, nooit als exception.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

import config
import http_client
from models import PairData

log = logging.getLogger(__name__)

DEX = "dexscreener"
RPC = "solana-rpc"

# Adressen die geen echte holder zijn: burn-adres en de bekende
# system/incinerator accounts. Worden uit holder-concentratie gefilterd.
BURN_ADDRESSES = {
    "1nc1nerator11111111111111111111111111111111",
    "11111111111111111111111111111111",
}


# --------------------------------------------------------------------------- #
# Valuta
# --------------------------------------------------------------------------- #


def usd_to_eur(amount_usd: Optional[float]) -> Optional[float]:
    if amount_usd is None:
        return None
    rate = config.USD_PER_EUR or 1.0
    return amount_usd / rate


def eur_to_usd(amount_eur: Optional[float]) -> Optional[float]:
    if amount_eur is None:
        return None
    return amount_eur * (config.USD_PER_EUR or 1.0)


# --------------------------------------------------------------------------- #
# DexScreener
# --------------------------------------------------------------------------- #


def _f(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:  # NaN
        return None
    return out


def _i(value: Any) -> Optional[int]:
    f = _f(value)
    return int(f) if f is not None else None


def normalize_pair(raw: dict[str, Any]) -> Optional[PairData]:
    """Zet een ruwe DexScreener-pair om naar PairData."""
    if not isinstance(raw, dict):
        return None
    base = raw.get("baseToken") or {}
    address = base.get("address") or ""
    if not address:
        return None

    liq = raw.get("liquidity") or {}
    vol = raw.get("volume") or {}
    chg = raw.get("priceChange") or {}
    txns = raw.get("txns") or {}
    info = raw.get("info") or {}

    def _tx(window: str, side: str) -> Optional[int]:
        entry = txns.get(window) or {}
        return _i(entry.get(side))

    websites = []
    for w in info.get("websites") or []:
        if isinstance(w, dict) and w.get("url"):
            websites.append(str(w["url"]))
        elif isinstance(w, str):
            websites.append(w)

    socials = []
    for s in info.get("socials") or []:
        if isinstance(s, dict):
            socials.append({"type": str(s.get("type", "")), "url": str(s.get("url", ""))})

    return PairData(
        token_address=address,
        pair_address=raw.get("pairAddress") or "",
        dex_id=raw.get("dexId") or "",
        symbol=(base.get("symbol") or "").strip(),
        name=(base.get("name") or "").strip(),
        price_usd=_f(raw.get("priceUsd")),
        market_cap_usd=_f(raw.get("marketCap")),
        fdv_usd=_f(raw.get("fdv")),
        liquidity_usd=_f(liq.get("usd")),
        volume_h1_usd=_f(vol.get("h1")),
        volume_h6_usd=_f(vol.get("h6")),
        volume_h24_usd=_f(vol.get("h24")),
        price_change_h1=_f(chg.get("h1")),
        price_change_h24=_f(chg.get("h24")),
        buys_h1=_tx("h1", "buys"),
        sells_h1=_tx("h1", "sells"),
        buys_h24=_tx("h24", "buys"),
        sells_h24=_tx("h24", "sells"),
        pair_created_at_ms=_i(raw.get("pairCreatedAt")),
        url=raw.get("url") or "",
        websites=websites,
        socials=socials,
        raw=raw,
    )


def _pairs_from_payload(payload: Any) -> list[dict[str, Any]]:
    """DexScreener geeft soms {'pairs': [...]}, soms een kale lijst."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    if isinstance(payload, dict):
        for key in ("pairs", "pair"):
            value = payload.get(key)
            if isinstance(value, list):
                return [p for p in value if isinstance(p, dict)]
            if isinstance(value, dict):
                return [value]
    return []


def _dex_get(path: str, params: Optional[dict[str, Any]] = None) -> http_client.ApiResponse:
    return http_client.get_json(
        f"{config.DEXSCREENER_BASE}{path}",
        host_key=DEX,
        min_interval=config.DEXSCREENER_MIN_INTERVAL_SECONDS,
        params=params,
    )


def search_pairs(query: str) -> list[PairData]:
    """DexScreener search — levert pairs voor een zoekterm."""
    resp = _dex_get("/latest/dex/search", {"q": query})
    if not resp.ok:
        log.warning("DexScreener search '%s' faalde: %s", query, resp.error)
        return []
    out = []
    for raw in _pairs_from_payload(resp.data):
        if (raw.get("chainId") or "").lower() != config.CHAIN_ID:
            continue
        pair = normalize_pair(raw)
        if pair:
            out.append(pair)
    return out


def fetch_pairs_for_token(token_address: str) -> tuple[list[PairData], str]:
    """Pairs van één token, MET statusveld (bugfix 4.3).

    Status is "ok", "not_found" of "error". Dat onderscheid is essentieel voor
    de follow-up: "de API antwoordde niet" is iets heel anders dan "deze munt
    heeft geen markt meer". Zonder dit onderscheid boek je storingen als
    totaalverlies en vervuil je je eigen meting.
    """
    resp = _dex_get(f"/token-pairs/v1/{config.CHAIN_ID}/{token_address}")
    if not resp.ok:
        log.warning("token-pairs faalde voor %s: %s", token_address, resp.error)
        return [], "error"
    pairs = [p for p in (normalize_pair(r) for r in _pairs_from_payload(resp.data)) if p]
    # Alleen pairs waarin dít token de basis is; anders lees je de prijs van
    # de tegenpartij af.
    pairs = [p for p in pairs if p.token_address == token_address] or pairs
    if not pairs:
        return [], "not_found"
    return pairs, "ok"


def get_pairs_for_token(token_address: str) -> list[PairData]:
    """Alle pairs van één token. Zie fetch_pairs_for_token voor de status."""
    pairs, _status = fetch_pairs_for_token(token_address)
    return pairs


def get_pairs_for_tokens(token_addresses: Iterable[str]) -> dict[str, list[PairData]]:
    """Batch-lookup (max 30 adressen per call volgens DexScreener)."""
    addresses = [a for a in dict.fromkeys(token_addresses) if a]
    out: dict[str, list[PairData]] = {a: [] for a in addresses}
    for i in range(0, len(addresses), 30):
        chunk = addresses[i : i + 30]
        resp = _dex_get(f"/tokens/v1/{config.CHAIN_ID}/{','.join(chunk)}")
        if not resp.ok:
            log.warning("tokens/v1 batch faalde: %s", resp.error)
            continue
        for raw in _pairs_from_payload(resp.data):
            pair = normalize_pair(raw)
            if pair and pair.token_address in out:
                out[pair.token_address].append(pair)
    return out


def best_pair(pairs: list[PairData]) -> Optional[PairData]:
    """De pair met de meeste liquiditeit is de maatgevende markt."""
    usable = [p for p in pairs if p.liquidity_usd is not None]
    if not usable:
        return pairs[0] if pairs else None
    return max(usable, key=lambda p: p.liquidity_usd or 0.0)


def latest_token_profiles() -> list[str]:
    """Recent aangemaakte/geüpdatete tokenprofielen op Solana."""
    addresses: list[str] = []
    for path in ("/token-profiles/latest/v1", "/token-boosts/latest/v1"):
        resp = _dex_get(path)
        if not resp.ok:
            log.warning("%s faalde: %s", path, resp.error)
            continue
        payload = resp.data
        entries = payload if isinstance(payload, list) else [payload]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if (entry.get("chainId") or "").lower() != config.CHAIN_ID:
                continue
            addr = entry.get("tokenAddress")
            if addr:
                addresses.append(str(addr))
    return list(dict.fromkeys(addresses))


def discover_candidates(
    limit: Optional[int] = None, seen: Optional[set[str]] = None
) -> list[PairData]:
    """Verzamelt verse Solana-pairs uit meerdere DexScreener-ingangen.

    De publieke API heeft geen 'nieuwe pairs'-endpoint, dus we combineren de
    tokenprofielen-feed met een paar zoekopdrachten en filteren daarna op
    pair-leeftijd.

    `seen` bevat de munten die we in eerdere runs al hebben doorgemeten. Die
    gaan achteraan in de lijst: 67% van alle alerts valt op de eerste keer dat
    we een munt zien, en elke volgende keer is drie tot tien keer minder
    productief. Ze worden niet weggegooid — een munt kan later alsnog door de
    filters komen, en de veranderingen tussen twee waarnemingen zijn zelf een
    signaal — maar ze verdringen geen verse kandidaat meer.
    """
    limit = limit or config.MAX_CANDIDATES_PER_RUN
    collected: dict[str, PairData] = {}

    profile_addresses = latest_token_profiles()
    if profile_addresses:
        for address, pairs in get_pairs_for_tokens(profile_addresses).items():
            pair = best_pair(pairs)
            if pair:
                collected[address] = pair

    for query in config.DEXSCREENER_SEARCH_QUERIES:
        for pair in search_pairs(query):
            existing = collected.get(pair.token_address)
            if existing is None or (pair.liquidity_usd or 0) > (existing.liquidity_usd or 0):
                collected[pair.token_address] = pair

    fresh = [p for p in collected.values() if _is_fresh(p)]
    # Onbekende munten eerst, en binnen elke groep de nieuwste pair eerst —
    # daar zit de meeste kans, en het houdt de run kort.
    bekend = seen or set()
    prioriteer = config.PRIORITEER_NIEUWE_MUNTEN and bool(bekend)
    fresh.sort(
        key=lambda p: (
            (p.token_address in bekend) if prioriteer else False,
            -(p.pair_created_at_ms or 0),
        )
    )
    nieuw = sum(1 for p in fresh[:limit] if p.token_address not in bekend)
    log.info(
        "Kandidaten: %d gevonden, %d binnen leeftijdsvenster, %d meegenomen (%d nog niet eerder gezien)",
        len(collected),
        len(fresh),
        min(len(fresh), limit),
        nieuw,
    )
    return fresh[:limit]


def _is_fresh(pair: PairData) -> bool:
    age = pair.age_minutes
    if age is None:
        # Zonder aanmaakdatum kunnen we niet zeggen of het nieuw is.
        return False
    return config.MIN_PAIR_AGE_MINUTES <= age <= config.MAX_PAIR_AGE_HOURS * 60


# --------------------------------------------------------------------------- #
# Solana RPC
# --------------------------------------------------------------------------- #


def rpc_call(method: str, params: list[Any]) -> http_client.ApiResponse:
    resp = http_client.post_json(
        config.SOLANA_RPC_URL,
        host_key=RPC,
        min_interval=config.SOLANA_RPC_MIN_INTERVAL_SECONDS,
        json_body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        headers={"Content-Type": "application/json"},
        treat_404_as_empty=False,
    )
    if resp.ok and isinstance(resp.data, dict) and "error" in resp.data:
        err = resp.data.get("error") or {}
        return http_client.ApiResponse(
            ok=False,
            status_code=resp.status_code,
            error=f"RPC error {err.get('code')}: {str(err.get('message'))[:160]}",
            attempts=resp.attempts,
            host=RPC,
        )
    return resp


def get_mint_info(mint: str) -> dict[str, Any]:
    """Mint authority / freeze authority / supply via getAccountInfo.

    Retourneert een dict met 'available' False als het niet lukt.
    """
    resp = rpc_call(
        "getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}]
    )
    if not resp.ok:
        return {"available": False, "error": resp.error}

    value = ((resp.data or {}).get("result") or {}).get("value")
    if not value:
        return {"available": False, "error": "mint-account niet gevonden"}

    parsed = ((value.get("data") or {}).get("parsed") or {}).get("info")
    if not isinstance(parsed, dict):
        return {"available": False, "error": "mint-account niet parseerbaar"}

    mint_authority = parsed.get("mintAuthority")
    freeze_authority = parsed.get("freezeAuthority")
    return {
        "available": True,
        "owner_program": value.get("owner"),
        "mint_authority": mint_authority,
        "freeze_authority": freeze_authority,
        "mint_authority_renounced": mint_authority in (None, "", "null"),
        "freeze_authority_renounced": freeze_authority in (None, "", "null"),
        "supply": _f(parsed.get("supply")),
        "decimals": _i(parsed.get("decimals")),
        "is_initialized": parsed.get("isInitialized"),
    }


def get_token_supply(mint: str) -> Optional[float]:
    resp = rpc_call("getTokenSupply", [mint, {"commitment": "confirmed"}])
    if not resp.ok:
        return None
    value = ((resp.data or {}).get("result") or {}).get("value") or {}
    return _f(value.get("uiAmount"))


def get_top_holders(mint: str, exclude: Optional[set[str]] = None) -> dict[str, Any]:
    """Top-20 token-accounts via getTokenLargestAccounts.

    Let op: dit zijn token-ACCOUNTS, niet wallets. Voor concentratie is dat
    een goede proxy; rugcheck geeft, als die beschikbaar is, betere data.

    `exclude` bevat pool-/burn-adressen die geen echte houder zijn.
    """
    exclude = set(exclude or set()) | BURN_ADDRESSES
    supply = get_token_supply(mint)
    resp = rpc_call("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
    if not resp.ok:
        return {"available": False, "error": resp.error}

    value = ((resp.data or {}).get("result") or {}).get("value")
    if not isinstance(value, list) or supply in (None, 0):
        return {"available": False, "error": "geen holder-data of supply onbekend"}

    holders = []
    for entry in value:
        amount = _f((entry or {}).get("uiAmount"))
        if amount is None:
            continue
        address = (entry or {}).get("address")
        if address in exclude:
            continue
        holders.append({"address": address, "amount": amount, "pct": amount / supply * 100.0})
    holders.sort(key=lambda h: h["pct"], reverse=True)
    top10 = sum(h["pct"] for h in holders[:10])
    return {
        "available": True,
        "holders": holders,
        "top10_pct": top10,
        "largest_pct": holders[0]["pct"] if holders else 0.0,
        "supply": supply,
    }


def get_signatures_for_address(address: str, limit: int) -> list[dict[str, Any]]:
    resp = rpc_call("getSignaturesForAddress", [address, {"limit": max(1, min(limit, 1000))}])
    if not resp.ok:
        log.warning("getSignaturesForAddress faalde voor %s: %s", address, resp.error)
        return []
    result = (resp.data or {}).get("result")
    return result if isinstance(result, list) else []


def get_social_account_age_days(handle: str) -> dict[str, Any]:
    """Leeftijd van een X/Twitter-account in dagen.

    PUUR leeftijd — geen sentiment, geen inhoud (plan §3.3). Werkt alleen met
    een X API bearer token in X_BEARER_TOKEN. Zonder token geven we
    available=False terug en telt de check als 'unknown'.
    """
    if not handle:
        return {"available": False, "error": "geen X-handle gevonden"}
    if not config.X_BEARER_TOKEN:
        return {"available": False, "error": "X_BEARER_TOKEN niet ingesteld"}

    resp = http_client.get_json(
        f"{config.X_API_BASE}/2/users/by/username/{handle}",
        host_key="x-api",
        min_interval=config.X_MIN_INTERVAL_SECONDS,
        params={"user.fields": "created_at,public_metrics"},
        headers={"Authorization": f"Bearer {config.X_BEARER_TOKEN}"},
    )
    if not resp.ok or not isinstance(resp.data, dict):
        return {"available": False, "error": resp.error or "lege respons"}

    user = (resp.data or {}).get("data") or {}
    created_at = user.get("created_at")
    if not created_at:
        return {"available": False, "error": "account niet gevonden of geen created_at"}

    from datetime import datetime, timezone

    try:
        created = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except ValueError:
        return {"available": False, "error": f"onparseerbare datum: {created_at}"}

    age_days = (datetime.now(timezone.utc) - created).total_seconds() / 86400.0
    metrics = user.get("public_metrics") or {}
    return {
        "available": True,
        "handle": handle,
        "created_at": created_at,
        "age_days": age_days,
        "followers": metrics.get("followers_count"),
    }


def get_transaction(signature: str) -> Optional[dict[str, Any]]:
    resp = rpc_call(
        "getTransaction",
        [
            signature,
            {
                "encoding": "jsonParsed",
                "commitment": "confirmed",
                "maxSupportedTransactionVersion": 0,
            },
        ],
    )
    if not resp.ok:
        return None
    return (resp.data or {}).get("result")
