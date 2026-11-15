"""Quality check: are the rows THIS task will use well-formed?

Only rows inside the task's scope are checked (e.g. product SKU-001 in warehouses WH-A and WH-C), because a bad row
in an unrelated warehouse should not block this task. Rules (all optional, set in the profile):

  unique_key      no two rows share the key (duplicates)
  not_null        value present
  non_negative    a number >= 0
  allowed_values  one of the listed codes, exactly
  iso_dates       YYYY-MM-DD

Passing does not prove the values are TRUE (a quantity can be well-formed and wrong); it proves they are usable.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date

from app.checks import Outcome, errored, failed, passed
from app.profiles import QualityRule

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SAMPLE = 5


def in_scope(rule: QualityRule, context: dict[str, str], row: dict[str, str]) -> bool:
    for column, keys in rule.scope.items():
        wanted = {context.get(k, "") for k in keys}
        if str(row.get(column, "")).strip() not in wanted:
            return False
    return True


def row_key(rule: QualityRule, row: dict[str, str], index: int) -> str:
    if not rule.unique_key:
        return f"row {index + 1}"
    return "|".join(str(row.get(k, "")).strip() for k in rule.unique_key)


def is_iso_date(value: str) -> bool:
    if not ISO_DATE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def violations(rule: QualityRule, rows: list[dict[str, str]]) -> dict[str, dict]:
    """rule name -> {"count": n, "keys": [first few row keys], "column": ...}. Shared with the remediation planner."""
    found: dict[str, dict] = {}

    def hit(name: str, key: str, column: str | None = None) -> None:
        entry = found.setdefault(name, {"count": 0, "keys": [], "columns": []})
        entry["count"] += 1
        if len(entry["keys"]) < SAMPLE and key not in entry["keys"]:
            entry["keys"].append(key)
        if column and column not in entry["columns"]:
            entry["columns"].append(column)

    keys = [row_key(rule, r, i) for i, r in enumerate(rows)]
    if rule.unique_key:
        for key, n in Counter(keys).items():
            for _ in range(n - 1):
                hit("unique_key", key)
    for i, row in enumerate(rows):
        for col in rule.not_null:
            if not str(row.get(col) or "").strip():
                hit("not_null", keys[i], col)
        for col in rule.non_negative:
            value = str(row.get(col) or "").strip()
            if not value:
                continue  # reported by not_null
            try:
                if float(value) < 0:
                    hit("non_negative", keys[i], col)
            except ValueError:
                hit("non_negative", keys[i], col)
        for col, allowed in rule.allowed_values.items():
            value = row.get(col)
            if value is not None and str(value) != "" and str(value) not in allowed:
                hit("allowed_values", keys[i], col)
        for col in rule.iso_dates:
            value = str(row.get(col) or "").strip()
            if value and not is_iso_date(value):
                hit("iso_dates", keys[i], col)
    return found


def check_quality(rule: QualityRule, context: dict[str, str], rows: list[dict[str, str]] | None) -> Outcome:
    if rows is None:
        return errored("No row-level evidence is available, so data quality cannot be checked.")
    scoped = [r for r in rows if in_scope(rule, context, r)]
    found = violations(rule, scoped)
    required = len(scoped) * len(rule.not_null)  # required-field completeness: populated / required values
    missing = found.get("not_null", {}).get("count", 0)
    evidence = {
        "rows_checked": len(scoped),
        "rows_total": len(rows),
        "violations": found,
        "required_values": required,
        "required_values_present": required - missing,
    }
    if found:
        parts = [f"{v['count']} {name.replace('_', ' ')}" for name, v in found.items()]
        return failed(f"Data quality problems in the rows this task uses: {', '.join(parts)}.", **evidence)
    return passed(f"{len(scoped)} row(s) in scope pass the quality rules.", **evidence)
