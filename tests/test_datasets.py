"""Faulty-dataset lab: 10 injected faults are detected from the data, corrected without inventing values, re-checked."""

import hashlib
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.adapters.demo import DemoAdapter
from app.dataset_lab import DatasetLab
from app.identity import resolve_demo_identity
from app.main import create_app
from app.models import DemoIdentity
from app.profiles import load_profile
from app.remediation import validate_proposal
from app.settings import ROOT, Settings
from tests.conftest import NOW

CASES = sorted(p.name for p in (ROOT / "datasets" / "cases").iterdir())
FAULTY = [c for c in CASES if c != "00_clean"]


def digest(folder: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(folder.rglob("*")):
        if p.is_file():
            h.update(p.name.encode() + p.read_bytes())
    return h.hexdigest()


@pytest.fixture
def lab(tmp_path):
    authz = DemoAdapter.from_dir(ROOT / "catalog")
    return DatasetLab(ROOT / "datasets", tmp_path / "work", authz, lambda: NOW, None)


@pytest.fixture
def who(lab):
    def make(user="planner_all"):
        u, a = resolve_demo_identity(DemoIdentity(user=user, agent="scinsight_supervisor"), lab.authz.directory)
        return u, a, "demo_selector"

    return make


def fault(case: str) -> dict:
    return yaml.safe_load((ROOT / "datasets" / "cases" / case / "fault.yaml").read_text())


def test_ten_faulty_cases_and_one_clean_control():
    assert len(FAULTY) == 10 and "00_clean" in CASES


def test_clean_case_is_ready_with_nothing_to_fix(lab, who):
    r = lab.auto_correct("00_clean", *who())
    assert r["before"]["decision"] == "READY" and r["after"]["decision"] == "READY" and r["passes"] == []


@pytest.mark.parametrize("case", FAULTY)
def test_fault_is_detected_from_the_data(lab, who, case):
    summary = lab.check(case, *who())["summary"]
    assert summary["decision"] == "BLOCKED"
    failing = {
        f"{r}:{c}"
        for r, cells in summary["matrix"].items()
        for c, cell in cells.items()
        if cell["status"] in ("FAIL", "ERROR")
    }
    assert set(fault(case)["expected_detection"]) <= failing, failing


@pytest.mark.parametrize("case", FAULTY)
def test_fault_is_corrected_and_rechecked_ready(lab, who, case):
    original = digest(ROOT / "datasets" / "cases" / case)
    r = lab.auto_correct(case, *who())
    applied = [c["action"] for p in r["passes"] for c in p["changes"] if c.get("status") == "applied"]
    assert set(fault(case)["expected_fix"]) <= set(applied), applied
    assert r["after"]["decision"] == "READY" and r["needs_human"] == []
    assert digest(ROOT / "datasets" / "cases" / case) == original  # the faulty original is never modified
    log = yaml.safe_load((lab.work_root / case / "manifest.yaml").read_text())["remediation_log"]
    assert log and all(entry["source"] for entry in log)  # every change says where its data came from


def test_fixes_use_source_values_not_guesses(lab, who):
    lab.auto_correct("05_negative_quantity", *who())
    rows = {r["batch_id"]: r for r in lab.remediator._source("sap_inventory_snapshot")[2]}
    fixed = (lab.work_root / "05_negative_quantity" / "inventory.csv").read_text()
    assert "-120" not in fixed and "-40" not in fixed
    assert f"A-001-2,{rows['A-001-2']['quantity']}," in fixed.replace("SKU-001,WH-A,", "")


def test_missing_owner_is_filled_from_registry_and_noted(lab, who):
    lab.auto_correct("10_missing_owner", *who())
    meta = yaml.safe_load((lab.work_root / "10_missing_owner" / "manifest.yaml").read_text())["assets"]
    inv = meta["sap_inventory_snapshot"]
    assert inv["business_owner"] == "Demo Inventory Control (fictional)"
    assert "steward registry" in inv["provenance_notes"][0]


def test_date_reformat_requires_a_declared_format(lab, who, tmp_path):
    lab.reset("08_bad_date_format")
    manifest_path = lab.work_root / "08_bad_date_format" / "manifest.yaml"
    m = yaml.safe_load(manifest_path.read_text())
    m["assets"]["sap_inventory_snapshot"].pop("export_settings")
    manifest_path.write_text(yaml.safe_dump(m))
    out = lab.apply(
        "08_bad_date_format",
        [{"requirement": "inventory", "action": "reformat_dates", "column": "expiry_date"}],
        *who(),
    )
    assert out["changes"][0]["status"] == "skipped" and out["after"]["decision"] == "BLOCKED"


def test_access_problems_are_escalated_not_fixed(lab, who):
    r = lab.auto_correct("02_missing_warehouse_c", *who("planner_ab"))
    assert r["after"]["decision"] == "BLOCKED" and r["passes"] == []
    assert r["needs_human"] and all(p["action"] == "escalate" for p in r["needs_human"])
    out = lab.apply(
        "02_missing_warehouse_c",
        [{"requirement": "inventory", "action": "reextract_rows", "column": "warehouse_id", "values": ["WH-C"]}],
        *who("planner_ab"),
    )
    assert out["changes"] == []  # nothing is changed for a caller without access


def test_allow_list_rejects_unknown_or_malformed_proposals(lab):
    profile = lab.profile
    for raw in (
        {"requirement": "inventory", "action": "delete_everything"},
        {"requirement": "nope", "action": "deduplicate"},
        {"requirement": "inventory", "action": "normalise_codes", "column": "quantity"},
        {"requirement": "inventory", "action": "reextract_rows", "column": "warehouse_id", "values": ["../x y"]},
        {"requirement": "inventory", "action": "lookup_owner", "values": ["source_version"]},
        {"requirement": "inventory", "action": "deduplicate", "surprise": 1},
    ):
        p, err = validate_proposal(raw, profile)
        assert p is None and err


def test_dataset_profile_only_adds_quality_rules():
    base = load_profile(ROOT / "profiles" / "warehouse_transfer.yaml").model_dump(by_alias=True)
    lab_profile = load_profile(ROOT / "datasets" / "profiles" / "warehouse_transfer.yaml").model_dump(by_alias=True)
    for p in (base, lab_profile):
        p.pop("version"), p.pop("demo_values_note")
        for a in p["assets"]:
            a.pop("quality")
    assert base == lab_profile


# ------------------------------------------------------------------ API + dashboard


@pytest.fixture
def api(tmp_path):
    return TestClient(create_app(Settings(audit_path=None, datasets_work_dir=tmp_path / "w"), clock=lambda: NOW))


ID = {"user": "planner_all", "agent": "scinsight_supervisor"}


def test_api_check_propose_apply_recheck(api):
    cases = api.get("/demo/datasets").json()
    assert len(cases["cases"]) == 11 and "configured" in cases["claude"]
    assert (
        api.post("/demo/datasets/04_duplicate_rows/check", json={"demo_identity": ID}).json()["summary"]["decision"]
        == "BLOCKED"
    )
    props = api.post("/demo/datasets/04_duplicate_rows/propose", json={"demo_identity": ID}).json()["proposals"]
    assert [p["action"] for p in props] == ["deduplicate"]
    out = api.post("/demo/datasets/04_duplicate_rows/apply", json={"demo_identity": ID, "proposals": props}).json()
    assert out["before"]["decision"] == "BLOCKED" and out["after"]["decision"] == "READY"
    assert (
        api.post("/demo/datasets/04_duplicate_rows/check", json={"demo_identity": ID, "target": "original"}).json()[
            "summary"
        ]["decision"]
        == "BLOCKED"
    )  # original untouched


def test_api_run_all_and_dashboard(api):
    results = api.post("/demo/datasets/run-all", json={"demo_identity": ID}).json()["results"]
    assert [r["after"] for r in results] == ["READY"] * 11
    assert sum(r["before"] == "BLOCKED" for r in results) == 10
    assert "Data Readiness Lab" in api.get("/lab").text  # the earlier lab moved to /lab
    for path in ("/", "/dashboard", "/table"):  # the main UI is the data readiness table
        page = api.get(path)
        assert page.status_code == 200 and "Task Data Readiness" in page.text


def test_api_errors(api):
    r = api.post("/demo/datasets/99_nope/check", json={"demo_identity": ID})
    assert r.status_code == 404 and r.json()["error"]["code"] == "unknown_dataset_case"
    r = api.post("/demo/datasets/..%2Fcatalog/check", json={"demo_identity": ID})
    assert r.status_code == 404
    out = api.post(
        "/demo/datasets/04_duplicate_rows/apply",
        json={"demo_identity": ID, "proposals": [{"requirement": "inventory", "action": "rm -rf"}]},
    ).json()
    assert out["changes"] == [] and out["rejected"]


def test_dataset_lab_not_available_outside_demo_mode(tmp_path):
    c = TestClient(
        create_app(Settings(mode="integration", audit_path=None, datasets_work_dir=tmp_path), clock=lambda: NOW)
    )
    assert c.get("/dashboard").status_code == 404
    assert c.get("/demo/datasets").json()["error"]["code"] == "not_available"
