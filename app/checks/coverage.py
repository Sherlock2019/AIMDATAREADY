"""Coverage check: does the data actually contain the product, warehouses, period or lane THIS task is about?

A table can have a perfect schema and still have no rows for Warehouse C. Schema completeness is not task coverage.

Dimensions:
  product, warehouse  every requested value must be in the covered list;
  lane                "<origin>><destination>" must be in the covered list (e.g. "WH-A>WH-C");
  period              the requested "start/end" range must lie inside one covered "start/end" range.
"""

from __future__ import annotations

from app.checks import Outcome, errored, failed, passed
from app.checks.context import period_bounds
from app.profiles import CoverageRule


def requested_values(rule: CoverageRule, context: dict[str, str]) -> list[str]:
    if rule.dimension == "lane":
        return [f"{context.get(rule.context_keys[0], '')}>{context.get(rule.context_keys[1], '')}"]
    return [context.get(k, "") for k in rule.context_keys]


def check_coverage(rule: CoverageRule, context: dict[str, str], covered: list[str] | None) -> Outcome:
    outcome = _check(rule, context, covered)
    outcome.evidence["dimension"] = rule.dimension  # lets a remediation planner know what is missing
    return outcome


def _check(rule: CoverageRule, context: dict[str, str], covered: list[str] | None) -> Outcome:
    requested = requested_values(rule, context)
    if covered is None:
        return errored(f"No {rule.dimension} coverage evidence is available for this asset.", requested=requested)
    if rule.dimension == "period":
        wanted = period_bounds(requested[0])
        if wanted is None:
            return failed("The requested planning period is not a valid 'YYYY-MM-DD/YYYY-MM-DD' range.")
        for span in covered:
            have = period_bounds(span)
            if have and have[0] <= wanted[0] and wanted[1] <= have[1]:
                return passed(f"Period {requested[0]} is covered.", requested=requested, covered=covered)
        return failed(
            f"Period {requested[0]} is not fully covered by the available data.", requested=requested, covered=covered
        )
    missing = [v for v in requested if v not in set(covered)]
    if missing:
        return failed(
            f"No {rule.dimension} data for: " + ", ".join(missing) + ".", requested=requested, missing=missing
        )
    return passed(
        f"{rule.dimension.capitalize()} coverage present for: " + ", ".join(requested) + ".", requested=requested
    )
