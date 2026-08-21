"""Ruw archief, positie-monitor, weekrapport, reden-zin en gezondheidsalarm."""

from __future__ import annotations

import time

import config
import csv_log
import data_sources
import filters
import main
import monitor
import notify
import rapport
import raw_store
from tests.conftest import make_deployer, make_narrative, make_pair, make_report, make_social


def _eval(monkeypatch, **kw):
    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    return filters.evaluate(
        "MINTabc", kw.get("pair", make_pair()), kw.get("report", make_report()),
        make_deployer(), make_social(), make_narrative(), {},
    )


# --------------------------------------------------------------------------- #
# Ruw archief (plan §7.5)
# --------------------------------------------------------------------------- #


def test_ruw_archief_schrijft_en_leest(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RAW_ARCHIVE_DIR", tmp_path / "raw")
    assert raw_store.save("scan1", "row1", "MINT1", {"dexscreener_pair": {"a": 1}}) is True
    assert raw_store.save("scan1", "row2", "MINT2", {"rugcheck": {"score": 5}}) is True

    pad = tmp_path / "raw" / "scan-scan1.jsonl.gz"
    records = raw_store.read(pad)
    assert len(records) == 2
    assert records[0]["row_id"] == "row1"
    assert records[1]["payloads"]["rugcheck"]["score"] == 5


def test_ruw_archief_kan_uitgezet(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RAW_ARCHIVE_DIR", tmp_path / "raw")
    monkeypatch.setattr(config, "RAW_ARCHIVE_ENABLED", False)
    assert raw_store.save("s", "r", "M", {"x": {"y": 1}}) is False
    assert not (tmp_path / "raw").exists()


def test_ruw_archief_ruimt_oude_bestanden_op(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RAW_ARCHIVE_DIR", tmp_path / "raw")
    monkeypatch.setattr(config, "RAW_ARCHIVE_RETENTION_DAYS", 1.0)
    raw_store.save("oud", "r", "M", {"x": {"y": 1}})
    oud = tmp_path / "raw" / "scan-oud.jsonl.gz"
    import os

    verleden = time.time() - 5 * 86400
    os.utime(oud, (verleden, verleden))
    raw_store.save("nieuw", "r", "M", {"x": {"y": 1}})

    assert raw_store.prune() == 1
    assert not oud.exists()
    assert (tmp_path / "raw" / "scan-nieuw.jsonl.gz").exists()


def test_kapot_archiefpad_breekt_de_run_niet(monkeypatch, tmp_path):
    bestand = tmp_path / "blokkeert"
    bestand.write_text("ik ben een bestand, geen map")
    monkeypatch.setattr(config, "RAW_ARCHIVE_DIR", bestand / "raw")
    assert raw_store.save("s", "r", "M", {"x": {"y": 1}}) is False


# --------------------------------------------------------------------------- #
# Positie-monitor (plan §8.3)
# --------------------------------------------------------------------------- #

RISK = {
    "per_trade": {
        "stop_loss_pct": -35,
        "take_profit_ladder": [
            {"at_pct": 100, "sell_pct": 50},
            {"at_pct": 300, "sell_pct": 25},
        ],
    }
}


def _posities(tmp_path, monkeypatch, inhoud: str):
    pad = tmp_path / "posities.yaml"
    pad.write_text(inhoud, encoding="utf-8")
    monkeypatch.setattr(config, "POSITIONS_PATH", pad)
    monkeypatch.setattr(config, "POSITION_STATE_PATH", tmp_path / "pos.json")


def test_geen_posities_geen_meldingen(tmp_path, monkeypatch):
    _posities(tmp_path, monkeypatch, "posities: []\n")
    assert monitor.check_positions(RISK) == []


def test_niveaus_uit_risk_config():
    levels = monitor.levels_from_risk_config(RISK)
    namen = [n for n, _, _ in levels]
    assert "stop_loss" in namen and "tp_100" in namen and "tp_300" in namen


def test_take_profit_wordt_gemeld(tmp_path, monkeypatch):
    _posities(tmp_path, monkeypatch,
              'posities:\n  - mint: "MINT1"\n    symbol: "TEST"\n'
              '    entry_price_usd: 0.001\n    inleg_eur: 130\n')
    monkeypatch.setattr(
        data_sources, "fetch_pairs_for_token",
        lambda a: ([make_pair(price_usd=0.0021)], "ok"),  # +110%
    )
    triggers = monitor.check_positions(RISK)
    assert len(triggers) == 1
    assert triggers[0].level == "tp_100"
    onderwerp, tekst = monitor.format_trigger(triggers[0])
    assert "TEST" in onderwerp
    assert "verkoop 50%" in tekst and "inleg eruit" in tekst
    assert "EUR 273" in tekst  # 130 * 2.10


def test_zelfde_niveau_wordt_niet_twee_keer_gemeld(tmp_path, monkeypatch):
    _posities(tmp_path, monkeypatch,
              'posities:\n  - mint: "MINT1"\n    entry_price_usd: 0.001\n')
    monkeypatch.setattr(
        data_sources, "fetch_pairs_for_token", lambda a: ([make_pair(price_usd=0.0021)], "ok")
    )
    assert len(monitor.check_positions(RISK)) == 1
    assert monitor.check_positions(RISK) == []  # tweede keer: al gemeld


def test_stop_loss_wordt_gemeld(tmp_path, monkeypatch):
    _posities(tmp_path, monkeypatch,
              'posities:\n  - mint: "MINT1"\n    entry_price_usd: 0.001\n')
    monkeypatch.setattr(
        data_sources, "fetch_pairs_for_token", lambda a: ([make_pair(price_usd=0.0004)], "ok")
    )
    triggers = monitor.check_positions(RISK)
    assert [t.level for t in triggers] == ["stop_loss"]


def test_instap_via_marketcap_werkt_ook(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    _posities(tmp_path, monkeypatch,
              'posities:\n  - mint: "MINT1"\n    entry_marketcap_eur: 77000\n')
    monkeypatch.setattr(
        data_sources, "fetch_pairs_for_token",
        lambda a: ([make_pair(market_cap_usd=160_000)], "ok"),
    )
    triggers = monitor.check_positions(RISK)
    assert triggers and triggers[0].level == "tp_100"


def test_api_fout_slaat_de_positie_over(tmp_path, monkeypatch):
    _posities(tmp_path, monkeypatch,
              'posities:\n  - mint: "MINT1"\n    entry_price_usd: 0.001\n')
    monkeypatch.setattr(data_sources, "fetch_pairs_for_token", lambda a: ([], "error"))
    assert monitor.check_positions(RISK) == []


def test_monitor_handelt_niet():
    """De monitor kent geen enkele manier om een order te plaatsen."""
    import inspect

    bron = inspect.getsource(monitor)
    for verboden in ("sendTransaction", "Keypair", "private_key", "swap("):
        assert verboden not in bron


# --------------------------------------------------------------------------- #
# Reden-zin in de mail (plan §8.2)
# --------------------------------------------------------------------------- #


def test_reden_zin_noemt_de_opvallende_waarden(monkeypatch):
    ev = _eval(monkeypatch)
    zin = notify.build_reason(ev)
    assert "marketcap" in zin or "trade" in zin
    assert "geld instapt" in zin


def test_reden_zin_is_eerlijk_als_niks_opvalt(monkeypatch):
    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    saai = make_pair(market_cap_usd=100_000, volume_h24_usd=800_000,
                     buys_h24=15_000, sells_h24=15_000)
    ev = _eval(monkeypatch, pair=saai)
    assert "geen enkel signaal" in notify.build_reason(ev)


def test_reden_zin_staat_in_de_mail(monkeypatch):
    ev = _eval(monkeypatch)
    html = notify.build_email_html(ev, risk={})
    assert notify.build_reason(ev)[:40] in html
    assert "Schaduw-sets" in html


# --------------------------------------------------------------------------- #
# Gezondheidsalarm (plan §8.4)
# --------------------------------------------------------------------------- #


def test_gezonde_run_geeft_geen_alarm(monkeypatch):
    rows = [{f"{n}__outcome": "pass" for n in filters.HARD_FILTER_NAMES} for _ in range(10)]
    assert main.health_problems(rows, alerts_sent=2, rate_limit_hits=0) == []


def test_alarm_bij_te_veel_alerts():
    rows = [{f"{n}__outcome": "pass" for n in filters.HARD_FILTER_NAMES}]
    problemen = main.health_problems(rows, alerts_sent=50, rate_limit_hits=0)
    assert any("alerts in één run" in p for p in problemen)


def test_alarm_bij_veel_ontbrekende_data():
    rows = [{f"{n}__outcome": "data_unavailable" for n in filters.HARD_FILTER_NAMES}] * 5
    problemen = main.health_problems(rows, alerts_sent=0, rate_limit_hits=0)
    assert any("geen data" in p for p in problemen)


def test_alarm_bij_rate_limits():
    rows = [{f"{n}__outcome": "pass" for n in filters.HARD_FILTER_NAMES}]
    problemen = main.health_problems(rows, alerts_sent=0, rate_limit_hits=99)
    assert any("rate-limit" in p for p in problemen)


def test_alarm_bij_lege_run():
    assert any("Geen enkele coin" in p for p in main.health_problems([], 0, 0))


def test_alarm_heeft_een_cooldown(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "HEALTH_STATE_PATH", tmp_path / "health.json")
    verstuurd = []
    monkeypatch.setattr(notify, "send_run_summary",
                        lambda o, b: (verstuurd.append(o), True)[1])
    main.check_health([], 0, 0, send=True)
    main.check_health([], 0, 0, send=True)
    assert len(verstuurd) == 1


# --------------------------------------------------------------------------- #
# Weekrapport (plan §8.1)
# --------------------------------------------------------------------------- #


def test_rapport_zonder_data_zegt_dat_gewoon():
    assert "Nog geen logregels" in rapport.build_report([])


def test_rapport_vergelijkt_de_vier_sets(monkeypatch):
    ev = _eval(monkeypatch)
    rijen = []
    for i in range(6):
        row = csv_log.build_row(ev, "s1")
        row["token_address"] = f"MINT{i}"
        row["price_usd"] = "0.001"
        row["price_24h"] = "0.002" if i < 3 else "0.0002"
        row["shadow_A_alert"] = "true"
        row["shadow_B_alert"] = "true" if i < 3 else "false"
        rijen.append(row)

    tekst = rapport.build_report(rijen)
    assert "WELKE DREMPELSET WINT?" in tekst
    assert "Set A:" in tekst and "Set B:" in tekst
    assert "GEEN echt geld" in tekst
    # Set B pakte alleen de winnaars, dus die hoort positief te staan.
    regel_b = [r for r in tekst.splitlines() if r.strip().startswith("Set B:")][0]
    assert "+" in regel_b


def test_rapport_toont_piek_versus_eindstand(monkeypatch):
    ev = _eval(monkeypatch)
    row = csv_log.build_row(ev, "s1")
    row.update({"alerted": "true", "price_usd": "0.001", "price_24h": "0.0005",
                "max_gain_pct": "250.0"})
    tekst = rapport.build_report([row])
    assert "PIEK VERSUS EINDSTAND" in tekst
    assert "uitstapmoment" in tekst


def test_rapport_waarschuwt_als_de_bot_stilstaat(monkeypatch):
    ev = _eval(monkeypatch)
    row = csv_log.build_row(ev, "s1")
    row["timestamp_utc"] = "2026-01-01T00:00:00+00:00"
    tekst = rapport.build_report([row], days=3650)
    assert "geen scan meer gedraaid" in tekst
