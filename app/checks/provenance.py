"""Provenance check: is it recorded who owns the asset, where it comes from and which version it is?

The PoC only reads what the catalog says. It does not discover or verify lineage, and it never invents a value for a
missing field: a missing owner or version is reported and handled as the profile says (block or warn).
"""

from __future__ import annotations

from app.catalog import CatalogAsset
from app.checks import Outcome, failed, passed, warned
from app.profiles import ProvenanceRule


def check_provenance(rule: ProvenanceRule, asset: CatalogAsset) -> Outcome:
    recorded = {
        "business_owner": asset.business_owner,
        "technical_owner": asset.technical_owner,
        "source_name": asset.source_name,
        "source_version": asset.source_version,
        "source_event_time": asset.source_event_time.isoformat() if asset.source_event_time else None,
        "ingested_at": asset.ingested_at.isoformat() if asset.ingested_at else None,
        "contract_ref": asset.contract_ref,
    }
    evidence = {"recorded": recorded, "known_limitations": asset.known_limitations, "synthetic": asset.synthetic}
    missing = [f for f in rule.required_fields if not recorded.get(f)]
    if missing:
        reason = "Critical provenance is missing: " + ", ".join(missing)
        if rule.on_missing == "warn":
            return warned(reason + " (the profile allows continuing with a warning).", missing=missing, **evidence)
        return failed(reason + " (the profile blocks in this case).", missing=missing, **evidence)
    return passed("Required provenance is recorded in the catalog.", **evidence)
