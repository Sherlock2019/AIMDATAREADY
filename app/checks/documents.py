"""Document check (for RAG tasks): is there at least one approved, currently valid, versioned document that this
user and agent may use for this purpose?

PASSING THIS CHECK DOES NOT MEAN THE QUESTION CAN BE ANSWERED. The retriever still has to find relevant passages,
and a post-retrieval evidence check must confirm that what was retrieved is enough (see app/protected.py).

Documents the caller is not allowed to see are only counted, never named, so the response cannot leak titles.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import date

from app.adapters.base import AuthzDecision
from app.catalog import DocumentRecord
from app.checks import Outcome, failed, passed
from app.profiles import DocumentRule

PREFLIGHT_NOTE = (
    "Preflight only: approved documents exist, but the retriever must still find relevant evidence "
    "and a post-retrieval check must confirm it is sufficient."
)


def exclusion_reason(rule: DocumentRule, doc: DocumentRecord, today: date) -> str | None:
    if doc.status != rule.require_status:
        return f"status {doc.status}"
    if rule.require_valid_on_evaluation_date:
        if doc.valid_from is None or doc.valid_from > today or (doc.valid_to is not None and doc.valid_to < today):
            return "not valid on the evaluation date"
    if rule.require_version_and_source_id and (not doc.version or not doc.source_id):
        return "missing version or source id"
    return None


def check_documents(
    rule: DocumentRule,
    docs: list[DocumentRecord],
    today: date,
    authorize: Callable[[DocumentRecord], list[AuthzDecision]],
) -> Outcome:
    usable, excluded = [], Counter()
    for doc in docs:
        decisions = authorize(doc)
        if not all(d.allowed is True for d in decisions):
            excluded["not authorised for this caller"] += 1  # never named: the caller may not know it exists
            continue
        why = exclusion_reason(rule, doc, today)
        if why:
            excluded[why] += 1
            continue
        usable.append({"doc_id": doc.doc_id, "version": doc.version, "source_id": doc.source_id})
    evidence = {"usable_documents": usable, "excluded_counts": dict(excluded), "note": PREFLIGHT_NOTE}
    if len(usable) < rule.min_usable_documents:
        return failed(
            f"{len(usable)} usable document(s); the profile needs at least {rule.min_usable_documents}.", **evidence
        )
    return passed(f"{len(usable)} approved, valid and authorised document(s) available. {PREFLIGHT_NOTE}", **evidence)
