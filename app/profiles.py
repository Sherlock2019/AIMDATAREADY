"""Task profiles: what data a task needs and which checks it must pass (profiles/*.yaml).

A profile is configuration, not code. It is validated when it is loaded: an unknown key, a missing version, a
threshold <= 0 or a coverage rule that refers to an undeclared context field stops the service from starting, so a
typo can never silently weaken a check.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class ProfileError(ValueError):
    """A profile file is missing, unreadable or invalid."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ContextField(_Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    pattern: str  # regular expression the value must match (fullmatch)
    description: str = ""

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, value: str) -> str:
        re.compile(value)
        return value


class AccessRule(_Strict):
    require_agent_access: bool = True  # the calling agent/service must be authorised too, not only the user
    row_scope_context_keys: list[str] = Field(default_factory=list)  # context values the user must be scoped to
    columns: list[str] = Field(default_factory=list)  # restricted columns the task needs (column-level ACL)


class FreshnessRule(_Strict):
    max_age_hours: float = Field(gt=0)  # older than this -> FAIL
    warn_age_hours: float | None = Field(default=None, gt=0)  # older than this (but within max) -> WARN
    on_missing_event_time: Literal["block", "warn"] = "block"

    @model_validator(mode="after")
    def _ordered(self) -> FreshnessRule:
        if self.warn_age_hours is not None and self.warn_age_hours >= self.max_age_hours:
            raise ValueError("warn_age_hours must be lower than max_age_hours")
        return self


class SchemaRule(_Strict):
    required_fields: list[str] = Field(min_length=1)


class CoverageRule(_Strict):
    dimension: Literal["product", "warehouse", "period", "lane"]
    context_keys: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _shape(self) -> CoverageRule:
        if self.dimension == "lane" and len(self.context_keys) != 2:
            raise ValueError("a lane coverage rule needs exactly two context keys (origin, destination)")
        if self.dimension == "period" and len(self.context_keys) != 1:
            raise ValueError("a period coverage rule needs exactly one context key")
        return self


class ProvenanceRule(_Strict):
    required_fields: list[
        Literal[
            "business_owner",
            "technical_owner",
            "source_name",
            "source_version",
            "source_event_time",
            "ingested_at",
            "contract_ref",
        ]
    ] = Field(min_length=1)
    on_missing: Literal["block", "warn"] = "block"


class DocumentRule(_Strict):
    require_status: Literal["approved"] = "approved"
    require_valid_on_evaluation_date: bool = True
    require_version_and_source_id: bool = True
    min_usable_documents: int = Field(default=1, ge=1)


class QualityRule(_Strict):
    """Row-level rules, evaluated only by adapters that can return rows (e.g. the dataset adapter)."""

    unique_key: list[str] = Field(default_factory=list)  # no two rows may share these values
    not_null: list[str] = Field(default_factory=list)  # value must be present
    non_negative: list[str] = Field(default_factory=list)  # value must be a number >= 0
    allowed_values: dict[str, list[str]] = Field(default_factory=dict)  # exact codes allowed per column
    iso_dates: list[str] = Field(default_factory=list)  # value must be YYYY-MM-DD
    # column -> context keys: only rows that belong to this task are checked (e.g. the requested warehouses)
    scope: dict[str, list[str]] = Field(default_factory=dict)


class MetadataRule(_Strict):
    """Catalog record and data contract must be complete (fields / sections listed here)."""

    catalog_fields: list[str] = Field(default_factory=list)
    contract_sections: list[str] = Field(default_factory=list)


class AssetRequirement(_Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")  # shown to callers instead of the catalog source name
    asset_id: str
    mandatory: bool = True
    access: AccessRule = Field(default_factory=AccessRule)
    freshness: FreshnessRule | None = None
    schema_rule: SchemaRule | None = Field(default=None, alias="schema")
    coverage: list[CoverageRule] = Field(default_factory=list)
    quality: QualityRule | None = None
    provenance: ProvenanceRule | None = None
    metadata: MetadataRule | None = None
    documents: DocumentRule | None = None


class TaskProfile(_Strict):
    task_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    description: str
    purpose: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")  # the declared purpose checked against asset permissions
    demo_values_note: str = Field(min_length=10)  # forces every profile to say its thresholds are examples
    allow_warnings: bool = False  # may a READY_WITH_WARNINGS result continue? (False -> warnings block)
    post_retrieval_check_required: bool = False
    required_context: list[ContextField] = Field(default_factory=list)
    assets: list[AssetRequirement] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> TaskProfile:
        declared = {f.name for f in self.required_context}
        names = [a.name for a in self.assets]
        if len(names) != len(set(names)):
            raise ValueError("asset requirement names must be unique")
        for asset in self.assets:
            used = set(asset.access.row_scope_context_keys)
            for rule in asset.coverage:
                used |= set(rule.context_keys)
            if asset.quality:
                for keys in asset.quality.scope.values():
                    used |= set(keys)
            unknown = used - declared
            if unknown:
                raise ValueError(f"asset {asset.name!r} refers to undeclared context fields: {sorted(unknown)}")
        return self


def load_profile(path: Path) -> TaskProfile:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ProfileError(f"{path.name}: cannot read profile: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProfileError(f"{path.name}: a profile must be a YAML mapping")
    try:
        return TaskProfile.model_validate(raw)
    except ValidationError as exc:
        raise ProfileError(f"{path.name}: invalid profile: {exc}") from exc


def load_profiles(directory: Path) -> dict[str, TaskProfile]:
    """Load and validate every profiles/*.yaml. Raises ProfileError on the first invalid file."""
    files = sorted(Path(directory).glob("*.yaml"))
    if not files:
        raise ProfileError(f"no task profiles found in {directory}")
    profiles: dict[str, TaskProfile] = {}
    for path in files:
        profile = load_profile(path)
        if profile.task_id in profiles:
            raise ProfileError(f"{path.name}: duplicate task_id {profile.task_id!r}")
        profiles[profile.task_id] = profile
    return profiles
