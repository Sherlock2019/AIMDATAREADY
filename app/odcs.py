"""ODCS catalog store: one Open Data Contract Standard (ODCS v3.1.0) file per data asset is the single source of
truth for the catalog record AND the data contract. The engine reads it through this adapter.

    pipeline/odcs/<asset>.odcs.yaml        edit these (one file per asset)
    pipeline/odcs/schema/...v3.1.0.json    the official ODCS JSON Schema (bitol-io/open-data-contract-standard)

Mapping (lossless, proven by tests/test_odcs.py):
    ODCS-native   id, name, version, status, domain, description.purpose, schema[0] (object description,
                  granularity, columns with logicalType/physicalType, required = not null, primaryKey = unique key,
                  description = business definition, quality = allowed values / non-negative / ISO dates),
                  slaProperties (latency = freshness, retention), team (owner, technical owner, steward),
                  support (contact), roles (permitted user groups and agents)
    customProperties   every other catalog field (catalogXxx) and contract field (contractXxx), values unchanged
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

API_VERSION = "v3.1.0"
SCHEMA_FILE = Path(__file__).resolve().parent.parent / "pipeline" / "odcs" / "schema" / "odcs-json-schema-v3.1.0.json"
LOGICAL = {"string": "string", "integer": "integer", "decimal": "number", "number": "number", "date": "date",
           "timestamp": "timestamp", "boolean": "boolean"}  # fmt: skip
ISO_DATE = r"^\d{4}-\d{2}-\d{2}$"
TEAM = (("business_owner", "owner"), ("technical_owner", "technical owner"), ("data_steward", "data steward"))
CATALOG_NATIVE = {"name", "business_domain", "description", "granularity", "contact", "retention", "permitted_users",
                  "permitted_agents", "schema_types", "business_definitions", *(k for k, _ in TEAM)}  # fmt: skip
CONTRACT_NATIVE = {"id", "version", "status", "description", "schema", "quality", "freshness"}
QUALITY_NATIVE = {"not_null", "non_negative", "allowed_values", "iso_dates"}
HEADER = (
    "# ODCS {api} data contract + catalog record for `{asset}` - SINGLE SOURCE OF TRUTH (edit this file).\n"
    "# Validated against the official ODCS JSON Schema at load time. SIMULATED SAP landscape, fictional owners.\n"
)


def _camel(prefix: str, key: str) -> str:
    return prefix + "".join(part.capitalize() for part in key.split("_"))


def _snake(prop: str, prefix: str) -> str:
    return re.sub(r"(?<!^)([A-Z])", r"_\1", prop[len(prefix) :]).lower()


def _custom(props: list[dict]) -> dict[str, Any]:
    return {p["property"]: p["value"] for p in props or []}


# ---------------------------------------------------------------------------------------------- write
def to_odcs(asset: str, rec: dict, con: dict) -> dict:
    """Catalog record + contract (the engine's internal dicts) -> one ODCS v3.1.0 document."""
    types, defs = rec.get("schema_types") or {}, rec.get("business_definitions") or {}
    sch, q = con.get("schema") or {}, con.get("quality") or {}
    keys = sch.get("unique_key") or []
    cols = list(dict.fromkeys([*types, *(sch.get("required_fields") or [])]))
    props = []
    for col in cols:
        p: dict[str, Any] = {"name": col}
        if col in types:
            p["logicalType"], p["physicalType"] = LOGICAL.get(types[col], "string"), types[col]
        if col in keys:
            p["primaryKey"], p["primaryKeyPosition"] = True, keys.index(col) + 1
        if col in (q.get("not_null") or []):
            p["required"] = True
        if col in defs:
            p["description"] = defs[col]
        rules = []
        if col in (q.get("allowed_values") or {}):
            rules.append({"name": "allowed_values", "type": "library", "metric": "invalidValues",
                          "dimension": "conformity", "arguments": {"validValues": q["allowed_values"][col]},
                          "mustBe": 0})  # fmt: skip
        if col in (q.get("non_negative") or []):
            rules.append({"name": "non_negative", "type": "sql", "dimension": "conformity", "mustBe": 0,
                          "query": f"SELECT COUNT(*) FROM {asset} WHERE {col} < 0"})  # fmt: skip
        if col in (q.get("iso_dates") or []):
            rules.append({"name": "iso_dates", "type": "library", "metric": "invalidValues", "dimension": "conformity",
                          "arguments": {"pattern": ISO_DATE}, "mustBe": 0})  # fmt: skip
        if rules:
            p["quality"] = rules
        props.append(p)
    obj: dict[str, Any] = {"name": asset, "physicalName": f"{asset}.csv", "physicalType": "file",
                           "logicalType": "object", "properties": props}  # fmt: skip
    if rec.get("description"):
        obj["description"] = rec["description"]
    if rec.get("granularity"):
        obj["dataGranularityDescription"] = rec["granularity"]
    obj_custom = [{"property": "requiredFields", "value": sch.get("required_fields") or []}]
    obj_custom += [{"property": "uniqueKeyDeclared", "value": "unique_key" in sch}]
    extra_defs = {k: v for k, v in defs.items() if k not in cols}
    if extra_defs:
        obj_custom.append({"property": "businessDefinitionsExtra", "value": extra_defs})
    extra_q = {k: v for k, v in q.items() if k not in QUALITY_NATIVE}
    if extra_q:
        obj_custom.append({"property": "qualityExtra", "value": extra_q})
    obj["customProperties"] = obj_custom

    doc: dict[str, Any] = {"apiVersion": API_VERSION, "kind": "DataContract", "id": con["id"],
                           "version": str(con.get("version", "1.0")), "status": con.get("status", "draft")}  # fmt: skip
    if rec.get("name"):
        doc["name"] = rec["name"]
    if rec.get("business_domain"):
        doc["domain"] = rec["business_domain"]
    if con.get("description"):
        doc["description"] = {"purpose": con["description"]}
    doc["schema"] = [obj]
    sla = []
    fresh = con.get("freshness") or {}
    if "max_age_hours" in fresh:
        sla.append({"property": "latency", "value": fresh["max_age_hours"], "unit": "h", "element": "source_event_time",
                    "description": "Maximum age of the source data (source event time) for any consumer."})  # fmt: skip
    if rec.get("retention"):
        sla.append({"property": "retention", "value": rec["retention"]})
    if sla:
        doc["slaProperties"] = sla
    members = [{"username": rec[k], "role": role} for k, role in TEAM if rec.get(k)]
    if members:
        doc["team"] = {"members": members}
    if rec.get("contact"):
        doc["support"] = [{"channel": rec["contact"], "tool": "slack", "scope": "issues"}]
    roles = [{"role": u, "access": "read", "customProperties": [{"property": "principalType", "value": "user group"}]}
             for u in rec.get("permitted_users") or []]  # fmt: skip
    roles += [{"role": a, "access": "read", "customProperties": [{"property": "principalType", "value": "agent"}]}
              for a in rec.get("permitted_agents") or []]  # fmt: skip
    if roles:
        doc["roles"] = roles
    custom = [{"property": _camel("catalog", k), "value": v} for k, v in rec.items() if k not in CATALOG_NATIVE]
    custom += [{"property": _camel("contract", k), "value": v} for k, v in con.items() if k not in CONTRACT_NATIVE]
    custom += [
        {"property": "contractFreshnessExtra", "value": {k: v for k, v in fresh.items() if k != "max_age_hours"}}
    ]
    custom.append({"property": "catalogKeysPresent", "value": sorted(k for k in CATALOG_NATIVE if k in rec)})
    doc["customProperties"] = custom
    return doc


# ---------------------------------------------------------------------------------------------- read
def from_odcs(doc: dict) -> tuple[str, dict, dict]:
    """One ODCS document -> (asset id, catalog record, contract). Inverse of to_odcs."""
    obj = doc["schema"][0]
    asset, top, oc = obj["name"], _custom(doc.get("customProperties")), _custom(obj.get("customProperties"))
    present = set(top.get("catalogKeysPresent", []))
    rec: dict[str, Any] = {_snake(k, "catalog"): v for k, v in top.items()
                           if k.startswith("catalog") and k != "catalogKeysPresent"}  # fmt: skip
    con: dict[str, Any] = {_snake(k, "contract"): v for k, v in top.items()
                           if k.startswith("contract") and k != "contractFreshnessExtra"}  # fmt: skip
    props = obj.get("properties") or []
    native = {
        "name": doc.get("name"),
        "business_domain": doc.get("domain"),
        "description": obj.get("description"),
        "granularity": obj.get("dataGranularityDescription"),
        "contact": (doc.get("support") or [{}])[0].get("channel"),
        "retention": next((s["value"] for s in doc.get("slaProperties") or [] if s["property"] == "retention"), None),
        "permitted_users": [r["role"] for r in doc.get("roles") or []
                            if _custom(r.get("customProperties")).get("principalType") == "user group"],
        "permitted_agents": [r["role"] for r in doc.get("roles") or []
                             if _custom(r.get("customProperties")).get("principalType") == "agent"],
        "schema_types": {p["name"]: p["physicalType"] for p in props if "physicalType" in p},
        "business_definitions": {**{p["name"]: p["description"] for p in props if "description" in p},
                                 **oc.get("businessDefinitionsExtra", {})},
    }  # fmt: skip
    members = {m["role"]: m["username"] for m in (doc.get("team") or {}).get("members", [])}
    native.update({k: members.get(role) for k, role in TEAM})
    rec.update({k: v for k, v in native.items() if k in present})

    q: dict[str, Any] = {}
    for p in props:
        if p.get("required"):
            q.setdefault("not_null", []).append(p["name"])
        for rule in p.get("quality") or []:
            if rule["name"] == "allowed_values":
                q.setdefault("allowed_values", {})[p["name"]] = rule["arguments"]["validValues"]
            elif rule["name"] in ("non_negative", "iso_dates"):
                q.setdefault(rule["name"], []).append(p["name"])
    q.update(oc.get("qualityExtra", {}))
    sch: dict[str, Any] = {"required_fields": oc.get("requiredFields", [])}
    if oc.get("uniqueKeyDeclared"):
        sch["unique_key"] = [p["name"] for p in sorted((p for p in props if p.get("primaryKey")),
                                                       key=lambda p: p["primaryKeyPosition"])]  # fmt: skip
    fresh = dict(top.get("contractFreshnessExtra") or {})
    latency = next((s for s in doc.get("slaProperties") or [] if s["property"] == "latency"), None)
    if latency:
        fresh["max_age_hours"] = latency["value"]
    con.update({"id": doc["id"], "version": doc["version"], "status": doc["status"], "schema": sch, "quality": q,
                "freshness": fresh})  # fmt: skip
    if (doc.get("description") or {}).get("purpose"):
        con["description"] = doc["description"]["purpose"]
    return asset, rec, con


# ---------------------------------------------------------------------------------------------- validate / store
_validator = None


def validate(doc: dict) -> list[str]:
    """Errors against the official ODCS v3.1.0 JSON Schema (empty list = valid)."""
    global _validator
    if _validator is None:
        from jsonschema.validators import validator_for

        schema = json.loads(SCHEMA_FILE.read_text())
        _validator = validator_for(schema)(schema)
    return [f"{'/'.join(map(str, e.absolute_path)) or '(root)'}: {e.message}" for e in _validator.iter_errors(doc)]


def dump(asset: str, doc: dict) -> str:
    return HEADER.format(api=API_VERSION, asset=asset) + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True,
                                                                        width=120)  # fmt: skip


class OdcsCatalog:
    """Catalog adapter over the ODCS folder: loads, validates and serves catalog records and contracts."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.catalog: dict[str, dict] = {}
        self.contracts: dict[str, dict] = {}
        self.files: dict[str, str] = {}
        self.errors: dict[str, list[str]] = {}
        for path in sorted(self.folder.glob("*.odcs.yaml")):
            doc = yaml.safe_load(path.read_text())
            asset, rec, con = from_odcs(doc)
            self.catalog[asset], self.contracts[asset] = rec, con
            self.files[asset] = f"{self.folder.name}/{path.name}"
            self.errors[asset] = validate(doc)

    @staticmethod
    def exists(folder: Path) -> bool:
        return any(Path(folder).glob("*.odcs.yaml"))

    def raw(self, asset: str) -> str:
        return (self.folder.parent / self.files[asset]).read_text()
