"""Email access gate for the public demo (app/access_gate.py)."""

from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings
from tests.conftest import NOW

VISITOR = {"name": "Ada Lovelace", "email": "Ada@Example.com", "company": "Analytical Engines"}


def _client(tmp_path, gate: bool, smtp: bool = True):
    app = create_app(
        Settings(audit_path=None, pipeline_work_dir=tmp_path / "p", access_gate=gate, data_dir=tmp_path / "d"),
        clock=lambda: NOW,
    )
    g = app.state.access_gate
    g.smtp_user, g.smtp_pass = ("demo@example.com", "x") if smtp else ("", "")
    g.notify_to = "owner@example.com"
    g.outbox = []
    g.send = g.outbox.append  # no real email in tests
    return TestClient(app), g


def _code(gate) -> str:
    msg = next(m for m in gate.outbox if m["To"] == "ada@example.com")
    return re.search(r"\b(\d{6})\b", msg["Subject"]).group(1)


def test_gate_off_changes_nothing(tmp_path):
    c, _ = _client(tmp_path, gate=False)
    assert c.get("/access/status").json()["verified"] is True
    assert c.get("/pipeline/role-view", params={"role": "scinsight:warehouse_manager"}).status_code == 200


def test_gate_on_keeps_the_landing_page_public_and_locks_the_demo(tmp_path):
    c, _ = _client(tmp_path, gate=True)
    for path in ("/", "/dashboard", "/health", "/pipeline/roles", "/pipeline/profiles", "/pipeline/agents"):
        assert c.get(path).status_code == 200, path
    assert c.get("/access/status").json() == {
        "gate": True,
        "verified": False,
        "name": None,
        "email": None,
        "available": True,
    }
    for path in ("/pipeline/role-view?role=scinsight:warehouse_manager", "/docs", "/lab"):
        r = c.get(path)
        assert r.status_code == 401 and r.json()["error"]["code"] == "access_required", path
    assert c.post("/pipeline/check", json={}).status_code == 401


def test_email_code_unlocks_the_demo_for_30_days_and_logs_the_lead(tmp_path):
    c, g = _client(tmp_path, gate=True)
    assert c.post("/access/request", json=VISITOR).json() == {"sent": True}
    code = _code(g)
    wrong = "000000" if code != "000000" else "111111"
    assert c.post("/access/verify", json={**VISITOR, "code": wrong}).json()["error"]["code"] == "code_wrong"
    r = c.post("/access/verify", json={**VISITOR, "code": code})
    assert r.status_code == 200 and r.json()["email"] == "ada@example.com"
    assert "max-age=2592000" in r.headers["set-cookie"].lower() and "httponly" in r.headers["set-cookie"].lower()
    status = c.get("/access/status").json()
    assert status["verified"] and status["name"] == "Ada Lovelace"
    assert c.get("/pipeline/role-view", params={"role": "scinsight:warehouse_manager"}).status_code == 200
    lead = json.loads((tmp_path / "d" / "leads.jsonl").read_text().splitlines()[0])
    assert lead["email"] == "ada@example.com" and lead["company"] == "Analytical Engines" and lead["type"] == "verified"
    # a code works once; the same visitor asking again is let straight in
    assert c.post("/access/verify", json={**VISITOR, "code": code}).json()["error"]["code"] == "code_expired"
    assert c.post("/access/request", json=VISITOR).json() == {"verified": True}
    c.post("/access/logout")
    assert c.get("/pipeline/role-view", params={"role": "scinsight:warehouse_manager"}).status_code == 401


def test_bad_input_throttling_and_tampering_are_refused(tmp_path):
    c, g = _client(tmp_path, gate=True)
    assert c.post("/access/request", json={**VISITOR, "name": "A"}).status_code == 422
    assert c.post("/access/request", json={**VISITOR, "email": "not-an-email"}).status_code == 422
    assert c.post("/access/request", json={**VISITOR, "email": "x@mailinator.com"}).status_code == 422
    r = c.post("/access/request", json=VISITOR, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    for _ in range(3):
        assert c.post("/access/request", json=VISITOR).status_code == 200
    assert c.post("/access/request", json=VISITOR).status_code == 429
    c.cookies.set("tdr_access", g.sign({"name": "x", "email": "x@y.io", "company": "", "exp": 0}))  # expired
    assert c.get("/pipeline/role-view", params={"role": "scinsight:warehouse_manager"}).status_code == 401
    c.cookies.set("tdr_access", "eyJ4IjoxfQ.forged")
    assert c.get("/pipeline/role-view", params={"role": "scinsight:warehouse_manager"}).status_code == 401


@pytest.mark.parametrize("tries", [5])
def test_code_locks_after_too_many_wrong_tries(tmp_path, tries):
    c, g = _client(tmp_path, gate=True)
    c.post("/access/request", json=VISITOR)
    code = _code(g)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(tries):
        c.post("/access/verify", json={**VISITOR, "code": wrong})
    assert c.post("/access/verify", json={**VISITOR, "code": code}).json()["error"]["code"] == "code_expired"


def test_without_smtp_the_gate_says_codes_are_unavailable(tmp_path):
    c, _ = _client(tmp_path, gate=True, smtp=False)
    assert c.get("/access/status").json()["available"] is False
    assert c.post("/access/request", json=VISITOR).status_code == 503
