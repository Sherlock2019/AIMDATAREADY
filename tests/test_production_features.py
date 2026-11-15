"""ODCS catalog store, OpenLineage events and permission-aware document search."""

import copy
import shutil

import pytest
import yaml
from fastapi.testclient import TestClient

from app.main import create_app
from app.odcs import OdcsCatalog, from_odcs, to_odcs, validate
from app.pipeline import PipelineService
from app.rag import DocumentIndex, HashingEmbedding
from app.settings import ROOT, Settings
from tests.conftest import NOW

ODCS = ROOT / "pipeline" / "odcs"
WH, PROC, DIR = "scinsight:warehouse_manager", "scinsight:procurement_manager", "scinsight:supply_chain_director"
KPV = "knowledge:kp_viewer"


@pytest.fixture
def svc(tmp_path):
    return PipelineService(ROOT / "pipeline", tmp_path, lambda: NOW)


# ------------------------------------------------------------------ 1. one catalog, contracts in ODCS
def test_every_odcs_file_is_valid_against_the_official_schema():
    store = OdcsCatalog(ODCS)
    assert len(store.catalog) == 10 and all(errors == [] for errors in store.errors.values())


def test_odcs_round_trip_is_lossless():
    for path in sorted(ODCS.glob("*.odcs.yaml")):  # source of truth -> internal -> ODCS
        doc = yaml.safe_load(path.read_text())
        assert to_odcs(*from_odcs(doc)) == doc, path.name
    legacy_cat = yaml.safe_load((ROOT / "pipeline" / "catalog.yaml").read_text())["assets"]
    legacy_con = yaml.safe_load((ROOT / "pipeline" / "contracts.yaml").read_text())["contracts"]
    for asset in legacy_cat:  # legacy YAML -> ODCS -> internal (the migration)
        assert from_odcs(to_odcs(asset, legacy_cat[asset], legacy_con[asset])) == (
            asset, legacy_cat[asset], legacy_con[asset])  # fmt: skip


def test_schema_validation_catches_a_broken_contract():
    doc = yaml.safe_load((ODCS / "s4_mm_stock.odcs.yaml").read_text())
    bad = copy.deepcopy(doc)
    bad["apiVersion"], bad["schema"][0]["properties"][0]["logicalType"] = "v9", "text"
    assert validate(doc) == [] and len(validate(bad)) >= 2


def test_the_engine_reads_the_catalog_through_the_odcs_adapter(svc):
    assert svc.odcs is not None and "Chroma" in svc.catalog["ariba_contracts"]["rag_index_version"]
    svc.extract("none")
    rows = svc.check(WH, "compare_stock")["checklist"][0]["rows"]
    row = next(r for r in rows if r["metric"] == "Is the contract a valid ODCS file in the catalog?")
    assert row["status"] == "PASS" and "odcs/s4_mm_stock.odcs.yaml" in row["value"]


def test_editing_the_odcs_file_changes_the_readiness_decision(tmp_path):
    """Single source of truth: tighten the forecast freshness SLA in ODCS and the same data becomes stale."""
    root = tmp_path / "pipeline"
    shutil.copytree(ROOT / "pipeline", root)
    path = root / "odcs" / "ibp_demand_forecast.odcs.yaml"
    doc = yaml.safe_load(path.read_text())
    next(s for s in doc["slaProperties"] if s["property"] == "latency")["value"] = 1
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    before = PipelineService(ROOT / "pipeline", tmp_path / "a", lambda: NOW)
    after = PipelineService(root, tmp_path / "b", lambda: NOW)
    for s in (before, after):
        s.extract("none")
    assert before.check(DIR, "forecast_demand")["decision"] == "READY"
    r = after.check(DIR, "forecast_demand")
    assert r["decision"] == "BLOCKED" and "max 1 h" in r["text"]


# ------------------------------------------------------------------ 2. OpenLineage
def test_openlineage_events_trace_extract_check_fix_and_retrieval(svc):
    svc.extract("mm_stale")
    svc.check(WH, "compare_stock")
    svc.propose(WH, "compare_stock")
    svc.apply(WH, "compare_stock")
    svc.retrieve(WH, "compare_stock")
    jobs = [svc.lineage.summary(e)["job"] for e in svc.lineage.events("s4_mm_stock")][::-1]
    assert (
        jobs[:3] == ["sap_extract", "readiness_gate.compare_stock", "apply_fixes"]
        and jobs[-1] == "retrieve.compare_stock"
    )
    gate = next(e for e in svc.lineage.events("s4_mm_stock") if e["job"]["name"].startswith("readiness_gate"))
    assertions = gate["inputs"][0]["facets"]["dataQualityAssertions"]["assertions"]
    assert {"assertion": "freshness", "success": True} in [
        {k: a[k] for k in ("assertion", "success")} for a in assertions
    ]
    fix = next(e for e in svc.lineage.events("s4_mm_stock") if e["job"]["name"] == "apply_fixes")
    assert "sap-sim://s4hana-mm" in {d["namespace"] for d in fix["inputs"]}  # corrected data came from the source
    extract = next(e for e in svc.lineage.events("s4_mm_stock") if e["job"]["name"] == "sap_extract")
    out = next(d for d in extract["outputs"] if d["name"] == "s4_mm_stock")
    assert out["outputFacets"]["outputStatistics"]["rowCount"] > 0 and out["facets"]["version"]["datasetVersion"]
    row = next(
        r for r in svc.check(WH, "compare_stock")["checklist"][0]["rows"] if r["metric"].startswith("Are lineage")
    )
    assert "OpenLineage:" in row["value"] and "sap_extract" in row["value"]


# ------------------------------------------------------------------ 3. permission-aware document search
def test_warehouse_manager_gets_only_the_transfer_policy(svc):
    svc.extract("none")
    text = svc.retrieve(WH, "read_transfer_policy")["text"]
    assert "POL-WH-020 v1.4" in text and "AGR-SUP" not in text and "Permission-aware search" in text


def test_procurement_never_gets_the_superseded_agreement(svc):
    svc.extract("none")
    found = svc.search(PROC, "supplier_policy_qa", "returns of defective units freight")
    docs = {p["doc_id"] for p in found["passages"]}
    assert docs and "AGR-SUP-A-V1" not in docs and "AGR-SUP-A-V1 v1.0" not in found["searchable"]
    assert "AGR-SUP-A v2.0" in found["searchable"]


def test_knowledge_viewer_cannot_retrieve_supplier_agreements(svc):
    svc.extract("none")
    found = svc.search(KPV, "supplier_policy_qa", "Alpine Dispensing returns 60 days supplier agreement")
    assert found["searchable"] == ["POL-PURCH-001 v4.1", "POL-WH-020 v1.4"]
    assert {p["doc_id"] for p in found["passages"]} <= {"POL-PURCH-001", "POL-WH-020"}


def test_search_is_refused_when_blocked(svc):
    svc.extract("contract_expired")
    out = svc.search(DIR, "supplier_policy_qa", "returns")
    assert out["searched"] is False and "Search refused" in out["text"]


def test_filter_lives_inside_the_vector_query(tmp_path):
    idx = DocumentIndex(ROOT / "pipeline" / "documents", tmp_path / "rag", embedding=HashingEmbedding())
    allowed = {"AGR-SUP-A": "2.0", "AGR-SUP-A-V1": "1.0", "POL-WH-020": "1.4"}
    assert idx.search("returns", "scinsight:sap_executor", allowed)["passages"] == []  # no document group at all
    ops = idx.search("returns freight supplier", WH, allowed)
    assert ops["searchable"] == ["POL-WH-020 v1.4"] and {p["doc_id"] for p in ops["passages"]} == {"POL-WH-020"}


# ------------------------------------------------------------------ 4. certified publish, RAG ingestion, approval queue
PL, T = "scinsight:planner", "recommend_stock_transfer"


def test_blocked_data_is_never_published(svc):
    svc.extract("mm_unit_mismatch")
    assert svc.check(PL, T)["published"] == [] and svc.publisher.list() == []


def test_planner_requests_approval_then_certified_data_is_published_and_searchable(svc):
    svc.extract("mm_unit_mismatch")
    svc.check(PL, T)
    svc.propose(PL, T)
    applied = svc.apply(PL, T)
    assert applied["applied"] == [] and applied["next"][0]["kind"] == "request"  # the planner may not approve
    assert "scinsight:warehouse_manager" in {r["key"] for r in applied["next"][0]["approver_roles"]}
    req = svc.request_approval(PL, T)["request"]
    assert svc.inbox(PL) == [] and [r["id"] for r in svc.inbox(WH)] == [req["id"]]
    out = svc.decide(WH, req["id"], approve=True)
    assert out["after"]["decision"] == "READY" and svc.inbox(WH) == []
    mm = next(p for p in out["after"]["published"] if p["asset"] == "s4_mm_stock")
    assert mm["version"] == 1 and mm["new"] and mm["chunks"] > 0
    assert [f["action"] for f in mm["fixes_applied"]] == [
        "replace_bad_rows_from_source"
    ]  # the skipped fix is not claimed
    manifest = svc.publisher.current("s4_mm_stock")
    assert manifest["certified_for"] == [T] and manifest["contract"]["odcs_file"] == "odcs/s4_mm_stock.odcs.yaml"
    assert "KG" not in svc.publisher.path("s4_mm_stock").read_text()
    assert all("certified v1" in s["version"] for s in svc.retrieve(PL, T)["sources"])  # agents read certified data
    assert [p["new"] for p in svc.check(PL, T)["published"]] == [False] * 4  # same content: no new version
    hits = svc.search_data(PL, T, "released stock SKU-001 warehouse WH-C")["hits"]
    assert hits and hits[0]["label"] == "Inventory snapshot" and not any("price" in h["text"] for h in hits)
    assert not any("WH-B" in h["text"] for h in hits)  # the task covers WH-A -> WH-C only (minimum necessary)
    assert "publish_certified" in {e["job"]["name"] for e in svc.lineage.events("s4_mm_stock")}


def test_whats_next_explains_why_no_request_is_needed(svc):
    svc.extract("mm_unit_mismatch")
    svc.propose(WH, "compare_stock")
    steps = svc.apply(WH, "compare_stock")["next"]
    assert [s["kind"] for s in steps] == ["ready", "info"] and "No approval request was needed" in steps[1]["text"]
    assert "(e.g. " in steps[1]["text"]  # names a role that can run Compare stock but may not approve inventory


def _ds(svc, faults, task):
    """Test-only lookup of a neutral data set id by its faults (the UI never shows them)."""
    sets = [d for d in svc.datasets(task) if "faults" in d]
    if faults == "most":
        return max(sets, key=lambda d: len(d["faults"]))["id"]
    return next(d["id"] for d in sets if d["faults"] == faults)


def test_each_task_gets_different_data_sets_with_faults_in_its_own_tables(svc):
    for task in svc.harness:
        sets = [d for d in svc.datasets(task) if "faults" in d]
        assets = set(svc.harness[task]["required_assets"])
        combos = [tuple(d["faults"]) for d in sets]
        assert len(combos) == len(set(combos)) and () in combos, task  # all different, one clean control
        for d in sets:
            assert all(svc.sys[next(f for f in svc.faults if f["id"] == x)["system"]]["asset"] in assets
                       for x in d["faults"]), (task, d)  # fmt: skip
    assert len(svc.datasets("compare_stock")) >= 6 and len(svc.datasets(T)) >= 12


def test_fixing_one_fault_does_not_hide_another_fault_of_the_same_table(svc):
    """Upstream rules are per specific fault: fixing the KG unit must not hide wrong status codes."""
    svc.extract_dataset(_ds(svc, ["mm_unit_mismatch"], "compare_stock"), "compare_stock")
    svc.propose(WH, "compare_stock")
    assert svc.apply(WH, "compare_stock")["after"]["decision"] == "READY"
    assert {r["fault"] for r in svc.state["rules"]} == {"mm_unit_mismatch"}
    svc.extract_dataset(_ds(svc, ["mm_bad_status"], "compare_stock"), "compare_stock")
    assert svc.check(WH, "compare_stock")["decision"] == "BLOCKED"  # still visible: nobody fixed it


def test_dataset_list_shows_test_status_and_the_saved_fixed_data_set(svc):
    ds = _ds(svc, ["mm_unit_mismatch"], "compare_stock")
    status = lambda task, role=WH: {d["id"]: d for d in svc.dataset_list(task, role)}  # noqa: E731
    assert status("compare_stock")[ds]["short"] == f"{ds} · not tested"  # nothing revealed before a test
    svc.extract_dataset(ds, "compare_stock")
    assert svc.check(WH, "compare_stock")["decision"] == "BLOCKED"
    assert status("compare_stock")[ds]["short"] == f"{ds} · ✗ BLOCKED"
    assert status("forecast_demand")[ds]["status"] == "NOT_TESTED"  # data sets are generated per task
    assert status("compare_stock", PL)[ds]["status"] == "TESTED_OTHER"  # results are per role + task profile
    assert "Warehouse manager · Compare stock: BLOCKED" in status("compare_stock", PL)[ds]["label"]
    svc.propose(WH, "compare_stock")
    after = svc.apply(WH, "compare_stock")["after"]
    fixed = after["fixed_dataset"]
    assert after["decision"] == "READY" and fixed == {"id": f"{ds}-F1", "label": fixed["label"], "new": True}
    row = status("compare_stock")[fixed["id"]]
    assert row["fixed"] and row["short"] == f"{ds}-F1 · fixed · ✓ READY" and "Compare stock" in row["label"]
    assert status("compare_stock")[ds]["short"] == f"{ds} · ✓ READY"  # the same extract after its fixes
    loaded = svc.extract_dataset(fixed["id"], "compare_stock")  # pick the fixed data set: no faults, no fixes needed
    assert "loaded fixed data set" in loaded["text"] and svc.check(WH, "compare_stock")["decision"] == "READY"
    assert "KG" not in (svc.staging / "s4_mm_stock.csv").read_text()


def test_request_needing_two_approvers_stays_open_until_both_decide(svc):
    svc.extract_dataset(_ds(svc, "most", T), T)
    svc.propose(PL, T)
    req = svc.request_approval(PL, T)["request"]
    titles = {r["title"] for r in req["approver_roles"]}
    assert {"Warehouse manager", "Procurement manager", "Logistics manager"} <= titles
    first = svc.decide(WH, req["id"], approve=True)  # inventory part only
    assert first["status"] == "pending" and "still wait for" in first["text"]
    assert [r["id"] for r in svc.inbox(WH)] == [] and [r["id"] for r in svc.inbox("scinsight:procurement_manager")] == [
        req["id"]
    ]
    svc.decide("scinsight:supply_chain_director", req["id"], approve=True)  # the director may approve the rest
    assert next(r for r in svc.state["requests"] if r["id"] == req["id"])["status"] == "approved"


def test_fixes_to_approve_lists_proposed_fixes_and_pending_requests(svc):
    svc.extract("mm_unit_mismatch")
    svc.propose(WH, "compare_stock")
    mine = svc.approvals_overview(WH)
    assert mine["total"] == 2 and all(f["mine"] and f["approvers"] == ["you"] for f in mine["proposed"])
    svc.propose(PL, "compare_stock")
    theirs = svc.approvals_overview(PL)["proposed"]
    assert theirs and all(not f["mine"] and "Warehouse manager" in f["approvers"] for f in theirs)
    req = svc.request_approval(PL, "compare_stock")["request"]
    overview = svc.approvals_overview(WH)
    assert len(overview["requests"]) == 1 and overview["proposed"] == []  # sent fixes are counted once, in the request
    svc.decide(WH, req["id"], approve=True)
    assert svc.approvals_overview(WH)["total"] == 0  # approved: nothing left to approve


def test_rejected_request_changes_nothing(svc):
    svc.extract("mm_unit_mismatch")
    svc.propose(PL, T)
    req = svc.request_approval(PL, T)["request"]
    assert svc.decide(WH, req["id"], approve=False)["status"] == "rejected"
    assert svc.check(PL, T)["decision"] == "BLOCKED" and svc.publisher.list() == []
    with pytest.raises(KeyError):
        svc.decide(WH, req["id"], approve=True)  # already decided


def test_certified_search_applies_row_access(tmp_path):
    idx = DocumentIndex(ROOT / "pipeline" / "documents", tmp_path / "rag", embedding=HashingEmbedding())
    rows = [
        {"warehouse_id": w, "product_id": "SKU-001", "quantity": "10", "price_eur": "9.99"} for w in ("WH-A", "WH-C")
    ]
    assert idx.index_rows("s4_mm_stock", 1, "Inventory snapshot", rows, {"price_eur"}) == 2
    only_a = idx.search_rows("SKU-001 quantity", ["s4_mm_stock"], ["WH-A", "WH-B"])
    assert [h["text"].count("WH-A") for h in only_a] == [1] and "9.99" not in str(only_a)
    assert idx.search_rows("SKU-001", ["tm_lanes"]) == []  # other data sets are never searched


def test_api_search_lineage_and_odcs_endpoints(tmp_path):
    c = TestClient(create_app(Settings(audit_path=None, pipeline_work_dir=tmp_path), clock=lambda: NOW))
    c.post("/pipeline/extract", json={"faults": "none"})
    r = c.post("/pipeline/search", json={"role": WH, "task": "read_transfer_policy", "query": "approval of transfers"})
    assert r.status_code == 200 and r.json()["passages"][0]["doc_id"] == "POL-WH-020"
    assert {e["job"] for e in c.get("/pipeline/lineage").json()} >= {"sap_extract", "rag_index_build"}
    odcs = c.get("/pipeline/odcs/s4_mm_stock")
    assert odcs.status_code == 200 and "apiVersion: v3.1.0" in odcs.text
    assert c.get("/pipeline/odcs/nope").status_code == 404
