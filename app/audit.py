"""Audit records for every readiness evaluation.

Recorded: evaluation id, task, profile version, time, decision, each check's outcome, asset references, adapter mode
and identity mode. NOT recorded: question text, prompts, raw data rows, secrets. The user id is stored as a
pseudonymous reference (a hash prefix) so the log does not become a list of who asked what.

PoC limits: a local JSON-lines file with no retention policy, no tamper evidence and no access control. Production
needs append-only storage, retention rules agreed with legal/compliance, access control on the log itself and
correlation with the downstream service's own logs (see docs/architecture.md, "Audit").
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any, Protocol

from app.models import Principal, ReadinessResult


def pseudonym(principal: Principal | None) -> str | None:
    if principal is None:
        return None
    return "sha256:" + hashlib.sha256(principal.principal_id.encode()).hexdigest()[:16]


def build_entry(
    result: ReadinessResult, user: Principal | None, agent: Principal | None, context: dict[str, str]
) -> dict[str, Any]:
    return {
        "evaluation_id": result.evaluation_id,
        "evaluated_at": result.evaluated_at.isoformat(),
        "task_id": result.task_id,
        "profile_version": result.profile_version,
        "decision": result.decision.value,
        "adapter_mode": result.adapter_mode,
        "identity_mode": result.identity_mode,
        "user_ref": pseudonym(user),
        "agent_id": agent.principal_id if agent else None,
        "context": context,  # only profile-declared fields, already validated against their patterns
        "checks": [
            {
                "check": c.check,
                "requirement": c.requirement,
                "status": c.status.value,
                "duration_ms": c.duration_ms,
                "reason": c.reason,
            }
            for c in result.checks
        ],
        "evidence_refs": result.evidence_refs,
        "latency_ms": result.latency_ms,
    }


class AuditSink(Protocol):
    def record(self, entry: dict[str, Any]) -> None: ...


class MemoryAuditSink:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def record(self, entry: dict[str, Any]) -> None:
        self.entries.append(entry)


class JsonlAuditSink:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def record(self, entry: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, sort_keys=True, default=str)
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
