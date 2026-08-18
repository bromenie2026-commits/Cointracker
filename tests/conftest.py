"""Gedeelde testfixtures. Alle API-calls zijn gemockt — geen netwerk in tests."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import http_client  # noqa: E402
from models import DeployerReputation, NarrativeCheck, PairData, RugcheckReport  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """Elke test krijgt eigen log/state-paden, zodat er niets lekt."""
    monkeypatch.setattr(config, "SCAN_LOG_PATH", tmp_path / "scan_log.csv")
    monkeypatch.setattr(config, "DEDUP_STATE_PATH", tmp_path / "dedup.json")
    monkeypatch.setattr(config, "HOLDER_HISTORY_PATH", tmp_path / "holders.json")
    http_client.reset_counters()
    yield


@pytest.fixture
def no_sleep(monkeypatch):
    """Backoff-sleeps overslaan zodat retry-tests snel zijn."""
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    monkeypatch.setattr(http_client.time, "sleep", lambda _s: None)


def make_pair(**overrides) -> PairData:
    """Een gezonde pair die alle harde marktfilters haalt."""
    now_ms = int(time.time() * 1000)
    defaults = dict(
        token_address="So11111111111111111111111111111111111111112",
        pair_address="PAIR1111111111111111111111111111111111111",
        dex_id="raydium",
        symbol="TEST",
        name="Test Coin",
        price_usd=0.00042,
        market_cap_usd=120_000.0,
        fdv_usd=120_000.0,
        liquidity_usd=30_000.0,
        volume_h1_usd=8_000.0,
        volume_h24_usd=90_000.0,
        buys_h1=120,
        sells_h1=100,
        buys_h24=1_400,
        sells_h24=1_300,
        pair_created_at_ms=now_ms - 6 * 3600 * 1000,
        url="https://dexscreener.com/solana/PAIR1",
        websites=["https://testcoin.xyz"],
        socials=[{"type": "twitter", "url": "https://x.com/testcoin"}],
    )
    defaults.update(overrides)
    return PairData(**defaults)


def make_report(**overrides) -> RugcheckReport:
    """Een rugcheck-rapport dat alle vier de rug-vectoren haalt."""
    defaults = dict(
        mint="So11111111111111111111111111111111111111112",
        available=True,
        mint_authority_renounced=True,
        freeze_authority_renounced=True,
        lp_locked_pct=99.5,
        lp_locked_or_burned=True,
        honeypot_ok=True,
        rugged=False,
        score=200,
        top_holders_pct=18.0,
        largest_holder_pct=5.0,
        total_holders=800,
        creator="Creator11111111111111111111111111111111111",
        source="rugcheck",
    )
    defaults.update(overrides)
    return RugcheckReport(**defaults)


def make_deployer(**overrides) -> DeployerReputation:
    defaults = dict(
        wallet="Creator11111111111111111111111111111111111",
        available=True,
        previous_deploys=2,
        dead_deploys=0,
        dead_ratio=0.0,
    )
    defaults.update(overrides)
    return DeployerReputation(**defaults)


def make_narrative(**overrides) -> NarrativeCheck:
    defaults = dict(available=True, score=70.0, verdict="ok", reasoning="")
    defaults.update(overrides)
    return NarrativeCheck(**defaults)


def make_social(**overrides) -> dict:
    defaults = {"available": True, "handle": "testcoin", "age_days": 400.0}
    defaults.update(overrides)
    return defaults
