"""Metadata check: is the catalog record complete and does the asset have a complete data contract?

The profile lists the catalog fields (identity, ownership, source, history, meaning, security) and contract sections
that must be present. Missing ones are reported by name. Presence is checked, not truth: an owner name is not proof
of accountability.
"""

from __future__ import annotations

from app.checks import Outcome, errored, failed, passed
from app.profiles import MetadataRule


def _present(value) -> bool:
    return value not in (None, "", [], {})


def check_metadata(rule: MetadataRule, record: dict | None, contract: dict | None) -> Outcome:
    if record is None and rule.catalog_fields:
        return errored("No catalog record is available for this asset.")
    if contract is None and rule.contract_sections:
        return failed("No data contract exists for this asset.", missing_contract_sections=rule.contract_sections)
    missing_fields = [f for f in rule.catalog_fields if not _present((record or {}).get(f))]
    missing_sections = [s for s in rule.contract_sections if not _present((contract or {}).get(s))]
    evidence = {
        "missing_catalog_fields": missing_fields,
        "missing_contract_sections": missing_sections,
        "catalog_fields_checked": len(rule.catalog_fields),
        "contract_sections_checked": len(rule.contract_sections),
        "contract_id": (contract or {}).get("id"),
        "contract_version": (contract or {}).get("version"),
    }
    if missing_fields or missing_sections:
        parts = (["catalog: " + ", ".join(missing_fields)] if missing_fields else []) + (
            ["contract: " + ", ".join(missing_sections)] if missing_sections else []
        )
        return failed("Incomplete metadata - " + "; ".join(parts) + ".", **evidence)
    return passed(
        f"Catalog record ({len(rule.catalog_fields)} fields) and contract "
        f"({len(rule.contract_sections)} sections) are complete.",
        **evidence,
    )
