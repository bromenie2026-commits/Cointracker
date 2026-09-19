"""Het opruimscript mag alleen de nagekeken foute munten aanraken."""

from __future__ import annotations

import repair_log

ZCAT = "HcRLc9VDgjLeK154xDawfb1dmVJ98DoSqcwTHGqiDeJR"
STONK = "6GmAFSYs4gk3FDao5FzzySQpPZaWsa4rUJHacpMpUNgx"


def _rij(adres, instap, **meting):
    rij = {"token_address": adres, "symbol": "X", "price_usd": str(instap),
           "max_price_seen": "", "max_gain_pct": ""}
    rij.update({k: str(v) for k, v in meting.items()})
    return rij


def test_zcat_wordt_nooit_aangeraakt():
    """De les van 19-09: ZCAT deed echt 5.405x."""
    rij = _rij(ZCAT, 0.0000306, price_7d=0.1654, max_price_seen=0.1654, max_gain_pct=540400)
    assert repair_log.repareer_rij(rij) == []
    assert rij["price_7d"] == "0.1654"
    assert rij["max_gain_pct"] == "540400"


def test_stonk_foute_meting_gaat_weg_normale_blijft():
    rij = _rij(STONK, 0.1330, price_1h=0.1312, price_7d=3605.58,
               max_price_seen=3605.58, max_gain_pct=2710900)
    geraakt = repair_log.repareer_rij(rij)
    assert geraakt == ["7d"]
    assert rij["price_7d"] == "" and rij["followup_7d_at"] == ""  # wordt hermeten
    assert rij["price_1h"] == "0.1312"                            # gewone meting blijft
    assert rij["max_gain_pct"] == ""


def test_onbekende_munt_met_enorme_stijging_blijft_staan():
    """Een echte 10.000x van een munt die niet op de lijst staat blijft staan."""
    rij = _rij("IETSANDERS", 0.000001, price_24h=0.01)
    assert repair_log.repareer_rij(rij) == []
    assert rij["price_24h"] == "0.01"
