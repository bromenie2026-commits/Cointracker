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


def test_bot_score_laag_bij_normaal_gedrag():
    score, comps = filters.compute_bot_score(make_pair())
    assert score is not None and score < 40
    assert "imbalance" in comps


def test_bot_score_hoog_bij_botpatroon():
    verdacht = make_pair(
        buys_h24=5_000,
        sells_h24=100,
        volume_h24_usd=20_000,      # gemiddelde trade ~4 dollar
        liquidity_usd=5_000,        # churn 4x... maar imbalance is extreem
        buys_h1=110,
        sells_h1=102,
    )
    score, _ = filters.compute_bot_score(verdacht)
    assert score is not None and score > 50


def test_bot_score_zonder_transacties_is_unknown():
    score, comps = filters.compute_bot_score(make_pair(buys_h24=0, sells_h24=0))
    assert score is None and "reason" in comps
    result = filters.filter_bot_score(make_pair(buys_h24=0, sells_h24=0))
    assert result.outcome is Outcome.DATA_UNAVAILABLE and result.hard is False


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
    assert "mint_authority_renounced=FAIL" in reason


def test_ontbrekende_rugdata_blokkeert_alert(monkeypatch):
    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    evaluation = _good_evaluation(report=RugcheckReport(mint="X", available=False, error="503"))
    ok, reason = filters.should_alert(evaluation)
    assert ok is False and "DATA_UNAVAILABLE" in reason


def test_lage_zachte_score_blokkeert(monkeypatch):
    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    monkeypatch.setattr(config, "MIN_SOFT_SCORE_TO_ALERT", 95.0)
    evaluation = _good_evaluation()
    ok, reason = filters.should_alert(evaluation)
    assert ok is False and "onder drempel" in reason


def test_alle_veertien_filters_worden_gedraaid(monkeypatch):
    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    evaluation = _good_evaluation()
    namen = [r.name for r in evaluation.results]
    assert namen == filters.FILTER_NAMES
    assert len(evaluation.hard_results) == 8
    assert len(evaluation.soft_results) == 6


def test_soft_score_gradueel():
    """Ruim binnen de drempel scoort hoger dan er net onder."""
    goed = filters.compute_soft_score(
        [filters.filter_bot_score(make_pair(buys_h24=500, sells_h24=480,
                                            volume_h24_usd=200_000))]
    )[0]
    slechter = filters.compute_soft_score(
        [filters.filter_bot_score(make_pair(buys_h24=900, sells_h24=200,
                                            volume_h24_usd=200_000))]
    )[0]
    assert goed > slechter


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
