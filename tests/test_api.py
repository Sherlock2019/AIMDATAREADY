"""HTTP API: endpoints, structured errors, demo vs integration mode, and every labelled scenario."""

import pytest

from app.scenario_runner import load_scenarios, run_scenario
from tests.conftest import TRANSFER_CONTEXT

IDENTITY = {"user": "planner_all", "agent": "scinsight_supervisor"}


def assert_error(resp, status, code, blocked=True):
    body = resp.json()
    assert resp.status_code == status
    assert set(body["error"]) == {"code", "message", "details"} and body["error"]["code"] == code
    assert (body.get("decision") == "BLOCKED") is blocked
    return body


def test_health_reports_demo_mode(client):
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["demo_mode"] is True and body["adapter_mode"] == "demo"
    assert "NOT authentication" in body["authentication"]
    assert body["profiles"] == {"policy_document_qa": "1.0.0", "warehouse_transfer": "1.0.0"}


def test_identify_endpoint(client):
    body = client.post(
        "/tasks/identify", json={"question": "Should we move SKU 001 from warehouse A to warehouse C?"}
    ).json()
    assert body["task_id"] == "warehouse_transfer" and body["status"] == "identified"
    assert "never an authorisation" in body["limitations"]
    body = client.post("/tasks/identify", json={"question": "x", "task_id": "policy_document_qa"}).json()
    assert body["method"] == "explicit_task_id" and body["status"] == "identified"


def test_readiness_ready_and_blocked(client, audit):
    ok = client.post(
        "/readiness/check",
        json={"task_id": "warehouse_transfer", "context": TRANSFER_CONTEXT, "demo_identity": IDENTITY},
    )
    assert ok.status_code == 200 and ok.json()["decision"] == "READY"
    blocked = client.post(
        "/readiness/check",
        json={
            "task_id": "warehouse_transfer",
            "context": TRANSFER_CONTEXT,
            "demo_identity": IDENTITY,
            "demo_scenario": "stale_inventory",
        },
    )
    assert blocked.status_code == 200 and blocked.json()["decision"] == "BLOCKED"
    assert [e["evaluation_id"] for e in audit.entries[-2:]] == [
        ok.json()["evaluation_id"],
        blocked.json()["evaluation_id"],
    ]


@pytest.mark.parametrize(
    "body",
    [
        {},  # task_id missing
        {"task_id": "warehouse_transfer", "context": {"product_id": 5}},  # non-string context value
        {"task_id": "warehouse_transfer", "context": [], "demo_identity": IDENTITY},
        {"task_id": "warehouse_transfer", "demo_identity": {"user": "planner_all", "roles": ["admin"]}},
    ],
)
def test_invalid_input_returns_structured_422(client, body):
    resp = client.post("/readiness/check", json=body)
    details = assert_error(resp, 422, "invalid_request")["error"]["details"]
    assert details and all({"loc", "msg", "type"} == set(d) for d in details)


def test_identity_fields_in_body_are_rejected(client):
    resp = client.post(
        "/readiness/check",
        json={
            "task_id": "warehouse_transfer",
            "context": TRANSFER_CONTEXT,
            "user_id": "admin",
            "roles": ["supply_chain_director"],
        },
    )
    locs = [d["loc"][-1] for d in assert_error(resp, 422, "invalid_request")["error"]["details"]]
    assert sorted(locs) == ["roles", "user_id"]


def test_unknown_profile_unknown_scenario_unknown_identity(client):
    body = assert_error(
        client.post("/readiness/check", json={"task_id": "price_optimisation", "demo_identity": IDENTITY}),
        404,
        "unknown_task_profile",
    )
    assert body["evaluation_id"].startswith("tdr-")
    assert_error(
        client.post(
            "/readiness/check",
            json={"task_id": "warehouse_transfer", "demo_identity": IDENTITY, "demo_scenario": "nope"},
        ),
        400,
        "unknown_demo_scenario",
    )
    assert_error(
        client.post("/readiness/check", json={"task_id": "warehouse_transfer", "demo_identity": {"user": "root"}}),
        400,
        "unknown_demo_identity",
    )


def test_errors_outside_gated_paths_have_same_shape(client):
    assert_error(client.get("/no-such-route"), 404, "http_error", blocked=False)
    assert_error(client.post("/tasks/identify", json={"question": ""}), 422, "invalid_request", blocked=False)


def test_integration_mode_refuses_demo_identity_and_fails_closed(integration_client):
    c = integration_client
    health = c.get("/health").json()
    assert health["demo_mode"] is False and "BLOCKED" in health["authentication"]
    assert_error(
        c.post(
            "/readiness/check",
            json={"task_id": "warehouse_transfer", "context": TRANSFER_CONTEXT, "demo_identity": IDENTITY},
        ),
        400,
        "demo_feature_disabled",
    )
    assert_error(
        c.post(
            "/readiness/check",
            json={"task_id": "warehouse_transfer", "context": TRANSFER_CONTEXT, "demo_scenario": "stale_inventory"},
        ),
        400,
        "demo_feature_disabled",
    )
    r = c.post("/readiness/check", json={"task_id": "warehouse_transfer", "context": TRANSFER_CONTEXT}).json()
    assert r["decision"] == "BLOCKED" and r["identity_mode"] == "trusted_provider:not_configured"
    assert r["authenticated"] is False
    # A spoofed identity header does nothing: no trusted provider is configured.
    r = c.post(
        "/readiness/check",
        json={"task_id": "warehouse_transfer", "context": TRANSFER_CONTEXT},
        headers={"X-User-Id": "demo-user-planner-all", "X-Agent-Id": "demo-agent-scinsight-supervisor"},
    ).json()
    assert r["decision"] == "BLOCKED"


def test_evaluate_reports_where_context_came_from(client):
    body = client.post(
        "/evaluate",
        json={
            "question": "Should we move SKU 001 from warehouse A to warehouse C?",
            "context": {"planning_period": "2026-11-16/2026-11-29"},
            "demo_identity": IDENTITY,
        },
    ).json()
    assert body["decision"] == "READY"
    assert body["context_sources"] == {
        "product_id": "router_suggestion",
        "origin_warehouse": "router_suggestion",
        "destination_warehouse": "router_suggestion",
        "planning_period": "request",
    }


@pytest.mark.parametrize("scenario", load_scenarios(), ids=lambda s: s["name"])
def test_labelled_scenarios(client, scenario):
    result = run_scenario(client, scenario)
    assert result["problems"] == [], result["body"]
