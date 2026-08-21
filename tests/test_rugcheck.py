"""rugcheck-parsing en de RPC-fallback."""

from __future__ import annotations

import config
import data_sources
import http_client
import rugcheck

FULL_REPORT = {
    "mint": "MINTabc",
    "token": {"mintAuthority": None, "freezeAuthority": None, "supply": "1000000000"},
    "creator": "CreatorWallet111",
    "score": 120,
    "score_normalised": 12,
    "rugged": False,
    "totalHolders": 950,
    "markets": [{"lp": {"lpLockedPct": 100.0}}],
    "topHolders": [
        {"owner": "A", "pct": 4.0},
        {"owner": "B", "pct": 3.0},
        {"owner": "1nc1nerator11111111111111111111111111111111", "pct": 60.0},
    ],
    "risks": [{"name": "Low amount of LP Providers", "level": "warn", "description": ""}],
}


def test_parse_gezond_rapport():
    report = rugcheck.parse_report("MINTabc", FULL_REPORT)
    assert report.available is True
    assert report.mint_authority_renounced is True
    assert report.freeze_authority_renounced is True
    assert report.lp_locked_pct == 100.0
    assert report.lp_locked_or_burned is True
    assert report.honeypot_ok is True
    assert report.creator == "CreatorWallet111"
    assert report.total_holders == 950
    # Het burn-adres telt niet mee in de concentratie.
    assert round(report.top_holders_pct, 1) == 7.0
    assert round(report.largest_holder_pct, 1) == 4.0


def test_actieve_authorities_worden_herkend():
    payload = dict(FULL_REPORT)
    payload["token"] = {"mintAuthority": "Dev111", "freezeAuthority": "Dev111"}
    report = rugcheck.parse_report("MINTabc", payload)
    assert report.mint_authority_renounced is False
    assert report.freeze_authority_renounced is False


def test_authorities_uit_risks_als_token_ontbreekt():
    payload = {
        "score": 5000,
        "risks": [
            {"name": "Mint Authority still enabled", "description": "dev kan bijdrukken"},
            {"name": "Freeze Authority still enabled", "description": ""},
        ],
    }
    report = rugcheck.parse_report("MINTabc", payload)
    assert report.mint_authority_renounced is False
    assert report.freeze_authority_renounced is False


def test_honeypot_wordt_gefaald_bij_transfer_risico():
    payload = dict(FULL_REPORT)
    payload["risks"] = [{"name": "Transfer fee", "description": "sell tax 99%"}]
    report = rugcheck.parse_report("MINTabc", payload)
    assert report.honeypot_ok is False


def test_rugged_token_faalt_honeypot():
    payload = dict(FULL_REPORT)
    payload["rugged"] = True
    report = rugcheck.parse_report("MINTabc", payload)
    assert report.honeypot_ok is False


def test_lp_fractie_wordt_percentage():
    payload = {"lpLockedPct": 0.97, "risks": []}
    report = rugcheck.parse_report("MINTabc", payload)
    assert round(report.lp_locked_pct, 1) == 97.0
    assert report.lp_locked_or_burned is True


def test_lp_onbekend_blijft_none():
    report = rugcheck.parse_report("MINTabc", {"risks": []})
    assert report.lp_locked_pct is None
    assert report.lp_locked_or_burned is None


def test_lp_onder_drempel_faalt(monkeypatch):
    monkeypatch.setattr(config, "MIN_LP_LOCKED_PCT", 90.0)
    report = rugcheck.parse_report("MINTabc", {"lpLockedPct": 40.0, "risks": []})
    assert report.lp_locked_or_burned is False


def test_check_token_valt_terug_op_rpc(monkeypatch):
    """rugcheck onbereikbaar => mint/freeze via de RPC, rest blijft None."""
    monkeypatch.setattr(
        http_client, "get_json", lambda url, **kw: http_client.ApiResponse(ok=False, error="503")
    )
    monkeypatch.setattr(
        data_sources,
        "get_mint_info",
        lambda mint: {
            "available": True,
            "mint_authority_renounced": True,
            "freeze_authority_renounced": False,
        },
    )
    monkeypatch.setattr(
        data_sources, "get_top_holders", lambda mint, exclude=None: {"available": False, "error": "nope"}
    )

    report = rugcheck.check_token("MINTabc")
    assert report.available is False
    assert report.source == "rpc-fallback"
    assert report.mint_authority_renounced is True
    assert report.freeze_authority_renounced is False
    # LP en honeypot kunnen we niet zonder rugcheck vaststellen => onbekend
    # => fail-closed in filters.py.
    assert report.lp_locked_or_burned is None
    assert report.honeypot_ok is None


def test_check_token_gebruikt_summary_als_report_faalt(monkeypatch):
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        if url.endswith("/report"):
            return http_client.ApiResponse(ok=False, error="500")
        return http_client.ApiResponse(
            ok=True, data={"lpLockedPct": 100.0, "score": 10, "risks": []}
        )

    monkeypatch.setattr(http_client, "get_json", fake_get)
    monkeypatch.setattr(
        data_sources, "get_mint_info", lambda mint: {"available": False, "error": "x"}
    )
    monkeypatch.setattr(
        data_sources, "get_top_holders", lambda mint, exclude=None: {"available": False, "error": "x"}
    )

    report = rugcheck.check_token("MINTabc")
    assert len(calls) == 2 and calls[1].endswith("/report/summary")
    assert report.available is True and report.lp_locked_or_burned is True


# --------------------------------------------------------------------------- #
# Bugfix 4.1 — de liquiditeitspool is geen houder
# --------------------------------------------------------------------------- #

# Gebaseerd op een echt rapport: de pool stond op 12,32% en de grootste
# ECHTE houder op 3,54%. Met de oude code faalde deze munt op
# "grootste holder > 12%", terwijl de verdeling juist gezond was.
POOL_REPORT = {
    "mint": "MINTpool",
    "token": {"mintAuthority": None, "freezeAuthority": None},
    "totalHolders": 3216,
    "totalMarketLiquidity": 35412.5,
    "markets": [{"pubkey": "POOL1111", "lp": {"lpLockedPct": 100.0}}],
    "knownAccounts": {
        "AMM22222": {"name": "Pump Fun AMM", "type": "AMM"},
        "LOCK3333": {"name": "Streamflow", "type": "LOCKER"},
    },
    "topHolders": [
        {"owner": "POOL1111", "pct": 12.32},
        {"owner": "AMM22222", "pct": 8.10},
        {"owner": "LOCK3333", "pct": 5.00},
        {"owner": "WalletA", "pct": 3.54},
        {"owner": "WalletB", "pct": 3.53},
        {"owner": "WalletC", "pct": 3.12},
    ],
    "risks": [],
}


def test_pool_telt_niet_als_houder():
    report = rugcheck.parse_report("MINTpool", POOL_REPORT, {"POOL1111"})
    assert round(report.largest_holder_pct, 2) == 3.54
    assert round(report.top_holders_pct, 2) == 10.19


def test_known_accounts_van_type_amm_en_locker_tellen_niet_mee():
    report = rugcheck.parse_report("MINTpool", POOL_REPORT)
    # Zonder expliciet pair-adres worden AMM en LOCKER er al uit gehaald via
    # knownAccounts, en POOL1111 via markets[].pubkey.
    assert report.largest_holder_pct is not None
    assert report.largest_holder_pct < 12.0


def test_zonder_de_fix_zou_deze_munt_zijn_afgewezen():
    """De regressietest voor de bug die 20 van de 24 beste coins kostte."""
    import filters

    report = rugcheck.parse_report("MINTpool", POOL_REPORT, {"POOL1111"})
    resultaat = filters.filter_holder_concentration(report)
    assert resultaat.outcome.value == "pass"
    assert resultaat.raw_value["largest_pct"] == 3.54


def test_collect_non_holder_addresses():
    gevonden = rugcheck.collect_non_holder_addresses(POOL_REPORT)
    assert "POOL1111" in gevonden
    assert "AMM22222" in gevonden
    assert "LOCK3333" in gevonden
    assert "WalletA" not in gevonden


def test_total_market_liquidity_wordt_gelezen():
    report = rugcheck.parse_report("MINTpool", POOL_REPORT)
    assert report.total_market_liquidity_usd == 35412.5


def test_pair_adres_bereikt_de_holderberekening(monkeypatch):
    monkeypatch.setattr(
        http_client, "get_json", lambda url, **kw: http_client.ApiResponse(ok=True, data=POOL_REPORT)
    )
    report = rugcheck.check_token("MINTpool", pair_addresses=["POOL1111"])
    assert round(report.largest_holder_pct, 2) == 3.54
