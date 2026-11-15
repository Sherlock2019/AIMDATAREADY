"""Data Harness demo over a SIMULATED SAP landscape, called step by step by the Flowise Agentflow (flowise/).

The four components:
  catalog.yaml     data catalog (airport directory): identity, ownership, source, history, meaning, security
  contracts.yaml   data contracts (operating requirements): what each producer promises
  tasks.yaml       Data Harness Profiles (flight plans): what each AI task needs, ACL, failure policy, actions
  this module      the readiness gate (preflight inspection), using the deterministic engine in app/engine.py

One run = one ROUND:
  POST /pipeline/extract  {faults}        round N: copy the simulated SAP systems into staging, inject faults
                                          (except those prevented by fixes approved in earlier rounds)
  POST /pipeline/check    {role, task}    1. DETECT  checklist, answer, data metrics, per-role / per-task summary
  POST /pipeline/propose  {role, task}    2. PROPOSE mock correction agent (deterministic, no LLM)
  POST /pipeline/apply    {role, task}    3. FIX     apply what the role may approve, re-check, record upstream rules
  POST /pipeline/reset                    forget rounds, rules and evaluation records
  GET  /pipeline/catalog/{asset}          the full catalog record, contract, coverage, quality-check history
  GET  /pipeline/odcs/{asset}             the asset's ODCS v3.1.0 file (single source of truth for catalog + contract)
  GET  /pipeline/lineage                  OpenLineage run events (extract, checks, fixes, index build, retrieval)
  GET  /pipeline/metrics                  decisions, access failures, data issues, latency, last evaluation

Roles: every role defined in ~/Supplychain-AI-agent (roles.yaml). Choosing a role is a DEMO selector, not
authentication. Approval follows SCInsight's REVIEW_AUTHORITY: humans only, per domain.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import shutil
import statistics
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from fastapi import FastAPI
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from app.adapters.base import AdapterUnavailable, AuthzDecision
from app.adapters.dataset import DatasetAdapter, coverage_from_rows, read_csv, write_csv
from app.catalog import CatalogAsset, DocumentRecord, load_yaml
from app.checks.access import row_scope
from app.checks.quality import in_scope, is_iso_date
from app.engine import ReadinessEngine
from app.lineage import InputDataset, Lineage, OutputDataset, source_ds, staging_in, staging_out
from app.models import CheckStatus, Principal
from app.odcs import OdcsCatalog
from app.profiles import TaskProfile
from app.publish import Publisher
from app.rag import DocumentIndex
from app.remediation import FixProposal, Remediator, plan_fixes

PURPOSE = "supply_chain_planning"
DOCUMENT_ASSETS = {"ariba_contracts"}
DOC_TASKS = {"read_transfer_policy", "supplier_policy_qa"}  # answered from passages (permission-aware search)
DIM = {
    "product": ["product_id"],
    "warehouse": ["origin_warehouse", "destination_warehouse"],
    "lane": ["origin_warehouse", "destination_warehouse"],
    "period": ["planning_period"],
}
CONTEXT = [
    {"name": "product_id", "pattern": "^[A-Z0-9][A-Z0-9-]{1,40}$"},
    {"name": "origin_warehouse", "pattern": "^WH-[A-Z0-9]{1,10}$"},
    {"name": "destination_warehouse", "pattern": "^WH-[A-Z0-9]{1,10}$"},
    {"name": "planning_period", "pattern": r"^\d{4}-\d{2}-\d{2}/\d{4}-\d{2}-\d{2}$"},
]
DEFAULT_TASK = "recommend_stock_transfer"
MY_ROLE = "my_role"  # use the data readiness profile of the selected user profile


class RoleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str = Field(max_length=80)
    task: str = Field(default=DEFAULT_TASK, max_length=80)
    agent: Literal["mock", "claude"] = "mock"  # used by /pipeline/propose only


class SearchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str = Field(max_length=80)
    task: str = Field(max_length=80)
    query: str = Field(min_length=2, max_length=300)


class DecideBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str = Field(max_length=80)
    approve: bool


class ExtractBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    faults: list[str] | str = "all"  # "all", "none", one fault id, or a list of ids
    dataset: str | None = Field(default=None, pattern=r"^DS-\d{2}(-F\d{1,3})?$")  # from /pipeline/datasets
    task: str | None = Field(default=None, max_length=80)  # data sets are generated per task


class PipelineAdapter(DatasetAdapter):
    """Evidence from the staged files and catalog record; ACL from roles.yaml + catalog (demo, not production)."""

    def __init__(self, folder: Path, roles: dict, contracts: dict, systems_by_asset: dict):
        super().__init__(folder, authz=None)
        self.roles, self.contracts, self.systems_by_asset = roles, contracts, systems_by_asset

    def get_record(self, asset_id: str) -> dict | None:
        return self.asset_meta(asset_id)

    def get_contract(self, asset_id: str) -> dict | None:
        return self.contracts.get(asset_id)

    def get_asset(self, asset_id: str) -> CatalogAsset | None:
        path, rec = self.asset_file(asset_id), self.asset_meta(asset_id)
        if path is None:
            return None
        header, rows = read_csv(path)
        fields = {
            k: rec.get(k)
            for k in (
                "business_owner",
                "technical_owner",
                "source_name",
                "source_version",
                "source_event_time",
                "ingested_at",
                "known_limitations",
            )
        }
        fields["known_limitations"] = fields["known_limitations"] or []
        kind = "document_set" if asset_id in DOCUMENT_ASSETS else "table"
        return CatalogAsset.model_validate(
            {
                "asset_id": asset_id,
                "kind": kind,
                "classification": rec.get("classification") or "internal",
                **fields,
                "contract_ref": (self.contracts.get(asset_id) or {}).get("id"),
                "schema": header,
                "coverage": coverage_from_rows(header, rows),
            }
        )

    def get_documents(self, asset_id: str) -> list[DocumentRecord]:
        if asset_id not in DOCUMENT_ASSETS or not self.asset_file(asset_id):
            return []
        return [
            DocumentRecord(
                doc_id=r["doc_id"],
                title=r["title"],
                version=r.get("version") or None,
                source_id=r.get("source_id") or None,
                status=r["status"],
                valid_from=r.get("valid_from") or None,
                valid_to=r.get("valid_to") or None,
                access_roles=["*"],
                purposes=[PURPOSE],
            )
            for r in read_csv(self.asset_file(asset_id))[1]
        ]

    def authorize(self, principal: Principal, asset_id, purpose, scope, document=None) -> AuthzDecision:
        if self.manifest.get("authorization_unavailable"):
            raise AdapterUnavailable("authorization service unavailable (simulated)")
        rec = self.asset_meta(asset_id) or {}
        if purpose not in (rec.get("permitted_purposes") or []):
            return AuthzDecision(False, f"purpose {purpose} is not permitted for this data")
        if principal.kind == "service":  # agent-level ACL from the catalog record
            if principal.principal_id in (rec.get("permitted_agents") or []):
                return AuthzDecision(True, "agent permitted")
            return AuthzDecision(False, f"{principal.principal_id} may not read this data")
        role = self.roles.get(principal.principal_id.removeprefix("role:"))
        if role is None:
            return AuthzDecision(None, "unknown role")
        system = self.systems_by_asset[asset_id]
        if not (role["read"] == "all" or system in role["read"]):  # dataset / document level
            return AuthzDecision(False, "the role may not read this data set")
        denied_rows = [w for w in scope.get("warehouse", []) if role.get("warehouses") and w not in role["warehouses"]]
        if denied_rows:  # row level
            return AuthzDecision(False, "no access to warehouse " + ", ".join(denied_rows), tuple(denied_rows))
        denied_cols = [c for c in scope.get("columns", []) if c not in (role.get("columns") or [])]
        if denied_cols:  # column level
            return AuthzDecision(False, "no access to column " + ", ".join(denied_cols))
        return AuthzDecision(True, "role permitted (demo mapping)")


def apply_fault(staging: Path, manifest: dict, asset: str, fault: dict) -> None:
    op = fault["op"]
    if op == "meta":
        manifest["assets"][asset].update(fault["set"])
        return
    if op == "authz_unavailable":
        manifest["authorization_unavailable"] = True
        return
    path = staging / f"{asset}.csv"
    header, rows = read_csv(path)
    where = fault.get("where", {})

    def hit(r):
        return all(r.get(k) in v for k, v in where.items())

    if op == "drop_rows":
        rows = [r for r in rows if not hit(r)]
    elif op == "drop_column":
        header = [c for c in header if c != fault["column"]]
    elif op == "duplicate_rows":
        rows += [dict(r) for r in rows if hit(r)]
    elif op == "set_values":
        for r, value in zip([r for r in rows if hit(r)], fault["values"], strict=False):
            r[fault["column"]] = value
    elif op == "date_format":
        for r in rows:
            r[fault["column"]] = datetime.strptime(r[fault["column"]], "%Y-%m-%d").strftime(fault["format"])
        manifest["assets"][asset]["export_settings"] = {"date_format": fault["format"]}
    write_csv(path, header, rows)


class PipelineService:
    def __init__(self, root: Path, work: Path, clock, audit=None):
        self.root, self.staging, self.clock = Path(root), Path(work) / "staging", clock
        self.lineage = Lineage(Path(work) / "lineage" / "openlineage.jsonl", clock)
        self.publisher = Publisher(Path(work) / "published")
        self.fixed_root = Path(work) / "fixed"  # corrected extracts, selectable in step 5 (DS-nn-Fk)
        self.rag = DocumentIndex(self.root / "documents", Path(work) / "rag", self.lineage)
        cfg = load_yaml(self.root / "pipeline.yaml")
        self.context, self.cfg = cfg["context"], cfg
        self.systems, self.faults = cfg["systems"], cfg["faults"]
        self.sys = {s["id"]: s for s in self.systems}
        self.by_asset = {s["asset"]: s for s in self.systems}
        self.by_requirement = {s["requirement"]: s for s in self.systems}
        if OdcsCatalog.exists(self.root / "odcs"):  # single source of truth: one ODCS v3.1.0 file per asset
            self.odcs = OdcsCatalog(self.root / "odcs")
            self.catalog, self.contracts = self.odcs.catalog, self.odcs.contracts
        else:  # legacy YAML (only before scripts/export_odcs.py has been run)
            self.odcs = None
            self.catalog = load_yaml(self.root / "catalog.yaml")["assets"]
            self.contracts = load_yaml(self.root / "contracts.yaml")["contracts"]
        roles_cfg = load_yaml(self.root / "roles.yaml")
        self.domains = roles_cfg["domains"]
        self.roles = {
            f"{app['id']}:{r['id']}": {**r, "app": app["name"], "key": f"{app['id']}:{r['id']}"}
            for app in roles_cfg["apps"]
            for r in app["roles"]
        }
        self.harness = {p["id"]: p for p in load_yaml(self.root / "tasks.yaml")["profiles"]}
        self.profiles = {tid: self._profile(p) for tid, p in self.harness.items()}
        everything = {
            "id": "all_assets",
            "version": "1.0.0",
            "name": "all assets",
            "required_assets": {a: {} for a in self.by_asset},
        }
        self.union = self._profile(everything)  # validates and applies fixes for any asset
        self.engine = ReadinessEngine(self.profiles, None, clock, audit)  # the evaluation the user asked for
        self.quiet = ReadinessEngine(self.profiles, None, clock, None)  # per-role / per-task summaries
        self.remediator = Remediator(self.union, self.root / "sap_source", self.root / "steward_registry.yaml", clock)
        self.proposals: list = []
        self.claude_client = None  # tests inject a stub; None = real Anthropic client when credentials exist
        self.state_path = Path(work) / "rounds.json"
        empty = {"rounds": [], "rules": [], "history": {}, "evaluations": []}
        self.state = {**empty, **json.loads(self.state_path.read_text())} if self.state_path.exists() else empty

    # ------------------------------------------------------------------ Data Harness Profile = contract + task
    def _profile(self, p: dict) -> TaskProfile:
        assets = []
        for asset_id, opts in p["required_assets"].items():
            c, s = self.contracts[asset_id], self.by_asset[asset_id]
            doc = asset_id in DOCUMENT_ASSETS
            rows = opts.get("rows", False)
            req = {
                "name": s["requirement"],
                "asset_id": asset_id,
                "mandatory": opts.get("mandatory", True),
                "access": {
                    "require_agent_access": True,
                    "row_scope_context_keys": DIM["warehouse"] if rows else [],
                    "columns": opts.get("columns", []),
                },
                "freshness": {"max_age_hours": opts.get("max_age_hours", c["freshness"]["max_age_hours"])},
                "schema": {"required_fields": c["schema"]["required_fields"]},
                "coverage": [{"dimension": d, "context_keys": DIM[d]} for d in opts.get("coverage", [])],
                "quality": {"unique_key": c["schema"]["unique_key"], **c["quality"]},
                "provenance": {
                    "required_fields": ["business_owner", "technical_owner", "source_version", "source_event_time"],
                    "on_missing": "block",
                },
                "metadata": {
                    "catalog_fields": self.cfg["catalog_required_fields"]
                    + (self.cfg["document_catalog_fields"] if doc else []),
                    "contract_sections": self.cfg["contract_required_sections"],
                },
            }
            if doc:
                req["documents"] = {"min_usable_documents": 1}
            assets.append(req)
        return TaskProfile.model_validate(
            {
                "task_id": p["id"],
                "version": p["version"],
                "description": p["name"],
                "purpose": PURPOSE,
                "demo_values_note": "DEMO VALUES. Thresholds come from demo contracts and demo task requirements.",
                "allow_warnings": True,
                "required_context": CONTEXT,
                "assets": assets,
            }
        )

    # ------------------------------------------------------------------ helpers
    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, indent=1))

    def reset(self) -> dict[str, Any]:
        self.state, self.proposals = {"rounds": [], "rules": [], "history": {}, "evaluations": []}, []
        self._save()
        if self.staging.exists():
            shutil.rmtree(self.staging)
        self.lineage.path.unlink(missing_ok=True)
        self.publisher.clear()
        self.rag.clear_certified()
        if self.fixed_root.exists():
            shutil.rmtree(self.fixed_root)
        return {"text": "All fixes reversed: upstream rules, fixed data sets, certified versions, approval requests, "
                        "test results and lineage events cleared. The next Run checks extracts the original faulty data "
                        "again (round 1)."}  # fmt: skip

    def role(self, key: str) -> dict:
        if key not in self.roles:
            raise KeyError(key)
        return self.roles[key]

    def task(self, key: str) -> dict:
        if key not in self.harness:
            raise KeyError(key)
        return self.harness[key]

    def resolve(self, role_key: str, task_id: str) -> str:
        """'my_role' = the first task of the role (Role -> Task -> Profile in roles.yaml)."""
        self.role(role_key)
        if task_id == MY_ROLE:
            return self.role_tasks(role_key)[0]["id"]
        self.task(task_id)
        return task_id

    def role_tasks(self, role_key: str) -> list[dict]:
        """The tasks this role may request (roles.yaml `tasks:`); each task has exactly one readiness profile."""
        ids = self.role(role_key).get("tasks") or list(self.harness)
        return [{"id": t, "name": self.harness[t]["name"], "question": self.harness[t].get("question", "")}
                for t in ids]  # fmt: skip

    def role_view(self, role_key: str, task_id: str = MY_ROLE) -> dict[str, Any]:
        """1 user role -> 2 requested task -> 3 data authorized for user + agent and task (ALLOWED vs REQUIRED)
        -> 4 task readiness profile. Checks (5) and retrieval or explanation (6) follow in check() / retrieve()."""
        role = self.role(role_key)
        tasks = self.role_tasks(role_key)
        tid = self.resolve(role_key, task_id)
        own = None
        p = self.harness[tid]
        adapter = self._adapter()
        user = Principal(principal_id=f"role:{role_key}", kind="user", source="demo_selector", authenticated=False)
        agent = Principal(principal_id=p["agent"], kind="service", source="demo_selector", authenticated=False)
        reqs = {a.asset_id: a for a in self.profiles[tid].assets}
        sources = []
        for asset_id, opts in p["required_assets"].items():
            s, c = self.by_asset[asset_id], self.contracts[asset_id]
            try:
                ud = adapter.authorize(user, asset_id, PURPOSE, row_scope(reqs[asset_id], self.context))
                ad = adapter.authorize(agent, asset_id, PURPOSE, row_scope(reqs[asset_id], self.context))
                user_ok, agent_ok, user_why, agent_why = ud.allowed is True, ad.allowed is True, ud.reason, ad.reason
            except AdapterUnavailable:
                user_ok = agent_ok = False
                user_why = agent_why = "unknown: authorization service did not answer"
            rules = [f"fresh ≤ {opts.get('max_age_hours', c['freshness']['max_age_hours']):g} h"]
            if opts.get("coverage"):
                rules.append("covers " + ", ".join(opts["coverage"]))
            if opts.get("rows"):
                rules.append("row access to WH-A and WH-C")
            if opts.get("columns"):
                rules.append("column access to " + ", ".join(opts["columns"]))
            rules.append("schema + quality per contract " + c["id"])
            sources.append(
                {
                    "system": s["name"],
                    "data": s["label"],
                    "mandatory": opts.get("mandatory", True),
                    "can_read": user_ok,
                    "user_authorized": user_ok,
                    "user_reason": user_why,
                    "agent_authorized": agent_ok,
                    "agent_reason": agent_why,
                    "allowed": user_ok and agent_ok,
                    "can_approve": self.can_approve(role, s["id"]),
                    "approvers": self.domains[s["domain"]],
                    "owner": self.catalog[asset_id]["business_owner"],
                    "rules": rules,
                }
            )
        return {
            "role": {k: role.get(k) for k in ("key", "title", "app", "human", "warehouses", "columns")},
            "own_profile": own,
            "profile": {
                k: p.get(k)
                for k in (
                    "id",
                    "name",
                    "version",
                    "approved_by",
                    "agent",
                    "output",
                    "actions",
                    "question",
                    "can_still_do",
                )
            },
            "sources": sources,
            "tasks": tasks,
        }

    # ------------------------------------------------------------------ 6. retrieve authorized data, run the task
    def retrieve(self, role_key: str, task_id: str = DEFAULT_TASK) -> dict[str, Any]:
        """Only when READY: re-check at use time, retrieve ONLY rows/columns the user may see and the task needs,
        then run the specialist. A real deployment enforces the same ACL in the database / API / retriever."""
        task_id = self.resolve(role_key, task_id)
        role, task = self.role(role_key), self.task(task_id)
        result = self._evaluate(role_key, task_id)  # use-time recheck: the data may have changed since the check
        systems = self._systems(result, task_id)
        if result.decision.value == "BLOCKED":
            return {"retrieved": False, "decision": "BLOCKED", "evaluation_id": result.evaluation_id,
                    "text": "Retrieval refused: the re-check at use time is BLOCKED. " + self.answer(task, systems, "BLOCKED")}  # fmt: skip
        self._publish(role_key, task_id, result, systems)  # certify what passed (no-op when already published)
        reqs = {a.asset_id: a for a in self.profiles[task_id].assets}
        manifest = load_yaml(self.staging / "manifest.yaml")["assets"]
        today = self.clock().date().isoformat()
        sources, data = [], {}
        for sid, v in systems.items():
            if v["status"] == "NO_ACCESS":
                continue  # an optional source the user may not read is simply not retrieved
            s = self.sys[sid]
            asset, req = s["asset"], reqs[s["asset"]]
            cert = self.publisher.current(asset) if v["status"] == "PASS" else None
            header, rows = read_csv(self.publisher.path(asset) if cert else self.staging / f"{asset}.csv")
            dims = {r.dimension for r in req.coverage} | ({"warehouse"} if req.access.row_scope_context_keys else set())
            ctx = self.context
            whs = {ctx["origin_warehouse"], ctx["destination_warehouse"]}
            start, end = ctx["planning_period"].split("/")
            if "product" in dims:
                rows = [r for r in rows if r.get("product_id", ctx["product_id"]) == ctx["product_id"]]
            if "warehouse" in dims:
                rows = [r for r in rows if r.get("warehouse_id", next(iter(whs))) in whs]
            if "lane" in dims:
                rows = [r for r in rows if (r.get("origin_warehouse"), r.get("destination_warehouse"))
                        == (ctx["origin_warehouse"], ctx["destination_warehouse"])]  # fmt: skip
            if "period" in dims:
                rows = [r for r in rows if start <= r.get("week_start", start) <= end]
            if asset in DOCUMENT_ASSETS:
                rows = [r for r in rows if r["status"] == "approved" and r["valid_from"] <= today
                        and (not r.get("valid_to") or r["valid_to"] >= today)]  # fmt: skip
            hidden = [
                c for c in (self.catalog[asset].get("restricted_columns") or {}) if c not in (role.get("columns") or [])
            ]
            cols = [h for h in header if h not in hidden]
            rows = [{k: r.get(k, "") for k in cols} for r in rows]
            data[asset] = rows
            sources.append({"data": s["label"], "system": s["name"],
                            "version": f"{manifest[asset].get('source_version')} · "
                                       + (f"certified v{cert['version']}" if cert else "not certified (warnings)"),
                            "records": len(rows), "scope": ", ".join(sorted(dims)) or "all approved records",
                            "hidden_columns": hidden})  # fmt: skip
        lines = ["| Authorized data retrieved | Version | Records | Scope | Hidden columns |", "|---|---|---|---|---|"]
        lines += [f"| **{x['data']}** ({x['system']}) | `{x['version']}` | {x['records']} | {x['scope']} | "
                  f"{', '.join(x['hidden_columns']) or '—'} |" for x in sources]  # fmt: skip
        doc_task = task_id in DOC_TASKS and "ariba_contracts" in data
        if doc_task:  # policy question: permission-aware search over the document texts
            found = self.rag.search(task.get("question", ""), role_key, self._valid_docs(data["ariba_contracts"]))
            body = self._passages_md(found, task.get("question", ""))
        else:
            body = self.specialist(task_id, task, data)
        inputs = [staging_in(a) for a in data] + (
            [InputDataset(namespace="chroma://rag", name="documents")] if doc_task else []
        )
        self.lineage.run(
            f"retrieve.{task_id}",
            inputs,
            [OutputDataset(namespace=f"agent://{task['agent']}", name=f"{task_id}.context")],
        )
        return {"retrieved": True, "decision": result.decision.value, "evaluation_id": result.evaluation_id,
                "sources": sources, "search": doc_task, "text": "\n".join(lines) + "\n\n" + body}  # fmt: skip

    def _valid_docs(self, rows: list[dict]) -> dict[str, str]:
        today = self.clock().date().isoformat()
        return {r["doc_id"]: r["version"] for r in rows if r.get("status") == "approved"
                and r.get("valid_from", "9999") <= today and (not r.get("valid_to") or r["valid_to"] >= today)}  # fmt: skip

    def _passages_md(self, found: dict, query: str) -> str:
        head = (f"**Permission-aware search** (Chroma index `{self.rag.version()}`) · query: *{query}* · your document "
                f"groups: {', '.join(found['groups']) or 'none'} · searchable for you: "
                f"{', '.join(found['searchable']) or 'nothing'}")  # fmt: skip
        if not found["passages"]:
            return head + ("\n\nNo passage you are permitted to read answers this. Ask the document owner for access; "
                           "documents outside your groups are never searched.")  # fmt: skip
        rows = ["| # | Source (cite this) | Section | Passage |", "|---|---|---|---|"]
        rows += [f"| {i} | **{p['doc_id']} v{p['version']}** - {p['title']} | {p['section']} | {p['text']} |"
                 for i, p in enumerate(found["passages"], 1)]  # fmt: skip
        return (head + "\n\n" + "\n".join(rows) + "\n\nOnly approved documents valid today that your groups may read "
                "were searched (filter inside the vector query). An LLM agent composes the answer from these passages "
                "only and cites them; the mock agent shows the passages.")  # fmt: skip

    def search(self, role_key: str, task_id: str, query: str) -> dict[str, Any]:
        """Free-text policy search for a READY document task (re-checked at use time, like retrieve)."""
        task_id = self.resolve(role_key, task_id)
        task = self.task(task_id)
        if "ariba_contracts" not in task["required_assets"]:
            return {"searched": False, "text": "This task does not use the document collection."}
        result = self._evaluate(role_key, task_id)
        if result.decision.value == "BLOCKED":
            return {"searched": False, "text": "Search refused: the re-check at use time is BLOCKED. "
                    + self.answer(task, self._systems(result, task_id), "BLOCKED")}  # fmt: skip
        _, rows = read_csv(self.staging / "ariba_contracts.csv")
        found = self.rag.search(query, role_key, self._valid_docs(rows))
        self.lineage.run(f"retrieve.{task_id}", [staging_in("ariba_contracts"), InputDataset(namespace="chroma://rag", name="documents")],
                         [OutputDataset(namespace=f"agent://{task['agent']}", name=f"{task_id}.context")])  # fmt: skip
        return {"searched": True, **found, "text": self._passages_md(found, query)}

    def specialist(self, task_id: str, task: dict, data: dict) -> str:
        """Deterministic demo specialists for two tasks; the others only state what would run next."""
        ctx, today = self.context, self.clock().date().isoformat()
        a, c = ctx["origin_warehouse"], ctx["destination_warehouse"]

        def usable(wh: str) -> int:
            return sum(int(r["quantity"]) for r in data.get("s4_mm_stock", []) if r["warehouse_id"] == wh
                       and r["status"] == "released" and r["expiry_date"] >= today)  # fmt: skip

        def on_hand(wh: str) -> int:
            return sum(int(r["quantity"]) for r in data.get("s4_mm_stock", []) if r["warehouse_id"] == wh)

        if task_id == "compare_stock":
            ua, uc = usable(a), usable(c)
            return (f"**Specialist `{task['agent']}` - stock comparison for {ctx['product_id']}**\n\n"
                    "| Warehouse | On hand | Usable (released, not expired) |\n|---|---|---|\n"
                    f"| {a} | {on_hand(a)} | {ua} |\n| {c} | {on_hand(c)} | {uc} |\n\n"
                    f"{a} holds {abs(ua - uc)} usable units {'more' if ua >= uc else 'fewer'} than {c}.")  # fmt: skip
        if task_id == "recommend_stock_transfer":
            fc = {wh: sum(int(r["forecast_units"]) for r in data.get("ibp_demand_forecast", []) if r["warehouse_id"] == wh)
                  for wh in (a, c)}  # fmt: skip
            ua, uc = usable(a), usable(c)
            need, surplus = max(0, fc[c] - uc), max(0, ua - fc[a])
            qty = min(need, surplus)
            lane = next(iter(data.get("tm_lanes", [])), {})
            body = (f"**Specialist `{task['agent']}` - transfer recommendation for {ctx['product_id']}, "
                    f"{ctx['planning_period']}**\n\n| | {a} | {c} |\n|---|---|---|\n| Usable stock | {ua} | {uc} |\n"
                    f"| Forecast in period | {fc[a]} | {fc[c]} |\n\n")  # fmt: skip
            if qty:
                body += (
                    f"Recommendation: move **{qty} units** from {a} to {c} (lead time {lane.get('lead_time_days', '?')} "
                    f"day(s), {lane.get('cost_per_unit_eur', '?')} EUR/unit). Executing it still needs human approval."
                )
            else:
                body += f"No transfer needed: {c} covers its forecast, or {a} has no surplus after its own forecast."
            return body
        records = sum(len(v) for v in data.values())
        return (
            f"Retrieved {records} authorized record(s). The `{task['agent']}` specialist would now produce the "
            f"**{task['output']}** (this specialist is not implemented in the demo)."
        )

    def _adapter(self) -> PipelineAdapter:
        if not self.staging.exists():
            self.extract("none")
        return PipelineAdapter(self.staging, self.roles, self.contracts, {a: s["id"] for a, s in self.by_asset.items()})

    def _evaluate(self, role_key: str, task_id: str, quiet: bool = False):
        user = Principal(principal_id=f"role:{role_key}", kind="user", source="demo_selector", authenticated=False)
        agent = Principal(
            principal_id=self.harness[task_id]["agent"], kind="service", source="demo_selector", authenticated=False
        )
        engine = self.quiet if quiet else self.engine
        return engine.evaluate(
            task_id, self.context, user, agent, identity_mode="demo_selector", adapter=self._adapter()
        )

    def can_approve(self, role: dict, system_id: str) -> bool:
        return bool(role["human"]) and self.sys[system_id]["domain"] in role["approve"]

    def _systems(self, result, task_id: str) -> dict[str, dict]:
        out = {}
        for asset_id, opts in self.harness[task_id]["required_assets"].items():
            s = self.by_asset[asset_id]
            checks = [c for c in result.checks if c.requirement == s["requirement"]]
            bad = [c for c in checks if c.status in (CheckStatus.FAIL, CheckStatus.ERROR)]
            status = (
                "NO_ACCESS"
                if any(c.check == "access" for c in bad)
                else "FAIL"
                if bad
                else "WARN"
                if any(c.status is CheckStatus.WARN for c in checks)
                else "PASS"
            )
            if status == "FAIL" and not opts.get("mandatory", True):
                status = "WARN"  # optional asset: the agreed rule makes it a warning
            out[s["id"]] = {"status": status, "mandatory": opts.get("mandatory", True), "checks": checks, "bad": bad}
        return out

    @staticmethod
    def score(systems: dict) -> int:
        """Share of the task's data requirements met (PASS or WARN), in percent."""
        return round(100 * sum(v["status"] in ("PASS", "WARN") for v in systems.values()) / max(1, len(systems)))

    # ------------------------------------------------------------------ steps
    def _task(self, task_id: str | None) -> str:
        return task_id if task_id in self.harness else DEFAULT_TASK

    def datasets(self, task_id: str | None = None) -> list[dict]:
        """Neutral test extracts FOR ONE TASK: every data set has a different combination of faults, all in tables
        this task reads (a fault elsewhere would be invisible to it), plus one clean control. The user picks one
        WITHOUT knowing whether it is faulty; the readiness checks find out. Fixed shuffled order per task."""
        task_id = self._task(task_id)
        assets = set(self.harness[task_id]["required_assets"])
        relevant = [f["id"] for f in self.faults if self.sys[f["system"]]["asset"] in assets]
        data_faults = [f for f in relevant if f != "authz_down"]  # authz_down is an access outage, tested alone
        combos: list[list[str]] = [[], *[[f] for f in relevant]]
        combos += [[a, b] for a, b in zip(data_faults, data_faults[1:], strict=False)]  # neighbouring pairs
        if len(data_faults) > 2:
            combos.append(data_faults)  # everything at once
        unique = list({tuple(c): c for c in combos}.values())
        random.Random(sum(map(ord, task_id))).shuffle(unique)
        base = [{"id": f"DS-{i:02d}", "label": f"SAP extract DS-{i:02d} for {self.harness[task_id]['name']}",
                 "short": f"DS-{i:02d}", "faults": c, "task": task_id} for i, c in enumerate(unique, 1)]  # fmt: skip
        return base + self.fixed_datasets(task_id)

    def dataset_list(self, task_id: str | None = None, role_key: str | None = None) -> list[dict]:
        """Step 5 dropdown: each data set says whether the readiness engine has tested it, and the result for the
        selected role + task profile (access differs per role, rules per task). Nothing is revealed before a test."""
        task_id = self._task(task_id)
        results = self.state.get("dataset_results", {})
        mark = {"READY": "✓ READY", "READY_WITH_WARNINGS": "⚠ WARNINGS", "BLOCKED": "✗ BLOCKED"}
        out = []
        for d in self.datasets(task_id):
            tested = results.get(f"{task_id}/{d['id']}", {})
            mine = tested.get(f"{role_key}|{task_id}") if task_id and role_key else None
            prefix = d["id"] + (" · fixed" if d.get("fixed") else "")
            status = mark[mine["decision"]] if mine else ("tested for other roles/tasks" if tested else "not tested")
            detail = "; ".join(f"{r['role']} · {self.harness[r['task']]['name']}: {r['decision']} "
                               f"({r['at'][:16].replace('T', ' ')})" for r in tested.values() if r.get("task") in self.harness)  # fmt: skip
            out.append({"id": d["id"], "short": f"{prefix} · {status}", "fixed": bool(d.get("fixed")),
                        "label": d["label"] + (f" — tested: {detail}" if detail else " — not tested yet"),
                        "status": mine["decision"] if mine else ("TESTED_OTHER" if tested else "NOT_TESTED")})  # fmt: skip
        return out

    def dataset_faults(self, dataset_id: str, task_id: str | None = None) -> list[str]:
        found = next((d["faults"] for d in self.datasets(task_id) if d["id"] == dataset_id and "faults" in d), None)
        if found is None:
            raise KeyError(dataset_id)
        return found

    # ------------------------------------------------------------------ fixed data sets (corrected extracts)
    def fixed_datasets(self, task_id: str | None = None) -> list[dict]:
        task_id = self._task(task_id)
        metas = [json.loads(p.read_text()) for p in (self.fixed_root / task_id).glob("*/meta.json")]
        return [{"id": m["id"], "label": m["label"], "short": f"{m['id']} · fixed ✓", "fixed": True, "task": task_id}
                for m in sorted(metas, key=lambda m: (m["base"], m["version"]))]  # fmt: skip

    def extract_dataset(self, dataset_id: str, task_id: str | None = None) -> dict[str, Any]:
        """Step 5: a neutral SAP extract for the task (DS-nn) or a saved fixed data set (DS-nn-Fk)."""
        task_id = self._task(task_id)
        if re.fullmatch(r"DS-\d{2}-F\d{1,3}", dataset_id):
            return self.load_fixed(dataset_id, task_id)
        out = self.extract(self.dataset_faults(dataset_id, task_id))
        self.state.update(current_dataset=dataset_id, current_dataset_task=task_id)
        self._save()
        return out

    def load_fixed(self, dataset_id: str, task_id: str | None = None) -> dict[str, Any]:
        task_id = self._task(task_id)
        folder = self.fixed_root / task_id / dataset_id
        if not (folder / "meta.json").exists():
            raise KeyError(dataset_id)
        meta = json.loads((folder / "meta.json").read_text())
        if self.staging.exists():
            shutil.rmtree(self.staging)
        shutil.copytree(folder / "staging", self.staging)
        self.proposals = []
        self.state.update(current_dataset=dataset_id, current_dataset_task=task_id, staging_fixes=[])
        counts = {s["id"]: len(read_csv(self.staging / f"{s['asset']}.csv")[1]) for s in self.systems}
        self.lineage.run("load_fixed_dataset", [InputDataset(namespace="file://fixed", name=dataset_id)],
                         [staging_out(s["asset"], counts[s["id"]]) for s in self.systems])  # fmt: skip
        n = len(self.state["rounds"]) + 1
        self.state["rounds"].append({"round": n, "dataset": dataset_id, "faults": [], "prevented": [], "fixed": True})
        self._save()
        tasks = ", ".join(self.harness[t]["name"] for t in meta["certified_for"])
        return {"round": n, "rows": counts, "faults": [], "prevented": [],
                "text": f"**Round {n}**: loaded fixed data set **{dataset_id}**: the corrected extract of {meta['base']} "
                        f"saved when its {len(meta['fixes'])} approved fix(es) made it READY for {tasks}."}  # fmt: skip

    def _save_fixed(self, task_id: str) -> dict | None:
        """After fixes made a task READY, keep the whole corrected extract as a selectable data set."""
        cur, fixes = self.state.get("current_dataset"), self.state.get("staging_fixes") or []
        ds_task = self.state.get("current_dataset_task")
        if not cur or not fixes or ds_task != task_id:
            return None  # a fixed data set belongs to the task whose test extract it came from
        base = cur.split("-F")[0]
        root = self.fixed_root / task_id
        digest = hashlib.sha256(b"".join(p.read_bytes() for p in sorted(self.staging.glob("*.csv")))).hexdigest()[:16]
        metas = [json.loads(p.read_text()) for p in root.glob(f"{base}-F*/meta.json")]
        meta = next((m for m in metas if m["sha256"] == digest), None)
        new = meta is None
        if new:
            meta = {"id": f"{base}-F{len(metas) + 1}", "base": base, "version": len(metas) + 1, "sha256": digest,
                    "certified_for": [], "fixes": fixes, "saved_at": self.clock().isoformat()}  # fmt: skip
            shutil.copytree(self.staging, root / meta["id"] / "staging")
        if task_id not in meta["certified_for"]:
            meta["certified_for"].append(task_id)
        meta["label"] = (f"{base} · fixed v{meta['version']} (certified for "
                         f"{', '.join(self.harness[t]['name'] for t in meta['certified_for'])})")  # fmt: skip
        (root / meta["id"] / "meta.json").write_text(json.dumps(meta, indent=2))
        return {"id": meta["id"], "label": meta["label"], "new": new}

    def _approver_roles(self, role: dict, domains) -> list[dict]:
        """Human roles of the same app that may approve at least one of these data domains."""
        return [{"key": k, "title": r["title"]} for k, r in self.roles.items()
                if r["human"] and r["app"] == role["app"] and any(d in r["approve"] for d in domains)]  # fmt: skip

    def extract(self, faults) -> dict[str, Any]:
        ids = (
            [f["id"] for f in self.faults if f.get("in_all", True)]
            if faults == "all"
            else []
            if faults == "none"
            else ([faults] if isinstance(faults, str) else list(faults))
        )
        unknown = sorted(set(ids) - {f["id"] for f in self.faults})
        if unknown:
            raise KeyError(", ".join(unknown))
        if self.staging.exists():
            shutil.rmtree(self.staging)
        shutil.copytree(self.root / "sap_source", self.staging)
        self.state.update(current_dataset=None, staging_fixes=[])  # extract_dataset() names it afterwards
        manifest = load_yaml(self.staging / "manifest.yaml")
        for asset, dynamic in manifest["assets"].items():  # the catalog record = static catalog + extraction facts
            manifest["assets"][asset] = {**self.catalog[asset], **dynamic}
        fixed_upstream = {r["fault"] for r in self.state["rules"] if "fault" in r}  # one rule = one specific fault
        prevented = [f for f in self.faults if f["id"] in ids and f["id"] in fixed_upstream]
        chosen = [f for f in self.faults if f["id"] in ids and f not in prevented]
        for f in chosen:
            apply_fault(self.staging, manifest, self.sys[f["system"]]["asset"], f)
        (self.staging / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
        self.proposals = []
        counts = {s["id"]: len(read_csv(self.staging / f"{s['asset']}.csv")[1]) for s in self.systems}
        versions = {a: m.get("source_version") for a, m in manifest["assets"].items()}
        self.lineage.run("sap_extract", [source_ds(s["name"], s["asset"], versions.get(s["asset"])) for s in self.systems],
                         [staging_out(s["asset"], counts[s["id"]], versions.get(s["asset"])) for s in self.systems])  # fmt: skip
        n = len(self.state["rounds"]) + 1
        self.state["rounds"].append(
            {"round": n, "faults": [f["id"] for f in chosen], "prevented": [f["id"] for f in prevented]}
        )
        self._save()
        text = (
            f"**Round {n}**: extracted {sum(counts.values())} records from {len(counts)} simulated SAP systems."
        ) + (f" {len(prevented)} known issue(s) prevented by upstream rules from earlier rounds." if prevented else "")
        return {
            "round": n,
            "rows": counts,
            "faults": [f["id"] for f in chosen],
            "prevented": [f["id"] for f in prevented],
            "text": text,
        }

    def data_metrics(self, systems: dict) -> dict[str, dict]:
        """Data-readiness metrics per asset, only where evidence exists (nothing is estimated)."""
        out = {}
        manifest = load_yaml(self.staging / "manifest.yaml")
        for sid, v in systems.items():
            if v["status"] == "NO_ACCESS":
                out[sid] = {"restricted": True}
                continue
            asset = self.sys[sid]["asset"]
            rec = manifest["assets"][asset]
            by = {c.check: c for c in v["checks"]}
            m: dict[str, Any] = {"records": len(read_csv(self.staging / f"{asset}.csv")[1])}
            if "freshness" in by and "age_hours" in by["freshness"].evidence:
                m["source_data_age_hours"] = by["freshness"].evidence["age_hours"]
            try:
                delay = datetime.fromisoformat(rec["ingested_at"]) - datetime.fromisoformat(rec["source_event_time"])
                m["ingestion_delay_hours"] = round(delay.total_seconds() / 3600, 2)
            except (KeyError, TypeError, ValueError):
                pass
            q = by.get("quality")
            if q and q.evidence.get("required_values"):
                m["required_field_completeness_pct"] = round(
                    100 * q.evidence["required_values_present"] / q.evidence["required_values"], 1
                )
            cov = [c for c in v["checks"] if c.check == "coverage"]
            if cov:
                m["task_coverage"] = f"{sum(c.status is CheckStatus.PASS for c in cov)}/{len(cov)}"
            if "schema" in by:
                m["schema_compliance"] = (
                    "pass"
                    if by["schema"].status is CheckStatus.PASS
                    else ("missing " + ", ".join(by["schema"].evidence.get("missing", [])))
                )
            contract_checks = [c for c in v["checks"] if c.check in ("schema", "quality", "freshness")]
            m["contract_validation"] = "pass" if all(c.status is CheckStatus.PASS for c in contract_checks) else "fail"
            if "provenance" in by:
                m["provenance_complete"] = by["provenance"].status is CheckStatus.PASS
            if "metadata" in by:
                m["catalog_and_contract_complete"] = by["metadata"].status is CheckStatus.PASS
            out[sid] = m
        return out

    def _record(self, role_key: str, task_id: str, result, systems: dict) -> None:
        """One evaluation record (no raw data, no prompt) + per-asset quality history."""
        rnd = len(self.state["rounds"])
        self.state["evaluations"] = (
            self.state["evaluations"]
            + [
                {
                    "evaluation_id": result.evaluation_id,
                    "task_id": task_id,
                    "task_profile_version": result.profile_version,
                    "timestamp": result.evaluated_at.isoformat(),
                    "role": role_key,
                    "decision": result.decision.value,
                    "latency_ms": result.latency_ms,
                    "adapter_mode": result.adapter_mode,
                    "evidence_refs": result.evidence_refs,
                    "checks": [
                        {
                            "requirement": c.requirement,
                            "check": c.check,
                            "status": c.status.value,
                            "duration_ms": c.duration_ms,
                            "reason": c.reason,
                        }
                        for c in result.checks
                    ],
                }
            ]
        )[-300:]
        for sid, v in systems.items():
            if v["status"] != "NO_ACCESS":
                hist = self.state["history"].setdefault(self.sys[sid]["asset"], [])
                hist.append(
                    {
                        "round": rnd,
                        "at": result.evaluated_at.isoformat(),
                        "status": v["status"],
                        "evaluation_id": result.evaluation_id,
                        "task": task_id,
                    }
                )
                del hist[:-10]
        self._save()

    def answer(self, task: dict, systems: dict, decision: str) -> str:
        """The user-facing sentence: what is possible, what blocks it, what to do. Restricted data is not named."""
        actions = [f"{a.replace('_', ' ')} {v}" for a, v in task["actions"].items() if "human approval" in v]
        act = (" Passing the data gate is not authorization to act: " + "; ".join(actions) + ".") if actions else ""
        out = task["output"].replace("_", " ")
        if decision == "READY":
            return f"All data requirements for the **{out}** are met.{act}"
        if decision == "READY_WITH_WARNINGS":
            warn = [
                f"{self.sys[s]['label']} - {self.short((v['bad'] or [self._warn(v)])[0])}"
                for s, v in systems.items()
                if v["status"] == "WARN"
            ]
            return f"I can prepare the {out}, with warnings: {'; '.join(warn)}.{act}"
        reasons, restricted = [], 0
        for sid, v in systems.items():
            if v["status"] == "NO_ACCESS" and v["mandatory"]:
                denied = [c for c in v["bad"] if c.check == "access"]
                rows = denied[0].evidence.get("denied_scope") if denied else None
                if rows:
                    reasons.append(f"you do not have access to {', '.join(rows)} data")
                elif denied and "no access to column" in denied[0].reason:
                    reasons.append(
                        "you do not have access to " + denied[0].reason.split("no access to ", 1)[1].rstrip(".")
                    )
                elif denied and "did not answer" not in denied[0].reason:
                    restricted += 1
                else:
                    reasons.append("the authorization service did not answer, so access is unknown (blocked)")
            elif v["status"] == "FAIL" and v["mandatory"]:
                reasons.append(f"{self.sys[sid]['label']} - {self.short(v['bad'][0])}")
        if restricted:
            reasons.append(f"you do not have access to {restricted} required data set(s)")
        ok = {self.sys[s]["asset"] for s, v in systems.items() if v["status"] in ("PASS", "WARN")}
        partial = [what for what, needs in (task.get("can_still_do") or {}).items() if set(needs) <= ok]
        text = f"I can't prepare the {out} yet: " + "; ".join(dict.fromkeys(reasons)) + "."
        if partial:
            text += f" I can still provide {partial[0]}."
        return text + " Approve the proposed fixes, or contact the data owner for access." + act

    @staticmethod
    def _warn(v) -> Any:
        return next(c for c in v["checks"] if c.status is CheckStatus.WARN)

    def check(self, role_key: str, task_id: str = DEFAULT_TASK) -> dict[str, Any]:
        task_id = self.resolve(role_key, task_id)
        role, task = self.role(role_key), self.task(task_id)
        result = self._evaluate(role_key, task_id)
        systems = self._systems(result, task_id)
        decision = result.decision.value
        by_role = {}
        for key in self.roles:  # same data and task, every role profile
            sy = self._systems(self._evaluate(key, task_id, quiet=True), task_id)
            by_role[key] = {"score": self.score(sy), "no_access": sum(v["status"] == "NO_ACCESS" for v in sy.values())}
        by_task = {tid: self._evaluate(role_key, tid, quiet=True).decision.value for tid in self.harness}
        self._record(role_key, task_id, result, systems)
        ok = (CheckStatus.PASS, CheckStatus.WARN)
        self.lineage.run(f"readiness_gate.{task_id}",
                         [staging_in(self.sys[sid]["asset"], [(c.check, c.status in ok) for c in v["checks"]])
                          for sid, v in systems.items()], [])  # fmt: skip
        self._learn_rules(systems)
        published = self._publish(role_key, task_id, result, systems) if decision != "BLOCKED" else []
        fixed = self._save_fixed(task_id) if decision != "BLOCKED" else None
        ds = self.state.get("current_dataset")
        if self.state.get("current_dataset_task") != task_id:
            ds = None  # the loaded extract was made for another task: its list entry is not this task's result
        if ds:  # the data set list shows which data sets were tested, and the result per ROLE + TASK profile
            entry = {"decision": decision, "role": role["title"], "task": task_id, "at": self.clock().isoformat()}
            for target in {ds, fixed["id"] if fixed else ds}:
                self.state.setdefault("dataset_results", {}).setdefault(f"{task_id}/{target}", {})[
                    f"{role_key}|{task_id}"
                ] = entry
        if ds or fixed:
            self._save()

        def passing(check: str) -> str:
            per = [[c for c in v["checks"] if c.check == check] for v in systems.values()]
            per = [cs for cs in per if cs]
            return f"{sum(all(c.status in (CheckStatus.PASS, CheckStatus.WARN) for c in cs) for cs in per)}/{len(per)}"

        steps = (
            f"Engine: 1 Data Harness Profile `{task_id}` v{result.profile_version} → 2 Catalog "
            f"{passing('catalog')} → 3 Access {passing('access')} → 4 Freshness {passing('freshness')} → "
            f"5 Coverage {passing('coverage')} (+ fields {passing('schema')}, quality {passing('quality')}, "
            f"provenance {passing('provenance')}, catalog/contract {passing('metadata')}) → 6 Decision "
            f"**{decision}** · {result.latency_ms:g} ms · evaluation `{result.evaluation_id}`"
        )
        full = sum(v["score"] == 100 for v in by_role.values())
        no_access = sum(v["no_access"] > 0 for v in by_role.values())
        mark = {"READY": "✓", "READY_WITH_WARNINGS": "⚠", "BLOCKED": "✗"}
        text = (
            f"**{task['name']}** as {role['title']} ({role['app'].split(' (')[0]}) · agent `{task['agent']}`\n\n"
            f"{self.answer(task, systems, decision)}\n\n{steps}\n\n"
            f"Same data, other tasks: "
            + ", ".join(f"{self.harness[t]['name']} {mark[d]}" for t, d in by_task.items())
            + f"\n\nSame task, {len(by_role)} role profiles: {full} fully ready, "
            f"{len(by_role) - full - no_access} blocked by missing data, {no_access} without some access."
        )
        fixes = plan_fixes(result, self.profiles[task_id])
        table = self.table(role, result, systems, fixes)
        return {
            "checklist": self.checklist(role_key, task_id, result, systems, fixes),
            "answer": self.answer(task, systems, decision),
            "decision": decision,
            "ready": decision != "BLOCKED",
            "role": role_key,
            "task": task_id,
            "score": self.score(systems),
            "systems": {k: {"status": v["status"]} for k, v in systems.items()},
            "data_metrics": self.data_metrics(systems),
            "by_role": by_role,
            "by_task": by_task,
            "evaluation_id": result.evaluation_id,
            "latency_ms": result.latency_ms,
            "evidence_refs": result.evidence_refs,
            "published": published,
            "fixed_dataset": fixed,
            "table": table,
            "text": text,
        }

    # ------------------------------------------------------------------ certified data: publish + RAG ingestion
    def _publish(self, role_key: str, task_id: str, result, systems: dict) -> list[dict]:
        """READY only: every data set that passed all its checks becomes an immutable, versioned certified copy,
        and is ingested into the RAG index (restricted columns excluded). Same content = same version."""
        manifest = load_yaml(self.staging / "manifest.yaml")["assets"]
        out = []
        for sid, v in systems.items():
            if v["status"] != "PASS":
                continue  # warnings, failures and no-access data are never certified
            s, c = self.sys[sid], self.contracts[self.sys[sid]["asset"]]
            asset = s["asset"]
            fixes = self.state.setdefault("unpublished_fixes", {}).get(asset, [])
            meta = {"task": task_id, "label": s["label"], "system": s["name"],
                    "source_version": manifest[asset].get("source_version"), "evaluation_id": result.evaluation_id,
                    "certified_at": result.evaluated_at.isoformat(), "certified_by_check_as": role_key,
                    "contract": {"id": c["id"], "version": c["version"],
                                 "odcs_file": self.odcs.files.get(asset) if self.odcs else None},
                    "fixes_applied": fixes, "rows": len(read_csv(self.staging / f"{asset}.csv")[1])}  # fmt: skip
            m, new = self.publisher.publish(asset, self.staging / f"{asset}.csv", meta)
            chunks = None
            if new:
                _, rows = read_csv(self.publisher.path(asset))
                restricted = set((self.catalog[asset].get("restricted_columns") or {}).keys())
                chunks = self.rag.index_rows(asset, m["version"], s["label"], rows, restricted)
                self.state["unpublished_fixes"].pop(asset, None)
                self.lineage.run("publish_certified", [staging_in(asset)],
                                 [OutputDataset(namespace="file://published", name=asset),
                                  OutputDataset(namespace="chroma://rag", name="certified_data")])  # fmt: skip
            out.append({"asset": asset, "label": s["label"], "version": m["version"], "new": new,
                        "sha256": m["sha256"], "chunks": chunks, "certified_for": m["certified_for"],
                        "fixes_applied": m.get("fixes_applied", [])})  # fmt: skip
        if out:
            self._save()
        return out

    def search_data(self, role_key: str, task_id: str, query: str) -> dict[str, Any]:
        """RAG over the CERTIFIED (published) data sets of a READY task: only the task's authorized data sets,
        row ACL on the role's warehouses, restricted columns never indexed."""
        task_id = self.resolve(role_key, task_id)
        task, role = self.task(task_id), self.role(role_key)
        result = self._evaluate(role_key, task_id)
        if result.decision.value == "BLOCKED":
            return {"searched": False, "hits": [], "text": "Search refused: the re-check at use time is BLOCKED. "
                    + self.answer(task, self._systems(result, task_id), "BLOCKED")}  # fmt: skip
        systems = self._systems(result, task_id)
        assets = [self.sys[sid]["asset"] for sid, v in systems.items()
                  if v["status"] in ("PASS", "WARN") and self.publisher.current(self.sys[sid]["asset"])]  # fmt: skip
        # minimum necessary: the task's warehouses only, further limited by the role's row access
        whs = [self.context["origin_warehouse"], self.context["destination_warehouse"]]
        whs = [w for w in whs if not role.get("warehouses") or w in role["warehouses"]]
        hits = self.rag.search_rows(query, assets, whs)
        self.lineage.run(f"retrieve.{task_id}", [InputDataset(namespace="chroma://rag", name="certified_data")],
                         [OutputDataset(namespace=f"agent://{task['agent']}", name=f"{task_id}.context")])  # fmt: skip
        head = (f"**Certified data search** (published versions only) · query: *{query}* · data sets: "
                + (", ".join(f"{self.by_asset[a]['label']} v{self.publisher.current(a)['version']}" for a in assets)
                   or "none published yet"))  # fmt: skip
        if not hits:
            return {"searched": True, "hits": [], "text": head + "\n\nNo certified record matches."}
        rows = ["| # | Data set (cite) | Record |", "|---|---|---|"]
        rows += [f"| {i} | **{h['label']} v{h['version']}** | {h['text']} |" for i, h in enumerate(hits, 1)]
        return {"searched": True, "hits": hits, "text": head + "\n\n" + "\n".join(rows)}

    # ------------------------------------------------------------------ approval requests (planner -> approver)
    def request_approval(self, role_key: str, task_id: str) -> dict[str, Any]:
        """Queue the waiting fixes (the ones this role may not approve) for the named approvers."""
        task_id = self.resolve(role_key, task_id)
        role = self.role(role_key)
        if not self.proposals:
            self.propose(role_key, task_id)
        waiting = [p for p in self.proposals if p.action != "escalate" and p.requirement in self.by_requirement
                   and not self.can_approve(role, self.by_requirement[p.requirement]["id"])]  # fmt: skip
        if not waiting:
            return {"queued": False, "text": "Nothing to request: you can approve every proposed fix yourself."}
        domains = sorted({self.by_requirement[p.requirement]["domain"] for p in waiting})
        approver_roles = self._approver_roles(role, domains)
        req = {"id": len(self.state.setdefault("requests", [])) + 1, "status": "pending", "task": task_id,
               "requested_by": role_key, "requested_by_title": role["title"], "requested_at": self.clock().isoformat(),
               "round": len(self.state["rounds"]), "dataset": self.state.get("current_dataset"), "domains": domains,
               "approver_roles": approver_roles, "fixes": [p.model_dump() for p in waiting], "history": []}  # fmt: skip
        self.state["requests"].append(req)
        self.proposals = [p for p in self.proposals if p not in waiting]  # the request now owns these fixes
        self._save()
        names = " or ".join(r["title"] for r in approver_roles)
        return {"queued": True, "request": req,
                "text": f"Request #{req['id']} sent to {names}: {len(waiting)} fix(es) for {self.task(task_id)['name']}. "
                        "It is approved when one of them opens this page as their role (User role) and clicks "
                        "Approve & apply under Approval requests for you."}  # fmt: skip

    def approvals_overview(self, role_key: str) -> dict[str, Any]:
        """Step 9 "Fixes to approve": the proposed fixes not applied yet (with who approves each) + pending requests."""
        role = self.role(role_key)
        proposed = []
        for p in self.proposals:
            s = self.by_requirement.get(p.requirement)
            label = self.FIX_LABEL[p.action].format(column=p.column, values=", ".join(p.values), rationale=p.rationale)
            if p.action == "escalate" or s is None:
                proposed.append({"data": s["label"] if s else p.requirement, "fix": label, "mine": False,
                                 "approvers": ["a person (data owner / access admin)"], "kind": "escalate"})  # fmt: skip
                continue
            mine = self.can_approve(role, s["id"])
            who = ["you"] if mine else [r["title"] for r in self._approver_roles(role, {s["domain"]})]
            proposed.append({"data": s["label"], "fix": label, "mine": mine, "approvers": who,
                             "kind": "apply" if mine else "request"})  # fmt: skip
        requests = [r for r in self.state.get("requests", []) if r["status"] == "pending"]
        return {"proposed": proposed, "requests": requests,
                "total": len(proposed) + sum(len(r["fixes"]) for r in requests)}  # fmt: skip

    def inbox(self, role_key: str) -> list[dict]:
        role = self.role(role_key)
        return [r for r in self.state.get("requests", []) if r["status"] == "pending" and role["human"]
                and any(d in role["approve"] for d in r["domains"])]  # fmt: skip

    def decide(self, role_key: str, request_id: int, approve: bool) -> dict[str, Any]:
        role = self.role(role_key)
        req = next((r for r in self.state.get("requests", []) if r["id"] == request_id), None)
        if req is None or req not in self.inbox(role_key):
            raise KeyError(f"request {request_id}")
        is_mine = lambda p: self.by_requirement[p["requirement"]]["domain"] in role["approve"]  # noqa: E731
        mine = [FixProposal.model_validate(p) for p in req["fixes"] if is_mine(p)]
        rest = [p for p in req["fixes"] if not is_mine(p)]  # fixes another approver must decide
        now = self.clock().isoformat()
        if not approve:
            req.update(status="rejected", decided_by=role_key, decided_at=now)
            req["history"].append({"by": role["title"], "at": now, "decision": "rejected", "fixes": len(req["fixes"])})
            self._save()
            return {"status": "rejected", "text": f"Request #{request_id} rejected by {role['title']}. The requester's "
                    "task stays BLOCKED; nothing was changed."}  # fmt: skip
        changes = self.remediator.apply(self.staging, mine)
        self._after_fixes(mine, role_key, changes)
        done_keys = {(p.requirement, p.action, p.column, tuple(p.values)) for p in mine}
        self.proposals = [p for p in self.proposals
                          if (p.requirement, p.action, p.column, tuple(p.values)) not in done_keys]  # fmt: skip
        req["history"].append({"by": role["title"], "at": now, "decision": "approved", "fixes": len(mine)})
        if rest:  # partly approved: the request stays open for the other approver(s)
            domains = sorted({self.by_requirement[p["requirement"]]["domain"] for p in rest})
            req.update(
                fixes=rest,
                domains=domains,
                approver_roles=self._approver_roles(self.role(req["requested_by"]), domains),
            )
        else:
            req.update(status="approved", decided_by=role_key, decided_at=now)
        after = self.check(req["requested_by"], req["task"])  # re-check as the requester, for their task
        self._save()
        done = [c for c in changes if c.get("status") != "skipped"]
        tail = (f" {len(rest)} fix(es) still wait for {' or '.join(r['title'] for r in req['approver_roles'])}."
                if rest else "")  # fmt: skip
        fixed = f" Saved as data set {after['fixed_dataset']['id']}." if after.get("fixed_dataset") else ""
        return {"status": req["status"], "applied": [c["action"] for c in done], "after": after,
                "text": f"Request #{request_id}: {role['title']} approved {len(mine)} fix(es), {len(done)} changed data. "
                        f"Re-check for {self.role(req['requested_by'])['title']}: **{after['decision']}**.{tail}{fixed}"}  # fmt: skip

    def _learn_rules(self, systems: dict) -> None:
        """Upstream rule = one specific fault of this extract that approved fixes removed (its check now passes).
        Only those faults are prevented in later extractions; other faults of the same table or check stay visible."""
        fixes = self.state.get("staging_fixes") or []
        if not fixes or not self.state["rounds"]:
            return
        known = {r.get("fault") for r in self.state["rules"]}
        touched = {f["asset"] for f in fixes}  # tables an approved fix really changed
        by_sys = {self.sys[sid]["requirement"]: v for sid, v in systems.items()}
        for fid in self.state["rounds"][-1].get("faults", []):
            f = next((x for x in self.faults if x["id"] == fid), None)
            v = by_sys.get(self.sys[f["system"]]["requirement"]) if f else None
            if f is None or v is None or fid in known or self.sys[f["system"]]["asset"] not in touched:
                continue  # fault in a table this task does not use, or no fix touched it: not confirmed fixed
            checks = [c for c in v["checks"] if c.check == f["check"]]
            if checks and all(c.status is CheckStatus.PASS for c in checks):
                self.state["rules"].append({"fault": fid, "title": f["title"], "requirement": self.sys[f["system"]]["requirement"],
                                            "check": f["check"], "round": len(self.state["rounds"]),
                                            "approved_by": fixes[-1]["approved_by"]})  # fmt: skip

    def _after_fixes(self, applied: list, role_key: str, changes: list[dict]) -> None:
        """Record approved fixes: upstream rules for the next extraction + 'fixes since last publish' per data set
        (only changes that really modified data; skipped ones are not claimed)."""
        n = len(self.state["rounds"])
        pending = self.state.setdefault("unpublished_fixes", {})
        for c in changes:
            if c.get("status") in ("skipped", "needs_human") or c["requirement"] not in self.by_requirement:
                continue
            asset = self.by_requirement[c["requirement"]]["asset"]
            fix = {
                "asset": asset,
                "action": c["action"],
                "detail": c.get("detail", ""),
                "approved_by": role_key,
                "round": n,
            }
            pending.setdefault(asset, []).append({k: v for k, v in fix.items() if k != "asset"})
            self.state.setdefault("staging_fixes", []).append(fix)  # fixes in the current extract (fixed data set)
        # upstream rules are learned in check(): only for the specific faults the re-check confirms are gone
        if applied:
            ins, outs = {}, {}
            for p in applied:
                s = self.by_requirement[p.requirement]
                if "source of record" in p.source:
                    ins[("src", s["asset"])] = source_ds(s["name"], s["asset"])
                if "steward registry" in p.source:
                    ins[("reg",)] = InputDataset(namespace="file://pipeline", name="steward_registry.yaml")
                ins[("stg", s["asset"])] = staging_in(s["asset"])
                outs[s["asset"]] = staging_out(s["asset"])
            self.lineage.run("apply_fixes", list(ins.values()), list(outs.values()))

    # ------------------------------------------------------------------ the 3-column table
    FIX_LABEL = {
        "refresh_from_source": "Reload from the SAP source of record",
        "restore_columns": "Restore column(s) {values} from source",
        "reextract_rows": "Re-extract {values} from source",
        "deduplicate": "Remove duplicate rows",
        "normalise_codes": "Normalise codes in {column}",
        "reformat_dates": "Convert {column} to YYYY-MM-DD (declared export format)",
        "replace_bad_rows_from_source": "Replace bad rows with source rows",
        "lookup_owner": "Fill {values} from the steward registry",
        "escalate": "Needs a person: {rationale}",
    }
    QUALITY = {
        "unique_key": "duplicate rows",
        "not_null": "empty values",
        "non_negative": "negative values",
        "allowed_values": "unknown codes",
        "iso_dates": "non-ISO dates",
    }

    def short(self, c) -> str:
        """One short checkpoint label, e.g. 'stale: 74 h old (max 2 h)'."""
        ev = c.evidence
        if c.check == "freshness" and "age_hours" in ev:
            return f"stale: {ev['age_hours']:g} h old (max {ev['max_age_hours']:g} h)"
        if c.check == "schema" and ev.get("missing"):
            return "missing column " + ", ".join(ev["missing"])
        if c.check == "coverage" and ev.get("dimension") == "period":
            return "does not cover " + ", ".join(ev.get("requested", []))
        if c.check == "coverage" and ev.get("missing"):
            return "no data for " + ", ".join(ev["missing"])
        if c.check == "quality" and ev.get("violations"):
            return ", ".join(
                f"{v['count']} {self.QUALITY[k]}" + (f" in {', '.join(v['columns'])}" if v["columns"] else "")
                for k, v in ev["violations"].items()
            )
        if c.check == "provenance" and ev.get("missing"):
            return "not recorded: " + ", ".join(ev["missing"])
        if c.check == "metadata":
            gaps = ev.get("missing_catalog_fields", []) + ev.get("missing_contract_sections", [])
            return "incomplete catalog/contract: " + ", ".join(gaps)
        if c.check == "documents":
            return "no approved, currently valid document"
        if c.check == "access" and "did not answer" in c.reason:
            return "access unknown: the authorization service did not answer"
        if c.check == "access":
            return "not accessible: " + c.reason.split(": ", 1)[-1].rstrip(".")
        if c.check == "evidence":
            return "could not be checked (service unavailable)"
        return f"{c.check}: {c.reason.split('. ')[0].rstrip('.')}"

    def table(self, role: dict, result, systems: dict, proposals) -> str:
        """| current data metric | missing checkpoints | recommended fix | - one row per required data set."""
        cell = lambda s: str(s).replace("|", "/").replace("\n", " ")  # noqa: E731
        metrics = self.data_metrics(systems)
        icon = {"PASS": "✓", "WARN": "⚠", "FAIL": "✗", "NO_ACCESS": "✗"}
        rows = ["| Data · current metric | Missing checkpoints | Recommended fix |", "|---|---|---|"]
        for sid, v in systems.items():
            s = self.sys[sid]
            denied = [c for c in v["bad"] if c.check == "access"]
            visible = any(c.evidence.get("denied_scope") or "no access to column" in c.reason for c in denied)
            if v["status"] == "NO_ACCESS" and not visible and not any("did not answer" in c.reason for c in denied):
                rows.append(
                    f"| Restricted data set{'' if v['mandatory'] else ' (optional)'} | ✗ no access for "
                    "this role | Ask the data owner for access |"
                )  # name and metrics are not disclosed
                continue
            m = metrics[sid]
            metric = f"**{s['label']}** ({s['name']}){'' if v['mandatory'] else ' · optional'}"
            if "records" in m:
                metric += f" · {m['records']} records"
            if "source_data_age_hours" in m:
                metric += f" · {m['source_data_age_hours']:g} h old"
            if "required_field_completeness_pct" in m:
                metric += f" · {m['required_field_completeness_pct']:g}% complete"
            hist = self.state["history"].get(s["asset"], [])[-5:]
            if hist:
                metric += " · history " + "".join(icon.get(h["status"], "?") for h in hist)
            bad = v["bad"] or [c for c in v["checks"] if c.status is CheckStatus.WARN]
            missing = (icon[v["status"]] + " " + "; ".join(self.short(c) for c in bad)) if bad else "✓ none"
            fixes = []
            for p in [p for p in proposals if p.requirement == s["requirement"]]:
                label = self.FIX_LABEL[p.action].format(
                    column=p.column, values=", ".join(p.values), rationale=p.rationale
                )
                if p.action != "escalate":
                    label += (
                        " *(you can approve)*"
                        if self.can_approve(role, sid)
                        else (f" *(approver: {' or '.join(self.domains[s['domain']])})*")
                    )
                fixes.append(label)
            rows.append(f"| {cell(metric)} | {cell(missing)} | {cell('; '.join(fixes) or '—')} |")
        refs = ", ".join(f"`{r}`" for r in result.evidence_refs)
        return "\n".join(rows) + (f"\n\nEvidence: {refs} · validation run `{result.evaluation_id}`" if refs else "")

    def _lineage_note(self, asset: str) -> str:
        evs = self.lineage.events(asset, limit=200)
        if not evs:
            return " · OpenLineage: no run recorded yet"
        last = evs[0]
        jobs = ", ".join(dict.fromkeys(e["job"]["name"].split(".")[0] for e in evs))
        return (f" · OpenLineage: {len(evs)} run(s) recorded ({jobs}); last `{last['job']['name']}` "
                f"run {last['run']['runId'][:8]}")  # fmt: skip

    # ------------------------------------------------------------------ root cause (from measured evidence only)
    @staticmethod
    def _access_cause(d, who: str) -> str:
        if d is None:
            return "The authorization service did not answer, so access cannot be confirmed. Not a data problem."
        if "warehouse" in d.reason:
            return f"{who} has row access to other warehouses only ({d.reason}). Access policy, not data quality."
        if "column" in d.reason:
            return f"{who} may not see a restricted column ({d.reason}). Access policy, not data quality."
        return f"{who} has no read right on this data set ({d.reason}). Access policy, not data quality."

    @staticmethod
    def _check_cause(chk, contract: dict) -> str:
        ev, reason = chk.evidence, chk.reason.rstrip(".")
        if chk.status is CheckStatus.ERROR:
            return f"Unknown: {reason}. The evidence needed for this check is not available."
        if chk.check == "freshness":
            return (f"The newest SAP snapshot ({ev.get('source_event_time') or 'unknown time'}) is "
                    f"{ev.get('age_hours', '?')} h old; this task allows {ev.get('max_age_hours', 0):g} h. "
                    "The scheduled extraction did not deliver a newer snapshot.")  # fmt: skip
        if chk.check == "schema":
            return (f"Contract {contract['id']} requires {', '.join(ev.get('missing', [])) or 'more columns'}, but the "
                    "extract does not contain them: the export or connector mapping dropped the column(s).")  # fmt: skip
        if chk.check == "coverage":
            return f"{reason}: that partition is not in the extract (extraction filter or partial load)."
        if chk.check == "provenance":
            return f"{reason}: the extraction did not record where the data came from or who owns it."
        if chk.check == "metadata":
            gaps = list(ev.get("missing_catalog_fields", [])) + list(ev.get("missing_contract_sections", []))
            return (
                f"Not maintained by the data owner / steward in the catalog or contract: {', '.join(gaps) or reason}."
            )
        if chk.check == "documents":
            return (f"{reason.split('. ')[0]}: the agreement or policy expired or was superseded and no newer approved "
                    "version was published.")  # fmt: skip
        if chk.check == "access":
            return f"{reason}. Access policy, not data quality."
        return reason

    def _quality_causes(self, adapter, asset: str, req, viol: dict) -> dict[str, str]:
        """Name the offending values (e.g. the unit code) so the cause is concrete. Keys: rule or rule:column."""
        q = req.quality
        if not q or not viol:
            return {}
        rows = [r for r in (adapter.get_rows(asset) or []) if in_scope(q, self.context, r)]

        def values(col: str, bad) -> str:
            found = sorted({str(r.get(col) or "").strip() for r in rows if bad(str(r.get(col) or "").strip())})
            return ", ".join(f"'{x}'" for x in found[:4])

        out = {}
        for kind, v in viol.items():
            n, cols = v["count"], v["columns"]
            if kind == "allowed_values":
                for col in cols:
                    ok = q.allowed_values[col]
                    out[f"allowed_values:{col}"] = (
                        f"{col} contains {values(col, lambda x, ok=ok: x != '' and x not in ok)}, which the contract "
                        f"does not allow ({', '.join(ok)}): the SAP source uses another code or spelling.")  # fmt: skip
            elif kind == "iso_dates":
                for col in cols:
                    out[f"iso_dates:{col}"] = (f"{col} has dates such as {values(col, lambda x: x != '' and not is_iso_date(x))}: "
                                               "exported in a local format instead of YYYY-MM-DD.")  # fmt: skip
            elif kind == "non_negative":
                out[kind] = f"{n} negative or non-numeric value(s) in {', '.join(cols)}: invalid entries in the source."
            elif kind == "not_null":
                out[kind] = (
                    f"{n} empty value(s) in {', '.join(cols)}: the records were incomplete in the source or the extract dropped them."
                )
            elif kind == "unique_key":
                out[kind] = (f"{n} duplicate key(s) ({', '.join(v['keys'][:3])}): the same records were loaded twice, or two "
                             "versions of one record were extracted.")  # fmt: skip
        return out

    # ------------------------------------------------------------------ AI-readiness checklist (one row per metric)
    def checklist(self, role_key: str, task_id: str, result, systems: dict, proposals) -> list[dict]:
        """Per data source: every metric the data must satisfy to be AI-ready, with value, status and fix.
        Values come from the catalog record, the contract, the extraction and the checks; nothing is estimated."""
        role, adapter = self.roles[role_key], self._adapter()
        user = Principal(principal_id=f"role:{role_key}", kind="user", source="demo_selector", authenticated=False)
        agent = Principal(principal_id=self.harness[task_id]["agent"], kind="service", source="demo_selector",
                          authenticated=False)  # fmt: skip
        reqs = {a.asset_id: a for a in self.profiles[task_id].assets}
        icon_hist = {"PASS": "✓", "WARN": "⚠", "FAIL": "✗"}
        out = []
        for sid, v in systems.items():
            s = self.sys[sid]
            asset, req, c = s["asset"], reqs[s["asset"]], self.contracts[s["asset"]]
            by: dict[str, list] = {}
            for chk in v["checks"]:
                by.setdefault(chk.check, []).append(chk)
            fixes: dict[str, list[str]] = {}
            for p in proposals:
                if p.requirement == s["requirement"]:
                    label = self.FIX_LABEL[p.action].format(column=p.column, values=", ".join(p.values),
                                                            rationale=p.rationale)  # fmt: skip
                    who = (
                        "you can approve"
                        if p.action != "escalate" and self.can_approve(role, sid)
                        else ("approver: " + " or ".join(self.domains[s["domain"]]) if p.action != "escalate" else "")
                    )
                    fixes.setdefault(p.check, []).append(label + (f" ({who})" if who else ""))
            rows: list[dict] = []
            causes = {chk.check: self._check_cause(chk, c) for chk in v["checks"] if chk.status is not CheckStatus.PASS}

            def add(group, metric, value, status, checks=(), cause=None, fixes=fixes, rows=rows, causes=causes):
                fix = (
                    "; ".join(dict.fromkeys(f for k in checks for f in fixes.get(k, [])))
                    if status in ("FAIL", "WARN")
                    else ""
                )
                why = ""
                if status in ("FAIL", "WARN", "ERROR"):
                    why = cause or "; ".join(dict.fromkeys(causes[k] for k in checks if causes.get(k)))
                rows.append({"group": group, "metric": metric, "value": str(value), "status": status, "fix": fix,
                             "cause": why})  # fmt: skip

            def worst(name, by=by):
                order = {"PASS": 0, "SKIPPED": 0, "WARN": 1, "FAIL": 2, "ERROR": 2}
                return max(by.get(name, []), key=lambda one: order[one.status.value], default=None)

            # access first: it decides whether anything else may be shown
            scope = row_scope(req, self.context)
            try:
                ud = adapter.authorize(user, asset, PURPOSE, scope)
                ad = adapter.authorize(agent, asset, PURPOSE, scope)
            except AdapterUnavailable:
                ud = ad = None
            add("Access", "Has the user been authorized to use it?", ud.reason if ud else
                "unknown: the authorization service did not answer", "PASS" if ud and ud.allowed else "FAIL", ["access"],
                self._access_cause(ud, "The user's role"))  # fmt: skip
            add("Access", "Has the agent been authorized to use it?", f"{agent.principal_id}: {ad.reason}" if ad else
                "unknown: the authorization service did not answer", "PASS" if ad and ad.allowed else "FAIL", ["access"],
                self._access_cause(ad, f"Agent {agent.principal_id}"))  # fmt: skip
            denied = [x for x in by.get("access", []) if x.status is not CheckStatus.PASS]
            restricted = v["status"] == "NO_ACCESS" and not any(
                x.evidence.get("denied_scope") or "no access to column" in x.reason or "did not answer" in x.reason
                for x in denied)  # fmt: skip
            if restricted:  # do not disclose anything else about data the role may not read
                out.append({"source": "Restricted data set", "status": v["status"], "rows": rows})
                continue
            rec = adapter.get_record(asset) or {}
            meta = worst("metadata")
            missing_meta = set(meta.evidence.get("missing_catalog_fields", [])) if meta else set()
            has = lambda *fs, rec=rec: all(rec.get(f) not in (None, "", [], {}) for f in fs)  # noqa: E731

            add("Ownership", "Who owns the data?",
                f"owner {rec.get('business_owner') or '—'} · steward {rec.get('data_steward') or '—'} · technical "
                f"{rec.get('technical_owner') or '—'} · contact {rec.get('contact') or '—'}",
                "PASS" if has("business_owner", "data_steward", "technical_owner", "contact") else "FAIL",
                ["provenance", "metadata"])  # fmt: skip
            add(
                "Ownership",
                "Is ownership reviewed and the asset active?",
                f"reviewed {rec.get('owner_reviewed_on') or '—'} · {rec.get('lifecycle_status') or '—'}",
                "PASS" if has("owner_reviewed_on") and rec.get("lifecycle_status") == "active" else "FAIL",
                ["metadata"],
            )
            add(
                "Source & history",
                "Which SAP system or file did it come from?",
                f"{rec.get('source_system') or '—'} · {rec.get('source_location') or '—'} · record id "
                f"{rec.get('source_record_id') or '—'}",
                "PASS" if has("source_system", "source_location", "source_record_id") else "FAIL",
                ["metadata"],
            )
            add(
                "Source & history",
                "Which version / snapshot was used?",
                rec.get("source_version") or "—",
                "PASS" if has("source_version") else "FAIL",
                ["provenance"],
            )
            fr = worst("freshness")
            if fr is not None:
                ev = fr.evidence
                delay = self.data_metrics({sid: v}).get(sid, {}).get("ingestion_delay_hours")
                add("Source & history", "When was it last refreshed?",
                    f"source data {ev.get('age_hours', '?')} h old (limit {ev.get('max_age_hours', 0):g} h) · event "
                    f"{ev.get('source_event_time') or '—'} · ingested {ev.get('ingested_at') or '—'}"
                    + (f" (delay {delay} h)" if delay is not None else ""), fr.status.value, ["freshness"])  # fmt: skip
            add(
                "Source & history",
                "Are lineage, transformations and changes recorded?",
                " → ".join(rec.get("lineage") or ["—"])
                + f" · {len(rec.get('change_history') or [])} change(s)"
                + self._lineage_note(asset),
                "PASS" if has("lineage", "transformation_history", "change_history") else "FAIL",
                ["metadata"],
            )
            hist = self.state["history"].get(asset, [])[-5:]
            add(
                "Source & history",
                "What do past quality checks show?",
                " ".join(icon_hist.get(h["status"], "?") for h in hist) or "no earlier check",
                "INFO",
            )
            add(
                "Meaning & coverage",
                "Are business definitions and the schema documented?",
                f"{len(rec.get('business_definitions') or {})} definition(s) · {len(rec.get('schema_types') or {})} "
                "typed field(s)",
                "PASS" if has("business_definitions", "schema_types") else "FAIL",
                ["metadata"],
            )
            sc = worst("schema")
            if sc is not None:
                add("Meaning & coverage", "Are the required fields present?",
                    "all present" if sc.status is CheckStatus.PASS else "missing " + ", ".join(sc.evidence.get("missing", [])),
                    sc.status.value, ["schema"])  # fmt: skip
            cov = by.get("coverage", [])
            if cov:
                bad = [x for x in cov if x.status is not CheckStatus.PASS]
                add("Meaning & coverage", "Does it cover the requested warehouses, products and period?",
                    "; ".join(x.reason.rstrip(".") for x in (bad or cov)), (bad[0].status.value if bad else "PASS"),
                    ["coverage"])  # fmt: skip
            else:
                add(
                    "Meaning & coverage",
                    "Does it cover the requested warehouses, products and period?",
                    "no coverage requirement in this profile",
                    "NA",
                )
            q = worst("quality")
            viol = (q.evidence.get("violations") or {}) if q else {}
            qc = self._quality_causes(adapter, asset, req, viol)
            if qc:
                causes["quality"] = "; ".join(qc.values())
            units = c.get("quality", {}).get("allowed_values", {}).get("uom")
            if units:
                bad_units = [k for k, x in viol.items() if "uom" in x.get("columns", [])]
                add("Meaning & coverage", "Are units consistent?",
                    f"allowed {', '.join(units)}" + (" · " + ", ".join(f"{viol[k]['count']} row(s) with another unit"
                                                                      for k in bad_units) if bad_units else " · all rows comply"),
                    "FAIL" if bad_units else "PASS", ["quality"], qc.get("allowed_values:uom"))  # fmt: skip
            else:
                add(
                    "Meaning & coverage",
                    "Are units consistent?",
                    f"no unit column · units: {rec.get('units') or '—'}",
                    "NA",
                )
            add(
                "Meaning & coverage",
                "Granularity, limitations and approved use documented?",
                f"{rec.get('granularity') or '—'} · limitations: {'; '.join(rec.get('known_limitations') or ['—'])} · "
                f"use: {', '.join(rec.get('approved_use_cases') or ['—'])}",
                "PASS" if has("granularity", "known_limitations", "approved_use_cases") else "FAIL",
                ["metadata"],
            )
            if q is not None:
                total, present = q.evidence.get("required_values", 0), q.evidence.get("required_values_present", 0)
                add("Quality", "Are required values complete?",
                    f"{present}/{total} required values populated" + (f" ({100 * present / total:.1f}%)" if total else ""),
                    "PASS" if present == total else "FAIL", ["quality"], qc.get("not_null"))  # fmt: skip
                validity = {k: x for k, x in viol.items() if k in ("allowed_values", "iso_dates", "non_negative")}
                add("Quality", "Are values valid and consistent (codes, dates, ranges)?",
                    ", ".join(f"{x['count']} {self.QUALITY[k]} in {', '.join(x['columns'])}" for k, x in validity.items())
                    or "all valid", "FAIL" if validity else "PASS", ["quality"],
                    "; ".join(x for k, x in qc.items() if k.split(":")[0] in validity) or None)  # fmt: skip
                dup = viol.get("unique_key")
                add("Quality", "Are there duplicates?",
                    f"{dup['count']} duplicate row(s) on {', '.join(c['schema']['unique_key'])}" if dup else
                    f"none on {', '.join(c['schema']['unique_key'])}", "FAIL" if dup else "PASS", ["quality"],
                    qc.get("unique_key"))  # fmt: skip
            add(
                "Quality",
                "Is it reconciled with SAP (accuracy)?",
                "not checked in this demo: needs a trusted reconciliation source",
                "NA",
            )
            contract_checks = [x for k in ("schema", "quality", "freshness") for x in by.get(k, [])]
            failed_c = [x.check for x in contract_checks if x.status is not CheckStatus.PASS]
            add("Quality", "Have the relevant quality checks passed?",
                f"contract {c['id']} v{c['version']}: " + ("schema, quality and freshness pass" if not failed_c else
                                                            "failing: " + ", ".join(dict.fromkeys(failed_c))),
                "FAIL" if failed_c else "PASS", list(dict.fromkeys(failed_c)))  # fmt: skip
            add(
                "Security",
                "Is the purpose permitted?",
                f"{PURPOSE} · permitted: {', '.join(rec.get('permitted_purposes') or ['—'])}",
                "PASS" if PURPOSE in (rec.get("permitted_purposes") or []) else "FAIL",
            )
            add(
                "Security",
                "Classification and retention defined?",
                f"{rec.get('classification') or '—'} · {rec.get('retention') or '—'}",
                "PASS" if has("classification", "retention") else "FAIL",
                ["metadata"],
            )
            if asset in DOCUMENT_ASSETS:
                d = worst("documents")
                if d is not None:
                    add(
                        "Documents",
                        "Is the document approved and currently valid?",
                        d.reason.split(". ")[0],
                        d.status.value,
                        ["documents"],
                    )
                add(
                    "Documents",
                    "Document authority and RAG index version?",
                    f"{rec.get('document_authority') or '—'} · declared: {rec.get('rag_index_version') or '—'}"
                    + (f" · live index {self.rag._version}" if self.rag._version else ""),
                    "PASS" if has("document_authority", "rag_index_version") else "FAIL",
                    ["metadata"],
                )
            sections = meta.evidence.get("missing_contract_sections", []) if meta else []
            add(
                "Contract",
                "Is there a complete data contract?",
                f"{c['id']} v{c['version']} ({c.get('status')}) · producer {c.get('producer')}"
                + (f" · missing {', '.join(sections)}" if sections else ""),
                "FAIL" if sections else "PASS",
                ["metadata"],
            )
            if self.odcs is not None:
                errs = self.odcs.errors.get(asset, [])
                add("Contract", "Is the contract a valid ODCS file in the catalog?",
                    f"{self.odcs.files.get(asset, '—')} · ODCS v3.1.0 JSON Schema: "
                    + ("valid" if not errs else f"{len(errs)} error(s): {'; '.join(errs[:2])}"),
                    "FAIL" if errs else "PASS", (),
                    f"The ODCS file does not match the official schema: {'; '.join(errs[:2])}" if errs else None)  # fmt: skip
            add(
                "Contract",
                "Is there validation evidence?",
                f"run {result.evaluation_id} on {rec.get('source_version') or '—'} at {result.evaluated_at.isoformat()}",
                "PASS",
            )
            if missing_meta - {"business_owner", "technical_owner", "data_steward", "contact"}:
                add("Ownership", "Other catalog fields missing", ", ".join(sorted(missing_meta)), "FAIL", ["metadata"])
            out.append({"source": f"{s['label']} ({s['name']})", "status": v["status"],
                        "mandatory": v["mandatory"], "rows": rows})  # fmt: skip
        return out

    ACTION_CHECKS = {  # which failing check an action repairs (Claude names the action, not the check)
        "refresh_from_source": ("freshness", "provenance", "schema", "coverage", "quality"),
        "restore_columns": ("schema",),
        "reextract_rows": ("coverage",),
        "deduplicate": ("quality",),
        "normalise_codes": ("quality",),
        "reformat_dates": ("quality",),
        "replace_bad_rows_from_source": ("quality",),
        "lookup_owner": ("provenance", "metadata"),
    }

    def _with_check(self, p: FixProposal, systems: dict) -> FixProposal:
        if p.check:
            return p
        s = self.by_requirement.get(p.requirement)
        failing = [
            c for c in (systems.get(s["id"], {}).get("checks", []) if s else []) if c.status is not CheckStatus.PASS
        ]
        wanted = self.ACTION_CHECKS.get(p.action)
        hit = next((c for c in failing if wanted is None or c.check in wanted), None)
        return p.model_copy(update={"check": hit.check}) if hit else p

    def propose(self, role_key: str, task_id: str = DEFAULT_TASK, agent: str = "mock") -> dict[str, Any]:
        """Correction agent. agent='mock': the deterministic planner (no LLM). agent='claude': Claude proposes through
        the same allow-list (app/claude_agent.py); it needs an Anthropic credential and never applies anything."""
        task_id = self.resolve(role_key, task_id)
        role = self.role(role_key)
        result = self._evaluate(role_key, task_id, quiet=True)
        systems = self._systems(result, task_id)
        summary = ""
        if agent == "claude":
            from app.claude_agent import propose_with_claude

            out = propose_with_claude(title=self.harness[task_id]["name"], result=result, profile=self.profiles[task_id],
                                      adapter=self._adapter(), context=self.context, client=self.claude_client)  # fmt: skip
            if not out.get("available") or not out.get("proposals"):
                self.proposals = []
                note = out.get("message") or out.get("summary") or "no proposals"
                return {"agent": "claude", "available": bool(out.get("available")), "proposals": [],
                        "text": f"**Claude agent**: {note}"}  # fmt: skip
            self.proposals = [self._with_check(FixProposal.model_validate(p), systems) for p in out["proposals"]]
            summary, agent_name = out.get("summary", ""), f"**Claude agent** (`{out.get('model')}`)"
        else:
            self.proposals = plan_fixes(result, self.profiles[task_id])
            agent_name = "**Mock correction agent** (deterministic, no LLM)"
        rows = [
            "| # | Data set | Missing data metric | Proposed fix | Data from | Who approves |",
            "|---|---|---|---|---|---|",
        ]
        mine = 0
        for i, p in enumerate(self.proposals, 1):
            s = self.by_requirement.get(p.requirement)
            v = systems.get(s["id"]) if s else None
            hit = [c for c in (v["checks"] if v else []) if c.check == p.check and c.status is not CheckStatus.PASS]
            denied = [c for c in (v["checks"] if v else []) if c.check == "access" and c.status is not CheckStatus.PASS]
            restricted = bool(
                v
                and v["status"] == "NO_ACCESS"
                and not any(  # do not name data the role cannot see
                    c.evidence.get("denied_scope") or "no access to column" in c.reason or "did not answer" in c.reason
                    for c in denied
                )
            )
            name = "Restricted data set" if restricted else (s["label"] if s else p.requirement)
            metric = "no access for this role" if restricted else (self.short(hit[0]) if hit else p.check)
            fix = self.FIX_LABEL[p.action].format(column=p.column, values=", ".join(p.values), rationale=p.rationale)
            if p.action == "escalate":
                who = "data owner / access admin"
            elif self.can_approve(role, s["id"]):
                who, mine = "**you**", mine + 1
            else:
                who = " or ".join(self.domains[s["domain"]])
            rows.append(f"| {i} | {name} | {metric} | {fix} | {p.source} | {who} |".replace("\n", " "))
        head = (
            f"{agent_name}: {len(self.proposals)} missing data metric(s), "
            f"one fix each; as {role['title']} you can approve **{mine}**. Nothing is invented: every fix takes "
            "its data from the SAP source of record, the steward registry or a lossless transform."
        )
        body = (f"\n\n{summary}" if summary else "") + "\n\n" + "\n".join(rows)
        text = head + body if self.proposals else f"{agent_name}: nothing to fix."
        return {"agent": agent, "proposals": [p.model_dump() for p in self.proposals], "text": text}

    def apply(self, role_key: str, task_id: str = DEFAULT_TASK) -> dict[str, Any]:
        task_id = self.resolve(role_key, task_id)
        role = self.role(role_key)
        before = self.score(self._systems(self._evaluate(role_key, task_id, quiet=True), task_id))
        if not self.proposals:
            self.propose(role_key, task_id)
        allowed, waiting = [], []
        for p in self.proposals:
            s = self.by_requirement.get(p.requirement)
            if p.action == "escalate" or s is None:
                waiting.append(f"{p.requirement}: needs a person ({p.rationale})")
            elif self.can_approve(role, s["id"]):
                allowed.append(p)
            else:
                waiting.append(
                    f"{s['name']}: {p.action} needs approval by {', '.join(self.domains[s['domain']])}"
                    + ("" if role["human"] else " (an agent can never approve)")
                )
        changes = self.remediator.apply(self.staging, allowed) if allowed else []
        self._after_fixes(allowed, role_key, changes)  # lineage, upstream rules, fixes-since-last-publish
        self.proposals = [p for p in self.proposals if p not in allowed]
        after = self.check(role_key, task_id)
        if self.state["rounds"]:
            self.state["rounds"][-1].setdefault("score_before", before)
            self.state["rounds"][-1].update(role=role_key, task=task_id, score_after=after["score"])
        self._save()
        done = [c for c in changes if c["action"] != "escalate" and c.get("status") != "skipped"]
        history = " · ".join(
            f"round {r['round']}: {r.get('score_before', '?')}% → {r.get('score_after', '?')}%"
            for r in self.state["rounds"]
        )
        answer = after["text"].split("\n\n")[1]
        text = (
            f"**Applied {len(done)} fix(es)** as {role['title']}; {len(waiting)} wait for another approver. "
            f"Re-check: **{after['decision']}** · data requirements met **{before}% → {after['score']}%**\n\n"
            f"{answer}\n\n{after['table']}\n\n**Loop**: {len(self.state['rules'])} fix(es) recorded upstream, "
            f"so the next round extracts without those faults. Run the flow again. ({history})"
        )
        return {
            "applied": [c["action"] for c in changes],
            "waiting": waiting,
            "score_before": before,
            "score_after": after["score"],
            "after": after,
            "rules": self.state["rules"],
            "next": self.next_steps(role_key, after),
            "text": text,
        }

    def next_steps(self, role_key: str, after: dict) -> list[dict]:
        """What happens next after Apply: who must act, and with which button."""
        role = self.role(role_key)
        if after["ready"]:
            pub = [f"{p['label']} v{p['version']}" for p in after.get("published", [])]
            steps = [{"kind": "ready", "text": "READY: the authorized data is retrieved and the task runs below."
                      + (f" Certified and published: {', '.join(pub)}, ingested into the RAG index." if pub else "")}]  # fmt: skip
            waiting = [p for p in self.proposals if p.action != "escalate" and p.requirement in self.by_requirement
                       and not self.can_approve(role, self.by_requirement[p.requirement]["id"])]  # fmt: skip
            if not waiting:  # explain why there is no Request approval button, and how to see it
                domains = {self.by_asset[a]["domain"] for a in self.harness[after["task"]]["required_assets"]}
                cands = sorted(r["title"] for r in self.roles.values() if r["human"] and r["key"] != role_key
                                and r["app"] == role["app"] and after["task"] in (r.get("tasks") or [])
                                and not domains <= set(r["approve"]))  # fmt: skip
                hint = f" (e.g. {cands[0]})" if cands else ""
                steps.append({"kind": "info", "text": f"No approval request was needed: as {role['title']} you may "
                              "approve every fix for this data yourself. To try Request approval, choose a role that "
                              f"may not approve it{hint} and run the same task and data set."})  # fmt: skip
            return steps
        steps = []
        others = [p for p in self.proposals if p.action != "escalate" and p.requirement in self.by_requirement
                  and not self.can_approve(role, self.by_requirement[p.requirement]["id"])]  # fmt: skip
        if others:
            approver_roles = self._approver_roles(role, {self.by_requirement[p.requirement]["domain"] for p in others})
            names = " or ".join(r["title"] for r in approver_roles)
            steps.append({"kind": "request", "approver_roles": approver_roles, "count": len(others),
                          "text": f"{len(others)} fix(es) need approval by {names}. Click Request approval: they "
                                  "see it under Approval requests for you when they open this page as their role."})  # fmt: skip
        people = [p for p in self.proposals if p.action == "escalate"]
        if people:
            steps.append({"kind": "escalate", "text": f"{len(people)} problem(s) cannot be fixed by changing data "
                          "(access, missing context, unavailable service): contact the data owner or access admin."})  # fmt: skip
        if not steps:
            steps.append({"kind": "propose", "text": "Still BLOCKED: click Propose again for the next round of fixes."})
        return steps

    # ------------------------------------------------------------------ catalog view and metrics
    def catalog_view(self, asset_id: str) -> dict[str, Any]:
        if asset_id not in self.catalog:
            raise KeyError(asset_id)
        adapter = self._adapter()
        asset = adapter.get_asset(asset_id)
        hist = self.state["history"].get(asset_id, [])
        return {
            "asset_id": asset_id,
            "catalog_record": adapter.get_record(asset_id),
            "computed": {
                "coverage": asset.coverage if asset else None,
                "last_successful_update": (adapter.get_record(asset_id) or {}).get("ingested_at"),
                "quality_check_history": hist,
            },
            "contract": {**self.contracts[asset_id], "evidence_runs": [h["evaluation_id"] for h in hist]},
        }

    def metrics(self) -> dict[str, Any]:
        ev = self.state["evaluations"]
        lat = sorted(e["latency_ms"] for e in ev if e.get("latency_ms") is not None)
        issues = Counter(
            c["check"]
            for e in ev
            for c in e["checks"]
            if c["status"] in ("FAIL", "ERROR") and c["check"] not in ("access", "other_checks")
        )
        return {
            "evaluations": len(ev),
            "decisions": dict(Counter(e["decision"] for e in ev)),
            "access_failures": sum(
                any(c["check"] == "access" and c["status"] in ("FAIL", "ERROR") for c in e["checks"]) for e in ev
            ),
            "data_issues_by_check": dict(issues),
            "latency_ms": {
                "mean": round(statistics.mean(lat), 3) if lat else None,
                "p95": lat[max(0, int(round(0.95 * len(lat))) - 1)] if lat else None,
            },
            "last_evaluation": ev[-1] if ev else None,
        }


def register(app: FastAPI, service: PipelineService, api_error) -> None:
    def guard(fn, *args):
        try:
            return fn(*args)
        except KeyError as exc:
            raise api_error(
                400, "unknown_role_task_or_fault", f"Unknown role, task, asset or fault: {exc.args[0]}"
            ) from exc

    tag = ["Data Harness pipeline (demo, called by Flowise)"]

    @app.get("/pipeline/roles", tags=tag)
    def roles() -> list[dict]:
        return [{"key": k, "app": r["app"], "title": r["title"], "human": r["human"]} for k, r in service.roles.items()]

    @app.get("/pipeline/profiles", tags=tag)
    def profiles() -> list[dict]:
        return list(service.harness.values())

    @app.get("/pipeline/faults", tags=tag)
    def faults() -> list[dict]:
        return [{"id": f["id"], "title": f["title"], "system": f["system"]} for f in service.faults]

    @app.get("/pipeline/role-view", tags=tag)
    def role_view(role: str, task: str = "my_role") -> dict:
        """1 user role -> 2 related data sources -> 3 the role's data readiness profile."""
        return guard(service.role_view, role, task)

    @app.get("/", include_in_schema=False)
    @app.get("/dashboard", include_in_schema=False)
    @app.get("/table", include_in_schema=False)
    def table_page() -> FileResponse:
        """The app UI: current data metric | missing checkpoints | recommended fix (+ agent fixes, user profiles)."""
        return FileResponse(Path(__file__).resolve().parent / "static" / "table.html", media_type="text/html")

    @app.get("/pipeline/datasets", tags=tag)
    def datasets(task: str | None = None, role: str | None = None) -> list[dict]:
        """Data set choices: neutral SAP extracts plus saved fixed data sets. A data set says only whether the
        readiness engine has tested it (and the result for this role + task); nothing is revealed before a test."""
        return service.dataset_list(task, role)

    @app.post("/pipeline/extract", tags=tag)
    def extract(body: ExtractBody) -> dict:
        if body.dataset:  # neutral pick: do not echo which faults it contains
            out = guard(service.extract_dataset, body.dataset, body.task)
            return {k: v for k, v in out.items() if k not in ("faults", "prevented")}
        return guard(service.extract, body.faults)

    @app.post("/pipeline/check", tags=tag)
    def check(body: RoleBody) -> dict:
        return guard(service.check, body.role, body.task)

    @app.post("/pipeline/propose", tags=tag)
    def propose(body: RoleBody) -> dict:
        return guard(service.propose, body.role, body.task, body.agent)

    @app.get("/pipeline/agents", tags=tag)
    def agents() -> list[dict]:
        from app.claude_agent import claude_status

        st = claude_status()
        return [{"id": "mock", "name": "Mock agent (deterministic, no LLM)", "available": True},
                {"id": "claude", "name": f"Claude agent ({st['model']})", "available": st["configured"],
                 "how_to_enable": st["how_to_enable"]}]  # fmt: skip

    @app.post("/pipeline/apply", tags=tag)
    def apply(body: RoleBody) -> dict:
        return guard(service.apply, body.role, body.task)

    @app.post("/pipeline/retrieve", tags=tag)
    def retrieve(body: RoleBody) -> dict:
        """Stage 6 when READY: re-check, retrieve only authorized data, run the specialist."""
        return guard(service.retrieve, body.role, body.task)

    @app.post("/pipeline/reset", tags=tag)
    def reset() -> dict:
        return service.reset()

    @app.get("/pipeline/catalog/{asset_id}", tags=tag)
    def catalog(asset_id: str) -> dict:
        return guard(service.catalog_view, asset_id)

    @app.post("/pipeline/search", tags=tag)
    def search(body: SearchBody) -> dict:
        """Permission-aware document search for a READY policy task (role + task + free-text query)."""
        return guard(service.search, body.role, body.task, body.query)

    @app.post("/pipeline/search-data", tags=tag)
    def search_data(body: SearchBody) -> dict:
        """RAG over the certified (published) data sets of a READY task, with the role's access rules."""
        return guard(service.search_data, body.role, body.task, body.query)

    @app.get("/pipeline/published", tags=tag)
    def published() -> list[dict]:
        """Current certified version of every published data set (manifest: evaluation, tasks, contract, fixes)."""
        return service.publisher.list()

    @app.post("/pipeline/request-approval", tags=tag)
    def request_approval(body: RoleBody) -> dict:
        """Queue the fixes this role may not approve for the named approvers."""
        return guard(service.request_approval, body.role, body.task)

    @app.get("/pipeline/approvals", tags=tag)
    def approvals(role: str) -> dict:
        """Fixes to approve: proposed fixes not applied yet (who approves each) + pending approval requests."""
        return guard(service.approvals_overview, role)

    @app.get("/pipeline/requests/pending", tags=tag)
    def pending_requests() -> list[dict]:
        """Every pending approval request, with the roles that may approve it (header list "Fixes to approve")."""
        return [r for r in service.state.get("requests", []) if r["status"] == "pending"]

    @app.get("/pipeline/requests", tags=tag)
    def requests(role: str) -> list[dict]:
        """Pending approval requests this role may decide (its inbox)."""
        return guard(service.inbox, role)

    @app.post("/pipeline/requests/{request_id}/decide", tags=tag)
    def decide(request_id: int, body: DecideBody) -> dict:
        return guard(service.decide, body.role, request_id, body.approve)

    @app.get("/pipeline/lineage", tags=tag)
    def lineage(asset: str | None = None, limit: int = 20) -> list[dict]:
        """OpenLineage COMPLETE events, newest first (optionally only runs touching one asset)."""
        return [service.lineage.summary(e) for e in service.lineage.events(asset, min(max(limit, 1), 200))]

    @app.get("/pipeline/odcs/{asset_id}", tags=tag)
    def odcs(asset_id: str) -> Response:
        """The asset's ODCS v3.1.0 file (catalog record + contract): the single source of truth."""
        if service.odcs is None or asset_id not in service.odcs.files:
            raise api_error(404, "unknown_asset", f"No ODCS file for {asset_id}")
        return Response(service.odcs.raw(asset_id), media_type="application/yaml")

    @app.get("/pipeline/metrics", tags=tag)
    def metrics() -> dict:
        return service.metrics()
