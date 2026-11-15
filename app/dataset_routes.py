"""HTTP routes for the faulty-dataset lab and the /dashboard page (demo mode only)."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse

from app.claude_agent import claude_status
from app.dataset_lab import DEFAULT_CONTEXT, DatasetLab
from app.models import DatasetApplyRequest, DatasetCheckRequest, DatasetProposeRequest, DatasetRequest

DASHBOARD = Path(__file__).resolve().parent / "static" / "dashboard.html"
TAG = ["faulty-dataset lab (demo)"]


def register(app: FastAPI, lab: DatasetLab, principals, api_error, demo_mode: bool) -> None:
    def guard(case_id: str | None = None) -> None:
        if not demo_mode:
            raise api_error(404, "not_available", "The dataset lab only exists in demo mode.")
        if case_id is not None and case_id not in lab.case_ids():
            raise api_error(404, "unknown_dataset_case", f"Unknown dataset case {case_id!r}.", lab.case_ids())

    def who(request: Request, body: DatasetRequest):
        return principals(request, body.demo_identity)

    @app.get("/lab", include_in_schema=False)
    def lab_page() -> FileResponse:  # the earlier faulty-dataset lab (the main UI is now the table at /dashboard)
        guard()
        return FileResponse(DASHBOARD, media_type="text/html")

    @app.get("/demo/datasets", tags=TAG)
    def list_cases() -> dict:
        """The 11 dataset cases (1 clean + 10 faulty), the task context and whether Claude is configured."""
        guard()
        return {
            "task_id": "warehouse_transfer",
            "context": DEFAULT_CONTEXT,
            "claude": claude_status(),
            "profile_version": lab.profile.version,
            "cases": [lab.info(c) for c in lab.case_ids()],
        }

    @app.post("/demo/datasets/run-all", tags=TAG)
    def run_all(body: DatasetRequest, request: Request) -> dict:
        """Reset every case, auto-correct it with the rules planner, report before/after."""
        guard()
        user, agent, mode = who(request, body)
        return {"results": lab.run_all(user, agent, mode)}

    @app.post("/demo/datasets/{case_id}/check", tags=TAG)
    def check(case_id: str, body: DatasetCheckRequest, request: Request) -> dict:
        """Readiness of the original (faulty) dataset or of its working copy."""
        guard(case_id)
        user, agent, mode = who(request, body)
        return lab.check(case_id, user, agent, mode, body.target)

    @app.post("/demo/datasets/{case_id}/propose", tags=TAG)
    def propose(case_id: str, body: DatasetProposeRequest, request: Request) -> dict:
        """Proposed fixes for the working copy: from the deterministic rules planner or from Claude."""
        guard(case_id)
        user, agent, mode = who(request, body)
        return lab.propose(case_id, user, agent, mode, body.proposer, client=getattr(app.state, "claude_client", None))

    @app.post("/demo/datasets/{case_id}/apply", tags=TAG)
    def apply(case_id: str, body: DatasetApplyRequest, request: Request) -> dict:
        """Apply chosen proposals to the working copy (allow-list validated), then re-check."""
        guard(case_id)
        user, agent, mode = who(request, body)
        return lab.apply(case_id, body.proposals, user, agent, mode)

    @app.post("/demo/datasets/{case_id}/reset", tags=TAG)
    def reset(case_id: str) -> dict:
        """Throw the working copy away and start again from the original faulty dataset."""
        guard(case_id)
        return lab.reset(case_id)

    @app.post("/demo/datasets/{case_id}/auto-correct", tags=TAG)
    def auto_correct(case_id: str, body: DatasetRequest, request: Request) -> dict:
        """Reset, then check -> rules plan -> apply -> re-check until not BLOCKED (max 3 passes)."""
        guard(case_id)
        user, agent, mode = who(request, body)
        return lab.auto_correct(case_id, user, agent, mode)
