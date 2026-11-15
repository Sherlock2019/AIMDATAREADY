"""Data shapes shared by the task router, the readiness engine and the API (pydantic v2).

Request models use extra="forbid": an unexpected field such as "user_id" or "roles" in a JSON body is rejected
instead of silently ignored, so nobody can believe that sending an identity in the body has any effect.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Decision(StrEnum):
    READY = "READY"
    READY_WITH_WARNINGS = "READY_WITH_WARNINGS"
    BLOCKED = "BLOCKED"


class CheckStatus(StrEnum):
    PASS = "PASS"
    WARN = "WARN"  # a soft threshold or an optional requirement was not met
    FAIL = "FAIL"  # the requirement was evaluated and not met
    ERROR = "ERROR"  # the requirement could not be evaluated (missing evidence, service unavailable, ...)
    SKIPPED = "SKIPPED"  # not evaluated because access to the asset was not granted


class Principal(BaseModel):
    """Who is asking. Built by an identity component (identity.py), never from the request body."""

    model_config = ConfigDict(frozen=True)

    principal_id: str
    kind: Literal["user", "service"]
    source: Literal["demo_selector", "trusted_provider"]
    # True only when a trusted provider verified a credential. Always False for the demo selector.
    authenticated: bool


class CheckResult(BaseModel):
    # context | catalog | access | freshness | schema | coverage | provenance | documents | other_checks
    check: str
    # the profile's name for the requirement (e.g. "inventory"), never a restricted source name
    requirement: str
    status: CheckStatus
    mandatory: bool
    reason: str  # plain-language, safe to show to the caller
    evidence: dict[str, Any] = Field(default_factory=dict)  # only filled for assets the caller is authorised to see
    duration_ms: float | None = None  # time spent on this check


class ReadinessResult(BaseModel):
    evaluation_id: str  # correlation id: the same id is written to the audit log
    task_id: str
    profile_version: str | None
    evaluated_at: datetime
    decision: Decision
    reasons: list[str]  # why it is BLOCKED or carries warnings (empty when READY)
    checks: list[CheckResult]
    evidence_refs: list[str]  # asset@version references, only for assets the caller may see
    adapter_mode: Literal["demo", "real"]
    identity_mode: str
    authenticated: bool  # False in demo mode: the identity was selected, not authenticated
    post_retrieval_check_required: bool = False
    latency_ms: float | None = None  # total evaluation time inside the engine
    notice: str


class TaskIdentification(BaseModel):
    task_id: str  # a known task id, or "unknown"
    status: Literal["identified", "clarification_required"]
    method: Literal["explicit_task_id", "demo_keyword_rules", "supervisor_intent"]
    matched_rules: list[str] = Field(default_factory=list)
    suggested_context: dict[str, str] = Field(default_factory=dict)  # hints only; the caller must confirm them
    explanation: str
    limitations: str


# ---------------------------------------------------------------- API request bodies


class DemoIdentity(BaseModel):
    """DEMO ONLY: picks a synthetic identity by name from catalog/demo_identities.yaml. Not authentication."""

    model_config = ConfigDict(extra="forbid")

    user: str = Field(min_length=1, max_length=64)
    agent: str | None = Field(default=None, min_length=1, max_length=64)


class IdentifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)
    # A trusted calling application (or the existing Supervisor) may already know the task.
    task_id: str | None = Field(default=None, max_length=64)


class ReadinessCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=64)
    context: dict[str, str] = Field(default_factory=dict)
    demo_identity: DemoIdentity | None = None
    demo_scenario: str | None = Field(default=None, max_length=64)


class EvaluateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)
    task_id: str | None = Field(default=None, max_length=64)
    context: dict[str, str] = Field(default_factory=dict)
    demo_identity: DemoIdentity | None = None
    demo_scenario: str | None = Field(default=None, max_length=64)


class PolicySearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)
    demo_identity: DemoIdentity | None = None
    demo_scenario: str | None = Field(default=None, max_length=64)


# ---------------------------------------------------------------- faulty-dataset lab (demo mode only)


class DatasetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    demo_identity: DemoIdentity | None = None


class DatasetCheckRequest(DatasetRequest):
    target: Literal["original", "working"] = "original"


class DatasetProposeRequest(DatasetRequest):
    proposer: Literal["rules", "claude"] = "rules"


class DatasetApplyRequest(DatasetRequest):
    # Validated against the remediation allow-list on the server, whoever proposed them.
    proposals: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
