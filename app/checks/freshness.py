"""Freshness check: is the SOURCE data recent enough for the task?

Two timestamps matter and they are never mixed up:
  source_event_time  when the data was true at the source (e.g. the SAP snapshot "as of" time);
  ingested_at        when the copy was loaded here.
A copy of three-day-old data loaded five minutes ago is three days old. If the event time is missing, the profile
decides (block or warn); the ingestion time is never silently used instead.

All timestamps must be timezone-aware. "now" is passed in (an injectable clock) so tests do not depend on today.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.checks import Outcome, failed, passed, warned
from app.profiles import FreshnessRule

CLOCK_SKEW = timedelta(minutes=5)  # tolerated difference between the source clock and ours


def _hours(delta: timedelta) -> float:
    return round(delta.total_seconds() / 3600, 1)


def check_freshness(
    rule: FreshnessRule, event_time: datetime | None, ingested_at: datetime | None, now: datetime
) -> Outcome:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    evidence = {
        "source_event_time": event_time.isoformat() if event_time else None,
        "ingested_at": ingested_at.isoformat() if ingested_at else None,
        "max_age_hours": rule.max_age_hours,
        "warn_age_hours": rule.warn_age_hours,
    }
    if event_time is None:
        reason = "The source event time is unknown"
        if ingested_at is not None:
            reason += " (an ingestion time exists, but it is not used as a substitute for source time)"
        if rule.on_missing_event_time == "warn":
            return warned(reason + "; the profile allows continuing with a warning.", **evidence)
        return failed(reason + "; the profile blocks in this case.", **evidence)
    if event_time.tzinfo is None:
        return failed("The source event time has no timezone and cannot be compared safely.", **evidence)
    if event_time > now + CLOCK_SKEW:
        return failed("The source event time is in the future; the timestamp cannot be trusted.", **evidence)

    age = now - event_time
    evidence["age_hours"] = _hours(age)
    if age > timedelta(hours=rule.max_age_hours):
        reason = f"Source data is {_hours(age)} h old; the profile allows at most {rule.max_age_hours:g} h."
        if ingested_at is not None and now - ingested_at <= timedelta(hours=rule.max_age_hours):
            reason += (
                f" It was ingested {_hours(now - ingested_at)} h ago, but a recent ingestion does not make "
                "old source data fresh."
            )
        return failed(reason, **evidence)
    if rule.warn_age_hours is not None and age > timedelta(hours=rule.warn_age_hours):
        return warned(
            f"Source data is {_hours(age)} h old: within the {rule.max_age_hours:g} h limit but past the "
            f"{rule.warn_age_hours:g} h warning threshold.",
            **evidence,
        )
    return passed(f"Source data is {_hours(age)} h old (limit {rule.max_age_hours:g} h).", **evidence)
