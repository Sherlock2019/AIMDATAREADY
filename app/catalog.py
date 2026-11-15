"""A lightweight data catalog for the PoC (catalog/demo_catalog.yaml).

One record per data asset: who owns it, where it comes from, which version/snapshot it is, when the source data was
produced (source_event_time) and when it was loaded (ingested_at), its schema, its coverage and its access policy.

Everything in the demo catalog is SYNTHETIC. The lineage fields describe the demo records only; they are not
production lineage. Missing fields are allowed on purpose, so the provenance check can show what happens when an
owner or a version is unknown.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError


class CatalogError(ValueError):
    """The catalog file is missing, unreadable or invalid."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class AccessPolicy(_Strict):
    """Demo access policy. A real deployment keeps this in the identity/policy system, not in a YAML file."""

    roles: list[str] = Field(default_factory=list)  # user roles allowed to use the asset
    agents: list[str] = Field(default_factory=list)  # agent/service ids allowed to use the asset
    purposes: list[str] = Field(default_factory=list)  # purposes the asset may be used for
    row_scope: Literal["warehouse"] | None = None  # rows are restricted by the user's warehouse scope


class DocumentRecord(_Strict):
    doc_id: str
    title: str
    version: str | None = None
    source_id: str | None = None  # where the approved copy lives (document management system id)
    status: Literal["approved", "draft", "revoked", "superseded"]
    valid_from: date | None = None
    valid_to: date | None = None
    classification: Literal["public", "internal", "confidential", "restricted"] = "internal"
    access_roles: list[str] = Field(default_factory=list)
    purposes: list[str] = Field(default_factory=list)
    text: str = ""  # demo body used by the demo protected retriever


class CatalogAsset(_Strict):
    asset_id: str
    kind: Literal["table", "document_set"]
    description: str = ""
    classification: Literal["public", "internal", "confidential", "restricted"]
    synthetic: bool = True
    # provenance (any of these may be missing; the profile decides whether that blocks)
    business_owner: str | None = None
    technical_owner: str | None = None
    source_name: str | None = None
    source_version: str | None = None
    source_event_time: AwareDatetime | None = None  # when the data was true at the source (e.g. snapshot as-of)
    ingested_at: AwareDatetime | None = None  # when it was loaded here (NOT a substitute for event time)
    contract_ref: str | None = None
    known_limitations: list[str] = Field(default_factory=list)
    # evidence used by the checks
    schema_fields: list[str] | None = Field(default=None, alias="schema")
    coverage: dict[str, list[str]] = Field(default_factory=dict)  # dimension -> covered values
    access: AccessPolicy = Field(default_factory=AccessPolicy)
    documents: list[DocumentRecord] = Field(default_factory=list)


class Catalog(_Strict):
    catalog_version: str
    notice: str
    assets: list[CatalogAsset]

    def by_id(self) -> dict[str, CatalogAsset]:
        return {a.asset_id: a for a in self.assets}


def load_yaml(path: Path) -> dict:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CatalogError(f"{Path(path).name}: cannot read: {exc}") from exc
    if not isinstance(raw, dict):
        raise CatalogError(f"{Path(path).name}: must be a YAML mapping")
    return raw


def load_catalog(path: Path) -> Catalog:
    try:
        return Catalog.model_validate(load_yaml(path))
    except ValidationError as exc:
        raise CatalogError(f"{Path(path).name}: invalid catalog: {exc}") from exc
