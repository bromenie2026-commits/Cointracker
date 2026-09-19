"""Tests voor vroege_kopers.py. Geen netwerk: alle RPC-antwoorden zijn nep."""

from __future__ import annotations

import csv

import pytest

import config
import csv_log
import data_sources
import http_client
import vroege_kopers as vk

MINT = "MUNT1111111111111111111111111111111111111"


def _tx(betaler, voor, na, mint=MINT, err=None):
    """Een jsonParsed-transactie waarin `betaler` van `voor` naar `na` tokens gaat."""
    bal = lambda x: [{"mint": mint, "owner": betaler, "uiTokenAmount": {"uiAmount": x}}] if x is not None else []
    return {
        "meta": {"err": err, "preTokenBalances": bal(voor), "postTokenBalances": bal(na)},
        "transaction": {"message": {"accountKeys": [{"pubkey": betaler, "signer": True}]}},
    }


# --------------------------------------------------------------------------- #
# Wie kocht er?
# --------------------------------------------------------------------------- #


def test_koop_wordt_herkend():
    assert vk.koper_uit_transactie(_tx("ALICE", None, 1000.0), MINT) == "ALICE"
    assert vk.koper_uit_transactie(_tx("ALICE", 5.0, 10.0), MINT) == "ALICE"


def test_verkoop_en_liquiditeit_tellen_niet():
    assert vk.koper_uit_transactie(_tx("ALICE", 1000.0, 0.0), MINT) is None  # verkoop / pool vullen
    assert vk.koper_uit_transactie(_tx("ALICE", 5.0, 5.0), MINT) is None


def test_mislukte_of_andere_munt_telt_niet():
    assert vk.koper_uit_transactie(_tx("ALICE", None, 10.0, err={"x": 1}), MINT) is None
    assert vk.koper_uit_transactie(_tx("ALICE", None, 10.0, mint="ANDERE"), MINT) is None
    assert vk.koper_uit_transactie(None, MINT) is None


# --------------------------------------------------------------------------- #
# Terugbladeren naar het begin
# --------------------------------------------------------------------------- #


def _paginas(monkeypatch, totaal):
    """Nep-RPC met `totaal` handtekeningen, nieuwste eerst: sig{totaal-1} .. sig0."""
    alle = [{"signature": f"sig{i}", "err": None} for i in range(totaal - 1, -1, -1)]
    aanroepen = []

    def fake(method, params):
        aanroepen.append(params[1].get("before"))
        start = 0
        if params[1].get("before"):
            start = [s["signature"] for s in alle].index(params[1]["before"]) + 1
        return http_client.ApiResponse(ok=True, status_code=200, data={"result": alle[start:start + 1000]})

    monkeypatch.setattr(data_sources, "rpc_call", fake)
    return aanroepen


def test_korte_geschiedenis_een_pagina(monkeypatch):
    _paginas(monkeypatch, 300)
    sigs, status = vk.oudste_handtekeningen("POOL", n=5)
    assert status == "ok"
    assert sigs == ["sig0", "sig1", "sig2", "sig3", "sig4"]  # oudste eerst


def test_lange_geschiedenis_wordt_teruggebladerd(monkeypatch):
    aanroepen = _paginas(monkeypatch, 2503)
    sigs, status = vk.oudste_handtekeningen("POOL", n=5, max_paginas=10)
    assert status == "ok" and sigs[0] == "sig0"
    assert len(aanroepen) == 3


def test_laatste_pagina_te_kort_vult_aan_uit_de_vorige(monkeypatch):
    """1003 transacties: de laatste pagina heeft er 3, we willen er 5."""
    _paginas(monkeypatch, 1003)
    sigs, status = vk.oudste_handtekeningen("POOL", n=5)
    assert sigs == ["sig0", "sig1", "sig2", "sig3", "sig4"]


def test_precies_duizend(monkeypatch):
    _paginas(monkeypatch, 1000)
    sigs, status = vk.oudste_handtekeningen("POOL", n=3)
    assert status == "ok" and sigs == ["sig0", "sig1", "sig2"]


def test_te_druk_wordt_gemeld_niet_verzonnen(monkeypatch):
    _paginas(monkeypatch, 5000)
    sigs, status = vk.oudste_handtekeningen("POOL", n=5, max_paginas=2)
    assert sigs == [] and status == "begin niet bereikt"


def test_rpc_fout(monkeypatch):
    monkeypatch.setattr(
        data_sources, "rpc_call",
        lambda m, p: http_client.ApiResponse(ok=False, status_code=429, error="te veel"),
    )
    sigs, status = vk.oudste_handtekeningen("POOL")
    assert sigs == [] and status.startswith("fout")


# --------------------------------------------------------------------------- #
# Analyse — vooral: niet jezelf voor de gek houden
# --------------------------------------------------------------------------- #


@pytest.fixture
def paden(tmp_path, monkeypatch):
    monkeypatch.setattr(vk, "UITVOER", tmp_path / "vk.csv")
    monkeypatch.setattr(vk, "RAPPORT", tmp_path / "vk.txt")
    return tmp_path


def _logboek(munten):
    """munten: lijst van (adres, symbool, iso-tijd, piek%)"""
    rows = []
    for adres, sym, ts, piek in munten:
        rows.append({"token_address": adres, "symbol": sym, "timestamp_utc": ts,
                     "alerted": "true", "max_gain_pct": str(piek), "price_usd": "0.001",
                     "pair_address": "P" + adres})
    csv_log.append_rows(rows)


def _kopers(regels):
    with vk.UITVOER.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=vk.KOLOMMEN)
        w.writeheader()
        for mint, wallet in regels:
            w.writerow({"token_address": mint, "wallet": wallet, "status": "ok", "rang": 1})


def test_een_wallet_in_een_winnaar_voorspelt_zichzelf_niet(paden):
    """De klassieke fout: 'X zat in ZCAT, ZCAT won, dus X voorspelt winnaars'."""
    _logboek([("A", "ZCAT", "2026-09-01T00:00:00+00:00", 50000),
              ("B", "NUL", "2026-09-02T00:00:00+00:00", -90)])
    _kopers([("A", "SLIM"), ("B", "IEMAND")])
    tekst = vk.analyseer()
    # Geen enkele munt heeft een wallet uit een ÁNDERE winnaar.
    assert "Munten met minstens één 'winnaarswallet' vroeg erin : 0" in tekst


def test_terugkerende_wallet_wordt_gezien(paden, monkeypatch):
    # Met maar drie munten zit elke terugkerende wallet in 67% van alles; zet de
    # bot-grens hier dus even ruim, anders wordt SLIM als sniper weggezet.
    monkeypatch.setattr(vk, "BOT_DREMPEL", 0.9)
    _logboek([("A", "W1", "2026-09-01T00:00:00+00:00", 2000),
              ("B", "W2", "2026-09-03T00:00:00+00:00", 3000),
              ("C", "L1", "2026-09-04T00:00:00+00:00", -80)])
    _kopers([("A", "SLIM"), ("B", "SLIM"), ("C", "ANDER")])
    tekst = vk.analyseer()
    assert "SLIM" in tekst
    # In de strenge variant telt alleen W2 (W1 was al eerder winnaar).
    streng = tekst.split("Alleen eerdere winnaars")[1]
    assert "Munten met minstens één 'winnaarswallet' vroeg erin : 1" in streng


def test_bots_die_overal_in_zitten_tellen_niet(paden):
    munten = [(f"M{i}", f"S{i}", f"2026-09-{i + 1:02d}T00:00:00+00:00", 2000 if i < 2 else -90)
              for i in range(6)]
    _logboek(munten)
    _kopers([(m[0], "SNIPER") for m in munten])
    tekst = vk.analyseer()
    assert "Bots/snipers  : 1" in tekst
    assert "Munten met minstens één 'winnaarswallet' vroeg erin : 0" in tekst


def test_foute_munten_tellen_niet_als_winnaar(paden):
    stonk = "6GmAFSYs4gk3FDao5FzzySQpPZaWsa4rUJHacpMpUNgx"
    _logboek([(stonk, "STONK", "2026-09-07T00:00:00+00:00", 2_710_900)])
    assert vk.uitkomsten()[stonk]["winnaar"] is None


def test_leest_alleen():
    bron = (config.BASE_DIR / "vroege_kopers.py").read_text(encoding="utf-8")
    assert "sendTransaction" not in bron and "Keypair" not in bron
