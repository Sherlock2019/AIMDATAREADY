"""Demonstration of the two controls that must live OUTSIDE the readiness preflight.

1. guarded_call(): re-run the readiness check at USE time, immediately before the protected operation, and only call
   the operation when the result is not BLOCKED. A preflight answer from a few minutes ago may be outdated (a
   document revoked, a permission removed, a snapshot replaced). Tests prove that a BLOCKED result never invokes the
   downstream function.

2. DemoProtectedPolicyRetriever: authorisation enforced INSIDE retrieval. Even if a caller skipped the preflight,
   it only returns approved, currently valid documents that this user and agent may read. Then
   post_retrieval_evidence_check() decides whether what was found is enough to answer.

DEMO ONLY: keyword scoring over the synthetic documents in catalog/demo_catalog.yaml. A real retriever applies the
same filters in the vector store query itself (e.g. a metadata filter on tenant / roles / validity, or PostgreSQL
row-level security under pgvector), so unauthorised chunks are never loaded, not filtered afterwards.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from typing import Any, TypeVar

from app.adapters.base import AdapterUnavailable, ReadinessAdapter
from app.checks.documents import exclusion_reason
from app.engine import ReadinessEngine
from app.models import Decision, Principal, ReadinessResult
from app.profiles import DocumentRule

T = TypeVar("T")
POLICY_ASSET = "policy_documents"
STOPWORDS = {
    "what",
    "does",
    "the",
    "about",
    "say",
    "says",
    "policy",
    "policies",
    "and",
    "for",
    "our",
    "with",
    "how",
    "are",
    "is",
    "a",
    "an",
    "of",
    "on",
    "to",
    "in",
    "we",
    "us",
}


def guarded_call(
    engine: ReadinessEngine,
    *,
    task_id: str,
    context: dict[str, str],
    user: Principal | None,
    agent: Principal | None,
    identity_mode: str,
    downstream: Callable[[ReadinessResult], T],
    adapter: ReadinessAdapter | None = None,
) -> tuple[ReadinessResult, T | None]:
    """Use-time recheck: evaluate now, then call downstream only if READY / READY_WITH_WARNINGS."""
    result = engine.evaluate(task_id, context, user, agent, identity_mode=identity_mode, adapter=adapter)
    if result.decision is Decision.BLOCKED:
        return result, None
    return result, downstream(result)


def post_retrieval_evidence_check(hits: list[dict[str, Any]], min_hits: int = 1, min_score: int = 2) -> dict[str, Any]:
    """Was enough relevant, authorised evidence retrieved to answer? (demo thresholds)"""
    strong = [h for h in hits if h["score"] >= min_score]
    if len(strong) >= min_hits:
        return {"sufficient": True, "reason": f"{len(strong)} passage(s) matched at least {min_score} query terms."}
    return {
        "sufficient": False,
        "reason": "No authorised document matched the question well enough; answer 'not found' rather than guess.",
    }


class DemoProtectedPolicyRetriever:
    def __init__(self, adapter: ReadinessAdapter, clock: Callable[[], datetime], purpose: str = "policy_lookup"):
        self.adapter, self.clock, self.purpose = adapter, clock, purpose

    def search(self, user: Principal | None, agent: Principal | None, question: str, top_k: int = 3) -> dict[str, Any]:
        if user is None or agent is None:
            return {
                "hits": [],
                "filtered_out": None,
                "error": "no identity: retrieval refused",
                "evidence_check": post_retrieval_evidence_check([]),
            }
        terms = {t for t in re.findall(r"[a-z0-9]+", question.lower()) if len(t) > 2 and t not in STOPWORDS}
        today = self.clock().date()
        rule = DocumentRule()
        hits, filtered_out = [], 0
        try:
            docs = self.adapter.get_documents(POLICY_ASSET)
            for doc in docs:
                allowed = all(
                    self.adapter.authorize(p, POLICY_ASSET, self.purpose, {}, doc).allowed is True
                    for p in (user, agent)
                )
                if not allowed or exclusion_reason(rule, doc, today):
                    filtered_out += 1  # counted only: never named in the response
                    continue
                words = set(re.findall(r"[a-z0-9]+", (doc.title + " " + doc.text).lower()))
                score = len(terms & words)
                if score:
                    hits.append(
                        {
                            "doc_id": doc.doc_id,
                            "version": doc.version,
                            "source_id": doc.source_id,
                            "title": doc.title,
                            "score": score,
                            "excerpt": doc.text[:240],
                        }
                    )
        except AdapterUnavailable:
            return {
                "hits": [],
                "filtered_out": None,
                "error": "authorisation or catalog unavailable: retrieval refused",
                "evidence_check": post_retrieval_evidence_check([]),
            }
        hits.sort(key=lambda h: (-h["score"], h["doc_id"]))
        hits = hits[:top_k]
        return {"hits": hits, "filtered_out": filtered_out, "evidence_check": post_retrieval_evidence_check(hits)}
