"""Deployer-wallet-historie."""

from __future__ import annotations

import config
import data_sources
import deployer_reputation as dr
from tests.conftest import make_pair

INIT_MINT_TX = {
    "transaction": {
        "message": {
            "accountKeys": [
                {"pubkey": "DeployerWallet1", "signer": True, "writable": True},
                {"pubkey": "Other", "signer": False, "writable": True},
            ],
            "instructions": [
                {
                    "programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                    "parsed": {"type": "initializeMint2", "info": {"mint": "OLDMINT1"}},
                }
            ],
        }
    },
    "meta": {"innerInstructions": []},
}


def test_extract_created_mints():
    assert dr.extract_created_mints(INIT_MINT_TX) == ["OLDMINT1"]


def test_extract_negeert_andere_programmas():
    tx = {
        "transaction": {
            "message": {
                "instructions": [
                    {"programId": "11111111111111111111111111111111",
                     "parsed": {"type": "transfer", "info": {}}}
                ]
            }
        }
    }
    assert dr.extract_created_mints(tx) == []


def test_extract_leest_ook_inner_instructions():
    tx = {
        "transaction": {"message": {"instructions": []}},
        "meta": {
            "innerInstructions": [
                {
                    "instructions": [
                        {
                            "programId": "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
                            "parsed": {"type": "initializeMint", "info": {"mint": "INNER1"}},
                        }
                    ]
                }
            ]
        },
    }
    assert dr.extract_created_mints(tx) == ["INNER1"]


def test_find_creator_from_chain(monkeypatch):
    monkeypatch.setattr(
        data_sources, "get_signatures_for_address", lambda a, limit: [{"signature": "sig1"}]
    )
    monkeypatch.setattr(data_sources, "get_transaction", lambda s: INIT_MINT_TX)
    assert dr.find_creator_from_chain("MINT") == "DeployerWallet1"


def test_classify_mints_dood_bij_geen_markt(monkeypatch):
    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    monkeypatch.setattr(data_sources, "get_pairs_for_tokens", lambda mints: {m: [] for m in mints})
    dead, lijst = dr.classify_mints(["A", "B"])
    assert dead == 2 and lijst == ["A", "B"]


def test_classify_mints_dood_bij_lage_mc(monkeypatch):
    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    monkeypatch.setattr(config, "DEPLOYER_DEAD_MC_EUR", 5_000.0)
    monkeypatch.setattr(
        data_sources,
        "get_pairs_for_tokens",
        lambda mints: {
            "LEVEND": [make_pair(market_cap_usd=200_000)],
            "DOOD": [make_pair(market_cap_usd=100)],
        },
    )
    dead, lijst = dr.classify_mints(["LEVEND", "DOOD"])
    assert dead == 1 and lijst == ["DOOD"]


def test_reputatie_serial_rugger(monkeypatch):
    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    monkeypatch.setattr(
        data_sources,
        "get_signatures_for_address",
        lambda a, limit: [{"signature": f"s{i}"} for i in range(3)],
    )
    monkeypatch.setattr(data_sources, "get_transaction", lambda s: INIT_MINT_TX)
    monkeypatch.setattr(data_sources, "get_pairs_for_tokens", lambda mints: {m: [] for m in mints})

    rep = dr.get_reputation("NEWMINT", known_creator="DeployerWallet1")
    assert rep.available is True
    assert rep.previous_deploys == 1 and rep.dead_deploys == 1
    assert rep.dead_ratio == 1.0


def test_eerste_launch_is_geldig_resultaat(monkeypatch):
    monkeypatch.setattr(
        data_sources, "get_signatures_for_address", lambda a, limit: [{"signature": "s1"}]
    )
    monkeypatch.setattr(
        data_sources,
        "get_transaction",
        lambda s: {"transaction": {"message": {"instructions": []}}},
    )
    rep = dr.get_reputation("NEWMINT", known_creator="Wallet1")
    assert rep.available is True and rep.previous_deploys == 0 and rep.dead_ratio == 0.0


def test_geen_historie_is_niet_beschikbaar(monkeypatch):
    monkeypatch.setattr(data_sources, "get_signatures_for_address", lambda a, limit: [])
    rep = dr.get_reputation("NEWMINT", known_creator="Wallet1")
    assert rep.available is False and "geen transactiegeschiedenis" in rep.error


def test_onbekende_deployer(monkeypatch):
    monkeypatch.setattr(dr, "find_creator_from_chain", lambda mint: None)
    rep = dr.get_reputation("NEWMINT")
    assert rep.available is False and rep.wallet is None


def test_tx_fetch_is_begrensd(monkeypatch):
    monkeypatch.setattr(config, "DEPLOYER_MAX_TX_FETCH", 3)
    monkeypatch.setattr(
        data_sources,
        "get_signatures_for_address",
        lambda a, limit: [{"signature": f"s{i}"} for i in range(50)],
    )
    calls = {"n": 0}

    def counting(sig):
        calls["n"] += 1
        return INIT_MINT_TX

    monkeypatch.setattr(data_sources, "get_transaction", counting)
    monkeypatch.setattr(data_sources, "get_pairs_for_tokens", lambda mints: {m: [] for m in mints})
    dr.get_reputation("NEWMINT", known_creator="W")
    assert calls["n"] == 3
