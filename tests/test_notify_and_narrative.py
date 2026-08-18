"""E-mailopbouw en de Claude narratief-check (beide zonder netwerk)."""

from __future__ import annotations

import claude_meta_check
import config
import filters
import http_client
import notify
from tests.conftest import make_deployer, make_narrative, make_pair, make_report, make_social


def _evaluation(monkeypatch, **kw):
    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    return filters.evaluate(
        token_address="MINTabc",
        pair=kw.get("pair", make_pair()),
        report=kw.get("report", make_report()),
        deployer=make_deployer(),
        social=make_social(),
        narrative=kw.get("narrative", make_narrative(score=82.0, verdict="sterk eigen concept")),
        holder_history={},
    )


def test_mail_toont_alle_rugvectoren(monkeypatch):
    html = notify.build_email_html(_evaluation(monkeypatch), risk={})
    for naam in (
        "mint_authority_renounced",
        "freeze_authority_renounced",
        "lp_locked_or_burned",
        "honeypot_check",
    ):
        assert naam in html


def test_mail_bevat_x_zoeklink(monkeypatch):
    evaluation = _evaluation(monkeypatch)
    html = notify.build_email_html(evaluation, risk={})
    assert "x.com/search" in html
    assert "MINTabc" in evaluation.x_search_url
    assert "%24TEST" in evaluation.x_search_url  # $TEST url-encoded


def test_mail_bevat_risicochecklist(monkeypatch):
    risk = {
        "pre_buy_checklist": ["Stop-loss vooraf bepaald"],
        "per_trade": {"stop_loss_pct": -35},
    }
    html = notify.build_email_html(_evaluation(monkeypatch), risk=risk)
    assert "Stop-loss vooraf bepaald" in html
    assert "stop_loss_pct" in html


def test_mail_benadrukt_dat_er_niet_gehandeld_wordt(monkeypatch):
    html = notify.build_email_html(_evaluation(monkeypatch), risk={})
    assert "geen trading-bot" in html
    assert "automatisch gekocht of verkocht" in html
    assert "geen wallet gekoppeld" in html


def test_mail_toont_narratiefscore(monkeypatch):
    html = notify.build_email_html(_evaluation(monkeypatch), risk={})
    assert "sterk eigen concept" in html


def test_plaintext_variant(monkeypatch):
    text = notify.build_email_text(_evaluation(monkeypatch))
    assert "HARDE FILTERS" in text and "ZACHTE SIGNALEN" in text
    assert "koopt en verkoopt niets" in text


def test_html_escaping(monkeypatch):
    kwaad = make_pair(name="<script>alert(1)</script>", symbol="XSS")
    html = notify.build_email_html(_evaluation(monkeypatch, pair=kwaad), risk={})
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_risk_config_wordt_ingelezen():
    risk = notify.load_risk_config()
    assert "pre_buy_checklist" in risk
    assert risk["portfolio"]["max_pct_per_trade"] == 5


def test_send_alert_faalt_zacht(monkeypatch):
    """Een SMTP-fout mag de run niet slopen."""
    monkeypatch.setattr(notify, "ensure_configured", lambda: None)
    monkeypatch.setattr(config, "GMAIL_ADDRESS", "a@b.c")
    monkeypatch.setattr(config, "ALERT_RECIPIENT", "a@b.c")

    def boom(*a, **kw):
        raise OSError("smtp weg")

    monkeypatch.setattr(notify.smtplib, "SMTP", boom)
    assert notify.send_alert(_evaluation(monkeypatch)) is False


# --------------------------------------------------------------------------- #
# Narratief-check
# --------------------------------------------------------------------------- #


def _mock_claude(monkeypatch, text):
    monkeypatch.setattr(config, "CLAUDE_META_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(
        http_client,
        "post_json",
        lambda url, **kw: http_client.ApiResponse(
            ok=True, data={"content": [{"type": "text", "text": text}]}
        ),
    )


def test_narratief_parse_schone_json(monkeypatch):
    _mock_claude(monkeypatch, '{"score": 72, "verdict": "eigen hoek", "reasoning": "ok"}')
    check = claude_meta_check.check(make_pair(), "MINT")
    assert check.available is True and check.score == 72.0
    assert check.verdict == "eigen hoek"


def test_narratief_parse_json_in_tekst(monkeypatch):
    _mock_claude(monkeypatch, 'Hier is mijn oordeel:\n{"score": 30, "verdict": "kopie"}\nKlaar.')
    check = claude_meta_check.check(make_pair(), "MINT")
    assert check.available is True and check.score == 30.0


def test_narratief_onparseerbaar_is_niet_fataal(monkeypatch):
    _mock_claude(monkeypatch, "geen idee eigenlijk")
    check = claude_meta_check.check(make_pair(), "MINT")
    assert check.available is False and "onparseerbaar" in check.error


def test_narratief_score_wordt_geklemd(monkeypatch):
    _mock_claude(monkeypatch, '{"score": 480}')
    assert claude_meta_check.check(make_pair(), "MINT").score == 100.0


def test_narratief_api_fout_is_zacht(monkeypatch):
    monkeypatch.setattr(config, "CLAUDE_META_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(
        http_client, "post_json", lambda url, **kw: http_client.ApiResponse(ok=False, error="429")
    )
    check = claude_meta_check.check(make_pair(), "MINT")
    assert check.available is False and check.error == "429"


def test_prompt_bevat_metadata_maar_geen_koersdata():
    prompt = claude_meta_check.build_prompt(make_pair(), "MINT")
    assert "Ticker: TEST" in prompt
    assert "x.com/testcoin" in prompt
    assert "priceUsd" not in prompt and "0.00042" not in prompt
