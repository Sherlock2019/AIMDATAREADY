"""Schema check: are the fields the task needs present?

This checks field PRESENCE only. It does not prove that the values are correct, complete or mean what the task
assumes (a quantity column can exist and still hold wrong numbers). Coverage is a separate check.
"""

from __future__ import annotations

from app.checks import Outcome, errored, failed, passed
from app.profiles import SchemaRule

NOTE = "Field presence only; this does not prove the values are semantically correct."


def check_schema(rule: SchemaRule, available_fields: list[str] | None) -> Outcome:
    if available_fields is None:
        return errored("The asset's schema is unknown, so required fields cannot be checked.")
    missing = [f for f in rule.required_fields if f not in set(available_fields)]
    if missing:
        return failed("Required field(s) missing: " + ", ".join(missing) + ".", missing=missing, note=NOTE)
    return passed("All required fields are present.", required=rule.required_fields, note=NOTE)
