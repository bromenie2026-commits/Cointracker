"""Dedup/cooldown en de CSV-log."""

from __future__ import annotations

import time

import config
import csv_log
import filters
from dedup import DedupStore
from tests.conftest import make_deployer, make_narrative, make_pair, make_report, make_social


# --------------------------------------------------------------------------- #
# Dedup
# --------------------------------------------------------------------------- #


def test_eerste_alert_mag_altijd():
    store = DedupStore()
    ok, reason = store.should_alert("MINT1")
    assert ok is True and reason == ""


def test_tweede_alert_binnen_cooldown_wordt_geblokkeerd():
    store = DedupStore(cooldown_hours=6.0)
    now = time.time()
    store.record_alert("MINT1", "TEST", now=now)
    ok, reason = store.should_alert("MINT1", now=now + 3600)
    assert ok is False and "cooldown" in reason


def test_na_cooldown_mag_weer():
    store = DedupStore(cooldown_hours=6.0)
    now = time.time()
    store.record_alert("MINT1", "TEST", now=now)
    ok, _ = store.should_alert("MINT1", now=now + 7 * 3600)
    assert ok is True


def test_alert_count_loopt_op():
    store = DedupStore()
    store.record_alert("MINT1")
    store.record_alert("MINT1")
    assert store.data["MINT1"]["alert_count"] == 2


def test_state_overleeft_herstart():
    store = DedupStore(cooldown_hours=6.0)
    store.record_alert("MINT1", "TEST")
    store.save()
    opnieuw = DedupStore(cooldown_hours=6.0)
    ok, _ = opnieuw.should_alert("MINT1")
    assert ok is False


def test_oude_entries_worden_opgeruimd(monkeypatch):
    monkeypatch.setattr(config, "DEDUP_RETENTION_DAYS", 1.0)
    store = DedupStore()
    store.data["OUD"] = {"last_alert_ts": time.time() - 5 * 86400}
    store.record_alert("NIEUW")
    store.save()
    assert "OUD" not in DedupStore().data
    assert "NIEUW" in DedupStore().data


def test_corrupte_state_breekt_niets():
    config.DEDUP_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.DEDUP_STATE_PATH.write_text("{niet_json", encoding="utf-8")
    store = DedupStore()
    assert store.data == {}
    ok, _ = store.should_alert("MINT1")
    assert ok is True


# --------------------------------------------------------------------------- #
# CSV-log
# --------------------------------------------------------------------------- #


def _evaluation(monkeypatch):
    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    return filters.evaluate(
        token_address="MINTabc",
        pair=make_pair(),
        report=make_report(),
        deployer=make_deployer(),
        social=make_social(),
        narrative=make_narrative(),
        holder_history={},
    )


def test_header_bevat_elke_filter_twee_keer():
    cols = csv_log.header()
    for name in filters.FILTER_NAMES:
        assert f"{name}__outcome" in cols
        assert f"{name}__raw" in cols
    assert "price_24h" in cols and "price_7d" in cols
    assert len(cols) == len(set(cols)), "dubbele kolomnamen"


def test_row_bevat_ruwe_waardes_niet_alleen_passfail(monkeypatch):
    evaluation = _evaluation(monkeypatch)
    row = csv_log.build_row(evaluation, scan_id="abc123")
    # Dit is het hele punt van §5: de daadwerkelijke bot-score staat erin.
    assert row["vol_mc_ratio__raw"] not in ("", None)
    assert float(row["vol_mc_ratio__raw"]) >= 0
    assert row["marketcap_eur__raw"] == "120000.0"
    assert row["holder_concentration__raw"].startswith("{")
    assert row["hard_pass"] == "true"
    assert row["scan_id"] == "abc123"


def test_afgewezen_coin_wordt_ook_gelogd(monkeypatch):
    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    evaluation = filters.evaluate(
        token_address="MINTbad",
        pair=make_pair(),
        report=make_report(mint_authority_renounced=False),
        deployer=make_deployer(),
        social=make_social(),
        narrative=make_narrative(),
        holder_history={},
    )
    evaluation.alert_suppressed_reason = "harde filter"
    row = csv_log.build_row(evaluation, "scan1")
    csv_log.append_rows([row])
    rows = csv_log.read_rows()
    assert len(rows) == 1
    assert rows[0]["alerted"] == "false"
    assert rows[0]["mint_authority_renounced__outcome"] == "fail"
    # ...maar de ruwe marktwaardes staan er nog steeds in, voor tuning.
    assert rows[0]["marketcap_eur__raw"] == "120000.0"


def test_data_unavailable_wordt_apart_gelabeld(monkeypatch):
    from models import RugcheckReport

    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    evaluation = filters.evaluate(
        token_address="MINTx",
        pair=make_pair(),
        report=RugcheckReport(mint="MINTx", available=False, error="rugcheck timeout"),
        deployer=None,
        social=None,
        narrative=None,
        holder_history={},
    )
    row = csv_log.build_row(evaluation, "scan1")
    assert row["mint_authority_renounced__outcome"] == "data_unavailable"
    assert "rugcheck timeout" in row["data_unavailable_filters"]
    # Een echte FAIL is iets anders dan ontbrekende data:
    assert "mint_authority_renounced=FAIL" not in row["blocking_reasons"]
    assert "DATA_UNAVAILABLE" in row["blocking_reasons"]


def test_append_en_lezen_roundtrip(monkeypatch):
    evaluation = _evaluation(monkeypatch)
    csv_log.append_rows([csv_log.build_row(evaluation, "s1")])
    csv_log.append_rows([csv_log.build_row(evaluation, "s2")])
    rows = csv_log.read_rows()
    assert len(rows) == 2
    assert {r["scan_id"] for r in rows} == {"s1", "s2"}


def test_rewrite_migreert_oud_schema(monkeypatch):
    """Een oud logbestand zonder de follow-up-kolommen blijft bruikbaar."""
    path = config.SCAN_LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "timestamp_utc,token_address,symbol\n2026-01-01T00:00:00+00:00,MINT1,OLD\n",
        encoding="utf-8",
    )
    rows = csv_log.read_rows()
    rows[0]["price_24h"] = "0.5"
    csv_log.rewrite_rows(rows)
    opnieuw = csv_log.read_rows()
    assert opnieuw[0]["symbol"] == "OLD"
    assert opnieuw[0]["price_24h"] == "0.5"
    assert "vol_mc_ratio__raw" in opnieuw[0]


def test_oud_logbestand_krijgt_de_nieuwe_kolommen(monkeypatch):
    """Regressietest: nieuwe signalen moeten in een bestaand logboek belanden.

    Zonder migratie gebruikt append_rows de oude kopregel en verdwijnen de
    nieuwe kolommen geruisloos — dan meet je wel, maar log je niets.
    """
    path = config.SCAN_LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "timestamp_utc,token_address,symbol,bot_score__raw\n"
        "2026-08-18T13:32:10+00:00,MINToud,OUD,37\n",
        encoding="utf-8",
    )

    evaluation = _evaluation(monkeypatch)
    csv_log.append_rows([csv_log.build_row(evaluation, "nieuw")])

    rows = csv_log.read_rows()
    assert len(rows) == 2
    # De oude regel blijft intact, inclusief zijn oude kolom.
    assert rows[0]["symbol"] == "OUD" and rows[0]["bot_score__raw"] == "37"
    # En de nieuwe regel heeft de nieuwe signalen echt gevuld.
    assert rows[1]["vol_mc_ratio__raw"] not in ("", None)
    assert rows[1]["avg_trade_eur__raw"] not in ("", None)
    assert rows[1]["tx_per_min__raw"] not in ("", None)
