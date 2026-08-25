"""Tests voor de volglijst — het fijnmazig narekenen van gealerteerde munten."""

from __future__ import annotations

import pytest

import config
import data_sources
import watchlist
from tests.conftest import make_pair

TOKEN = "So11111111111111111111111111111111111111112"
T0 = 1_700_000_000.0


@pytest.fixture(autouse=True)
def _eigen_paden(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WATCHLIST_PATH", tmp_path / "watchlist.json")
    monkeypatch.setattr(config, "WATCHLIST_LOG_PATH", tmp_path / "watchlist.csv")
    monkeypatch.setattr(config, "WATCHLIST_ENABLED", True)
    monkeypatch.setattr(config, "WATCHLIST_NOTIFY_ENABLED", False)
    monkeypatch.setattr(config, "WATCHLIST_ALERT_LEVELS", [30.0, 50.0, 100.0])
    monkeypatch.setattr(config, "WATCHLIST_TRACK_HOURS", 12.0)
    monkeypatch.setattr(config, "WATCHLIST_MAX_TOKENS", 60)


def _mock_prijs(monkeypatch, prijs, status="ok"):
    def fake(token_address):
        if status != "ok":
            return [], status
        return [make_pair(token_address=token_address, price_usd=prijs)], "ok"

    monkeypatch.setattr(data_sources, "fetch_pairs_for_token", fake)


# --------------------------------------------------------------------------- #
# Lijst beheren
# --------------------------------------------------------------------------- #


def test_add_zet_instapprijs_vast():
    data = watchlist.add({}, TOKEN, "TEST", 0.001, now=T0)
    assert data[TOKEN]["entry_price_usd"] == 0.001
    assert data[TOKEN]["alert_ts"] == T0
    assert data[TOKEN]["max_pct"] == 0.0


def test_add_negeert_dubbele_en_prijsloze_munten():
    data = watchlist.add({}, TOKEN, "TEST", 0.001, now=T0)
    data = watchlist.add(data, TOKEN, "TEST", 0.002, now=T0 + 60)
    assert data[TOKEN]["entry_price_usd"] == 0.001  # eerste instap blijft staan

    zonder = watchlist.add({}, "AAA", "AAA", None, now=T0)
    assert zonder == {}
    nul = watchlist.add({}, "BBB", "BBB", 0.0, now=T0)
    assert nul == {}


def test_add_respecteert_maximum(monkeypatch):
    monkeypatch.setattr(config, "WATCHLIST_MAX_TOKENS", 2)
    data = {}
    for i in range(4):
        data = watchlist.add(data, f"token{i}", f"T{i}", 0.001, now=T0)
    assert len(data) == 2


def test_prune_haalt_verlopen_munten_weg():
    data = watchlist.add({}, TOKEN, "TEST", 0.001, now=T0)
    blijft, weg = watchlist.prune(dict(data), now=T0 + 3600)
    assert weg == 0 and TOKEN in blijft

    blijft, weg = watchlist.prune(dict(data), now=T0 + 13 * 3600)
    assert weg == 1 and blijft == {}


# --------------------------------------------------------------------------- #
# Meten
# --------------------------------------------------------------------------- #


def test_check_all_meet_rendement_en_schrijft_regel(monkeypatch):
    data = watchlist.add({}, TOKEN, "TEST", 0.001, now=T0)
    _mock_prijs(monkeypatch, 0.0014)

    data, crossings = watchlist.check_all(data, now=T0 + 600)

    assert data[TOKEN]["last_pct"] == pytest.approx(40.0, abs=0.01)
    assert data[TOKEN]["max_pct"] == pytest.approx(40.0, abs=0.01)
    assert data[TOKEN]["samples"] == 1
    assert [c.level for c in crossings] == [30.0]

    regels = config.WATCHLIST_LOG_PATH.read_text(encoding="utf-8").splitlines()
    assert regels[0].startswith("sample_ts_utc,")
    assert len(regels) == 2
    assert TOKEN in regels[1]


def test_niveau_wordt_maar_een_keer_gemeld(monkeypatch):
    data = watchlist.add({}, TOKEN, "TEST", 0.001, now=T0)
    _mock_prijs(monkeypatch, 0.0014)
    data, eerste = watchlist.check_all(data, now=T0 + 600)
    data, tweede = watchlist.check_all(data, now=T0 + 1200)
    assert [c.level for c in eerste] == [30.0]
    assert tweede == []


def test_hoogste_stand_blijft_staan_bij_terugval(monkeypatch):
    data = watchlist.add({}, TOKEN, "TEST", 0.001, now=T0)
    _mock_prijs(monkeypatch, 0.0025)
    data, _ = watchlist.check_all(data, now=T0 + 600)
    assert data[TOKEN]["max_pct"] == pytest.approx(150.0, abs=0.01)

    _mock_prijs(monkeypatch, 0.0005)
    data, _ = watchlist.check_all(data, now=T0 + 1200)
    assert data[TOKEN]["last_pct"] == pytest.approx(-50.0, abs=0.01)
    assert data[TOKEN]["max_pct"] == pytest.approx(150.0, abs=0.01)


def test_api_fout_schrijft_niets_weg(monkeypatch):
    """Bugfix 4.3, opnieuw: een storing is geen -100%."""
    data = watchlist.add({}, TOKEN, "TEST", 0.001, now=T0)
    _mock_prijs(monkeypatch, None, status="error")

    data, crossings = watchlist.check_all(data, now=T0 + 600)

    assert data[TOKEN]["samples"] == 0
    assert "last_pct" not in data[TOKEN]
    assert crossings == []
    assert not config.WATCHLIST_LOG_PATH.exists()


def test_markt_weg_telt_wel_als_meting(monkeypatch):
    data = watchlist.add({}, TOKEN, "TEST", 0.001, now=T0)
    _mock_prijs(monkeypatch, None, status="not_found")

    data, _ = watchlist.check_all(data, now=T0 + 600)

    assert data[TOKEN]["last_pct"] == -100.0
    inhoud = config.WATCHLIST_LOG_PATH.read_text(encoding="utf-8")
    assert "markt weg" in inhoud


def test_run_slaat_over_bij_lege_lijst(monkeypatch):
    assert watchlist.run(dry_run=True, now=T0) == 0


def test_run_dry_run_schrijft_geen_state(monkeypatch):
    data = watchlist.add({}, TOKEN, "TEST", 0.001, now=T0)
    watchlist.save(data)
    _mock_prijs(monkeypatch, 0.0011)

    assert watchlist.run(dry_run=True, now=T0 + 600) == 1
    opnieuw = watchlist.load()
    assert opnieuw[TOKEN]["samples"] == 0  # niet weggeschreven


def test_run_schrijft_state_wel_weg_zonder_dry_run(monkeypatch):
    data = watchlist.add({}, TOKEN, "TEST", 0.001, now=T0)
    watchlist.save(data)
    _mock_prijs(monkeypatch, 0.0011)

    watchlist.run(dry_run=False, now=T0 + 600)
    assert watchlist.load()[TOKEN]["samples"] == 1


def test_mail_staat_standaard_uit_en_is_informatief():
    c = watchlist.Crossing(
        token_address=TOKEN,
        symbol="TEST",
        level=30.0,
        pct_change=42.0,
        minutes_since_alert=25.0,
        price_usd=0.0014,
        market_cap_eur=180_000.0,
    )
    onderwerp, tekst = watchlist.format_crossing(c)
    assert "TEST" in onderwerp
    laag = tekst.lower()
    assert "meting, geen advies" in laag
    for verboden in ("koop", "verkoop nu", "buy", "sell"):
        assert verboden not in laag
