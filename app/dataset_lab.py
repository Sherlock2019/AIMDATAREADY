"""Faulty-dataset lab: check a dataset, propose fixes (rules or Claude), apply approved fixes, re-check.

  datasets/cases/<id>/        the ORIGINAL dataset (never modified)
  <work_root>/<id>/           the WORKING COPY that fixes are applied to (default .data/datasets/)

Used by the API (/demo/datasets/..., the /dashboard page) and by scripts/run_dataset_demo.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.adapters.dataset import DatasetAdapter
from app.adapters.demo import DemoAdapter
from app.audit import AuditSink
from app.catalog import load_yaml
from app.claude_agent import propose_with_claude
from app.engine import Clock, ReadinessEngine
from app.models import CheckStatus, Principal, ReadinessResult
from app.profiles import load_profiles
from app.remediation import FixProposal, Remediator, make_working_copy, plan_fixes, validate_proposal

TASK_ID = "warehouse_transfer"
DEFAULT_CONTEXT = {
    "product_id": "SKU-001",
    "origin_warehouse": "WH-A",
    "destination_warehouse": "WH-C",
    "planning_period": "2026-11-16/2026-11-29",
}
STATUS_RANK = {"PASS": 0, "SKIPPED": 1, "WARN": 2, "FAIL": 3, "ERROR": 4}


class UnknownCase(LookupError):
    pass


def summarize(result: ReadinessResult) -> dict[str, Any]:
    """Requirement x check matrix (worst status per cell) for the dashboard and the CLI."""
    matrix: dict[str, dict[str, dict]] = {}
    for c in result.checks:
        cell = matrix.setdefault(c.requirement, {}).setdefault(c.check, {"status": "PASS", "reasons": []})
        if STATUS_RANK[c.status.value] > STATUS_RANK[cell["status"]]:
            cell["status"] = c.status.value
        cell["reasons"].append(f"{c.status.value}: {c.reason}")
    return {
        "decision": result.decision.value,
        "evaluation_id": result.evaluation_id,
        "reasons": result.reasons,
        "evidence_refs": result.evidence_refs,
        "matrix": matrix,
        "failing": sum(1 for c in result.checks if c.status in (CheckStatus.FAIL, CheckStatus.ERROR)),
    }


class DatasetLab:
    def __init__(self, root: Path, work_root: Path, authz: DemoAdapter, clock: Clock, audit: AuditSink | None):
        self.root, self.work_root, self.authz, self.clock = Path(root), Path(work_root), authz, clock
        self.profiles = load_profiles(self.root / "profiles")
        self.profile = self.profiles[TASK_ID]
        self.engine = ReadinessEngine(self.profiles, authz, clock, audit)
        self.remediator = Remediator(
            self.profile, self.root / "source_of_record", self.root / "steward_registry.yaml", clock
        )

    # ------------------------------------------------------------------ cases
    def case_ids(self) -> list[str]:
        return sorted(p.name for p in (self.root / "cases").iterdir() if (p / "manifest.yaml").exists())

    def _original(self, case_id: str) -> Path:
        if case_id not in self.case_ids():  # also stops path traversal: only listed folder names are accepted
            raise UnknownCase(case_id)
        return self.root / "cases" / case_id

    def _working(self, case_id: str, create: bool = False) -> Path:
        work = self.work_root / self._original(case_id).name
        if create and not work.exists():
            make_working_copy(self._original(case_id), work)
        return work

    def info(self, case_id: str) -> dict[str, Any]:
        fault = load_yaml(self._original(case_id) / "fault.yaml")
        return {"case_id": case_id, **fault, "has_working_copy": self._working(case_id).exists()}

    # ------------------------------------------------------------------ check / propose / apply
    def evaluate(
        self,
        case_id: str,
        user: Principal | None,
        agent: Principal | None,
        identity_mode: str,
        target: str = "original",
        context: dict[str, str] | None = None,
    ) -> ReadinessResult:
        folder = self._working(case_id, create=True) if target == "working" else self._original(case_id)
        return self.engine.evaluate(
            TASK_ID,
            context or DEFAULT_CONTEXT,
            user,
            agent,
            identity_mode=identity_mode,
            adapter=DatasetAdapter(folder, self.authz),
        )

    def check(self, case_id: str, user, agent, identity_mode: str, target: str = "original") -> dict[str, Any]:
        result = self.evaluate(case_id, user, agent, identity_mode, target)
        return {
            "case": self.info(case_id),
            "target": target,
            "summary": summarize(result),
            "readiness": result.model_dump(mode="json"),
        }

    def propose(
        self, case_id: str, user, agent, identity_mode: str, proposer: str = "rules", client=None
    ) -> dict[str, Any]:
        result = self.evaluate(case_id, user, agent, identity_mode, "working")
        out: dict[str, Any] = {"case": self.info(case_id), "proposer": proposer, "summary": summarize(result)}
        if proposer == "claude":
            work = self._working(case_id, create=True)
            claude = propose_with_claude(
                title=self.info(case_id)["title"],
                result=result,
                profile=self.profile,
                adapter=DatasetAdapter(work, self.authz),
                context=DEFAULT_CONTEXT,
                client=client,
            )
            out.update(proposals=claude["proposals"], claude=claude)
        else:
            out["proposals"] = [p.model_dump() for p in plan_fixes(result, self.profile)]
        return out

    def apply(self, case_id: str, proposals: list[dict], user, agent, identity_mode: str) -> dict[str, Any]:
        before = self.evaluate(case_id, user, agent, identity_mode, "working")
        valid: list[FixProposal] = []
        rejected = []
        for raw in proposals:
            p, err = validate_proposal(raw, self.profile)
            (valid.append(p) if p else rejected.append({"proposal": raw, "error": err}))
        if before.decision.value == "BLOCKED" and any(
            c.check == "access" and c.status is not CheckStatus.PASS for c in before.checks
        ):
            # Never change data the caller may not even read.
            valid = [p for p in valid if p.action == "escalate"]
        changes = self.remediator.apply(self._working(case_id, create=True), valid)
        after = self.evaluate(case_id, user, agent, identity_mode, "working")
        return {
            "case": self.info(case_id),
            "before": summarize(before),
            "after": summarize(after),
            "changes": changes,
            "rejected": rejected,
        }

    def reset(self, case_id: str) -> dict[str, Any]:
        make_working_copy(self._original(case_id), self._working(case_id))
        return {"case": self.info(case_id), "reset": True}

    def auto_correct(self, case_id: str, user, agent, identity_mode: str, max_passes: int = 3) -> dict[str, Any]:
        """Reset, then: check -> rules plan -> apply -> re-check, until not BLOCKED or nothing more to do."""
        self.reset(case_id)
        original = summarize(self.evaluate(case_id, user, agent, identity_mode, "original"))
        passes = []
        for _ in range(max_passes):
            result = self.evaluate(case_id, user, agent, identity_mode, "working")
            if result.decision.value != "BLOCKED":
                break
            plan = [p for p in plan_fixes(result, self.profile) if p.auto_applicable]
            if not plan:
                break
            step = self.apply(case_id, [p.model_dump() for p in plan], user, agent, identity_mode)
            passes.append({"proposals": [p.model_dump() for p in plan], "changes": step["changes"]})
        final = self.evaluate(case_id, user, agent, identity_mode, "working")
        escalations = [p.model_dump() for p in plan_fixes(final, self.profile) if not p.auto_applicable]
        return {
            "case": self.info(case_id),
            "before": original,
            "passes": passes,
            "after": summarize(final),
            "needs_human": escalations,
        }

    def run_all(self, user, agent, identity_mode: str) -> list[dict[str, Any]]:
        out = []
        for cid in self.case_ids():
            r = self.auto_correct(cid, user, agent, identity_mode)
            out.append(
                {
                    "case_id": cid,
                    "title": r["case"]["title"],
                    "before": r["before"]["decision"],
                    "detected": sorted(
                        {
                            f"{req}:{chk}"
                            for req, cells in r["before"]["matrix"].items()
                            for chk, cell in cells.items()
                            if cell["status"] in ("FAIL", "ERROR")
                        }
                    ),
                    "fixes": [c["action"] for p in r["passes"] for c in p["changes"]],
                    "after": r["after"]["decision"],
                    "needs_human": len(r["needs_human"]),
                }
            )
        return out
