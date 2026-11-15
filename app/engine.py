"""The Task Data Readiness Engine: deterministic, no LLM, no network calls of its own.

Input : task id + task context + user/agent principals (from a trusted integration, or the demo selector)
        + the task's profile + an adapter that answers catalog / evidence / authorisation questions.
Output: READY | READY_WITH_WARNINGS | BLOCKED with every check result, safe reasons, evidence refs and an id.

Decision rules (in this order):
  1. Any MANDATORY check that FAILs or cannot be evaluated (ERROR)        -> BLOCKED
  2. Otherwise any WARN, or a failed OPTIONAL requirement:
       profile.allow_warnings is true                                   -> READY_WITH_WARNINGS
       profile.allow_warnings is false                                  -> BLOCKED
  3. Otherwise                                                          -> READY

The result is a PREFLIGHT decision taken at one moment. Data can change between this check and its use, and this
engine does not return the data. The consuming service must enforce authorisation itself and re-check what matters
at use time (protected.guarded_call shows the pattern).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from app.adapters.base import AdapterUnavailable, ReadinessAdapter
from app.audit import AuditSink, build_entry
from app.catalog import DocumentRecord
from app.checks import Outcome, errored, failed, passed
from app.checks.access import check_access, row_scope
from app.checks.context import check_context
from app.checks.coverage import check_coverage
from app.checks.documents import check_documents
from app.checks.freshness import check_freshness
from app.checks.metadata import check_metadata
from app.checks.provenance import check_provenance
from app.checks.quality import check_quality
from app.checks.schema import check_schema
from app.models import CheckResult, CheckStatus, Decision, Principal, ReadinessResult
from app.profiles import AssetRequirement, TaskProfile

Clock = Callable[[], datetime]

NOTICE = (
    "Preflight decision only. READY means the configured checks passed for the evidence available at "
    "evaluation time. It does not prove the data is correct, complete for every purpose, or safe for "
    "production use. The service that returns the data must enforce authorisation itself and re-check "
    "state at use time."
)
DEMO_NOTICE = " DEMO MODE: synthetic data, demo permissions and a demo identity selector (not authentication)."


class UnknownTaskProfile(LookupError):
    def __init__(self, task_id: str, evaluation_id: str):
        super().__init__(task_id)
        self.task_id, self.evaluation_id = task_id, evaluation_id


def system_clock() -> datetime:
    return datetime.now(UTC)


class ReadinessEngine:
    def __init__(
        self,
        profiles: dict[str, TaskProfile],
        adapter: ReadinessAdapter,
        clock: Clock = system_clock,
        audit: AuditSink | None = None,
    ):
        self.profiles, self.adapter, self.clock, self.audit = profiles, adapter, clock, audit

    def evaluate(
        self,
        task_id: str,
        context: dict[str, str],
        user: Principal | None,
        agent: Principal | None,
        *,
        identity_mode: str,
        adapter: ReadinessAdapter | None = None,
    ) -> ReadinessResult:
        adapter = adapter or self.adapter
        started = time.perf_counter()
        evaluation_id = f"tdr-{uuid.uuid4().hex[:16]}"
        now = self.clock()
        profile = self.profiles.get(task_id)
        if profile is None:
            # An unknown or ambiguous task never falls back to some other (weaker) profile.
            blocked = self._result(
                evaluation_id,
                task_id,
                None,
                now,
                [
                    CheckResult(
                        check="profile",
                        requirement=task_id,
                        status=CheckStatus.ERROR,
                        mandatory=True,
                        reason="No readiness profile exists for this task; nothing can be checked, so it is blocked.",
                    )
                ],
                [],
                adapter,
                identity_mode,
                user,
                allow_warnings=False,
                post_retrieval=False,
            )
            self._audit(blocked, user, agent, {})
            raise UnknownTaskProfile(task_id, evaluation_id)

        # Only profile-declared context fields are used (and audited); anything else is ignored.
        ctx = {f.name: str(context[f.name]).strip() for f in profile.required_context if f.name in context}
        checks: list[CheckResult] = []
        refs: list[str] = []
        context_outcome = check_context(profile, context)
        checks.append(self._wrap("context", "task_context", True, context_outcome))
        if context_outcome.status is CheckStatus.PASS:
            # Without complete context, coverage and row scope would be checked against blanks: stop here.
            for req in profile.assets:
                self._evaluate_asset(profile, req, ctx, user, agent, now, adapter, checks, refs)

        result = self._result(
            evaluation_id,
            task_id,
            profile.version,
            now,
            checks,
            refs,
            adapter,
            identity_mode,
            user,
            allow_warnings=profile.allow_warnings,
            post_retrieval=profile.post_retrieval_check_required,
        )
        result.latency_ms = round((time.perf_counter() - started) * 1000, 3)
        self._audit(result, user, agent, ctx)
        return result

    # ------------------------------------------------------------------ one asset requirement
    def _evaluate_asset(
        self,
        profile: TaskProfile,
        req: AssetRequirement,
        ctx: dict[str, str],
        user: Principal | None,
        agent: Principal | None,
        now: datetime,
        adapter: ReadinessAdapter,
        checks: list[CheckResult],
        refs: list[str],
    ) -> None:
        mark = [time.perf_counter()]

        def add(check: str, outcome: Outcome) -> None:  # also records how long the check took
            now_ = time.perf_counter()
            result = self._wrap(check, req.name, req.mandatory, outcome)
            result.duration_ms = round((now_ - mark[0]) * 1000, 3)
            checks.append(result)
            mark[0] = time.perf_counter()

        try:
            asset = adapter.get_asset(req.asset_id)
        except AdapterUnavailable:
            add("catalog", errored("The data catalog did not answer, so this requirement cannot be evaluated."))
            return
        if asset is None:
            add("catalog", failed(f"Required data '{req.name}' is not available."))
            return

        access = check_access(adapter, req, user, agent, profile.purpose, ctx)
        add("access", access)
        if access.status is not CheckStatus.PASS:
            # Do not run further checks or reveal metadata (source name, version, owners) without access.
            checks.append(
                CheckResult(
                    check="other_checks",
                    requirement=req.name,
                    status=CheckStatus.SKIPPED,
                    mandatory=req.mandatory,
                    reason="Not evaluated because access was not granted.",
                )
            )
            return

        ref = f"{asset.asset_id}@{asset.source_version or 'unversioned'}"
        refs.append(ref)
        add(
            "catalog",
            passed(
                "Asset is registered in the catalog.",
                asset_ref=ref,
                classification=asset.classification,
                synthetic=asset.synthetic,
                contract_ref=adapter.contract_status(asset.asset_id),
            ),
        )
        try:
            if req.freshness:
                event_time, ingested_at = adapter.get_timestamps(asset.asset_id)
                add("freshness", check_freshness(req.freshness, event_time, ingested_at, now))
            if req.schema_rule:
                add("schema", check_schema(req.schema_rule, adapter.get_schema(asset.asset_id)))
            for rule in req.coverage:
                add("coverage", check_coverage(rule, ctx, adapter.get_coverage(asset.asset_id, rule.dimension)))
            if req.quality:
                add("quality", check_quality(req.quality, ctx, adapter.get_rows(asset.asset_id)))
            if req.provenance:
                add("provenance", check_provenance(req.provenance, asset))
            if req.metadata:
                record, contract = adapter.get_record(asset.asset_id), adapter.get_contract(asset.asset_id)
                add("metadata", check_metadata(req.metadata, record, contract))
            if req.documents:
                scope = row_scope(req, ctx)

                def authorize(doc: DocumentRecord):
                    decisions = [adapter.authorize(user, asset.asset_id, profile.purpose, scope, doc)]
                    if req.access.require_agent_access and agent is not None:
                        decisions.append(adapter.authorize(agent, asset.asset_id, profile.purpose, scope, doc))
                    return decisions

                add(
                    "documents",
                    check_documents(req.documents, adapter.get_documents(asset.asset_id), now.date(), authorize),
                )
        except AdapterUnavailable:
            add("evidence", errored("Evidence for this requirement could not be retrieved; blocked (fail closed)."))

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _wrap(check: str, requirement: str, mandatory: bool, outcome: Outcome) -> CheckResult:
        return CheckResult(
            check=check,
            requirement=requirement,
            status=outcome.status,
            mandatory=mandatory,
            reason=outcome.reason,
            evidence=outcome.evidence,
        )

    @staticmethod
    def decide(checks: list[CheckResult], allow_warnings: bool) -> tuple[Decision, list[str]]:
        blocking = [c for c in checks if c.mandatory and c.status in (CheckStatus.FAIL, CheckStatus.ERROR)]
        if blocking:
            return Decision.BLOCKED, [f"{c.requirement}: {c.reason}" for c in blocking]
        warnings = [
            c
            for c in checks
            if c.status is CheckStatus.WARN or (not c.mandatory and c.status in (CheckStatus.FAIL, CheckStatus.ERROR))
        ]
        if warnings:
            reasons = [f"{c.requirement}: {c.reason}" for c in warnings]
            if allow_warnings:
                return Decision.READY_WITH_WARNINGS, reasons
            return Decision.BLOCKED, reasons + ["This profile does not permit continuing with warnings."]
        return Decision.READY, []

    def _result(
        self,
        evaluation_id: str,
        task_id: str,
        version: str | None,
        now: datetime,
        checks: list[CheckResult],
        refs: list[str],
        adapter: ReadinessAdapter,
        identity_mode: str,
        user: Principal | None,
        *,
        allow_warnings: bool,
        post_retrieval: bool,
    ) -> ReadinessResult:
        decision, reasons = self.decide(checks, allow_warnings)
        demo = adapter.mode == "demo" or identity_mode == "demo_selector"
        return ReadinessResult(
            evaluation_id=evaluation_id,
            task_id=task_id,
            profile_version=version,
            evaluated_at=now,
            decision=decision,
            reasons=reasons,
            checks=checks,
            evidence_refs=refs,
            adapter_mode=adapter.mode,
            identity_mode=identity_mode,
            authenticated=bool(user and user.authenticated),
            post_retrieval_check_required=post_retrieval,
            notice=NOTICE + (DEMO_NOTICE if demo else ""),
        )

    def _audit(
        self, result: ReadinessResult, user: Principal | None, agent: Principal | None, ctx: dict[str, str]
    ) -> None:
        if self.audit is not None:
            self.audit.record(build_entry(result, user, agent, ctx))
