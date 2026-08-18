"""Follow-up-job: vult 24u/72u/7d aan in dezelfde logregel."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import csv_log
import data_sources
import followup
from tests.conftest import make_pair

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


def _row(hours_ago: float, **extra) -> dict:
    row = {c: "" for c in csv_log.header()}
    row["timestamp_utc"] = (NOW - timedelta(hours=hours_ago)).isoformat()
    row["token_address"] = "MINTabc"
    row["symbol"] = "TEST"
    row.update(extra)
    return row


def test_verse_regel_is_nog_niet_toe():
    assert followup.due_intervals(_row(2), NOW) == []


def test_regel_van_25_uur_is_toe_aan_24h():
    assert followup.due_intervals(_row(25), NOW) == ["24h"]


def test_oude_regel_is_toe_aan_alle_intervallen():
    assert followup.due_intervals(_row(200), NOW) == ["24h", "72h", "7d"]


def test_al_ingevuld_interval_wordt_overgeslagen():
    row = _row(200, followup_24h_at="2026-08-10T00:00:00+00:00")
    assert followup.due_intervals(row, NOW) == ["72h", "7d"]


def test_gemiste_run_wordt_later_ingehaald():
    """Een regel van 5 dagen oud krijgt 24h en 72h alsnog ingevuld."""
    assert "24h" in followup.due_intervals(_row(120), NOW)


def test_kapotte_timestamp_geeft_geen_crash():
    row = _row(25)
    row["timestamp_utc"] = "niet-een-datum"
    assert followup.due_intervals(row, NOW) == []


def test_apply_followup_schrijft_terug(monkeypatch):
    import config

    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    monkeypatch.setattr(
        data_sources,
        "get_pairs_for_token",
        lambda addr: [make_pair(price_usd=0.001, market_cap_usd=250_000)],
    )
    row = _row(25)
    assert followup.apply_followup(row, ["24h"], NOW) is True
    assert row["price_24h"] == "0.001"
    assert row["mc_eur_24h"] == "250000.00"
    assert row["followup_24h_at"] == NOW.isoformat()
    # De andere intervallen blijven leeg tot ze aan de beurt zijn.
    assert row["price_72h"] == ""


def test_verdwenen_markt_wordt_als_nul_gelogd(monkeypatch):
    monkeypatch.setattr(data_sources, "get_pairs_for_token", lambda addr: [])
    row = _row(200)
    assert followup.apply_followup(row, ["24h", "72h"], NOW) is True
    assert row["price_24h"] == "0" and row["mc_eur_72h"] == "0"
    assert "naar nul" in row["followup_note"]


def test_run_werkt_ook_afgewezen_coins_bij(monkeypatch):
    """Het punt van §5.1: ook afwijzingen krijgen follow-up."""
    import config

    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    monkeypatch.setattr(
        data_sources,
        "get_pairs_for_token",
        lambda addr: [make_pair(price_usd=0.002, market_cap_usd=10_000)],
    )
    afgewezen = _row(30, alerted="false", alert_suppressed_reason="harde filter")
    gealerteerd = _row(30, alerted="true")
    csv_log.append_rows([afgewezen, gealerteerd])

    changed = followup.run(now=NOW)
    assert changed == 2

    rows = csv_log.read_rows()
    assert all(r["price_24h"] == "0.002" for r in rows)


def test_run_zonder_log_doet_niets():
    assert followup.run() == 0
