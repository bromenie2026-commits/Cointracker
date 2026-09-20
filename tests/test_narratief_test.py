"""Tests voor narratief_test.py. Geen netwerk: Wikipedia is nep."""

from __future__ import annotations

from datetime import date

import pytest

import config
import csv_log
import http_client
import narratief_test as nt


def test_woorden_filtert_ruis():
    assert nt.woorden("West_Nile_virus") == {"west", "nile", "virus"}
    assert nt.woorden("Deaths_in_2026") == set()                # altijd hoog, betekent niets
    assert nt.woorden("Superman_(2025_film)") == {"superman"}  # "(film)" eruit
    assert nt.woorden("WNV") == set()                           # te kort
    assert nt.woorden("Doge Coin Inu") == set()                 # crypto-ruis


def test_match_op_lanceerdag_en_dag_ervoor():
    trends = {
        date(2026, 9, 10): [("West_Nile_virus", 12, "nl.wikipedia")],
        date(2026, 9, 11): [("Charlie_Kirk", 3, "en.wikipedia")],
    }
    munt = {"symbol": "WNV", "naam": "West Nile Virus", "lanceerdag": date(2026, 9, 11)}
    m = nt.match(munt, trends)
    assert m is not None and m[1] == "West_Nile_virus" and m[2] == 12


def test_te_oud_nieuws_telt_niet():
    trends = {date(2026, 9, 1): [("West_Nile_virus", 12, "nl.wikipedia")]}
    munt = {"symbol": "WNV", "naam": "West Nile Virus", "lanceerdag": date(2026, 9, 11)}
    assert nt.match(munt, trends) is None


def test_strenge_variant_negeert_lage_rang():
    trends = {date(2026, 9, 11): [("Capybara", 850, "en.wikipedia")]}
    munt = {"symbol": "CAPY", "naam": "Capybara", "lanceerdag": date(2026, 9, 11)}
    assert nt.match(munt, trends, 1000) is not None
    assert nt.match(munt, trends, 200) is None


def test_wikipedia_antwoord_wordt_gelezen(monkeypatch):
    payload = {"items": [{"articles": [
        {"article": "Main_Page", "rank": 1},
        {"article": "Special:Search", "rank": 2},
        {"article": "West_Nile_virus", "rank": 3},
    ]}]}
    monkeypatch.setattr(http_client, "get_json",
                        lambda url, **k: http_client.ApiResponse(ok=True, status_code=200, data=payload))
    assert nt.top_artikelen("nl.wikipedia", date(2026, 9, 10)) == [("West_Nile_virus", 3)]


def test_wikipedia_fout_geeft_lege_lijst(monkeypatch):
    monkeypatch.setattr(http_client, "get_json",
                        lambda url, **k: http_client.ApiResponse(ok=False, status_code=500, error="stuk"))
    assert nt.top_artikelen("en.wikipedia", date(2026, 9, 10)) == []


@pytest.fixture
def paden(tmp_path, monkeypatch):
    monkeypatch.setattr(nt, "RAPPORT", tmp_path / "r.txt")
    monkeypatch.setattr(nt, "MATCHES", tmp_path / "m.csv")
    return tmp_path


def test_hele_run(paden, monkeypatch):
    rows = [
        {"token_address": "A", "symbol": "NILE", "name": "West Nile", "timestamp_utc": "2026-09-11T10:00:00+00:00",
         "pair_created_at_utc": "2026-09-11T09:00:00+00:00", "max_gain_pct": "1200", "hard_pass": "true", "alerted": "true"},
        {"token_address": "B", "symbol": "RAND", "name": "Randomcoin", "timestamp_utc": "2026-09-11T10:00:00+00:00",
         "pair_created_at_utc": "2026-09-11T09:00:00+00:00", "max_gain_pct": "-90", "hard_pass": "true", "alerted": "false"},
    ]
    csv_log.append_rows(rows)
    monkeypatch.setattr(nt, "haal_trends", lambda van, tot: {date(2026, 9, 11): [("West_Nile_virus", 5, "nl.wikipedia")]})
    tekst = nt.run()
    assert "1 munten matchen" in tekst
    assert "NILE" in tekst
    assert nt.MATCHES.read_text(encoding="utf-8").count("\n") == 2  # kop + 1 match


def test_leest_alleen():
    bron = (config.BASE_DIR / "narratief_test.py").read_text(encoding="utf-8")
    assert "sendTransaction" not in bron and "Keypair" not in bron
