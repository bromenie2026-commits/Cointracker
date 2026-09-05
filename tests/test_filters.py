"""Filterlogica — het hart van het systeem. Fail-closed is hier het punt."""

from __future__ import annotations

import time

import config
import filters
from models import Outcome, RugcheckReport
from tests.conftest import make_deployer, make_narrative, make_pair, make_report, make_social


# --------------------------------------------------------------------------- #
# Harde markt-filters
# --------------------------------------------------------------------------- #


def test_marketcap_binnen_bandbreedte(monkeypatch):
    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    assert filters.filter_marketcap(make_pair(market_cap_usd=50_000)).outcome is Outcome.PASS
    assert filters.filter_marketcap(make_pair(market_cap_usd=10_000)).outcome is Outcome.FAIL
    assert filters.filter_marketcap(make_pair(market_cap_usd=99_000_000)).outcome is Outcome.FAIL


def test_marketcap_valt_terug_op_fdv(monkeypatch):
    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    result = filters.filter_marketcap(make_pair(market_cap_usd=None, fdv_usd=60_000))
    assert result.outcome is Outcome.PASS and result.raw_value == 60_000.0


def test_marketcap_zonder_data_is_unavailable():
    result = filters.filter_marketcap(make_pair(market_cap_usd=None, fdv_usd=None))
    assert result.outcome is Outcome.DATA_UNAVAILABLE
    assert result.hard is True


def test_liq_mc_ratio_bandbreedte():
    goed = make_pair(market_cap_usd=100_000, liquidity_usd=20_000)  # 0.20
    dun = make_pair(market_cap_usd=100_000, liquidity_usd=1_000)  # 0.01
    raar = make_pair(market_cap_usd=100_000, liquidity_usd=400_000)  # 4.0
    assert filters.filter_liq_mc_ratio(goed).outcome is Outcome.PASS
    assert filters.filter_liq_mc_ratio(dun).outcome is Outcome.FAIL
    assert filters.filter_liq_mc_ratio(raar).outcome is Outcome.FAIL


def test_volume_spike_detecteert_wash_trading(monkeypatch):
    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    normaal = make_pair(market_cap_usd=100_000, volume_h24_usd=80_000, liquidity_usd=25_000,
                        volume_h1_usd=5_000)
    spike = make_pair(market_cap_usd=100_000, volume_h24_usd=9_000_000, liquidity_usd=25_000,
                      volume_h1_usd=5_000)
    dood = make_pair(market_cap_usd=100_000, volume_h24_usd=100, liquidity_usd=25_000,
                     volume_h1_usd=10)
    assert filters.filter_volume_spike(normaal).outcome is Outcome.PASS
    assert filters.filter_volume_spike(spike).outcome is Outcome.FAIL
    assert filters.filter_volume_spike(dood).outcome is Outcome.FAIL


def test_volume_spike_raw_bevat_alle_ratios(monkeypatch):
    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    result = filters.filter_volume_spike(make_pair())
    assert set(result.raw_value) == {"vol24_mc_ratio", "vol1h_liq_ratio", "vol24_eur"}


# --------------------------------------------------------------------------- #
# Harde rug-vectoren — fail-closed
# --------------------------------------------------------------------------- #


def test_vier_rugvectoren_pass():
    report = make_report()
    for fn in (
        filters.filter_mint_authority,
        filters.filter_freeze_authority,
        filters.filter_lp_locked,
        filters.filter_honeypot,
    ):
        assert fn(report).outcome is Outcome.PASS, fn.__name__


def test_actieve_mint_authority_faalt():
    result = filters.filter_mint_authority(make_report(mint_authority_renounced=False))
    assert result.outcome is Outcome.FAIL and result.hard is True


def test_ontbrekende_rugdata_is_fail_closed():
    """Geen data over een rug-vector => DATA_UNAVAILABLE => blokkeert."""
    leeg = RugcheckReport(mint="X", available=False, error="rugcheck 503")
    for fn in (
        filters.filter_mint_authority,
        filters.filter_freeze_authority,
        filters.filter_lp_locked,
        filters.filter_honeypot,
    ):
        result = fn(leeg)
        assert result.outcome is Outcome.DATA_UNAVAILABLE, fn.__name__
        assert result.outcome.is_blocking is True
        assert "503" in result.detail or result.detail


def test_fail_closed_uitzetten_laat_door(monkeypatch):
    monkeypatch.setattr(config, "FAIL_CLOSED_ON_MISSING_DATA", False)
    leeg = RugcheckReport(mint="X", available=False, error="geen data")
    result = filters.filter_mint_authority(leeg)
    assert result.outcome is Outcome.PASS
    assert "fail-closed staat UIT" in result.detail


def test_rugvector_uitzetten_geeft_skipped(monkeypatch):
    monkeypatch.setattr(config, "REQUIRE_HONEYPOT_PASS", False)
    result = filters.filter_honeypot(RugcheckReport(mint="X"))
    assert result.outcome is Outcome.SKIPPED
    assert result.outcome.is_blocking is False


# --------------------------------------------------------------------------- #
# Zachte signalen
# --------------------------------------------------------------------------- #


def test_activity_rustige_munt_haalt_alle_drie(monkeypatch):
    """Winnaarsprofiel: weinig maar grote trades, volume ~ marketcap."""
    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    rustig = make_pair()
    assert filters.filter_vol_mc_ratio(rustig).outcome is Outcome.PASS
    assert filters.filter_avg_trade_eur(rustig).outcome is Outcome.PASS
    assert filters.filter_tx_per_min(rustig).outcome is Outcome.PASS


def test_activity_maalstroom_faalt(monkeypatch):
    """Verliezersprofiel: duizenden kleine trades, volume pompt de mc rond."""
    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    maalstroom = make_pair(
        market_cap_usd=100_000,
        volume_h24_usd=900_000,   # vol/mc = 9
        buys_h24=20_000,
        sells_h24=20_000,         # 40.000 trades van ~22 dollar
        pair_created_at_ms=int(time.time() * 1000) - 120 * 60 * 1000,  # 2 uur oud
    )
    assert filters.filter_vol_mc_ratio(maalstroom).outcome is Outcome.FAIL
    assert filters.filter_avg_trade_eur(maalstroom).outcome is Outcome.FAIL
    assert filters.filter_tx_per_min(maalstroom).outcome is Outcome.FAIL


def test_activity_zonder_transacties_is_unknown():
    leeg = make_pair(buys_h24=0, sells_h24=0)
    assert filters.filter_avg_trade_eur(leeg).outcome is Outcome.DATA_UNAVAILABLE
    assert filters.filter_tx_per_min(leeg).outcome is Outcome.DATA_UNAVAILABLE
    # vol/mc kan wél zonder transactiedata berekend worden
    assert filters.filter_vol_mc_ratio(leeg).outcome is Outcome.PASS


def test_activity_componenten_kloppen(monkeypatch):
    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    a = filters.compute_activity(
        make_pair(market_cap_usd=100_000, volume_h24_usd=200_000, buys_h24=1_000, sells_h24=1_000)
    )
    assert a["vol_mc"] == 2.0
    assert a["avg_trade_eur"] == 100.0
    assert a["tx24"] == 2_000


def test_holder_concentratie():
    ok = filters.filter_holder_concentration(make_report(top_holders_pct=20.0, largest_holder_pct=6.0))
    slecht = filters.filter_holder_concentration(
        make_report(top_holders_pct=70.0, largest_holder_pct=40.0)
    )
    assert ok.outcome is Outcome.PASS
    assert slecht.outcome is Outcome.FAIL
    assert slecht.raw_value["top10_pct"] == 70.0


def test_holder_growth_since_launch():
    rate, detail = filters.compute_holder_growth("T", 600, 60.0, {})
    assert rate == 10.0 and detail["mode"] == "since_launch"


def test_holder_growth_delta_tussen_runs():
    now = time.time()
    history = {"T": {"holders": 100, "ts": now - 600}}  # 10 minuten geleden
    rate, detail = filters.compute_holder_growth("T", 400, 1000.0, history, now=now)
    assert round(rate, 1) == 30.0 and detail["mode"] == "delta"


def test_holder_growth_te_snel_faalt(monkeypatch):
    monkeypatch.setattr(config, "MAX_HOLDER_GROWTH_PER_MIN", 40.0)
    result = filters.filter_holder_growth(
        "T", make_report(total_holders=6000), make_pair(), {}
    )
    # 6000 holders in 6 uur = ~16.7/min -> pass
    assert result.outcome is Outcome.PASS
    snel = filters.filter_holder_growth(
        "T", make_report(total_holders=60000), make_pair(), {}
    )
    assert snel.outcome is Outcome.FAIL
    assert "airdrop-farming" in snel.detail


def test_deployer_reputatie():
    schoon = filters.filter_deployer_reputation(make_deployer(previous_deploys=1, dead_deploys=0,
                                                              dead_ratio=0.0))
    vies = filters.filter_deployer_reputation(
        make_deployer(previous_deploys=8, dead_deploys=7, dead_ratio=0.875)
    )
    assert schoon.outcome is Outcome.PASS
    assert vies.outcome is Outcome.FAIL
    assert "naar nul" in vies.detail


def test_deployer_onbekend_is_unavailable():
    result = filters.filter_deployer_reputation(make_deployer(available=False, error="geen historie"))
    assert result.outcome is Outcome.DATA_UNAVAILABLE and result.hard is False


def test_social_account_age():
    assert filters.filter_social_account_age(make_social(age_days=400)).outcome is Outcome.PASS
    assert filters.filter_social_account_age(make_social(age_days=2)).outcome is Outcome.FAIL
    assert (
        filters.filter_social_account_age({"available": False, "error": "geen token"}).outcome
        is Outcome.DATA_UNAVAILABLE
    )


def test_narratief_check(monkeypatch):
    monkeypatch.setattr(config, "CLAUDE_META_CHECK_ENABLED", True)
    assert filters.filter_narrative(make_narrative(score=80)).outcome is Outcome.PASS
    assert filters.filter_narrative(make_narrative(score=10)).outcome is Outcome.FAIL
    monkeypatch.setattr(config, "CLAUDE_META_CHECK_ENABLED", False)
    assert filters.filter_narrative(None).outcome is Outcome.SKIPPED


# --------------------------------------------------------------------------- #
# Zachte score + eindoordeel
# --------------------------------------------------------------------------- #


def _good_evaluation(**kw):
    return filters.evaluate(
        token_address=kw.get("token", "MINTabc"),
        pair=kw.get("pair", make_pair()),
        report=kw.get("report", make_report()),
        deployer=kw.get("deployer", make_deployer()),
        social=kw.get("social", make_social()),
        narrative=kw.get("narrative", make_narrative()),
        holder_history=kw.get("history", {}),
    )


def test_gezonde_coin_haalt_alles(monkeypatch):
    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    evaluation = _good_evaluation()
    assert evaluation.hard_pass is True
    assert evaluation.soft_score is not None and evaluation.soft_score > 55
    ok, reason = filters.should_alert(evaluation)
    assert ok is True and reason == ""


def test_harde_fail_blokkeert_ondanks_perfecte_zachte_score(monkeypatch):
    """Kernregel uit het plan §3.2."""
    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    evaluation = _good_evaluation(report=make_report(mint_authority_renounced=False))
    assert evaluation.hard_pass is False
    ok, reason = filters.should_alert(evaluation)
    assert ok is False
    assert "mint_authority_renounced" in reason and "universeel" in reason


def test_ontbrekende_rugdata_blokkeert_alert(monkeypatch):
    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    evaluation = _good_evaluation(report=RugcheckReport(mint="X", available=False, error="503"))
    ok, reason = filters.should_alert(evaluation)
    assert ok is False and "data_unavailable" in reason


def test_lage_zachte_score_blokkeert(monkeypatch):
    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    monkeypatch.setattr(config, "MIN_SOFT_SCORE_TO_ALERT", 95.0)
    evaluation = _good_evaluation()
    ok, reason = filters.should_alert(evaluation)
    assert ok is False and "zachte score" in reason and "onder" in reason


def test_alle_filters_worden_gedraaid(monkeypatch):
    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    evaluation = _good_evaluation()
    namen = [r.name for r in evaluation.results]
    assert namen == filters.FILTER_NAMES
    assert len(evaluation.hard_results) == 8
    assert len(evaluation.soft_results) == 8


def test_bot_score_bestaat_niet_meer():
    """De oude bot_score correleerde niet met het resultaat en is verwijderd."""
    assert "bot_score" not in filters.FILTER_NAMES
    assert not hasattr(filters, "filter_bot_score")
    assert config.SOFT_WEIGHTS.get("bot_score") is None


def test_soft_score_gradueel(monkeypatch):
    """Ruim binnen de drempel scoort hoger dan er net onder."""
    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    rustig = filters.compute_soft_score(
        [filters.filter_vol_mc_ratio(make_pair(market_cap_usd=200_000, volume_h24_usd=100_000))]
    )[0]
    druk = filters.compute_soft_score(
        [filters.filter_vol_mc_ratio(make_pair(market_cap_usd=200_000, volume_h24_usd=800_000))]
    )[0]
    assert rustig > druk


def test_gewichten_tellen_op_tot_honderd():
    assert round(sum(config.SOFT_WEIGHTS.values()), 6) == 100.0


def test_unknown_telt_als_neutraal(monkeypatch):
    monkeypatch.setattr(config, "SOFT_UNKNOWN_SCORE", 40.0)
    result = filters.filter_deployer_reputation(None)
    score, breakdown = filters.compute_soft_score([result])
    assert score == 40.0 and breakdown["deployer_reputation"] == 40.0


def test_holder_history_opslag(monkeypatch):
    history = {}
    filters.record_holder_observation(history, "T", 500)
    assert history["T"]["holders"] == 500
    filters.record_holder_observation(history, "T2", None)
    assert "T2" not in history
    filters.save_holder_history(history)
    assert filters.load_holder_history()["T"]["holders"] == 500


# --------------------------------------------------------------------------- #
# Bugfix 4.2 — liquiditeit via rugcheck als DexScreener niets geeft
# --------------------------------------------------------------------------- #


def test_liquiditeit_valt_terug_op_rugcheck(monkeypatch):
    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    zonder = make_pair(liquidity_usd=None)
    rapport = make_report(total_market_liquidity_usd=25_000.0)

    # Zonder terugval: fail-closed, coin valt af.
    assert filters.filter_liquidity(zonder, None).outcome is Outcome.DATA_UNAVAILABLE
    # Mét terugval: gewoon een geldige meting.
    resultaat = filters.filter_liquidity(zonder, rapport)
    assert resultaat.outcome is Outcome.PASS
    assert resultaat.raw_value == 25_000.0
    assert "rugcheck" in resultaat.detail


def test_liq_mc_ratio_gebruikt_dezelfde_terugval(monkeypatch):
    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    zonder = make_pair(liquidity_usd=None, market_cap_usd=100_000)
    rapport = make_report(total_market_liquidity_usd=20_000.0)
    assert filters.filter_liq_mc_ratio(zonder, None).outcome is Outcome.DATA_UNAVAILABLE
    resultaat = filters.filter_liq_mc_ratio(zonder, rapport)
    assert resultaat.outcome is Outcome.PASS and resultaat.raw_value == 0.2


def test_dexscreener_gaat_voor_op_rugcheck(monkeypatch):
    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    resultaat = filters.filter_liquidity(
        make_pair(liquidity_usd=30_000.0), make_report(total_market_liquidity_usd=999_999.0)
    )
    assert resultaat.raw_value == 30_000.0
    assert "dexscreener" in resultaat.detail


def test_beide_bronnen_leeg_blijft_fail_closed():
    resultaat = filters.filter_liquidity(make_pair(liquidity_usd=None), make_report(total_market_liquidity_usd=None))
    assert resultaat.outcome is Outcome.DATA_UNAVAILABLE
    assert resultaat.hard is True


# --------------------------------------------------------------------------- #
# Schaduw-configuraties (plan §7.4)
# --------------------------------------------------------------------------- #


def _eval_met(monkeypatch, **pairkw):
    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    return filters.evaluate(
        "MINT", make_pair(**pairkw), make_report(), make_deployer(),
        make_social(), make_narrative(), {},
    )


def test_alle_sets_worden_beoordeeld(monkeypatch):
    ev = _eval_met(monkeypatch)
    assert set(ev.shadow_sets) == set(config.SHADOW_SETS)
    assert all(isinstance(v, bool) for v in ev.shadow_sets.values())


def test_set_E_is_strenger_op_vol_mc(monkeypatch):
    """Set E laat alleen munten door waarvan het volume onder de marketcap zit.

    Gemeten op 3.851 munten: 4,4% van alles deed ooit +100%, 22% van wat set B
    doorliet, 37% van wat ook onder vol/mc 1,0 zat.
    """
    # EUR 150.000 volume op EUR 150.000 marketcap -> vol/mc = 1,0: net goed.
    laag = _eval_met(monkeypatch, market_cap_usd=150_000.0, fdv_usd=150_000.0)
    assert laag.shadow_sets["B"] is True
    assert laag.shadow_sets["E"] is True

    # Zelfde volume op een kleinere marketcap -> 1,25. Alleen E blokkeert.
    hoog = _eval_met(monkeypatch)
    assert hoog.shadow_sets["E"] is False
    assert hoog.shadow_sets["B"] is True


def test_set_E_mailt_niet(monkeypatch):
    """Schaduwsets veranderen niets aan welke mails er uitgaan."""
    assert config.ACTIVE_SET != "E"
    ev = _eval_met(monkeypatch)
    ok, _reden = filters.should_alert(ev)
    zou_B, _ = filters.set_would_alert(ev, "B")
    assert ok == zou_B


def test_sets_verschillen_op_marketcap(monkeypatch):
    """EUR 120.000: te groot voor C (max 75k), goed voor A, B en D."""
    ev = _eval_met(monkeypatch, market_cap_usd=120_000, fdv_usd=120_000)
    assert ev.shadow_sets["A"] is True
    assert ev.shadow_sets["B"] is True
    assert ev.shadow_sets["C"] is False
    assert ev.shadow_sets["D"] is True


def test_alleen_de_tail_hunter_pakt_een_grote_munt(monkeypatch):
    """EUR 800.000 valt buiten B en C, maar A en D laten hem door."""
    ev = _eval_met(monkeypatch, market_cap_usd=800_000, fdv_usd=800_000,
                   volume_h24_usd=900_000, liquidity_usd=200_000)
    assert ev.shadow_sets["B"] is False
    assert ev.shadow_sets["C"] is False
    assert ev.shadow_sets["D"] is True


def test_kleine_munt_alleen_voor_b_c_en_d(monkeypatch):
    """EUR 20.000 is te klein voor A (ondergrens 35k)."""
    ev = _eval_met(monkeypatch, market_cap_usd=20_000, fdv_usd=20_000,
                   liquidity_usd=12_000, volume_h24_usd=25_000,
                   buys_h24=160, sells_h24=140, buys_h1=12, sells_h1=10)
    assert ev.shadow_sets["A"] is False
    assert ev.shadow_sets["B"] is True
    assert ev.shadow_sets["C"] is True


def test_universele_blokkade_geldt_voor_elke_set(monkeypatch):
    """Een rug-vector die faalt zet ALLE sets op false — ook de tail-hunter."""
    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    ev = filters.evaluate(
        "MINT", make_pair(), make_report(lp_locked_or_burned=False, lp_locked_pct=0.0),
        make_deployer(), make_social(), make_narrative(), {},
    )
    assert all(v is False for v in ev.shadow_sets.values())
    for naam in ("A", "B", "C", "D"):
        ok, reden = filters.set_would_alert(ev, naam)
        assert ok is False and "universeel" in reden


def test_actieve_set_bepaalt_de_mail(monkeypatch):
    monkeypatch.setattr(config, "ACTIVE_SET", "C")
    ev = _eval_met(monkeypatch, market_cap_usd=120_000, fdv_usd=120_000)
    ok, _ = filters.should_alert(ev)
    assert ok is False  # C wijst 120k af
    monkeypatch.setattr(config, "ACTIVE_SET", "D")
    ok, _ = filters.should_alert(ev)
    assert ok is True


def test_schaduwkolommen_komen_in_het_logboek(monkeypatch):
    import csv_log

    ev = _eval_met(monkeypatch, market_cap_usd=120_000, fdv_usd=120_000)
    row = csv_log.build_row(ev, "s1")
    assert row["shadow_A_alert"] == "true"
    assert row["shadow_C_alert"] == "false"
    assert row["active_set"] == config.ACTIVE_SET


# --------------------------------------------------------------------------- #
# Verschillen tussen scans (plan §7.2)
# --------------------------------------------------------------------------- #


def test_eerste_waarneming_heeft_geen_verschillen(monkeypatch):
    ev = _eval_met(monkeypatch)
    assert ev.deltas == {}


def test_liquiditeit_die_wegloopt_wordt_zichtbaar(monkeypatch):
    """Een LP die tussen twee scans leegloopt is het sterkste rug-signaal
    dat je in een momentopname nooit ziet."""
    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    history = {}

    ev1 = filters.evaluate("MINT", make_pair(liquidity_usd=30_000), make_report(),
                           make_deployer(), make_social(), make_narrative(), history)
    filters.record_holder_observation(history, "MINT", 800, filters.metrics_for_history(ev1))
    history["MINT"]["ts"] = time.time() - 600  # 10 minuten geleden

    ev2 = filters.evaluate("MINT", make_pair(liquidity_usd=9_000), make_report(),
                           make_deployer(), make_social(), make_narrative(), history)
    assert ev2.deltas["liquidity_eur_delta_pct"] == -70.0
    assert ev2.deltas["minutes_since_prev"] == 10.0


def test_groter_geld_dat_instapt_wordt_zichtbaar(monkeypatch):
    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    history = {"MINT": {"ts": time.time() - 1800, "avg_trade_eur": 40.0, "holders": 500}}
    ev = filters.evaluate("MINT", make_pair(volume_h24_usd=216_000, buys_h24=1_350,
                                            sells_h24=1_350),
                          make_report(), make_deployer(), make_social(), make_narrative(), history)
    assert ev.deltas["avg_trade_eur_delta_pct"] == 100.0


def test_te_snel_na_elkaar_geeft_geen_verschil(monkeypatch):
    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    history = {"MINT": {"ts": time.time() - 5, "liquidity_eur": 30_000.0}}
    ev = _eval_met(monkeypatch)
    assert filters.compute_deltas(history, "MINT", filters.metrics_for_history(ev)) == {}


def test_verschillen_komen_in_het_logboek(monkeypatch):
    import csv_log

    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    history = {"MINT": {"ts": time.time() - 900, "liquidity_eur": 60_000.0, "holders": 400}}
    ev = filters.evaluate("MINT", make_pair(liquidity_usd=30_000), make_report(),
                          make_deployer(), make_social(), make_narrative(), history)
    row = csv_log.build_row(ev, "s1")
    assert row["liquidity_eur_delta_pct"] == "-50.0"
    assert row["minutes_since_prev"] == "15.0"


def test_verschillen_filteren_nog_niet(monkeypatch):
    """Nieuw signaal: eerst meten op verse data, dan pas beslissen."""
    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    history = {"MINT": {"ts": time.time() - 900, "liquidity_eur": 100_000.0}}
    ev = filters.evaluate("MINT", make_pair(liquidity_usd=30_000), make_report(),
                          make_deployer(), make_social(), make_narrative(), history)
    assert ev.deltas["liquidity_eur_delta_pct"] < -50
    ok, _ = filters.should_alert(ev)
    assert ok is True  # wordt gelogd, blokkeert nog niets
