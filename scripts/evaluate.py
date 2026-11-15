"""Evaluate the Task Data Readiness Engine on labelled scenarios and write docs/evaluation_report.md.

    .venv/bin/python scripts/evaluate.py            # runs everything in-process (no server, no network)

Decision labels were set by hand before running. READY_WITH_WARNINGS counts as ALLOWED (downstream work can
continue). An HTTP 404/422 refusal on a gated endpoint counts as BLOCKED. Only executed results are reported.
"""

from __future__ import annotations

import statistics
import sys
import tempfile
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.adapters.demo import DemoAdapter  # noqa: E402
from app.audit import MemoryAuditSink  # noqa: E402
from app.dataset_lab import DatasetLab  # noqa: E402
from app.identity import resolve_demo_identity  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import DemoIdentity  # noqa: E402
from app.pipeline import PipelineService  # noqa: E402
from app.protected import DemoProtectedPolicyRetriever  # noqa: E402
from app.scenario_runner import load_scenarios, run_scenario  # noqa: E402
from app.settings import ROOT, Settings  # noqa: E402

NOW = datetime(2026, 11, 14, 8, 0, tzinfo=UTC)
DIR, WH = "scinsight:supply_chain_director", "scinsight:warehouse_manager"
T, R = "recommend_stock_transfer", "recommend_replenishment"
HARNESS = [  # (name, task, role, faults, expected) - labelled before running
    ("authorized transfer, clean data", T, DIR, "none", "allow"),
    ("open PO dates missing (optional asset -> warning)", T, DIR, "ariba_missing_dates", "allow"),
    ("stale inventory (74 h, max 2 h)", T, DIR, "mm_stale", "block"),
    ("incompatible unit in inventory", T, DIR, "mm_unit_mismatch", "block"),
    ("missing transfer lead time WH-A>WH-C", T, DIR, "tm_missing_lane", "block"),
    ("denied row access to WH-C", T, "harness:planner", "none", "block"),
    ("denied data-set access (no read)", T, "scinsight:sap_executor", "none", "block"),
    ("role cannot read the forecast", T, "aurelia:warehouse", "none", "block"),
    ("unknown authorization (service down)", T, DIR, "authz_down", "block"),
    ("agent service reading for itself", T, "scinsight:agent_service", "none", "allow"),
    ("denied column access (supplier prices)", R, WH, "none", "block"),
    ("authorized replenishment with prices", R, "scinsight:procurement_manager", "none", "allow"),
    ("forecast, clean data", "forecast_demand", DIR, "none", "allow"),
    ("catalog record incomplete (steward, contact)", "forecast_demand", DIR, "mdg_catalog_incomplete", "block"),
    ("forecast horizon too short", "forecast_demand", DIR, "ibp_short_horizon", "block"),
    ("supplier risk, clean data", "identify_supplier_risk", DIR, "none", "allow"),
    ("empty QM usage decisions", "identify_supplier_risk", DIR, "qm_null_decision", "block"),
    ("policy question, approved documents", "supplier_policy_qa", DIR, "none", "allow"),
    ("all supplier agreements expired", "supplier_policy_qa", DIR, "contract_expired", "block"),
    ("weekly report, clean data", "weekly_executive_report", DIR, "none", "allow"),
    ("missing warehouse (WH-C bin stock)", "weekly_executive_report", DIR, "ewm_missing_wh_c", "block"),
    # Role -> Task -> Profile (roles.yaml): each role's own tasks
    ("warehouse manager: compare stock, clean data", "compare_stock", WH, "none", "allow"),
    ("warehouse manager: compare stock, stale inventory (74 h, max 24 h)", "compare_stock", WH, "mm_stale", "block"),
    ("procurement manager: replenishment, clean data", R, "scinsight:procurement_manager", "none", "allow"),
    (
        "procurement manager: replenishment, duplicate POs",
        R,
        "scinsight:procurement_manager",
        "ariba_duplicates",
        "block",
    ),
    (
        "quality manager: batch release, empty usage decisions",
        "review_batch_release",
        "scinsight:quality_manager",
        "qm_null_decision",
        "block",
    ),
    (
        "production planner: production readiness, negative order qty",
        "check_production_readiness",
        "scinsight:production_planner",
        "pp_negative_qty",
        "block",
    ),
    ("logistics manager: plan shipments, clean data", "plan_shipments", "scinsight:logistics_manager", "none", "allow"),
    (
        "MLOps: evaluate forecast model, horizon too short",
        "evaluate_forecast_model",
        "scinsight:mlops_engineer",
        "ibp_short_horizon",
        "block",
    ),
    (
        "SAP executor: record execution (no read access)",
        "record_sap_execution",
        "scinsight:sap_executor",
        "none",
        "block",
    ),
    ("director: weekly report, clean data", "weekly_executive_report", DIR, "none", "allow"),
    # ALLOWED < REQUIRED on clean data: never a weaker profile
    ("planner WH-A/WH-B only: compare stock needs WH-C", "compare_stock", "harness:planner", "none", "block"),
    ("Aurelia demand: forecast needs MDG it may not read", "forecast_demand", "aurelia:demand", "none", "block"),
    (
        "Aurelia warehouse: transfer policy needs documents it may not read",
        "read_transfer_policy",
        "aurelia:warehouse",
        "none",
        "block",
    ),
    (
        "knowledge viewer: transfer policy, approved documents",
        "read_transfer_policy",
        "knowledge:kp_viewer",
        "none",
        "allow",
    ),
]


def outcome(decision: str | None) -> str:
    return "allow" if decision in ("READY", "READY_WITH_WARNINGS") else "block"


def pct(a: int, b: int) -> str:
    return f"{a}/{b} = {100 * a / b:.1f}%" if b else "n/a (no cases)"


def main() -> int:
    rows, latencies, check_errors, unknown, block_reasons = [], [], 0, 0, Counter()

    def count_checks(checks):
        nonlocal check_errors, unknown
        for c in checks:
            status, check, reason = c["status"], c["check"], c["reason"]
            check_errors += status == "ERROR"
            unknown += (
                check == "access"
                and status in ("FAIL", "ERROR")
                and ("unknown" in reason or "did not answer" in reason)
            )

    # A. the 36 labelled API scenarios (decision scenarios only; task-identification scenarios are listed apart)
    client = TestClient(create_app(Settings(audit_path=None), clock=lambda: NOW, audit=MemoryAuditSink()))
    ident_ok = ident_total = 0
    for sc in load_scenarios():
        res = run_scenario(client, sc)
        if sc["endpoint"] == "/tasks/identify":
            ident_total += 1
            ident_ok += not res["problems"]
            continue
        want = outcome(sc["expected"].get("decision"))
        readiness = res["body"].get("readiness") if sc["endpoint"] != "/readiness/check" else res["body"]
        got = outcome(res["body"].get("decision"))
        if isinstance(readiness, dict) and readiness.get("latency_ms") is not None:
            latencies.append(readiness["latency_ms"])
            count_checks([{**c, "status": c["status"]} for c in readiness.get("checks", [])])
            if got == "block":
                block_reasons.update(
                    c["check"] for c in readiness["checks"] if c["mandatory"] and c["status"] in ("FAIL", "ERROR")
                )
        rows.append(("A api", sc["name"], want, got))

    # B. the 11 faulty-dataset cases
    authz = DemoAdapter.from_dir(ROOT / "catalog")
    lab = DatasetLab(ROOT / "datasets", Path(tempfile.mkdtemp()), authz, lambda: NOW, None)
    u, a = resolve_demo_identity(DemoIdentity(user="planner_all", agent="scinsight_supervisor"), authz.directory)
    for cid in lab.case_ids():
        r = lab.evaluate(cid, u, a, "demo_selector")
        latencies.append(r.latency_ms)
        count_checks([c.model_dump(mode="json") for c in r.checks])
        if r.decision.value == "BLOCKED":
            block_reasons.update(c.check for c in r.checks if c.mandatory and c.status.value in ("FAIL", "ERROR"))
        rows.append(("B dataset", cid, "allow" if cid == "00_clean" else "block", outcome(r.decision.value)))

    # C. Data Harness scenarios over the simulated SAP landscape (each run 10 times for latency samples)
    svc = PipelineService(ROOT / "pipeline", Path(tempfile.mkdtemp()), lambda: NOW)
    sample = None
    for name, task, role, faults, want in HARNESS:
        svc.extract(faults)
        task = svc.resolve(role, task)
        for _ in range(10):
            res = svc._evaluate(role, task, quiet=True)
            latencies.append(res.latency_ms)
        count_checks([c.model_dump(mode="json") for c in res.checks])
        if res.decision.value == "BLOCKED":
            block_reasons.update(c.check for c in res.checks if c.mandatory and c.status.value in ("FAIL", "ERROR"))
        rows.append(("C harness", name, want, outcome(res.decision.value)))
        if name.startswith("authorized transfer"):
            sample = svc.check(role, task)["data_metrics"]

    # D. access: unauthorized exposure and bypass tests on the tested paths
    retriever = DemoProtectedPolicyRetriever(authz, lambda: NOW)
    q = "What does the supplier policy say about returns and payment terms?"
    exposures, exposure_tests = 0, 0
    for user, agent, forbidden in (
        ("contractor", "scinsight_supervisor", None),
        ("planner_all", "untrusted_agent", None),
        ("planner_all", "scinsight_supervisor", {"POL-FIN-900", "POL-RET-000", "POL-QA-050"}),
        ("policy_reader", "scinsight_supervisor", {"POL-FIN-900"}),
    ):
        uu, aa = resolve_demo_identity(DemoIdentity(user=user, agent=agent), authz.directory)
        hits = {h["doc_id"] for h in retriever.search(uu, aa, q)["hits"]}
        exposure_tests += 1
        exposures += len(hits) if forbidden is None else len(hits & forbidden)
    direct_bypass = "pass (0 documents returned without authorization)" if exposures == 0 else "FAIL"
    supervisor = "not run (SCInsight export not found)"
    try:
        import json  # noqa: I001

        from integrations.supplychain.build_readiness_supervisor import DEFAULT_SOURCE, GATE_COND, patch

        flow = patch(json.loads(DEFAULT_SOURCE.read_text()), "// gate")
        wh = next(
            n["id"]
            for n in flow["nodes"]
            if "Warehouse-Agentflow" in str(n["data"]["inputs"].get("executeFlowSelectedFlow"))
        )
        into = [e for e in flow["edges"] if e["target"] == wh]
        ok = len(into) == 1 and into[0]["sourceHandle"] == f"{GATE_COND}-output-0"
        supervisor = "pass (Warehouse specialist reachable only through the gate's READY output)" if ok else "FAIL"
    except FileNotFoundError:
        pass

    # E. metrics
    decided = [r for r in rows]
    should_block = [r for r in decided if r[2] == "block"]
    should_allow = [r for r in decided if r[2] == "allow"]
    unsafe = [r for r in should_block if r[3] == "allow"]
    false_block = [r for r in should_allow if r[3] == "block"]
    correct = [r for r in decided if r[2] == r[3]]
    lat = sorted(latencies)
    p = lambda q_: lat[max(0, int(round(q_ * len(lat))) - 1)]  # noqa: E731
    t = time.strftime("%Y-%m-%d %H:%M")
    lines = [
        "# Evaluation report (generated)",
        "",
        f"Generated {t} by `scripts/evaluate.py` (in-process, fixed demo clock {NOW.isoformat()}). "
        "Every number below was produced by this run; nothing is estimated.",
        "",
        "Labels were set by hand before running. READY_WITH_WARNINGS counts as **allowed**. A 404/422 refusal on a "
        "gated endpoint counts as **blocked**.",
        "",
        "## Decision quality",
        "",
        "| Metric | Definition (denominator) | Result |",
        "|---|---|---|",
        f"| Labelled decision scenarios | A: 31 API + B: 11 datasets + C: {len(HARNESS)} Data Harness | {len(decided)} |",
        f"| **Unsafe pass count** | should block, engine allowed | **{len(unsafe)}** |",
        f"| Unsafe pass rate | unsafe passes ÷ scenarios labelled block | {pct(len(unsafe), len(should_block))} |",
        f"| False block count | should allow, engine blocked | {len(false_block)} |",
        f"| False block rate | false blocks ÷ scenarios labelled allow | {pct(len(false_block), len(should_allow))} |",
        f"| Block detection rate | correctly blocked ÷ scenarios labelled block | "
        f"{pct(len(should_block) - len(unsafe), len(should_block))} |",
        f"| Correct decision rate | correct ÷ all labelled decision scenarios | {pct(len(correct), len(decided))} |",
        f"| Task identification (separate) | matching ÷ identification scenarios | {pct(ident_ok, ident_total)} |",
        "",
        "## Access control (tested paths only)",
        "",
        "| Metric | Result |",
        "|---|---|",
        f"| Unauthorized data exposure (demo protected retriever, {exposure_tests} identity/agent combinations) | "
        f"{exposures} documents |",
        f"| Access bypass - Supervisor path (generated SCInsight Agentflow copy, graph test) | {supervisor} |",
        f"| Access bypass - direct retrieval without preflight | {direct_bypass} |",
        "| Denied / unknown access scenarios blocked | "
        + pct(
            sum(
                1
                for r in rows
                if r[0] == "C harness"
                and ("denied" in r[1] or "unknown" in r[1] or "cannot read" in r[1])
                and r[3] == "block"
            ),
            sum(
                1
                for r in rows
                if r[0] == "C harness" and ("denied" in r[1] or "unknown" in r[1] or "cannot read" in r[1])
            ),
        )
        + " |",
        "| SCInsight direct paths (Chatflow SAP tools, pgvector retriever, MCP) | **not tested** - repository not modified |",
        "",
        "## Operations",
        "",
        "| Metric | Definition | Result |",
        "|---|---|---|",
        f"| Engine latency P50 | in-engine evaluation time, {len(lat)} evaluations | {statistics.median(lat):.3f} ms |",
        f"| Engine latency P95 | same samples | {p(0.95):.3f} ms |",
        f"| Engine latency max | same samples | {lat[-1]:.3f} ms |",
        f"| Check error count | checks that ended in ERROR (could not be evaluated), last run of each scenario | {check_errors} |",
        f"| Unknown-result count | access checks with unknown authorization, last run of each scenario | {unknown} |",
        "",
        "Latency excludes HTTP handling; it is measured inside the engine with the synthetic adapters.",
        "",
        "## Most common reasons for blocking",
        "",
        "| Check | Blocking occurrences |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in block_reasons.most_common()],
        "",
        "## Data-readiness metrics (authorized transfer, clean data)",
        "",
        "| Asset | Metrics (only where evidence exists) |",
        "|---|---|",
        *[f"| {k} | " + ", ".join(f"{mk}={mv}" for mk, mv in v.items()) + " |" for k, v in (sample or {}).items()],
        "",
        "## Scenario results",
        "",
        "| Set | Scenario | Expected | Engine | OK |",
        "|---|---|---|---|---|",
        *[f"| {s} | {n} | {w} | {g} | {'✓' if w == g else '✗'} |" for s, n, w, g in rows],
        "",
        "## Limits",
        "",
        "* Synthetic data, demo identities and demo permissions; the same person wrote rules, data and labels. A "
        "perfect score shows the rules do what was intended, not that they are the right rules.",
        "* Zero exposures/bypasses apply only to the tested paths. The SCInsight direct paths were not tested.",
        "* Passing schema, completeness and freshness checks does not prove values are accurate (no reconciliation).",
        "* Latency was measured on one machine, in-process, without concurrency or real connectors.",
        "* RAG metrics beyond the demo retriever (citation coverage/support, relevance) are not measured.",
    ]
    out = ROOT / "docs" / "evaluation_report.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        f"scenarios {len(decided)} | unsafe passes {len(unsafe)} | false blocks {len(false_block)} | "
        f"exposures {exposures} | P50 {statistics.median(lat):.3f} ms P95 {p(0.95):.3f} ms | report {out}"
    )
    for r in unsafe + false_block:
        print("MISMATCH", r)
    return 1 if unsafe or false_block else 0


if __name__ == "__main__":
    raise SystemExit(main())
