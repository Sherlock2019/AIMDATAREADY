"""Access check: may this user AND this calling agent use this asset, for this purpose and row scope?

* No identity -> ERROR (cannot be evaluated) -> blocks.
* Authorisation unknown -> FAIL (unknown is treated as denied).
* Authorisation service unavailable -> ERROR -> blocks (fail closed).
* Row scope: the warehouses named in the task context must all be inside the user's warehouse scope.

This is a PREFLIGHT. The service that actually returns the rows must enforce the same rules itself (for example
PostgreSQL row-level security or a protected API); this check does not and cannot replace that.
"""

from __future__ import annotations

from app.adapters.base import AdapterUnavailable, ReadinessAdapter
from app.checks import Outcome, errored, failed, passed
from app.models import Principal
from app.profiles import AssetRequirement


def row_scope(requirement: AssetRequirement, context: dict[str, str]) -> dict[str, list[str]]:
    values = [context[k] for k in requirement.access.row_scope_context_keys if context.get(k)]
    scope = {"warehouse": sorted(set(values))} if values else {}
    if requirement.access.columns:  # column-level ACL: restricted columns this task needs
        scope["columns"] = list(requirement.access.columns)
    return scope


def check_access(
    adapter: ReadinessAdapter,
    requirement: AssetRequirement,
    user: Principal | None,
    agent: Principal | None,
    purpose: str,
    context: dict[str, str],
) -> Outcome:
    if user is None:
        return errored("No trusted user identity was provided, so access cannot be evaluated.")
    if requirement.access.require_agent_access and agent is None:
        return errored("No calling-agent identity was provided, so agent access cannot be evaluated.")
    scope = row_scope(requirement, context)
    try:
        decisions = [("user", adapter.authorize(user, requirement.asset_id, purpose, scope))]
        if requirement.access.require_agent_access and agent is not None:
            decisions.append(("agent", adapter.authorize(agent, requirement.asset_id, purpose, scope)))
    except AdapterUnavailable:
        return errored("The authorisation service did not answer; blocked because access is unknown (fail closed).")
    for who, decision in decisions:
        if decision.allowed is None:
            return failed(f"Access for the {who} is unknown ({decision.reason}); unknown is treated as denied.")
        if decision.allowed is False:
            return failed(f"Access denied for the {who}: {decision.reason}.", denied_scope=list(decision.denied_scope))
    return passed(
        "User and agent are permitted for this purpose and scope (demo policy)."
        if len(decisions) > 1
        else "User is permitted for this purpose and scope (demo policy).",
        purpose=purpose,
        row_scope=scope,
    )
