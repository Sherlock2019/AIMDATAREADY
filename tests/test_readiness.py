"""Stage 2: the readiness engine and its checks (no HTTP)."""

from datetime import UTC, datetime, timedelta

import pytest

from app.checks.freshness import check_freshness
from app.engine import ReadinessEngine, UnknownTaskProfile
from app.models import CheckStatus, Decision, Principal
from app.profiles import FreshnessRule, ProfileError, TaskProfile, load_profile
from app.protected import DemoProtectedPolicyRetriever, guarded_call
from tests.conftest import NOW, TRANSFER_CONTEXT


def run(
    engine,
    principals,
    scenario=None,
    user="planner_all",
    agent="scinsight_supervisor",
    context=None,
    task="warehouse_transfer",
):
    u, a = principals(user, agent)
    adapter = engine.adapter.with_scenario(scenario)
    return engine.evaluate(task, context or TRANSFER_CONTEXT, u, a, identity_mode="demo_selector", adapter=adapter)


def failing(result):
    return {(c.requirement, c.check) for c in result.checks if c.status in (CheckStatus.FAIL, CheckStatus.ERROR)}


def test_valid_data_is_ready(engine, principals):
    r = run(engine, principals)
    assert r.decision is Decision.READY and r.reasons == []
    assert r.profile_version == "1.0.0" and r.evaluated_at == NOW and r.evaluation_id.startswith("tdr-")
    assert r.adapter_mode == "demo" and "not authentication" in r.notice
    assert "sap_inventory_snapshot@SAP-SYN-2026-11-14" in r.evidence_refs


def test_missing_mandatory_asset_blocks(engine, principals):
    r = run(engine, principals, "lead_times_unavailable")
    assert r.decision is Decision.BLOCKED
    assert ("transfer_lead_times", "catalog") in failing(r)


def test_stale_data_blocks_even_if_recently_ingested(engine, principals):
    r = run(engine, principals, "stale_inventory")
    assert r.decision is Decision.BLOCKED
    fresh = next(c for c in r.checks if c.requirement == "inventory" and c.check == "freshness")
    assert fresh.status is CheckStatus.FAIL
    assert "recent ingestion does not make old source data fresh" in fresh.reason


def test_missing_event_time_is_not_replaced_by_ingestion_time(engine, principals):
    r = run(engine, principals, "inventory_ingestion_time_only")
    fresh = next(c for c in r.checks if c.requirement == "inventory" and c.check == "freshness")
    assert r.decision is Decision.BLOCKED and fresh.status is CheckStatus.FAIL
    assert "not used as a substitute" in fresh.reason


def test_missing_schema_field_blocks(engine, principals):
    r = run(engine, principals, "inventory_missing_field")
    assert r.decision is Decision.BLOCKED and ("inventory", "schema") in failing(r)


def test_missing_requested_warehouse_blocks(engine, principals):
    r = run(engine, principals, "missing_warehouse_c")
    cov = [
        c for c in r.checks if c.requirement == "inventory" and c.check == "coverage" and c.status is CheckStatus.FAIL
    ]
    assert r.decision is Decision.BLOCKED and cov[0].evidence["missing"] == ["WH-C"]


def test_schema_complete_but_coverage_missing_still_blocks(engine, principals):
    r = run(engine, principals, "missing_warehouse_c")
    statuses = {c.check: c.status for c in r.checks if c.requirement == "inventory" and c.check == "schema"}
    assert statuses["schema"] is CheckStatus.PASS and r.decision is Decision.BLOCKED


def test_missing_lead_time_lane_blocks(engine, principals):
    r = run(engine, principals, "missing_lane")
    assert r.decision is Decision.BLOCKED and ("transfer_lead_times", "coverage") in failing(r)


def test_denied_row_scope_blocks_and_hides_metadata(engine, principals):
    r = run(engine, principals, user="planner_ab")
    assert r.decision is Decision.BLOCKED
    access = next(c for c in r.checks if c.requirement == "inventory" and c.check == "access")
    assert access.status is CheckStatus.FAIL and access.evidence["denied_scope"] == ["WH-C"]
    dumped = r.model_dump_json()
    # No asset reference, source name or version for the assets the user may not see.
    assert "sap_inventory_snapshot" not in dumped and "S4HANA" not in dumped and "SAP-SYN" not in dumped
    assert all(
        c.status is CheckStatus.SKIPPED for c in r.checks if c.requirement == "inventory" and c.check == "other_checks"
    )


def test_unknown_user_is_treated_as_denied(engine):
    ghost = Principal(principal_id="someone-else", kind="user", source="demo_selector", authenticated=False)
    agent = Principal(
        principal_id="demo-agent-scinsight-supervisor", kind="service", source="demo_selector", authenticated=False
    )
    r = engine.evaluate("warehouse_transfer", TRANSFER_CONTEXT, ghost, agent, identity_mode="demo_selector")
    access = next(c for c in r.checks if c.check == "access")
    assert r.decision is Decision.BLOCKED and "unknown" in access.reason


def test_no_identity_and_unavailable_authorisation_block(engine, principals):
    r = engine.evaluate("warehouse_transfer", TRANSFER_CONTEXT, None, None, identity_mode="demo_selector")
    assert r.decision is Decision.BLOCKED and all(
        c.status is CheckStatus.ERROR for c in r.checks if c.check == "access"
    )
    r = run(engine, principals, "authorization_unavailable")
    assert r.decision is Decision.BLOCKED and "fail closed" in r.reasons[0]


def test_agent_access_is_checked_separately(engine, principals):
    assert run(engine, principals, agent="untrusted_agent").decision is Decision.BLOCKED
    assert run(engine, principals, agent=None).decision is Decision.BLOCKED


def test_warning_continues_only_when_profile_permits(profiles, adapter, principals):
    assert profiles["warehouse_transfer"].allow_warnings is True
    engine = ReadinessEngine(profiles, adapter, clock=lambda: NOW)
    assert run(engine, principals, "forecast_aging").decision is Decision.READY_WITH_WARNINGS

    strict = dict(
        profiles, warehouse_transfer=profiles["warehouse_transfer"].model_copy(update={"allow_warnings": False})
    )
    r = run(ReadinessEngine(strict, adapter, clock=lambda: NOW), principals, "forecast_aging")
    assert r.decision is Decision.BLOCKED and "does not permit continuing with warnings" in r.reasons[-1]


def test_optional_asset_missing_is_a_warning(profiles, adapter, principals):
    p = profiles["warehouse_transfer"]
    assets = [a.model_copy(update={"mandatory": False}) if a.name == "transfer_lead_times" else a for a in p.assets]
    engine = ReadinessEngine(
        {"warehouse_transfer": p.model_copy(update={"assets": assets})}, adapter, clock=lambda: NOW
    )
    assert run(engine, principals, "lead_times_unavailable").decision is Decision.READY_WITH_WARNINGS


def test_missing_provenance_follows_profile(engine, principals):
    assert run(engine, principals, "inventory_missing_owner").decision is Decision.BLOCKED  # on_missing: block
    r = run(engine, principals, "forecast_missing_owner")  # on_missing: warn
    assert r.decision is Decision.READY_WITH_WARNINGS
    prov = next(c for c in r.checks if c.requirement == "demand_forecast" and c.check == "provenance")
    assert prov.status is CheckStatus.WARN and prov.evidence["missing"] == ["business_owner"]


def test_missing_context_blocks_before_asset_checks(engine, principals):
    ctx = {k: v for k, v in TRANSFER_CONTEXT.items() if k != "planning_period"}
    r = run(engine, principals, context=ctx)
    assert r.decision is Decision.BLOCKED and [c.check for c in r.checks] == ["context"]


def test_unknown_profile_raises_and_is_audited(engine, principals, audit):
    u, a = principals()
    with pytest.raises(UnknownTaskProfile):
        engine.evaluate("price_optimisation", {}, u, a, identity_mode="demo_selector")
    assert audit.entries[-1]["decision"] == "BLOCKED" and audit.entries[-1]["profile_version"] is None


def test_demo_identity_is_never_authenticated(engine, principals):
    u, a = principals()
    assert u.source == "demo_selector" and u.authenticated is False and a.authenticated is False
    r = run(engine, principals)
    assert r.authenticated is False and r.identity_mode == "demo_selector"


def test_audit_entry_is_pseudonymous_and_minimal(engine, principals, audit):
    run(engine, principals)
    e = audit.entries[-1]
    assert e["user_ref"].startswith("sha256:") and "demo-user-planner-all" not in str(e)
    assert set(e) >= {
        "evaluation_id",
        "task_id",
        "profile_version",
        "evaluated_at",
        "decision",
        "checks",
        "evidence_refs",
        "adapter_mode",
    }
    assert "question" not in e


def test_policy_preflight_counts_but_does_not_name_hidden_documents(engine, principals):
    r = run(engine, principals, task="policy_document_qa", context={})
    assert r.decision is Decision.READY and r.post_retrieval_check_required is True
    docs = next(c for c in r.checks if c.check == "documents")
    assert {d["doc_id"] for d in docs.evidence["usable_documents"]} == {"POL-RET-001", "POL-WH-020"}
    assert docs.evidence["excluded_counts"] == {
        "status revoked": 1,
        "status draft": 1,
        "not authorised for this caller": 1,
    }
    assert "POL-FIN-900" not in r.model_dump_json() and "retriever must still find" in docs.reason


def test_policy_blocked_for_user_without_rights(engine, principals):
    assert (
        run(engine, principals, task="policy_document_qa", context={}, user="contractor").decision is Decision.BLOCKED
    )


def test_protected_retriever_filters_revoked_draft_and_unauthorised(adapter, principals):
    u, a = principals()
    out = DemoProtectedPolicyRetriever(adapter, lambda: NOW).search(
        u, a, "What does the supplier policy say about returns?"
    )
    ids = [h["doc_id"] for h in out["hits"]]
    assert ids == ["POL-RET-001"] and out["filtered_out"] == 3 and out["evidence_check"]["sufficient"] is True
    none = DemoProtectedPolicyRetriever(adapter, lambda: NOW).search(u, a, "carbon offsets for air freight")
    assert none["hits"] == [] and none["evidence_check"]["sufficient"] is False


def test_direct_retrieval_without_preflight_still_enforces_access(adapter, principals):
    """Access-bypass test: skipping the readiness preflight must not expose documents."""
    retriever = DemoProtectedPolicyRetriever(adapter, lambda: NOW)
    question = "What does the supplier policy say about returns and payment terms?"
    for user, agent in (("contractor", "scinsight_supervisor"), ("planner_all", "untrusted_agent")):
        u, a = principals(user, agent)
        assert retriever.search(u, a, question)["hits"] == []
    u, a = principals("planner_all")
    assert "POL-FIN-900" not in [h["doc_id"] for h in retriever.search(u, a, question)["hits"]]
    assert retriever.search(None, None, question)["hits"] == []
    down = DemoProtectedPolicyRetriever(adapter.with_scenario("authorization_unavailable"), lambda: NOW)
    assert down.search(u, a, question)["hits"] == []


def test_blocked_result_never_invokes_downstream(engine, principals):
    calls = []
    u, a = principals("planner_ab")
    result, value = guarded_call(
        engine,
        task_id="warehouse_transfer",
        context=TRANSFER_CONTEXT,
        user=u,
        agent=a,
        identity_mode="demo_selector",
        downstream=lambda r: calls.append(r) or "ran",
    )
    assert result.decision is Decision.BLOCKED and value is None and calls == []


def test_use_time_recheck_catches_a_change_after_preflight(engine, principals):
    u, a = principals()
    assert (
        engine.evaluate("warehouse_transfer", TRANSFER_CONTEXT, u, a, identity_mode="demo_selector").decision
        is Decision.READY
    )
    calls = []
    changed = engine.adapter.with_scenario("stale_inventory")  # the source changed after the preflight
    result, value = guarded_call(
        engine,
        task_id="warehouse_transfer",
        context=TRANSFER_CONTEXT,
        user=u,
        agent=a,
        identity_mode="demo_selector",
        adapter=changed,
        downstream=lambda r: calls.append(r),
    )
    assert result.decision is Decision.BLOCKED and calls == []


# ------------------------------------------------------------------ check functions and profile validation


def test_freshness_unit_rules():
    rule = FreshnessRule(max_age_hours=24, warn_age_hours=12, on_missing_event_time="warn")
    assert check_freshness(rule, NOW - timedelta(hours=1), None, NOW).status is CheckStatus.PASS
    assert check_freshness(rule, NOW - timedelta(hours=13), None, NOW).status is CheckStatus.WARN
    assert check_freshness(rule, NOW - timedelta(hours=25), None, NOW).status is CheckStatus.FAIL
    assert check_freshness(rule, NOW + timedelta(hours=1), None, NOW).status is CheckStatus.FAIL  # future
    assert check_freshness(rule, None, NOW, NOW).status is CheckStatus.WARN  # profile: warn
    with pytest.raises(ValueError):
        check_freshness(rule, NOW, None, datetime(2026, 11, 14, 8, 0))  # naive clock


@pytest.mark.parametrize(
    "patch, message",
    [
        ({"surprise": 1}, "Extra inputs"),
        ({"version": "one"}, "version"),
        ({"demo_values_note": ""}, "demo_values_note"),
        ({"assets": []}, "assets"),
    ],
)
def test_invalid_profiles_are_rejected_on_load(tmp_path, profiles, patch, message):
    import yaml

    raw = profiles["warehouse_transfer"].model_dump(by_alias=True, mode="json", exclude_none=True)
    raw.update(patch)
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ProfileError, match=message):
        load_profile(path)


def test_profile_cannot_refer_to_undeclared_context(profiles):
    raw = profiles["warehouse_transfer"].model_dump(by_alias=True, mode="json", exclude_none=True)
    raw["required_context"] = [f for f in raw["required_context"] if f["name"] != "planning_period"]
    with pytest.raises(ValueError, match="undeclared context"):
        TaskProfile.model_validate(raw)


def test_freshness_threshold_order_is_validated():
    with pytest.raises(ValueError):
        FreshnessRule(max_age_hours=10, warn_age_hours=20)
    with pytest.raises(ValueError):
        FreshnessRule(max_age_hours=0)


def test_results_do_not_depend_on_the_real_date(profiles, adapter, principals):
    later = ReadinessEngine(profiles, adapter, clock=lambda: datetime(2027, 6, 1, tzinfo=UTC))
    assert run(later, principals).decision is Decision.BLOCKED  # the same synthetic data is stale in 2027
