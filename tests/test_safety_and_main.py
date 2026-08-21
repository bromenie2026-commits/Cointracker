"""
Veiligheidsgaranties (plan §9) en de orchestratie in main.py.

De belangrijkste test in dit bestand: er is nergens code die een koop- of
verkooptransactie kan initiëren.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import config
import main
import notify
from models import RugcheckReport
from tests.conftest import make_pair, make_report

ROOT = Path(__file__).resolve().parents[1]

# Termen die op transactie-uitvoering of sleutelbeheer wijzen.
FORBIDDEN_PATTERNS = [
    r"\bsendTransaction\b",
    r"\bsend_transaction\b",
    r"\bsignTransaction\b",
    r"\bsign_transaction\b",
    r"\bKeypair\b",
    r"\bprivate_key\b",
    r"\bPRIVATE_KEY\b",
    r"\bmnemonic\b",
    r"\bseed_phrase\b",
    r"\bjupiter\b",
    r"\bswap\(",
    r"\bplace_order\b",
]


def _source_files() -> list[Path]:
    return [
        p
        for p in ROOT.glob("*.py")
        if p.name not in {"conftest.py"}
    ]


def test_geen_trading_code_aanwezig():
    overtredingen = []
    for path in _source_files():
        text = path.read_text(encoding="utf-8")
        for pattern in FORBIDDEN_PATTERNS:
            if re.search(pattern, text):
                overtredingen.append(f"{path.name}: {pattern}")
    assert not overtredingen, "Mogelijke trading-/sleutelcode gevonden: " + str(overtredingen)


def test_trading_vlag_staat_uit():
    assert config.TRADING_ENABLED is False


def test_assert_no_trading_breekt_af_bij_true(monkeypatch):
    monkeypatch.setattr(config, "TRADING_ENABLED", True)
    with pytest.raises(SystemExit):
        main.assert_no_trading()


def test_er_wordt_geen_wallet_env_gelezen():
    """Geen enkele module leest een wallet-achtige environment variable."""
    for path in _source_files():
        text = path.read_text(encoding="utf-8")
        for match in re.findall(r"os\.getenv\(\s*[\"']([^\"']+)", text):
            assert not any(
                token in match.upper() for token in ("WALLET", "PRIVATE", "SECRET_KEY", "MNEMONIC")
            ), f"{path.name} leest {match}"


# --------------------------------------------------------------------------- #
# main-orchestratie
# --------------------------------------------------------------------------- #


def _patch_pipeline(monkeypatch, report=None, pair=None):
    import claude_meta_check
    import csv_log
    import data_sources
    import deployer_reputation
    import rugcheck
    from models import DeployerReputation, NarrativeCheck

    pair = pair or make_pair()
    report = report if report is not None else make_report()

    monkeypatch.setattr(config, "USD_PER_EUR", 1.0)
    monkeypatch.setattr(data_sources, "discover_candidates", lambda limit=None: [pair])
    monkeypatch.setattr(rugcheck, "check_token", lambda mint, pair_addresses=None: report)
    monkeypatch.setattr(
        deployer_reputation,
        "get_reputation",
        lambda mint, known_creator=None: DeployerReputation(
            wallet="W", available=True, previous_deploys=1, dead_deploys=0, dead_ratio=0.0
        ),
    )
    monkeypatch.setattr(
        data_sources,
        "get_social_account_age_days",
        lambda handle: {"available": True, "handle": handle, "age_days": 300.0},
    )
    monkeypatch.setattr(
        claude_meta_check, "check", lambda pair, addr: NarrativeCheck(available=True, score=75.0)
    )
    monkeypatch.setattr(claude_meta_check, "ensure_configured", lambda: None)
    return csv_log


def test_dry_run_logt_maar_mailt_niet(monkeypatch):
    csv_log = _patch_pipeline(monkeypatch)
    verzonden = []
    monkeypatch.setattr(notify, "send_alert", lambda e, r=None: verzonden.append(e) or True)

    alerts = main.run(dry_run=True)
    assert alerts == 0 and verzonden == []

    rows = csv_log.read_rows()
    assert len(rows) == 1
    assert rows[0]["alerted"] == "false"
    assert "dry-run" in rows[0]["alert_suppressed_reason"]


def test_gezonde_coin_triggert_mail(monkeypatch):
    csv_log = _patch_pipeline(monkeypatch)
    verzonden = []
    monkeypatch.setattr(notify, "send_alert", lambda e, r=None: (verzonden.append(e), True)[1])
    monkeypatch.setattr(notify, "ensure_configured", lambda: None)
    monkeypatch.setattr(notify, "load_risk_config", dict)

    alerts = main.run()
    assert alerts == 1 and len(verzonden) == 1
    assert csv_log.read_rows()[0]["alerted"] == "true"


def test_cooldown_voorkomt_tweede_mail(monkeypatch):
    csv_log = _patch_pipeline(monkeypatch)
    monkeypatch.setattr(notify, "send_alert", lambda e, r=None: True)
    monkeypatch.setattr(notify, "ensure_configured", lambda: None)
    monkeypatch.setattr(notify, "load_risk_config", dict)

    assert main.run() == 1
    assert main.run() == 0  # tweede run: cooldown actief

    rows = csv_log.read_rows()
    assert len(rows) == 2  # maar er is WEL twee keer gelogd
    assert "cooldown" in rows[1]["alert_suppressed_reason"]


def test_ontbrekende_rugdata_mailt_nooit(monkeypatch):
    csv_log = _patch_pipeline(
        monkeypatch, report=RugcheckReport(mint="MINT", available=False, error="rugcheck down")
    )
    verzonden = []
    monkeypatch.setattr(notify, "send_alert", lambda e, r=None: verzonden.append(e) or True)
    monkeypatch.setattr(notify, "ensure_configured", lambda: None)
    monkeypatch.setattr(notify, "load_risk_config", dict)

    assert main.run() == 0
    assert verzonden == []
    assert csv_log.read_rows()[0]["alerted"] == "false"


def test_een_kapotte_coin_stopt_de_run_niet(monkeypatch):
    import rugcheck

    csv_log = _patch_pipeline(monkeypatch)
    monkeypatch.setattr(notify, "ensure_configured", lambda: None)

    def boom(mint, pair_addresses=None):
        raise RuntimeError("API stuk")

    monkeypatch.setattr(rugcheck, "check_token", boom)
    assert main.run(dry_run=True) == 0
    assert csv_log.read_rows() == []  # niets gelogd, maar ook niet gecrasht


def test_max_alerts_per_run(monkeypatch):
    import data_sources

    _patch_pipeline(monkeypatch)
    monkeypatch.setattr(config, "MAX_ALERTS_PER_RUN", 1)
    monkeypatch.setattr(
        data_sources,
        "discover_candidates",
        lambda limit=None: [make_pair(token_address="A"), make_pair(token_address="B")],
    )
    monkeypatch.setattr(notify, "send_alert", lambda e, r=None: True)
    monkeypatch.setattr(notify, "ensure_configured", lambda: None)
    monkeypatch.setattr(notify, "load_risk_config", dict)
    assert main.run() == 1


def test_claude_key_ontbreekt_faalt_luid(monkeypatch):
    import claude_meta_check

    monkeypatch.setattr(config, "CLAUDE_META_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    with pytest.raises(claude_meta_check.ConfigError) as exc:
        claude_meta_check.ensure_configured()
    assert "ANTHROPIC_API_KEY" in str(exc.value)


def test_mail_ontbrekende_secrets_faalt_luid(monkeypatch):
    monkeypatch.setattr(config, "GMAIL_ADDRESS", "")
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "")
    monkeypatch.setattr(config, "ALERT_RECIPIENT", "")
    with pytest.raises(notify.NotifyError):
        notify.ensure_configured()
