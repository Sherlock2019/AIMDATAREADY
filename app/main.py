"""FastAPI service: Stage 1 (/tasks/identify), Stage 2 (/readiness/check), both (/evaluate) and a demo protected
retriever (/demo/policy/search). OpenAPI docs: http://127.0.0.1:8460/docs

LOCAL DEMONSTRATION. There is no authentication: in demo mode the caller picks a synthetic identity by name. Do not
expose this service on a network. In integration mode (TDR_MODE=integration) the demo selector is refused and, until
a trusted identity provider is implemented, every readiness check is BLOCKED.

Run:  .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8460
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.access_gate import AccessGate
from app.access_gate import register as register_access_gate
from app.adapters.demo import DemoAdapter
from app.audit import AuditSink, JsonlAuditSink, MemoryAuditSink
from app.dataset_lab import DatasetLab
from app.dataset_routes import register as register_dataset_routes
from app.engine import Clock, ReadinessEngine, UnknownTaskProfile
from app.identity import DEMO_IDENTITY_NOTICE, IdentityError, NotConfiguredIdentityProvider, resolve_demo_identity
from app.models import (
    DemoIdentity,
    EvaluateRequest,
    IdentifyRequest,
    PolicySearchRequest,
    ReadinessCheckRequest,
    ReadinessResult,
    TaskIdentification,
)
from app.pipeline import PipelineService
from app.pipeline import register as register_pipeline_routes
from app.profiles import load_profiles
from app.protected import DemoProtectedPolicyRetriever, guarded_call
from app.settings import Settings, make_clock
from app.task_router import KeywordDemoRouter, identify_explicit

log = logging.getLogger("taskDataReadiness")
GATED_PREFIXES = ("/readiness", "/evaluate", "/demo")  # error responses on these paths also say decision=BLOCKED


class ApiError(Exception):
    def __init__(
        self, status: int, code: str, message: str, details: list | None = None, evaluation_id: str | None = None
    ):
        super().__init__(message)
        self.status, self.code, self.message, self.details, self.evaluation_id = (
            status,
            code,
            message,
            details,
            evaluation_id,
        )


def _error(
    request: Request,
    status: int,
    code: str,
    message: str,
    details: list | None = None,
    evaluation_id: str | None = None,
) -> JSONResponse:
    body: dict = {"error": {"code": code, "message": message, "details": details or []}}
    if request.url.path.startswith(GATED_PREFIXES):
        body["decision"] = "BLOCKED"
    if evaluation_id:
        body["evaluation_id"] = evaluation_id
    return JSONResponse(status_code=status, content=body)


def create_app(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    audit: AuditSink | None = None,
    adapter: DemoAdapter | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    profiles = load_profiles(settings.profiles_dir)  # invalid profiles stop the service here
    adapter = adapter or DemoAdapter.from_dir(settings.catalog_dir)
    clock = clock or make_clock(settings.clock)
    if audit is None:
        audit = JsonlAuditSink(settings.audit_path) if settings.audit_path else MemoryAuditSink()
    engine = ReadinessEngine(profiles, adapter, clock, audit)
    router = KeywordDemoRouter(profiles)
    identity_provider = NotConfiguredIdentityProvider()

    app = FastAPI(
        title="taskDataReadiness (LOCAL DEMO)",
        version=__version__,
        description=(
            "Task identification and a deterministic data-readiness preflight. LOCAL DEMONSTRATION: "
            "synthetic data, demo permissions, no authentication. A READY result is a preflight decision, "
            "not an authorisation: the data service must enforce access itself."
        ),
    )
    app.state.engine, app.state.audit, app.state.adapter, app.state.settings = engine, audit, adapter, settings

    # ------------------------------------------------------------------ consistent structured errors
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return _error(request, exc.status, exc.code, exc.message, exc.details, exc.evaluation_id)

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Field locations and messages only; submitted values are not echoed back.
        details = [
            {"loc": [str(p) for p in e.get("loc", ())], "msg": e.get("msg", ""), "type": e.get("type", "")}
            for e in exc.errors()
        ]
        return _error(request, 422, "invalid_request", "The request body is invalid.", details)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _error(request, exc.status_code, "http_error", str(exc.detail))

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unexpected error")
        return _error(request, 500, "internal_error", "Unexpected error; treat the request as BLOCKED.")

    # ------------------------------------------------------------------ public demo: email access gate (off by default)
    register_access_gate(app, AccessGate(settings.access_gate, settings.data_dir))

    # ------------------------------------------------------------------ identity + demo scenario
    def principals(request: Request, demo_identity: DemoIdentity | None):
        if settings.demo_mode:
            try:
                user, agent = resolve_demo_identity(demo_identity, adapter.directory)
            except IdentityError as exc:
                raise ApiError(400, exc.code, exc.message) from exc
            return user, agent, "demo_selector"
        if demo_identity is not None:
            raise ApiError(400, "demo_feature_disabled", "demo_identity is only accepted when TDR_MODE=demo.")
        user, agent = identity_provider.resolve(request.headers)
        return user, agent, f"trusted_provider:{identity_provider.name}"

    def scenario_adapter(name: str | None) -> DemoAdapter:
        if name is None:
            return adapter
        if not settings.demo_mode:
            raise ApiError(400, "demo_feature_disabled", "demo_scenario is only accepted when TDR_MODE=demo.")
        try:
            return adapter.with_scenario(name)
        except KeyError as exc:
            raise ApiError(
                400, "unknown_demo_scenario", f"Unknown demo scenario {name!r}.", sorted(adapter.scenarios)
            ) from exc

    def evaluate(
        task_id: str,
        context: dict[str, str],
        request: Request,
        demo_identity: DemoIdentity | None,
        demo_scenario: str | None,
    ) -> ReadinessResult:
        user, agent, identity_mode = principals(request, demo_identity)
        use = scenario_adapter(demo_scenario)
        try:
            return engine.evaluate(task_id, context, user, agent, identity_mode=identity_mode, adapter=use)
        except UnknownTaskProfile as exc:
            raise ApiError(
                404,
                "unknown_task_profile",
                f"No readiness profile for task '{exc.task_id}'. Nothing was checked; treat as BLOCKED.",
                sorted(profiles),
                exc.evaluation_id,
            ) from exc

    # ------------------------------------------------------------------ routes
    @app.get("/health", tags=["service"])
    def health() -> dict:
        return {
            "status": "ok",
            "service": "taskDataReadiness",
            "version": __version__,
            "mode": settings.mode,
            "demo_mode": settings.demo_mode,
            "adapter_mode": adapter.mode,
            "authentication": (
                DEMO_IDENTITY_NOTICE
                if settings.demo_mode
                else "trusted identity provider not configured: every readiness check is BLOCKED"
            ),
            "clock": settings.clock,
            "profiles": {tid: p.version for tid, p in sorted(profiles.items())},
            "notice": "Local demonstration only. Not secure; do not expose on a network.",
        }

    @app.get("/profiles", tags=["service"])
    def list_profiles() -> list[dict]:
        return [
            {
                "task_id": p.task_id,
                "version": p.version,
                "description": p.description,
                "purpose": p.purpose,
                "allow_warnings": p.allow_warnings,
                "required_context": [f.name for f in p.required_context],
                "assets": [{"name": a.name, "mandatory": a.mandatory} for a in p.assets],
            }
            for p in sorted(profiles.values(), key=lambda p: p.task_id)
        ]

    @app.post("/tasks/identify", response_model=TaskIdentification, tags=["stage 1: task identification"])
    def identify(body: IdentifyRequest) -> TaskIdentification:
        """Explicit task id from a trusted caller if given, otherwise the transparent demo keyword router."""
        return identify_explicit(body.task_id, profiles) if body.task_id else router.identify(body.question)

    @app.post("/readiness/check", response_model=ReadinessResult, tags=["stage 2: data readiness"])
    def readiness_check(body: ReadinessCheckRequest, request: Request) -> ReadinessResult:
        """Preflight decision for a known task. HTTP 200 for every evaluated decision, including BLOCKED."""
        return evaluate(body.task_id, body.context, request, body.demo_identity, body.demo_scenario)

    @app.post("/evaluate", tags=["stage 1 + 2"])
    def evaluate_both(body: EvaluateRequest, request: Request) -> dict:
        """Demo convenience: identify the task, then check readiness. Both stages stay callable on their own."""
        ident = identify_explicit(body.task_id, profiles) if body.task_id else router.identify(body.question)
        if ident.status != "identified":
            return {
                "identification": ident.model_dump(),
                "readiness": None,
                "decision": "BLOCKED",
                "context_sources": {},
                "next_step": "Ask the user to clarify the task; nothing was checked.",
            }
        context = {**ident.suggested_context, **body.context}  # explicit request context wins over router hints
        sources = {k: ("request" if k in body.context else "router_suggestion") for k in context}
        result = evaluate(ident.task_id, context, request, body.demo_identity, body.demo_scenario)
        return {
            "identification": ident.model_dump(),
            "readiness": result.model_dump(mode="json"),
            "decision": result.decision.value,
            "context_sources": sources,
        }

    @app.post("/demo/policy/search", tags=["demo protected retriever"])
    def demo_policy_search(body: PolicySearchRequest, request: Request) -> dict:
        """DEMO: use-time readiness recheck -> retriever that enforces authorisation -> post-retrieval check."""
        if not settings.demo_mode:
            raise ApiError(404, "not_available", "The demo retriever only exists in demo mode.")
        user, agent, identity_mode = principals(request, body.demo_identity)
        use = scenario_adapter(body.demo_scenario)
        retriever = DemoProtectedPolicyRetriever(use, clock)
        result, retrieval = guarded_call(
            engine,
            task_id="policy_document_qa",
            context={},
            user=user,
            agent=agent,
            identity_mode=identity_mode,
            adapter=use,
            downstream=lambda _r: retriever.search(user, agent, body.question),
        )
        answerable = bool(retrieval and retrieval["evidence_check"]["sufficient"])
        return {
            "decision": result.decision.value,
            "retriever_called": retrieval is not None,
            "answerable": answerable,
            "readiness": result.model_dump(mode="json"),
            "retrieval": retrieval,
        }

    # ------------------------------------------------------------------ faulty-dataset lab + dashboard (demo)
    lab = DatasetLab(settings.datasets_dir, settings.datasets_work_dir, adapter, clock, audit)
    app.state.lab, app.state.claude_client = lab, None  # tests may inject a stub Claude client
    register_dataset_routes(app, lab, principals, ApiError, settings.demo_mode)
    if settings.demo_mode:  # SAP pipeline steps called by the Flowise Agentflow (flowise/)
        app.state.pipeline = PipelineService(
            settings.datasets_dir.parent / "pipeline", settings.pipeline_work_dir, clock, audit
        )
        register_pipeline_routes(app, app.state.pipeline, ApiError)
        if os.environ.get("TDR_RAG_EMBEDDING") != "hashing":  # real server: load the local embedding model early
            app.state.pipeline.rag.warm_up()

    return app


app = create_app()
