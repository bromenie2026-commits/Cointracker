"""DexScreener-normalisatie en RPC-parsing."""

from __future__ import annotations

import time

import config
import data_sources
import http_client

RAW_PAIR = {
    "chainId": "solana",
    "dexId": "raydium",
    "url": "https://dexscreener.com/solana/abc",
    "pairAddress": "PAIRabc",
    "baseToken": {"address": "MINTabc", "name": "Doge Killer", "symbol": "DOGEK"},
    "priceUsd": "0.0004213",
    "marketCap": 150000,
    "fdv": 160000,
    "liquidity": {"usd": 42000.5, "base": 1, "quote": 2},
    "volume": {"h24": 88000, "h6": 20000, "h1": 4000},
    "priceChange": {"h1": 3.4, "h24": -12.1},
    "txns": {"h24": {"buys": 900, "sells": 850}, "h1": {"buys": 40, "sells": 38}},
    "pairCreatedAt": 1_700_000_000_000,
    "info": {
        "websites": [{"url": "https://dogek.xyz"}],
        "socials": [{"type": "twitter", "url": "https://x.com/dogek"}],
    },
}


def test_normalize_pair():
    pair = data_sources.normalize_pair(RAW_PAIR)
    assert pair is not None
    assert pair.token_address == "MINTabc"
    assert pair.symbol == "DOGEK"
    assert pair.market_cap_usd == 150000
    assert pair.liquidity_usd == 42000.5
    assert pair.buys_h24 == 900 and pair.sells_h1 == 38
    assert pair.websites == ["https://dogek.xyz"]
    assert pair.twitter_handle() == "dogek"


def test_normalize_pair_zonder_adres_geeft_none():
    assert data_sources.normalize_pair({"baseToken": {}}) is None
    assert data_sources.normalize_pair("nonsens") is None


def test_normalize_pair_met_lege_velden():
    pair = data_sources.normalize_pair({"baseToken": {"address": "X"}})
    assert pair is not None
    assert pair.market_cap_usd is None and pair.liquidity_usd is None
    assert pair.age_minutes is None
    assert pair.twitter_handle() is None


def test_usd_eur_conversie(monkeypatch):
    monkeypatch.setattr(config, "USD_PER_EUR", 1.10)
    assert round(data_sources.usd_to_eur(110.0), 2) == 100.0
    assert round(data_sources.eur_to_usd(100.0), 2) == 110.0
    assert data_sources.usd_to_eur(None) is None


def test_pairs_from_payload_varianten():
    assert len(data_sources._pairs_from_payload([RAW_PAIR])) == 1
    assert len(data_sources._pairs_from_payload({"pairs": [RAW_PAIR]})) == 1
    assert len(data_sources._pairs_from_payload({"pair": RAW_PAIR})) == 1
    assert data_sources._pairs_from_payload(None) == []


def test_best_pair_kiest_meeste_liquiditeit():
    from tests.conftest import make_pair

    a = make_pair(liquidity_usd=1000.0)
    b = make_pair(liquidity_usd=50_000.0)
    assert data_sources.best_pair([a, b]) is b
    assert data_sources.best_pair([]) is None


def test_freshness_venster(monkeypatch):
    from tests.conftest import make_pair

    now_ms = int(time.time() * 1000)
    monkeypatch.setattr(config, "MIN_PAIR_AGE_MINUTES", 15.0)
    monkeypatch.setattr(config, "MAX_PAIR_AGE_HOURS", 24.0)

    te_vers = make_pair(pair_created_at_ms=now_ms - 60_000)
    goed = make_pair(pair_created_at_ms=now_ms - 3 * 3600 * 1000)
    te_oud = make_pair(pair_created_at_ms=now_ms - 48 * 3600 * 1000)
    geen_datum = make_pair(pair_created_at_ms=None)

    assert data_sources._is_fresh(te_vers) is False
    assert data_sources._is_fresh(goed) is True
    assert data_sources._is_fresh(te_oud) is False
    assert data_sources._is_fresh(geen_datum) is False


# --------------------------------------------------------------------------- #
# Kandidaten kiezen
# --------------------------------------------------------------------------- #


def _stub_discovery(monkeypatch, pairs):
    """Laat discover_candidates precies deze pairs 'vinden', zonder netwerk."""
    monkeypatch.setattr(data_sources, "latest_token_profiles", lambda: [])
    monkeypatch.setattr(data_sources, "get_pairs_for_tokens", lambda a: {})
    monkeypatch.setattr(config, "DEXSCREENER_SEARCH_QUERIES", ["X"])
    monkeypatch.setattr(data_sources, "search_pairs", lambda q: pairs)


def test_onbekende_munten_krijgen_voorrang(monkeypatch):
    """67% van de alerts valt op de eerste waarneming — die mag niet verdringen."""
    from tests.conftest import make_pair

    now_ms = int(time.time() * 1000)
    monkeypatch.setattr(config, "PRIORITEER_NIEUWE_MUNTEN", True)
    # De bekende munt heeft de nieuwste pair; zonder voorrang zou hij winnen.
    bekend = make_pair(token_address="OUD", pair_created_at_ms=now_ms - 30 * 60_000)
    onbekend = make_pair(token_address="NIEUW", pair_created_at_ms=now_ms - 5 * 3600 * 1000)
    _stub_discovery(monkeypatch, [bekend, onbekend])

    uit = data_sources.discover_candidates(limit=1, seen={"OUD"})
    assert [p.token_address for p in uit] == ["NIEUW"]

    # Bekende munten verdwijnen niet, ze staan achteraan.
    uit = data_sources.discover_candidates(limit=5, seen={"OUD"})
    assert [p.token_address for p in uit] == ["NIEUW", "OUD"]


def test_zonder_voorrang_wint_de_nieuwste_pair(monkeypatch):
    from tests.conftest import make_pair

    now_ms = int(time.time() * 1000)
    monkeypatch.setattr(config, "PRIORITEER_NIEUWE_MUNTEN", False)
    bekend = make_pair(token_address="OUD", pair_created_at_ms=now_ms - 30 * 60_000)
    onbekend = make_pair(token_address="NIEUW", pair_created_at_ms=now_ms - 5 * 3600 * 1000)
    _stub_discovery(monkeypatch, [bekend, onbekend])

    uit = data_sources.discover_candidates(limit=5, seen={"OUD"})
    assert [p.token_address for p in uit] == ["OUD", "NIEUW"]


def test_lege_geschiedenis_verandert_niets(monkeypatch):
    from tests.conftest import make_pair

    now_ms = int(time.time() * 1000)
    a = make_pair(token_address="A", pair_created_at_ms=now_ms - 30 * 60_000)
    b = make_pair(token_address="B", pair_created_at_ms=now_ms - 5 * 3600 * 1000)
    _stub_discovery(monkeypatch, [a, b])
    uit = data_sources.discover_candidates(limit=5, seen=set())
    assert [p.token_address for p in uit] == ["A", "B"]


def test_meerdere_zoektermen_worden_allemaal_bevraagd(monkeypatch):
    """De verbreding van de bron mag niet stilletjes één term gebruiken."""
    from tests.conftest import make_pair

    gevraagd = []
    now_ms = int(time.time() * 1000)
    monkeypatch.setattr(data_sources, "latest_token_profiles", lambda: [])
    monkeypatch.setattr(data_sources, "get_pairs_for_tokens", lambda a: {})
    monkeypatch.setattr(config, "DEXSCREENER_SEARCH_QUERIES", ["SOL", "pump", "meme"])

    def fake_search(q):
        gevraagd.append(q)
        return [make_pair(token_address=f"T{q}", pair_created_at_ms=now_ms - 3600_000)]

    monkeypatch.setattr(data_sources, "search_pairs", fake_search)
    uit = data_sources.discover_candidates(limit=10)
    assert gevraagd == ["SOL", "pump", "meme"]
    assert len(uit) == 3


def test_standaard_zoektermen_zijn_verbreed():
    assert len(config.DEXSCREENER_SEARCH_QUERIES) >= 8


# --------------------------------------------------------------------------- #
# RPC
# --------------------------------------------------------------------------- #


def _mock_rpc(monkeypatch, payload, ok=True, error=""):
    def fake_post(url, **kwargs):
        return http_client.ApiResponse(ok=ok, status_code=200, data=payload, error=error)

    monkeypatch.setattr(http_client, "post_json", fake_post)


def test_mint_info_renounced(monkeypatch):
    _mock_rpc(
        monkeypatch,
        {
            "result": {
                "value": {
                    "owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                    "data": {
                        "parsed": {
                            "info": {
                                "mintAuthority": None,
                                "freezeAuthority": None,
                                "supply": "1000000000",
                                "decimals": 6,
                                "isInitialized": True,
                            }
                        }
                    },
                }
            }
        },
    )
    info = data_sources.get_mint_info("MINT")
    assert info["available"] is True
    assert info["mint_authority_renounced"] is True
    assert info["freeze_authority_renounced"] is True


def test_mint_info_nog_actief(monkeypatch):
    _mock_rpc(
        monkeypatch,
        {
            "result": {
                "value": {
                    "data": {
                        "parsed": {
                            "info": {
                                "mintAuthority": "Dev1111111111111111111111111111111111111111",
                                "freezeAuthority": "Dev1111111111111111111111111111111111111111",
                                "supply": "1",
                                "decimals": 0,
                            }
                        }
                    }
                }
            }
        },
    )
    info = data_sources.get_mint_info("MINT")
    assert info["mint_authority_renounced"] is False
    assert info["freeze_authority_renounced"] is False


def test_mint_info_rpc_fout(monkeypatch):
    _mock_rpc(monkeypatch, None, ok=False, error="timeout")
    info = data_sources.get_mint_info("MINT")
    assert info["available"] is False and info["error"] == "timeout"


def test_rpc_error_object_wordt_fout(monkeypatch):
    _mock_rpc(monkeypatch, {"error": {"code": -32602, "message": "Invalid param"}})
    resp = data_sources.rpc_call("getAccountInfo", ["x"])
    assert resp.ok is False and "Invalid param" in resp.error


def test_top_holders_percentages(monkeypatch):
    calls = {"n": 0}

    def fake_post(url, **kwargs):
        calls["n"] += 1
        method = (kwargs.get("json_body") or {}).get("method")
        if method == "getTokenSupply":
            return http_client.ApiResponse(
                ok=True, data={"result": {"value": {"uiAmount": 1000.0}}}
            )
        return http_client.ApiResponse(
            ok=True,
            data={
                "result": {
                    "value": [
                        {"address": "A", "uiAmount": 200.0},
                        {"address": "B", "uiAmount": 100.0},
                        {"address": "C", "uiAmount": 50.0},
                    ]
                }
            },
        )

    monkeypatch.setattr(http_client, "post_json", fake_post)
    out = data_sources.get_top_holders("MINT")
    assert out["available"] is True
    assert round(out["top10_pct"], 1) == 35.0
    assert round(out["largest_pct"], 1) == 20.0


def test_social_age_zonder_token(monkeypatch):
    monkeypatch.setattr(config, "X_BEARER_TOKEN", "")
    out = data_sources.get_social_account_age_days("iemand")
    assert out["available"] is False and "X_BEARER_TOKEN" in out["error"]


def test_social_age_met_token(monkeypatch):
    monkeypatch.setattr(config, "X_BEARER_TOKEN", "fake")
    monkeypatch.setattr(
        http_client,
        "get_json",
        lambda url, **kw: http_client.ApiResponse(
            ok=True, data={"data": {"created_at": "2020-01-01T00:00:00.000Z"}}
        ),
    )
    out = data_sources.get_social_account_age_days("iemand")
    assert out["available"] is True and out["age_days"] > 1000
