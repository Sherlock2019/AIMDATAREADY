"""DatasetAdapter: evidence from REAL files (CSV + manifest.yaml) instead of catalog claims.

* schema    = the CSV header
* coverage  = distinct values actually present (products, warehouses, lanes, forecast weeks)
* rows      = the CSV rows, for the quality check
* metadata  = manifest.yaml (owners, versions, source event time, ingestion time)
* access    = delegated to the DemoAdapter (same synthetic identities and DEMO permissions)

The files under datasets/ are synthetic. This adapter shows how evidence can be derived from data; it is still
mode="demo" because permissions come from the demo directory.
"""

from __future__ import annotations

import csv
from datetime import date, timedelta
from pathlib import Path

from app.adapters.base import AuthzDecision, ReadinessAdapter
from app.adapters.demo import DemoAdapter
from app.catalog import CatalogAsset, DocumentRecord, load_yaml
from app.models import Principal

METADATA_FIELDS = (
    "business_owner",
    "technical_owner",
    "source_name",
    "source_version",
    "source_event_time",
    "ingested_at",
    "contract_ref",
    "known_limitations",
)


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with Path(path).open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows = [dict(r) for r in reader]
        return list(reader.fieldnames or []), rows


def write_csv(path: Path, header: list[str], rows: list[dict[str, str]]) -> None:
    with Path(path).open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def coverage_from_rows(header: list[str], rows: list[dict[str, str]]) -> dict[str, list[str]]:
    def distinct(col: str) -> list[str]:
        return sorted({str(r.get(col) or "").strip() for r in rows} - {""})

    cov: dict[str, list[str]] = {}
    if "product_id" in header:
        cov["product"] = distinct("product_id")
    if "warehouse_id" in header:
        cov["warehouse"] = distinct("warehouse_id")
    if {"origin_warehouse", "destination_warehouse"} <= set(header):
        cov["lane"] = sorted(
            {
                f"{(r.get('origin_warehouse') or '').strip()}>{(r.get('destination_warehouse') or '').strip()}"
                for r in rows
            }
        )
    if "week_start" in header:
        weeks = []
        for value in distinct("week_start"):
            try:
                weeks.append(date.fromisoformat(value))
            except ValueError:
                continue
        if weeks:
            cov["period"] = [f"{min(weeks).isoformat()}/{(max(weeks) + timedelta(days=6)).isoformat()}"]
    return cov


class DatasetAdapter(ReadinessAdapter):
    mode = "demo"

    def __init__(self, folder: Path, authz: DemoAdapter):
        self.folder, self.authz = Path(folder), authz
        self.manifest = load_yaml(self.folder / "manifest.yaml")

    def asset_meta(self, asset_id: str) -> dict | None:
        return (self.manifest.get("assets") or {}).get(asset_id)

    def asset_file(self, asset_id: str) -> Path | None:
        meta = self.asset_meta(asset_id)
        if not meta or not meta.get("file"):
            return None
        path = self.folder / meta["file"]
        return path if path.exists() else None

    def get_asset(self, asset_id: str) -> CatalogAsset | None:
        path, base = self.asset_file(asset_id), self.authz.catalog.by_id().get(asset_id)
        if path is None or base is None:
            return None
        header, rows = read_csv(path)
        meta = self.asset_meta(asset_id)
        fields = {k: meta.get(k) for k in METADATA_FIELDS if k in meta}
        fields["known_limitations"] = fields.get("known_limitations") or []
        return CatalogAsset.model_validate(
            {
                "asset_id": asset_id,
                "kind": base.kind,
                "description": base.description,
                "classification": base.classification,
                "synthetic": bool(self.manifest.get("synthetic", True)),
                **fields,
                "schema": header,
                "coverage": coverage_from_rows(header, rows),
                "access": base.access.model_dump(),
            }
        )

    def get_rows(self, asset_id: str) -> list[dict[str, str]] | None:
        path = self.asset_file(asset_id)
        return read_csv(path)[1] if path else None

    def authorize(
        self,
        principal: Principal,
        asset_id: str,
        purpose: str,
        scope: dict[str, list[str]],
        document: DocumentRecord | None = None,
    ) -> AuthzDecision:
        return self.authz.authorize(principal, asset_id, purpose, scope, document)
