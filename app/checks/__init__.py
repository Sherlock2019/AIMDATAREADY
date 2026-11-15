"""Independent, deterministic readiness checks. Each returns an Outcome; the engine adds the requirement name and
whether the requirement is mandatory, then aggregates the outcomes into READY / READY_WITH_WARNINGS / BLOCKED."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.models import CheckStatus


@dataclass
class Outcome:
    status: CheckStatus
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)


def passed(reason: str, **evidence: Any) -> Outcome:
    return Outcome(CheckStatus.PASS, reason, evidence)


def warned(reason: str, **evidence: Any) -> Outcome:
    return Outcome(CheckStatus.WARN, reason, evidence)


def failed(reason: str, **evidence: Any) -> Outcome:
    return Outcome(CheckStatus.FAIL, reason, evidence)


def errored(reason: str, **evidence: Any) -> Outcome:
    return Outcome(CheckStatus.ERROR, reason, evidence)
