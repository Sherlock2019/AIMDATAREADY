"""The adapter interface: everything the engine needs to know about data and permissions, behind one small class.

The engine never talks to SAP, PostgreSQL, a vector store, a catalog product or an identity system directly. It asks
an adapter. To connect a real system, write a new adapter (see README "How to connect a new data adapter"):

    get_asset()        -> catalog record (owner, version, timestamps, schema, coverage)   e.g. OpenMetadata
    get_schema()       -> available fields                                                e.g. information_schema
    get_coverage()     -> which products / warehouses / periods / lanes have data          e.g. SELECT DISTINCT
    get_documents()    -> document metadata (status, validity, version)                    e.g. the RAG ingestion table
    authorize()        -> allow / deny / unknown for a principal, purpose and row scope     e.g. OPA, Cedar, your IAM
    contract_status()  -> data-contract validation status, if the platform has one

Rules every adapter must follow:
  * raise AdapterUnavailable when a system cannot be reached (the engine then BLOCKS: fail closed);
  * return allowed=None when authorisation is unknown (the engine treats unknown as denied);
  * report mode="real" only when it really talks to the system of record.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from app.catalog import CatalogAsset, DocumentRecord
from app.models import Principal


class AdapterUnavailable(Exception):
    """A catalog, data source or authorisation service could not be reached or did not answer."""


@dataclass(frozen=True)
class AuthzDecision:
    allowed: bool | None  # None = unknown -> treated as denied
    reason: str  # safe to show to the caller
    denied_scope: tuple[str, ...] = field(default_factory=tuple)


class ReadinessAdapter(ABC):
    mode: Literal["demo", "real"] = "demo"

    @abstractmethod
    def get_asset(self, asset_id: str) -> CatalogAsset | None:
        """The catalog record, or None when the asset is not registered/available."""

    @abstractmethod
    def authorize(
        self,
        principal: Principal,
        asset_id: str,
        purpose: str,
        scope: dict[str, list[str]],
        document: DocumentRecord | None = None,
    ) -> AuthzDecision:
        """May this principal use this asset (or one document of it) for this purpose and row scope?"""

    # The defaults below read the catalog record. A real adapter can override them to query the source itself.
    def get_timestamps(self, asset_id: str) -> tuple[datetime | None, datetime | None]:
        asset = self.get_asset(asset_id)
        return (asset.source_event_time, asset.ingested_at) if asset else (None, None)

    def get_schema(self, asset_id: str) -> list[str] | None:
        asset = self.get_asset(asset_id)
        return asset.schema_fields if asset else None

    def get_coverage(self, asset_id: str, dimension: str) -> list[str] | None:
        asset = self.get_asset(asset_id)
        if asset is None or dimension not in asset.coverage:
            return None
        return asset.coverage[dimension]

    def get_documents(self, asset_id: str) -> list[DocumentRecord]:
        asset = self.get_asset(asset_id)
        return list(asset.documents) if asset else []

    def get_rows(self, asset_id: str) -> list[dict[str, str]] | None:
        """Row-level evidence for quality checks. None = this adapter cannot provide rows (quality -> ERROR)."""
        return None

    def get_record(self, asset_id: str) -> dict | None:
        """The full catalog record (all fields) for the metadata-completeness check; None = not available."""
        return None

    def get_contract(self, asset_id: str) -> dict | None:
        """The producer's data contract for the metadata-completeness check; None = not available."""
        return None

    def contract_status(self, asset_id: str) -> str | None:
        asset = self.get_asset(asset_id)
        return asset.contract_ref if asset else None
