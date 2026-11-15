"""Context check: does the request carry every task parameter the profile needs, in the expected format?"""

from __future__ import annotations

import re

from app.checks import Outcome, failed, passed
from app.profiles import TaskProfile


def check_context(profile: TaskProfile, context: dict[str, str]) -> Outcome:
    missing = [f.name for f in profile.required_context if not str(context.get(f.name, "")).strip()]
    invalid = [
        f.name
        for f in profile.required_context
        if f.name not in missing and not re.fullmatch(f.pattern, str(context[f.name]).strip())
    ]
    if missing or invalid:
        parts = []
        if missing:
            parts.append("missing task context: " + ", ".join(missing))
        if invalid:
            parts.append("invalid format for: " + ", ".join(invalid))
        return failed("; ".join(parts).capitalize() + ".", missing=missing, invalid=invalid)
    return passed("All required task context is present.", fields=[f.name for f in profile.required_context])


def period_bounds(value: str) -> tuple[str, str] | None:
    """'2026-11-16/2026-11-29' -> ('2026-11-16', '2026-11-29'); ISO dates compare correctly as strings."""
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})/(\d{4}-\d{2}-\d{2})", value.strip())
    if not match or match.group(1) > match.group(2):
        return None
    return match.group(1), match.group(2)
