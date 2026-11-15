"""Stage 1: task identification."""

from app.task_router import KeywordDemoRouter, from_supervisor_intent, identify_explicit

KNOWN = {"warehouse_transfer", "policy_document_qa"}
router = KeywordDemoRouter(KNOWN)


def test_recognises_warehouse_transfer_question():
    r = router.identify("Should we move SKU 001 from warehouse A to warehouse C?")
    assert (r.task_id, r.status, r.method) == ("warehouse_transfer", "identified", "demo_keyword_rules")
    assert r.suggested_context == {"product_id": "SKU-001", "origin_warehouse": "WH-A", "destination_warehouse": "WH-C"}
    assert r.matched_rules and "routing hint" in r.limitations


def test_recognises_policy_question():
    r = router.identify("What does the supplier policy say about returns?")
    assert (r.task_id, r.status) == ("policy_document_qa", "identified")
    assert r.suggested_context == {}


def test_question_matching_two_tasks_requires_clarification():
    r = router.identify("What does the policy say about how to move stock between warehouses?")
    assert (r.task_id, r.status) == ("unknown", "clarification_required")
    assert "more than one task" in r.explanation  # never silently picks one of them


def test_unsupported_question_requires_clarification():
    r = router.identify("Can you help me with the thing from yesterday?")
    assert (r.task_id, r.status) == ("unknown", "clarification_required")


def test_explicit_task_id_known_and_unknown():
    assert identify_explicit("warehouse_transfer", KNOWN).status == "identified"
    for bad in ("price_optimisation", "unknown", "warehouse"):
        r = identify_explicit(bad, KNOWN)
        assert (r.task_id, r.status) == ("unknown", "clarification_required")


def test_router_never_returns_a_task_without_profile():
    only_policy = KeywordDemoRouter({"policy_document_qa"})
    r = only_policy.identify("Should we move SKU 001 from warehouse A to warehouse C?")
    assert r.status == "clarification_required"


def test_reuses_supervisor_intent_without_a_second_classifier():
    assert from_supervisor_intent("warehouse", KNOWN).task_id == "warehouse_transfer"
    assert from_supervisor_intent(" Warehouse ", KNOWN).method == "supervisor_intent"
    for intent in ("procurement", "sap_write_request", "out_of_scope", ""):
        assert from_supervisor_intent(intent, KNOWN).status == "clarification_required"


def test_suggested_context_formats():
    r = router.identify("Transfer SUN-SPF50-100 from WH-B to WH-A for 2026-11-16/2026-11-29")
    assert r.suggested_context == {
        "product_id": "SUN-SPF50-100",
        "origin_warehouse": "WH-B",
        "destination_warehouse": "WH-A",
        "planning_period": "2026-11-16/2026-11-29",
    }
