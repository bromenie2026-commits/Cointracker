"""Follow-up-job: vult 1/4/12/24u, 72u en 7d aan in dezelfde logregel."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import config
import csv_log
import data_sources
import followup
from tests.conftest import make_pair

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


def _row(hours_ago: float, **extra) -> dict:
    row = {c: "" for c in csv_log.header()}
    row["timestamp_utc"] = (NOW - timedelta(hours=hours_ago)).isoformat()
    row["token_address"] = "MINTabc"
    row["symbol"] = "TEST"
    row["price_usd"] = "0.001"
    row.update(extra)
    return row


def _mock_pairs(monkeypatch, pairs, status="ok"):
    monkeypatch.setattr(data_sources, "fetch_pairs_for_token", lambda addr: (pairs, status))


# --------------------------------------------------------------------------- #
# Welke meetmomenten zijn toe?
# --------------------------------------------------------------------------- #


def test_hele_verse_regel_is_nog_nergens_aan_toe():
    assert followup.due_intervals(_row(0.5), NOW) == []


def test_na_een_uur_is_het_eerste_meetpunt_toe():
    assert followup.due_intervals(_row(1.5), NOW) == ["1h"]


def test_na_vijf_uur_zijn_er_twee():
    assert followup.due_intervals(_row(5), NOW) == ["1h", "4h"]


def test_oude_regel_is_toe_aan_alles_behalve_de_lange():
    """Een gewone afgewezen munt wordt niet dertig dagen gevolgd."""
    assert followup.due_intervals(_row(200), NOW) == ["1h", "4h", "12h", "24h", "72h", "7d"]


def test_al_ingevuld_interval_wordt_overgeslagen():
    row = _row(200, followup_1h_at="2026-08-14T00:00:00+00:00",
               followup_4h_at="2026-08-14T00:00:00+00:00")
    assert followup.due_intervals(row, NOW) == ["12h", "24h", "72h", "7d"]


# --------------------------------------------------------------------------- #
# Lange horizon — alleen voor munten die ertoe doen (na ZCAT, 05-09)
# --------------------------------------------------------------------------- #


def test_gealerteerde_munt_wordt_dertig_dagen_gevolgd():
    row = _row(24 * 40, alerted="true")
    assert followup.due_intervals(row, NOW)[-2:] == ["14d", "30d"]


def test_grote_winnaar_wordt_ook_zonder_alert_lang_gevolgd():
    """De uitschieters zijn het hele rendement; die mogen we niet afknippen."""
    row = _row(24 * 40, alerted="false", max_gain_pct="450")
    assert "30d" in followup.due_intervals(row, NOW)


def test_gewone_verliezer_wordt_niet_lang_gevolgd():
    row = _row(24 * 40, alerted="false", max_gain_pct="-96")
    due = followup.due_intervals(row, NOW)
    assert "14d" not in due and "30d" not in due


def test_lange_horizon_pas_als_de_tijd_er_is():
    row = _row(24 * 10, alerted="true")  # 10 dagen oud
    due = followup.due_intervals(row, NOW)
    assert "7d" in due
    assert "14d" not in due


def test_drempel_is_instelbaar(monkeypatch):
    monkeypatch.setattr(config, "FOLLOWUP_LONG_MIN_GAIN_PCT", 500.0)
    row = _row(24 * 40, alerted="false", max_gain_pct="450")
    assert "30d" not in followup.due_intervals(row, NOW)


def test_gemiste_run_wordt_later_ingehaald():
    assert "1h" in followup.due_intervals(_row(120), NOW)


def test_kapotte_timestamp_geeft_geen_crash():
    row = _row(25)
    row["timestamp_utc"] = "niet-een-datum"
    assert followup.due_intervals(row, NOW) == []


# --------------------------------------------------------------------------- #
# Terugschrijven
# --------------------------------------------------------------------------- #


def test_apply_followup_schrijft_terug(monkeypatch):
    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    _mock_pairs(monkeypatch, [make_pair(price_usd=0.002, market_cap_usd=250_000)])
    row = _row(25)
    assert followup.apply_followup(row, ["24h"], NOW) is True
    assert row["price_24h"] == "0.002"
    assert row["mc_eur_24h"] == "250000.00"
    assert row["followup_24h_at"] == NOW.isoformat()
    assert row["price_72h"] == ""


def test_verdwenen_markt_wordt_als_nul_gelogd(monkeypatch):
    _mock_pairs(monkeypatch, [], status="not_found")
    row = _row(200)
    assert followup.apply_followup(row, ["24h", "72h"], NOW) is True
    assert row["price_24h"] == "0" and row["mc_eur_72h"] == "0"
    assert "naar nul" in row["followup_note"]


def test_api_fout_wordt_niet_als_totaalverlies_geboekt(monkeypatch):
    """Bugfix 4.3 — dit is het verschil tussen meten en verzinnen."""
    _mock_pairs(monkeypatch, [], status="error")
    row = _row(200)
    assert followup.apply_followup(row, ["24h"], NOW) is False
    assert row["price_24h"] == ""          # NIET op 0 gezet
    assert row["followup_24h_at"] == ""    # dus volgende run opnieuw
    assert "API-fout" in row["followup_note"]


def test_na_een_geslaagde_run_verdwijnt_de_foutnotitie(monkeypatch):
    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    _mock_pairs(monkeypatch, [], status="error")
    row = _row(200)
    followup.apply_followup(row, ["24h"], NOW)
    assert "API-fout" in row["followup_note"]

    _mock_pairs(monkeypatch, [make_pair(price_usd=0.003, market_cap_usd=100_000)])
    followup.apply_followup(row, ["24h"], NOW)
    assert row["followup_note"] == "" and row["price_24h"] == "0.003"


# --------------------------------------------------------------------------- #
# Hoogste prijs sinds het alert
# --------------------------------------------------------------------------- #


def test_max_price_wordt_bijgehouden(monkeypatch):
    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    row = _row(2)  # instapprijs 0.001

    _mock_pairs(monkeypatch, [make_pair(price_usd=0.004, market_cap_usd=100_000)])
    followup.apply_followup(row, ["1h"], NOW)
    assert row["max_price_seen"] == "0.004"
    assert row["max_gain_pct"] == "300.0"

    # Zakt terug: de piek blijft staan.
    _mock_pairs(monkeypatch, [make_pair(price_usd=0.0005, market_cap_usd=20_000)])
    followup.apply_followup(row, ["4h"], NOW)
    assert row["max_price_seen"] == "0.004"
    assert row["max_gain_pct"] == "300.0"
    assert row["price_4h"] == "0.0005"


def test_piek_en_eindstand_vertellen_een_ander_verhaal(monkeypatch):
    """De coin sloot op -50% maar stond tussendoor op +300%."""
    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    row = _row(30)
    for prijs, interval in ((0.004, "1h"), (0.002, "4h"), (0.0005, "24h")):
        _mock_pairs(monkeypatch, [make_pair(price_usd=prijs, market_cap_usd=50_000)])
        followup.apply_followup(row, [interval], NOW)
    assert float(row["max_gain_pct"]) == 300.0
    assert float(row["price_24h"]) < float(row["price_usd"])


# --------------------------------------------------------------------------- #
# De hele run
# --------------------------------------------------------------------------- #


def test_run_werkt_ook_afgewezen_coins_bij(monkeypatch):
    """Het punt van §5.1: ook afwijzingen krijgen follow-up."""
    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    _mock_pairs(monkeypatch, [make_pair(price_usd=0.002, market_cap_usd=10_000)])

    afgewezen = _row(30, alerted="false", alert_suppressed_reason="harde filter")
    gealerteerd = _row(30, alerted="true")
    csv_log.append_rows([afgewezen, gealerteerd])

    changed = followup.run(now=NOW)
    assert changed == 2

    rows = csv_log.read_rows()
    assert all(r["price_24h"] == "0.002" for r in rows)
    assert all(r["price_1h"] == "0.002" for r in rows)


def test_run_zonder_log_doet_niets():
    assert followup.run(now=NOW) == 0
