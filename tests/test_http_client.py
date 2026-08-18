"""HTTP-laag: retry, backoff, rate-limit-telling, nooit exceptions."""

from __future__ import annotations

import requests

import config
import http_client


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("geen json")
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.headers = {}

    def request(self, *args, **kwargs):
        self.calls += 1
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _use(monkeypatch, session):
    monkeypatch.setattr(http_client, "get_session", lambda: session)


def test_succes_op_eerste_poging(monkeypatch):
    session = FakeSession([FakeResponse(200, {"ok": True})])
    _use(monkeypatch, session)
    resp = http_client.get_json("https://example.test/a", host_key="t")
    assert resp.ok and resp.data == {"ok": True} and session.calls == 1


def test_retry_bij_timeout_en_daarna_succes(monkeypatch, no_sleep):
    session = FakeSession([requests.Timeout(), FakeResponse(200, {"n": 1})])
    _use(monkeypatch, session)
    resp = http_client.get_json("https://example.test/a", host_key="t")
    assert resp.ok and resp.attempts == 2


def test_max_drie_pogingen(monkeypatch, no_sleep):
    session = FakeSession([requests.Timeout()] * 5)
    _use(monkeypatch, session)
    resp = http_client.get_json("https://example.test/a", host_key="t")
    assert resp.ok is False
    assert session.calls == config.HTTP_MAX_ATTEMPTS == 3
    assert "timeout" in resp.error


def test_429_wordt_geteld_als_rate_limit(monkeypatch, no_sleep):
    session = FakeSession([FakeResponse(429, headers={"Retry-After": "0"})] * 3)
    _use(monkeypatch, session)
    resp = http_client.get_json("https://example.test/a", host_key="dexscreener")
    assert resp.ok is False
    assert http_client.RATE_LIMIT_HITS["dexscreener"] == 3
    assert "dexscreener" in http_client.rate_limit_summary()


def test_404_geeft_lege_maar_geslaagde_respons(monkeypatch):
    session = FakeSession([FakeResponse(404)])
    _use(monkeypatch, session)
    resp = http_client.get_json("https://example.test/a", host_key="t")
    assert resp.ok is True and resp.data is None and resp.extra["not_found"] is True


def test_400_wordt_niet_geretried(monkeypatch, no_sleep):
    session = FakeSession([FakeResponse(400, text="bad"), FakeResponse(200, {"n": 1})])
    _use(monkeypatch, session)
    resp = http_client.get_json("https://example.test/a", host_key="t")
    assert resp.ok is False and session.calls == 1


def test_netwerkfout_gooit_nooit_door(monkeypatch, no_sleep):
    session = FakeSession([requests.ConnectionError("dns kapot")] * 3)
    _use(monkeypatch, session)
    resp = http_client.get_json("https://example.test/a", host_key="t")
    assert resp.ok is False and "netwerkfout" in resp.error
