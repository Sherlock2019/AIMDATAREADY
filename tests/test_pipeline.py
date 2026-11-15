"""Data Harness over the simulated SAP landscape (called by the Flowise Agentflow)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.main import create_app
from app.pipeline import PipelineService
from app.settings import ROOT, Settings
from tests.conftest import NOW

SC_REPO = Path("/home/dzoan/Supplychain-AI-agent")
DIR, WH, T = "scinsight:supply_chain_director", "scinsight:warehouse_manager", "recommend_stock_transfer"


@pytest.fixture
def svc(tmp_path):
    return PipelineService(ROOT / "pipeline", tmp_path, lambda: NOW)


def run(svc, faults, role=DIR, task=T):
    svc.extract(faults)
    return svc.check(role, task)


def test_roles_cover_every_role_defined_in_the_supply_chain_repo(svc):
    cfg = yaml.safe_load((ROOT / "pipeline" / "roles.yaml").read_text())
    assert len(svc.roles) == 28
    if not SC_REPO.exists():
        pytest.skip("supply-chain repo not present")
    for app in cfg["apps"]:
        for r in app["roles"]:
            assert f'"{r["id"]}"' in (SC_REPO / r.get("source", app["source"])).read_text(), r["id"]


def test_catalog_and_contracts_hold_every_required_field(svc):
    dynamic = ("source_version", "source_event_time", "ingested_at")  # filled from the extraction manifest
    for asset, record in svc.catalog.items():
        extra = svc.cfg["document_catalog_fields"] if asset == "ariba_contracts" else []
        static = [f for f in svc.cfg["catalog_required_fields"] + extra if f not in dynamic]
        assert not [f for f in static if not record.get(f)], asset
        assert not [s for s in svc.cfg["contract_required_sections"] if not svc.contracts[asset].get(s)], asset


def test_successful_authorized_request(svc):
    r = run(svc, "none")
    assert r["decision"] == "READY" and r["score"] == 100 and r["evidence_refs"]
    assert "not authorization to act" in r["text"] and r["latency_ms"] > 0
    m = r["data_metrics"]["s4_mm"]
    assert m["source_data_age_hours"] == 2.0 and m["required_field_completeness_pct"] == 100.0
    assert m["task_coverage"] == "2/2" and m["contract_validation"] == "pass"


@pytest.mark.parametrize(
    "faults, role, task, needle",
    [
        ("mm_stale", DIR, T, "stale: 74 h old (max 2 h)"),
        ("ewm_missing_wh_c", DIR, "weekly_executive_report", "no data for WH-C"),
        ("tm_missing_lane", DIR, T, "no data for WH-A>WH-C"),
        ("none", "harness:planner", T, "you do not have access to WH-C data"),
        ("authz_down", DIR, T, "authorization service did not answer"),
        ("none", WH, "recommend_replenishment", "you do not have access to column price_eur"),
        ("contract_expired", DIR, "supplier_policy_qa", "no approved, currently valid document"),
        ("mdg_catalog_incomplete", DIR, "forecast_demand", "incomplete catalog/contract: data_steward, contact"),
    ],
)
def test_blocked_cases_explain_why(svc, faults, role, task, needle):
    r = run(svc, faults, role, task)
    assert r["decision"] == "BLOCKED" and needle in r["text"]


def test_lead_time_missing_offers_the_safe_partial_output(svc):
    r = run(svc, ["tm_missing_lane", "ariba_missing_dates"])
    assert r["decision"] == "BLOCKED" and "inventory comparison WH-A vs WH-C" in r["text"]
    assert "⚠ 1 empty values in due_date" in r["table"]  # optional open POs: warning, not a block


def test_optional_asset_problem_is_only_a_warning(svc):
    assert run(svc, "ariba_missing_dates")["decision"] == "READY_WITH_WARNINGS"


def test_same_stale_data_fails_the_transfer_and_the_report_thresholds_differ(svc):
    svc.extract("mm_stale")
    by_task = svc.check(DIR, "weekly_executive_report")["by_task"]
    assert by_task["recommend_stock_transfer"] == "BLOCKED" and by_task["forecast_demand"] == "READY"


def test_restricted_data_sets_are_not_named(svc):
    r = run(svc, "none", "scinsight:sap_executor")
    assert "Restricted data set" in r["table"] and "Inventory snapshot" not in r["table"]
    assert "you do not have access to 3 required data set(s)" in r["text"]


def test_detect_propose_fix_loop_over_rounds(svc):
    svc.extract("all")
    assert svc.check(WH, T)["score"] < 100
    svc.propose(WH, T)
    r1 = svc.apply(WH, T)  # inventory + forecast fixes only
    assert r1["score_after"] > r1["score_before"] and r1["waiting"]
    assert svc.extract("all")["prevented"]
    assert svc.apply(DIR, T)["after"]["decision"] == "READY"
    assert svc.extract("all")["prevented"] and svc.check(DIR, T)["ready"]


def test_agents_cannot_approve(svc):
    svc.extract("all")
    out = svc.apply("scinsight:agent_service", T)
    assert out["applied"] == [] and all("agent can never approve" in w for w in out["waiting"] if "needs approval" in w)


def test_evaluation_records_and_metrics(svc):
    run(svc, "none")
    run(svc, "mm_stale")
    m = svc.metrics()
    assert m["evaluations"] == 2 and m["decisions"] == {"READY": 1, "BLOCKED": 1}
    last = m["last_evaluation"]
    assert last["task_profile_version"] == "1.0.0" and last["latency_ms"] > 0
    assert all("duration_ms" in c for c in last["checks"]) and "question" not in json.dumps(last)
    view = svc.catalog_view("s4_mm_stock")
    assert view["catalog_record"]["data_steward"] and view["computed"]["quality_check_history"]


def test_flowise_flow_has_task_role_and_fault_dropdowns():
    flow = json.loads((ROOT / "flowise" / "Data-Readiness-Agentflow-v1.json").read_text())
    start = next(n for n in flow["nodes"] if n["data"]["name"] == "startAgentflow")
    form = {f["name"]: [o["option"] for o in f["addOptions"]] for f in start["data"]["inputs"]["formInputTypes"]}
    assert (
        len(form["task"]) == 14
        and form["task"][0] == "my_role"
        and len(form["role"]) == 28
        and form["faults"][:2] == ["all", "none"]
    )
    human = next(n for n in flow["nodes"] if n["data"]["name"] == "humanInputAgentflow")
    assert {e["data"].get("edgeLabel") for e in flow["edges"] if e["source"] == human["id"]} == {"proceed", "reject"}


def test_pipeline_api(tmp_path):
    c = TestClient(create_app(Settings(audit_path=None, pipeline_work_dir=tmp_path), clock=lambda: NOW))
    assert len(c.get("/pipeline/profiles").json()) == 13
    c.post("/pipeline/extract", json={"faults": "tm_missing_lane"})
    assert c.post("/pipeline/check", json={"role": DIR, "task": T}).json()["decision"] == "BLOCKED"
    r = c.post("/pipeline/check", json={"role": DIR, "task": "nope"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "unknown_role_task_or_fault"
    assert c.get("/pipeline/metrics").json()["evaluations"] == 1
    view = c.get("/pipeline/role-view", params={"role": WH, "task": "compare_stock"}).json()
    assert [t["id"] for t in view["tasks"]] == ["compare_stock", "recommend_stock_transfer", "read_transfer_policy"]
    assert c.post("/pipeline/retrieve", json={"role": DIR, "task": T}).json()["retrieved"] is False  # lane missing
    agents = {a["id"]: a for a in c.get("/pipeline/agents").json()}
    assert agents["mock"]["available"] and "claude" in agents
    assert "Task Data Readiness" in c.get("/dashboard").text
    # step 5: neutral data sets - nothing tells the user whether one is faulty before the checks run
    ds = c.get("/pipeline/datasets").json()
    assert len(ds) >= 8 and all(d["short"] == f"{d['id']} · not tested" and d["status"] == "NOT_TESTED" for d in ds)
    assert all("faults" not in d and "fault" not in d["label"] for d in ds)
    out = c.post("/pipeline/extract", json={"dataset": ds[0]["id"]}).json()
    assert "faults" not in out and "fault" not in out["text"]
    assert c.post("/pipeline/extract", json={"dataset": "DS-99"}).status_code == 400


SCINSIGHT_ROLES = [
    "planner",
    "production_planner",
    "procurement_manager",
    "warehouse_manager",
    "logistics_manager",
    "quality_manager",
    "supply_chain_director",
    "agent_service",
    "mlops_engineer",
    "sap_executor",
]


def test_every_role_maps_to_tasks_and_each_task_to_one_profile(svc):
    """Role -> Task -> Profile for all 28 roles; my_role = the role's first task."""
    for key in svc.roles:
        tasks = [t["id"] for t in svc.role_tasks(key)]
        assert tasks and all(t in svc.harness for t in tasks), key
        assert svc.resolve(key, "my_role") == tasks[0]
    assert len(svc.harness) == len(svc.profiles) == 13
    # same role, different tasks, different profiles
    assert [t["id"] for t in svc.role_tasks(WH)] == [
        "compare_stock",
        "recommend_stock_transfer",
        "read_transfer_policy",
    ]
    assert svc.harness["compare_stock"]["required_assets"].keys() == {"s4_mm_stock"}
    assert "tm_lanes" in svc.harness["recommend_stock_transfer"]["required_assets"]


def test_scinsight_roles_first_task_on_clean_data(svc):
    svc.extract("none")
    for role in SCINSIGHT_ROLES:
        decision = svc.check(f"scinsight:{role}", "my_role")["decision"]
        assert decision == ("BLOCKED" if role == "sap_executor" else "READY"), role  # sap_executor reads nothing


@pytest.mark.parametrize(
    "role, task, system, reason",
    [
        ("harness:planner", "compare_stock", "S/4HANA MM", "no access to warehouse WH-C"),  # row-level ACL
        ("scinsight:planner", "recommend_replenishment", "SAP Ariba", "no access to column price_eur"),  # column
        ("aurelia:demand", "forecast_demand", "SAP MDG", "the role may not read this data set"),  # data set
    ],
)
def test_allowed_vs_required_blocks_on_clean_data(svc, role, task, system, reason):
    """Clean data is not enough: the engine never falls back to a weaker profile when ALLOWED < REQUIRED."""
    svc.extract("none")
    by = {s["system"]: s for s in svc.role_view(role, task)["sources"]}
    assert by[system]["user_reason"] == reason and not by[system]["allowed"] and by[system]["agent_authorized"]
    assert svc.check(role, task)["decision"] == "BLOCKED"


def test_role_view_stages_3_and_4(svc):
    v = svc.role_view("scinsight:production_planner", "check_production_readiness")
    assert v["profile"]["id"] == "check_production_readiness" and v["profile"]["agent"] == "agent:planning-agent"
    by = {s["system"]: s for s in v["sources"]}
    assert set(by) == {"S/4HANA PP", "S/4HANA MM", "SAP QM", "SAP MDG"}
    assert all(s["user_authorized"] and s["agent_authorized"] and s["allowed"] for s in by.values())
    assert by["S/4HANA PP"]["can_approve"] and not by["SAP QM"]["can_approve"]
    assert any(r.startswith("fresh ≤ 48 h") for r in by["S/4HANA PP"]["rules"])


def test_stage_5_checklist_lists_every_ai_readiness_metric(svc):
    svc.extract("none")
    rows = svc.check(WH, "compare_stock")["checklist"][0]["rows"]
    metrics = {r["metric"] for r in rows}
    for m in ("Who owns the data?", "Which SAP system or file did it come from?", "When was it last refreshed?",
              "Does it cover the requested warehouses, products and period?", "Are units consistent?",
              "Has the user been authorized to use it?", "Has the agent been authorized to use it?",
              "Have the relevant quality checks passed?"):  # fmt: skip
        assert m in metrics, m
    assert not [r for r in rows if r["status"] in ("FAIL", "ERROR")]


@pytest.mark.parametrize(
    "faults, metric, needle",
    [
        ("mm_unit_mismatch", "Are units consistent?", "which the contract does not allow (EA, CS)"),
        ("mm_stale", "When was it last refreshed?", "did not deliver a newer snapshot"),
    ],
)
def test_root_cause_comes_from_measured_evidence(svc, faults, metric, needle):
    svc.extract(faults)
    rows = svc.check(WH, T)["checklist"][0]["rows"]
    row = next(r for r in rows if r["metric"] == metric)
    assert row["status"] == "FAIL" and needle in row["cause"] and row["fix"]
    assert all(r["cause"] == "" for r in rows if r["status"] == "PASS")


def test_stage_6_ready_retrieves_only_authorized_rows_and_columns(svc):
    svc.extract("none")
    r = svc.retrieve(WH, "compare_stock")
    assert r["retrieved"] and "stock comparison for SKU-001" in r["text"] and "| WH-A | 1250 | 1250 |" in r["text"]
    assert [s["records"] for s in r["sources"]] == [4]  # SKU-001 in WH-A and WH-C only, not WH-B
    t = {s["system"]: s for s in svc.retrieve(WH, T)["sources"]}
    assert t["SAP Ariba"]["hidden_columns"] == ["price_eur"]  # the warehouse manager may not see prices
    assert svc.retrieve(DIR, T)["sources"][2]["hidden_columns"] == []  # the director may
    assert "transfer recommendation for SKU-001" in svc.retrieve(DIR, T)["text"]


def test_stage_6_blocked_refuses_retrieval_and_explains(svc):
    svc.extract("mm_stale")
    r = svc.retrieve(WH, T)
    assert r["retrieved"] is False and "Retrieval refused" in r["text"] and "stale: 74 h old (max 2 h)" in r["text"]


def test_mock_agent_lists_each_missing_metric_with_its_fix(svc):
    svc.extract(["mm_stale", "tm_missing_lane"])
    out = svc.propose(WH, T)
    assert out["agent"] == "mock" and "Mock correction agent" in out["text"]
    assert "| # | Data set | Missing data metric | Proposed fix | Data from | Who approves |" in out["text"]
    assert "stale: 74 h old (max 2 h)" in out["text"] and "Reload from the SAP source of record" in out["text"]
    assert "no data for WH-A>WH-C" in out["text"] and "logistics_manager or supply_chain_director" in out["text"]
    restricted = svc.propose("scinsight:sap_executor", "my_role")["text"]
    assert "Restricted data set" in restricted and "Inventory snapshot" not in restricted


def test_claude_agent_without_credentials_says_how_to_enable(svc, monkeypatch, tmp_path):
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    svc.extract("mm_stale")
    out = svc.propose(WH, T, agent="claude")
    assert out["agent"] == "claude" and not out["available"] and out["proposals"] == []
    assert "not configured" in out["text"] and svc.proposals == []


def test_claude_agent_proposals_use_the_same_table_and_approval(svc):
    """STUB client: proves the wiring and the allow-list, not how the real model behaves."""
    from tests.test_claude_agent import StubClient

    svc.extract("mm_stale")
    svc.claude_client = StubClient(
        [
            ("get_failures", {}),
            ("propose_fix", {"requirement": "inventory", "action": "refresh_from_source", "rationale": "stale"}),
            ("propose_fix", {"requirement": "inventory", "action": "drop_table", "rationale": "not allowed"}),
        ]
    )
    out = svc.propose(WH, T, agent="claude")
    assert out["agent"] == "claude" and "Claude agent" in out["text"] and len(out["proposals"]) == 1
    assert "stale: 74 h old (max 2 h)" in out["text"]
    assert svc.apply(WH, T)["after"]["decision"] == "READY"  # the human approves; the engine re-checks


def test_evaluation_script_reports_zero_unsafe_passes():
    out = subprocess.run([sys.executable, "scripts/evaluate.py"], cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "unsafe passes 0" in out.stdout and "false blocks 0" in out.stdout
