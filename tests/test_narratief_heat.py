"""Tests voor narratief_heat.py. Geen netwerk: Google Trends is nep."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

import config
import csv_log
import narratief_heat as nh


# --------------------------------------------------------------------------- #
# Kernwoord
# --------------------------------------------------------------------------- #


def test_kernwoord_pakt_meest_kenmerkende_woord():
    assert nh.kernwoord({"symbol": "PENGU", "naam": "Pudgy Penguin"}) == "penguin"
    assert nh.kernwoord({"symbol": "CAT", "naam": "Doge Coin"}) is None   # te kort / crypto-ruis
    assert nh.kernwoord({"symbol": "WNV", "naam": "West Nile Virus"}) == "virus"


# --------------------------------------------------------------------------- #
# Heat en stijging
# --------------------------------------------------------------------------- #


def test_heat_en_stijging_ziet_opkomst():
    lanceer = date(2026, 9, 10)
    reeks = {lanceer - timedelta(days=k): v for k, v in
             {7: 10, 6: 10, 5: 12, 4: 15, 3: 20, 2: 40, 1: 60, 0: 80}.items()}
    heat, stijging = nh.heat_en_stijging(reeks, lanceer)
    assert heat == 80
    assert stijging > 50            # 80 vs gemiddelde ~24 ervoor


def test_heat_en_stijging_pakt_dichtstbijzijnde_eerdere_dag():
    lanceer = date(2026, 9, 10)
    reeks = {date(2026, 9, 8): 33}   # geen waarde op de lanceerdag zelf
    heat, _ = nh.heat_en_stijging(reeks, lanceer)
    assert heat == 33


def test_heat_zonder_data_is_none():
    assert nh.heat_en_stijging({}, date(2026, 9, 10)) is None


# --------------------------------------------------------------------------- #
# Verzamelen + analyse
# --------------------------------------------------------------------------- #


@pytest.fixture
def paden(tmp_path, monkeypatch):
    monkeypatch.setattr(nh, "UITVOER", tmp_path / "heat.csv")
    monkeypatch.setattr(nh, "RAPPORT", tmp_path / "heat.txt")
    monkeypatch.setattr(nh, "INTERVAL", 0)
    monkeypatch.setattr(nh.time, "sleep", lambda _s: None)
    return tmp_path


def _munt(adres, sym, naam, piek, dag="2026-09-11"):
    return {"token_address": adres, "symbol": sym, "name": naam,
            "timestamp_utc": f"{dag}T10:00:00+00:00",
            "pair_created_at_utc": f"{dag}T09:00:00+00:00",
            "max_gain_pct": str(piek), "hard_pass": "true", "alerted": "true"}


def test_verzamelen_schrijft_en_is_hervatbaar(paden, monkeypatch):
    csv_log.append_rows([
        _munt("A", "PENGU", "Pudgy Penguin", 1500),
        _munt("B", "RAND", "Randomcoin", -90),
    ])
    calls = []

    def nep_reeks(term, van, tot):
        calls.append(term)
        # PENGU heet en stijgend; de rest koud
        if term == "penguin":
            return {tot - timedelta(days=k): v for k, v in
                    {2: 5, 1: 5, 0: 90}.items()}
        return {tot: 3, tot - timedelta(days=1): 3}

    monkeypatch.setattr(nh, "interesse_reeks", nep_reeks)
    n = nh.verzamel()
    assert n == 2
    assert set(calls) == {"penguin", "randomcoin"}

    # Tweede keer: alles al gedaan, niets nieuws opgehaald.
    calls.clear()
    assert nh.verzamel() == 0
    assert calls == []


def test_mislukt_ophalen_stopt_en_bewaart(paden, monkeypatch):
    csv_log.append_rows([_munt(f"M{i}", f"S{i}", f"Naam{i}woord", 100) for i in range(10)])
    monkeypatch.setattr(nh, "MAX_FOUT_OP_RIJ", 3)
    monkeypatch.setattr(nh, "interesse_reeks", lambda *a, **k: None)   # altijd mislukt
    nh.verzamel()
    # Na 3 fouten op rij gestopt: geen enkele 'ok'-rij weggeschreven, run kan later verder.
    assert nh.UITVOER.exists() is False or nh._gedaan() == set()


def test_analyse_meldt_signaal_of_niet(paden, monkeypatch):
    csv_log.append_rows([
        _munt("A", "PENGU", "Pudgy Penguin", 1500),
        _munt("B", "COLD", "Coldnaam iets", -90),
    ])

    def nep_reeks(term, van, tot):
        if term == "penguin":
            return {tot - timedelta(days=k): v for k, v in {2: 5, 1: 5, 0: 90}.items()}
        return {tot: 2, tot - timedelta(days=1): 2}

    monkeypatch.setattr(nh, "interesse_reeks", nep_reeks)
    tekst = nh.run()
    assert "NARRATIEF-HEAT" in tekst
    assert "PENGU" in tekst
    assert "1 heet" in tekst          # precies één munt boven de drempel


def test_leest_alleen():
    bron = (config.BASE_DIR / "narratief_heat.py").read_text(encoding="utf-8")
    for verboden in ("sendTransaction", "Keypair", "private_key", "swap("):
        assert verboden not in bron
