"""Stage 1: task identification. Which business task is the user asking for?

Three ways to get a task id, all returning the same TaskIdentification shape:

  1. explicit task id      a trusted calling application already knows the task (identify_explicit);
  2. supervisor intent     the existing Flowise/LangGraph Supervisor already classified the request
                           (from_supervisor_intent): reuse it instead of adding a second LLM call;
  3. demo keyword router   transparent rules for a few example phrasings (KeywordDemoRouter).

When unsure, the answer is task_id="unknown" with status="clarification_required". The router never picks a
"closest" or weaker profile for an ambiguous request.

A task id is a routing hint. It is NOT an authorisation: knowing (or claiming) that a request is a warehouse transfer
grants no permission to any data. Authorisation is checked in Stage 2 and, finally, by the data service itself.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Protocol

from app.models import TaskIdentification

UNKNOWN = "unknown"
LIMITATIONS = (
    "Demo router: transparent keyword rules for a few example phrasings. It does not understand language "
    "in general and can be fooled by wording. Its answer is a routing hint, never an authorisation; the "
    "calling application must confirm the task and the suggested context."
)

# A task matches when EVERY group has at least one word in the question. Terms ending in "*" match as prefixes.
RULES: dict[str, list[list[str]]] = {
    "warehouse_transfer": [
        ["move", "moving", "transfer", "transferring", "ship", "rebalance", "relocate", "send"],
        ["warehouse", "warehouses", "wh-*", "stock", "inventory", "sku", "skus"],
    ],
    "policy_document_qa": [
        ["policy", "policies", "procedure", "procedures", "sop", "agreement", "terms", "guideline", "guidelines"],
        ["what", "say", "says", "allowed", "rule", "rules", "explain", "require", "requires", "does"],
    ],
}

# The SCInsight Supervisor's intents (flowise/prompts/supervisor_classifier.md) that have a readiness profile today.
SUPERVISOR_INTENT_TO_TASK = {"warehouse": "warehouse_transfer"}


class TaskIdentifier(Protocol):
    """Anything that turns a question into a TaskIdentification (the demo router, or a future LLM classifier)."""

    def identify(self, question: str) -> TaskIdentification: ...


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", text.lower())


def _group_hit(tokens: Iterable[str], group: list[str]) -> str | None:
    tokens = list(tokens)
    for term in group:
        if term.endswith("*"):
            if any(t.startswith(term[:-1]) for t in tokens):
                return term
        elif term in tokens:
            return term
    return None


def suggest_transfer_context(question: str) -> dict[str, str]:
    """Best-effort hints from the text (e.g. 'SKU 001', 'from warehouse A', 'to WH-C'). Hints, not trusted input."""
    q = question.upper()
    out: dict[str, str] = {}
    sku = re.search(r"\bSKU[\s#-]*([A-Z0-9]+(?:-[A-Z0-9]+)*)\b", q)
    code = re.search(r"\b(?!WH-)([A-Z]{2,}-[A-Z0-9]+(?:-[A-Z0-9]+)*)\b", q)
    if sku:
        out["product_id"] = f"SKU-{sku.group(1)}"
    elif code:
        out["product_id"] = code.group(1)

    def warehouse(prefix: str) -> str | None:
        m = re.search(prefix + r"\s+(?:THE\s+)?(?:WAREHOUSE\s+([A-Z0-9])\b|(WH-[A-Z0-9]+)\b)", q)
        if not m:
            return None
        return f"WH-{m.group(1)}" if m.group(1) else m.group(2)

    if origin := warehouse(r"\bFROM"):
        out["origin_warehouse"] = origin
    if destination := warehouse(r"\b(?:TO|INTO)"):
        out["destination_warehouse"] = destination
    period = re.search(r"(\d{4}-\d{2}-\d{2})\s*(?:/|TO|UNTIL|-)\s*(\d{4}-\d{2}-\d{2})", q)
    if period:
        out["planning_period"] = f"{period.group(1)}/{period.group(2)}"
    return out


class KeywordDemoRouter:
    def __init__(self, known_tasks: Iterable[str]):
        self.known_tasks = set(known_tasks)

    def identify(self, question: str) -> TaskIdentification:
        tokens = _tokens(question)
        matches: dict[str, list[str]] = {}
        for task_id, groups in RULES.items():
            if task_id not in self.known_tasks:
                continue
            hits = [_group_hit(tokens, g) for g in groups]
            if all(hits):
                matches[task_id] = [f"{task_id}: '{h}'" for h in hits]
        if len(matches) == 1:
            task_id, rules = next(iter(matches.items()))
            suggested = suggest_transfer_context(question) if task_id == "warehouse_transfer" else {}
            return TaskIdentification(
                task_id=task_id,
                status="identified",
                method="demo_keyword_rules",
                matched_rules=rules,
                suggested_context=suggested,
                explanation=f"Matched the demo rules for {task_id}.",
                limitations=LIMITATIONS,
            )
        if len(matches) > 1:
            return TaskIdentification(
                task_id=UNKNOWN,
                status="clarification_required",
                method="demo_keyword_rules",
                matched_rules=[r for rules in matches.values() for r in rules],
                explanation="The question matches more than one task ("
                + ", ".join(sorted(matches))
                + "); ask the user which one they mean instead of guessing.",
                limitations=LIMITATIONS,
            )
        return TaskIdentification(
            task_id=UNKNOWN,
            status="clarification_required",
            method="demo_keyword_rules",
            explanation="The question does not match any supported task; ask the user to rephrase or choose a task.",
            limitations=LIMITATIONS,
        )


def identify_explicit(task_id: str, known_tasks: Iterable[str]) -> TaskIdentification:
    """A task id supplied by a trusted calling application. Unknown ids are not mapped to anything else."""
    known = set(known_tasks)
    if task_id in known:
        return TaskIdentification(
            task_id=task_id,
            status="identified",
            method="explicit_task_id",
            explanation="Task id supplied by the calling application.",
            limitations="The task id selects a readiness profile; it grants no permission.",
        )
    return TaskIdentification(
        task_id=UNKNOWN,
        status="clarification_required",
        method="explicit_task_id",
        explanation=f"'{task_id}' is not a supported task id (known: {', '.join(sorted(known))}).",
        limitations="The task id selects a readiness profile; it grants no permission.",
    )


def from_supervisor_intent(intent: str, known_tasks: Iterable[str]) -> TaskIdentification:
    """Reuse the existing Supervisor's classification (no second LLM call)."""
    task_id = SUPERVISOR_INTENT_TO_TASK.get(intent.strip().lower())
    if task_id and task_id in set(known_tasks):
        return TaskIdentification(
            task_id=task_id,
            status="identified",
            method="supervisor_intent",
            matched_rules=[f"supervisor intent '{intent}'"],
            explanation=f"The Supervisor classified the request as '{intent}'.",
            limitations="Reuses the Supervisor's LLM classification; it grants no permission.",
        )
    return TaskIdentification(
        task_id=UNKNOWN,
        status="clarification_required",
        method="supervisor_intent",
        explanation=f"No readiness profile is mapped to the Supervisor intent '{intent}'.",
        limitations="Reuses the Supervisor's LLM classification; it grants no permission.",
    )
