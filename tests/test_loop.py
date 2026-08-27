"""Tests voor de doorlopende lus.

De lus mag nooit echt slapen of echt netwerken in een test, dus tijd en
slaap worden erin geïnjecteerd en de drie taken worden vervangen door
tellers.
"""

from __future__ import annotations

import pytest

import config
import loop


class Klok:
    """Een tijdbron die alleen vooruit gaat als er 'geslapen' wordt."""

    def __init__(self, start: float = 1_700_000_000.0):
        self.nu = start
        self.geslapen: list[float] = []

    def tijd(self) -> float:
        return self.nu

    def slaap(self, seconden: float) -> None:
        self.geslapen.append(seconden)
        self.nu += seconden


@pytest.fixture
def taken(monkeypatch):
    """Vervangt de drie echte taken door tellers."""
    tel = {"volglijst": 0, "scan": 0, "followup": 0}

    def maak(naam):
        def uitvoeren(dry_run):
            tel[naam] += 1
            return True

        return uitvoeren

    monkeypatch.setattr(loop, "taak_volglijst", maak("volglijst"))
    monkeypatch.setattr(loop, "taak_scan", maak("scan"))
    monkeypatch.setattr(loop, "taak_followup", maak("followup"))
    monkeypatch.setattr(loop, "commit_and_push", lambda bericht, mislukt=0: (True, 0))
    monkeypatch.setattr(loop, "pull_first", lambda: None)
    return tel


# --------------------------------------------------------------------------- #
# Ritme
# --------------------------------------------------------------------------- #


def test_elke_taak_draait_meteen_de_eerste_ronde(taken):
    k = Klok()
    loop.run(duur_minuten=5, tijdbron=k.tijd, slaap=k.slaap)
    assert taken == {"volglijst": 1, "scan": 1, "followup": 1}


def test_ritme_over_een_uur(taken, monkeypatch):
    """In 60 minuten: 6x volglijst, 3x scan, 1x follow-up."""
    monkeypatch.setattr(config, "LOOP_WATCHLIST_MINUTES", 10.0)
    monkeypatch.setattr(config, "LOOP_SCAN_MINUTES", 20.0)
    monkeypatch.setattr(config, "LOOP_FOLLOWUP_MINUTES", 60.0)
    k = Klok()
    loop.run(duur_minuten=60, tijdbron=k.tijd, slaap=k.slaap)
    assert taken["volglijst"] == 6
    assert taken["scan"] == 3
    assert taken["followup"] == 1


def test_volglijst_draait_veel_vaker_dan_de_scan(taken):
    k = Klok()
    loop.run(duur_minuten=120, tijdbron=k.tijd, slaap=k.slaap)
    assert taken["volglijst"] > taken["scan"] > taken["followup"]


def test_lus_stopt_op_tijd(taken):
    k = Klok(start=0.0)
    loop.run(duur_minuten=45, tijdbron=k.tijd, slaap=k.slaap)
    assert k.nu <= 45 * 60


def test_kapotte_scan_stopt_de_lus_niet(monkeypatch):
    """Een scan die ontploft mag de volglijst niet meeslepen.

    Dit is het scenario van 26-08: de scan viel om en daarna gebeurde er
    achttien uur lang niets meer. In de lus mag dat niet kunnen.
    """
    import main as scan_module

    def kapot(**kwargs):
        raise RuntimeError("API plat")

    monkeypatch.setattr(scan_module, "run", kapot)
    assert loop.taak_scan(dry_run=True) is False  # gevangen, niet doorgegooid

    tel = {"volglijst": 0}
    monkeypatch.setattr(loop, "taak_volglijst", lambda d: tel.__setitem__("volglijst", tel["volglijst"] + 1) or True)
    monkeypatch.setattr(loop, "taak_followup", lambda d: True)
    monkeypatch.setattr(loop, "commit_and_push", lambda b, m=0: (True, 0))
    monkeypatch.setattr(loop, "pull_first", lambda: None)

    k = Klok()
    loop.run(duur_minuten=45, tijdbron=k.tijd, slaap=k.slaap)
    assert tel["volglijst"] >= 4  # gewoon doorgemeten ondanks de kapotte scan


def test_veilig_vangt_fouten_af():
    def stuk():
        raise ValueError("kapot")

    assert loop._veilig("stukke taak", stuk) is False
    assert loop._veilig("goede taak", lambda: 3) is True


# --------------------------------------------------------------------------- #
# Wegschrijven
# --------------------------------------------------------------------------- #


def test_dry_run_schrijft_niets_weg(monkeypatch):
    geschreven = []
    monkeypatch.setattr(loop, "taak_volglijst", lambda d: True)
    monkeypatch.setattr(loop, "taak_scan", lambda d: True)
    monkeypatch.setattr(loop, "taak_followup", lambda d: True)
    monkeypatch.setattr(loop, "commit_and_push", lambda b, m=0: geschreven.append(b) or (True, 0))
    monkeypatch.setattr(loop, "pull_first", lambda: geschreven.append("pull"))

    k = Klok()
    loop.run(duur_minuten=30, dry_run=True, tijdbron=k.tijd, slaap=k.slaap)
    assert geschreven == []


def test_er_wordt_na_elke_ronde_weggeschreven(monkeypatch):
    berichten = []
    monkeypatch.setattr(loop, "taak_volglijst", lambda d: True)
    monkeypatch.setattr(loop, "taak_scan", lambda d: True)
    monkeypatch.setattr(loop, "taak_followup", lambda d: True)
    monkeypatch.setattr(loop, "commit_and_push", lambda b, m=0: berichten.append(b) or (True, 0))
    monkeypatch.setattr(loop, "pull_first", lambda: None)

    k = Klok()
    loop.run(duur_minuten=45, tijdbron=k.tijd, slaap=k.slaap)
    assert len(berichten) >= 3
    assert "volglijst" in berichten[0]


def test_git_wordt_overgeslagen_zonder_checkout(monkeypatch, tmp_path):
    """Op een gewone laptop zonder .git-map mag de lus niet omvallen."""
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    assert loop.git_available() is False
    assert loop.commit_and_push("test") == (True, 0)
    loop.pull_first()  # mag geen fout geven


# --------------------------------------------------------------------------- #
# Veiligheid
# --------------------------------------------------------------------------- #


def test_lus_bevat_geen_handelscode():
    bron = (config.BASE_DIR / "loop.py").read_text(encoding="utf-8")
    for verboden in (
        "sendTransaction",
        "send_transaction",
        "signTransaction",
        "Keypair",
        "private_key",
        "PRIVATE_KEY",
        "mnemonic",
        "jupiter",
        "place_order",
    ):
        assert verboden not in bron, f"{verboden} hoort hier niet te staan"
