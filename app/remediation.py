"""Remediation: turn a BLOCKED readiness result into proposed fixes, apply approved fixes to a WORKING COPY, re-check.

Golden rule: a fix never invents a value. Every allowed action gets its data from one of three places:

  source of record   datasets/source_of_record (the clean, current extract)   refresh, re-extract, restore, replace
  steward registry   datasets/steward_registry.yaml (governed owner list)     lookup_owner
  lossless transform the value itself, with a declared rule                   deduplicate (exact copies), normalise
                                                                               codes (case/spaces), reformat dates
                                                                               (only with the export's declared format)

Anything that cannot be fixed by changing data (no access, missing task context, unreachable services) becomes an
`escalate` proposal for a human. A row that cannot be repaired from the source is moved to quarantine/, not guessed.

The engine re-checks the working copy after every apply. Proposals (from the rules planner OR from Claude) are only
suggestions: they are validated against the allow-list here before anything runs, and only the engine decides READY.
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.adapters.dataset import read_csv, write_csv
from app.catalog import load_yaml
from app.checks.quality import is_iso_date, row_key, violations
from app.models import CheckStatus, ReadinessResult
from app.profiles import AssetRequirement, QualityRule, TaskProfile

Action = Literal[
    "refresh_from_source",
    "restore_columns",
    "reextract_rows",
    "deduplicate",
    "normalise_codes",
    "reformat_dates",
    "replace_bad_rows_from_source",
    "lookup_owner",
    "escalate",
]

ACTIONS: dict[str, dict[str, str]] = {
    "refresh_from_source": {
        "source": "source of record",
        "description": "Replace the asset with the current source-of-record extract (data, "
        "version, source event time). Fixes stale or missing assets.",
    },
    "restore_columns": {
        "source": "source of record",
        "description": "Add missing columns (values) by joining each row to the source on its key. "
        "values = column names.",
    },
    "reextract_rows": {
        "source": "source of record",
        "description": "Append the missing partition from the source. column = product_id | "
        "warehouse_id | lane, values = the missing codes (lane: 'WH-A>WH-C').",
    },
    "deduplicate": {
        "source": "lossless transform + source of record",
        "description": "Drop exact duplicate rows; for duplicates that disagree, take the source row "
        "(or quarantine if the source has none).",
    },
    "normalise_codes": {
        "source": "lossless transform",
        "description": "Trim and lower-case codes in `column` when the result is an allowed code "
        "(e.g. 'Released' -> 'released'). Unknown codes are left for replacement.",
    },
    "reformat_dates": {
        "source": "lossless transform (declared export format)",
        "description": "Convert dates in `column` to YYYY-MM-DD using the date format the export "
        "declares in its manifest. Skipped if no format is declared.",
    },
    "replace_bad_rows_from_source": {
        "source": "source of record",
        "description": "Replace rows that break a quality rule (empty, negative, "
        "unknown code, bad date) with the source row of the same key; "
        "rows the source does not have are quarantined.",
    },
    "lookup_owner": {
        "source": "steward registry",
        "description": "Fill business_owner / technical_owner (values) from the governed steward "
        "registry. Never invented: if the registry has no entry, nothing changes.",
    },
    "escalate": {
        "source": "human",
        "description": "Cannot be fixed by changing data (access, missing task context, unavailable "
        "service). Nothing is applied; a person must act.",
    },
}
ORDER = [
    "refresh_from_source",
    "restore_columns",
    "reextract_rows",
    "deduplicate",
    "normalise_codes",
    "reformat_dates",
    "replace_bad_rows_from_source",
    "lookup_owner",
]
OWNER_FIELDS = ("business_owner", "technical_owner", "data_steward", "contact")  # fillable from the steward registry
CODE = re.compile(r"^[A-Za-z0-9_>-]{1,40}$")


class FixProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirement: str = Field(max_length=64)
    action: Action
    column: str = Field(default="", max_length=64)
    values: list[str] = Field(default_factory=list, max_length=20)
    rationale: str = Field(default="", max_length=1000)
    check: str = Field(default="", max_length=32)
    proposed_by: Literal["rules", "claude"] = "rules"
    source: str = ""  # filled from ACTIONS: where the corrected data comes from
    auto_applicable: bool = True


def _key(p: FixProposal) -> tuple:
    return p.requirement, p.action, p.column, tuple(p.values)


def validate_proposal(raw: dict | FixProposal, profile: TaskProfile) -> tuple[FixProposal | None, str]:
    """Allow-list check for proposals from ANY proposer (rules, Claude, a browser). Returns (proposal, error)."""
    try:
        p = raw if isinstance(raw, FixProposal) else FixProposal.model_validate(raw)
    except ValueError as exc:
        return None, f"invalid proposal: {str(exc).splitlines()[0]}"
    reqs = {a.name: a for a in profile.assets}
    if p.action == "escalate":
        return p.model_copy(update={"source": ACTIONS["escalate"]["source"], "auto_applicable": False}), ""
    req = reqs.get(p.requirement)
    if req is None:
        return None, f"unknown requirement {p.requirement!r} (known: {', '.join(reqs)})"
    if any(not CODE.match(v) for v in p.values) or (p.column and not CODE.match(p.column)):
        return None, "values and column must be plain codes"
    q = req.quality or QualityRule()
    if p.action == "normalise_codes" and p.column not in q.allowed_values:
        return None, f"normalise_codes needs a column with allowed codes ({', '.join(q.allowed_values) or 'none'})"
    if p.action == "reformat_dates" and p.column not in q.iso_dates:
        return None, f"reformat_dates needs a date column ({', '.join(q.iso_dates) or 'none'})"
    if p.action == "reextract_rows" and (p.column not in ("product_id", "warehouse_id", "lane") or not p.values):
        return None, "reextract_rows needs column product_id | warehouse_id | lane and the missing values"
    if p.action == "restore_columns" and not p.values:
        return None, "restore_columns needs the column names in values"
    if p.action == "lookup_owner" and (not p.values or set(p.values) - set(OWNER_FIELDS)):
        return None, "lookup_owner values must be business_owner and/or technical_owner"
    return p.model_copy(update={"source": ACTIONS[p.action]["source"], "auto_applicable": True}), ""


# ---------------------------------------------------------------------- deterministic planner (no LLM)


def plan_fixes(result: ReadinessResult, profile: TaskProfile) -> list[FixProposal]:
    """Map each failing or warning check to allowed actions. Same input -> same plan."""
    out: dict[tuple, FixProposal] = {}

    def add(requirement: str, check: str, action: str, rationale: str, column: str = "", values=()) -> None:
        p, err = validate_proposal(
            {
                "requirement": requirement,
                "action": action,
                "column": column,
                "values": list(values),
                "rationale": rationale,
                "check": check,
            },
            profile,
        )
        if p is not None:
            out.setdefault(_key(p), p)

    for c in result.checks:
        if c.status not in (CheckStatus.FAIL, CheckStatus.ERROR, CheckStatus.WARN):
            continue
        r, ev = c.requirement, c.evidence
        if c.check in ("context", "profile"):
            add(r, c.check, "escalate", f"Ask the user: {c.reason}")
        elif c.check == "access" or c.check == "evidence" or (c.check == "catalog" and c.status is CheckStatus.ERROR):
            add(r, c.check, "escalate", f"Not a data problem; request access or restore the service. ({c.reason})")
        elif c.check == "catalog":
            add(r, c.check, "refresh_from_source", "The asset is missing; re-extract it from the source of record.")
        elif c.check == "freshness":
            add(r, c.check, "refresh_from_source", f"{c.reason} Load the current source extract.")
        elif c.check == "schema":
            add(
                r,
                c.check,
                "restore_columns",
                f"{c.reason} Join the columns back from the source by key.",
                values=ev.get("missing", []),
            )
        elif c.check == "coverage":
            dim, missing = ev.get("dimension"), ev.get("missing", [])
            column = {"product": "product_id", "warehouse": "warehouse_id", "lane": "lane"}.get(dim or "")
            if column and missing:
                add(
                    r,
                    c.check,
                    "reextract_rows",
                    f"{c.reason} Re-extract that partition from the source.",
                    column=column,
                    values=missing,
                )
            else:
                add(r, c.check, "refresh_from_source", f"{c.reason} Load the current source extract.")
        elif c.check == "quality":
            for name, v in (ev.get("violations") or {}).items():
                if name == "unique_key":
                    add(r, c.check, "deduplicate", f"{v['count']} duplicate row(s), e.g. {v['keys'][:2]}.")
                    continue
                for col in v.get("columns", []):
                    if name == "allowed_values":
                        add(
                            r,
                            c.check,
                            "normalise_codes",
                            f"Non-standard codes in {col}; normalise case/spaces.",
                            column=col,
                        )
                    if name == "iso_dates":
                        add(
                            r,
                            c.check,
                            "reformat_dates",
                            f"Dates in {col} are not YYYY-MM-DD; use the declared export format.",
                            column=col,
                        )
                add(
                    r,
                    c.check,
                    "replace_bad_rows_from_source",
                    f"{v['count']} row(s) break '{name}', e.g. {v['keys'][:2]}; take those rows from the source.",
                )
        elif c.check == "provenance":
            missing = ev.get("missing", [])
            owners = [f for f in missing if f in OWNER_FIELDS]
            if owners:
                add(
                    r,
                    c.check,
                    "lookup_owner",
                    f"Owner(s) not recorded: {', '.join(owners)}; look them up in the steward registry.",
                    values=owners,
                )
            if set(missing) - set(OWNER_FIELDS):
                add(r, c.check, "refresh_from_source", "Version or source time not recorded; reload from source.")
        elif c.check == "metadata":
            fields = ev.get("missing_catalog_fields", [])
            fillable = [f for f in fields if f in OWNER_FIELDS]
            if fillable:
                add(
                    r,
                    c.check,
                    "lookup_owner",
                    f"Catalog fields missing: {', '.join(fillable)}; take them from the governed steward registry.",
                    values=fillable,
                )
            rest = [f for f in fields if f not in OWNER_FIELDS] + ev.get("missing_contract_sections", [])
            if rest:
                add(r, c.check, "escalate", f"The data owner must complete: {', '.join(rest)}.")
        elif c.check == "documents":
            add(
                r,
                c.check,
                "refresh_from_source",
                f"{c.reason.split('. ')[0]}. Re-sync the current approved documents from the source.",
            )
    return list(out.values())


# ---------------------------------------------------------------------- applying fixes to a working copy


class Remediator:
    def __init__(self, profile: TaskProfile, source_dir: Path, registry_path: Path, clock):
        self.profile, self.source_dir, self.registry_path, self.clock = profile, Path(source_dir), registry_path, clock
        self.source_manifest = load_yaml(self.source_dir / "manifest.yaml")
        self.registry = load_yaml(registry_path)

    # -- helpers
    def _req(self, name: str) -> AssetRequirement:
        return next(a for a in self.profile.assets if a.name == name)

    def _source(self, asset_id: str) -> tuple[dict, list[str], list[dict]]:
        meta = self.source_manifest["assets"][asset_id]
        header, rows = read_csv(self.source_dir / meta["file"])
        return meta, header, rows

    def apply(self, work_dir: Path, proposals: list[FixProposal]) -> list[dict]:
        work_dir = Path(work_dir)
        manifest = load_yaml(work_dir / "manifest.yaml")
        changes: list[dict] = []
        for action in ORDER:
            for p in [p for p in proposals if p.action == action]:
                req = self._req(p.requirement)
                change = getattr(self, f"_{action}")(work_dir, manifest, req, p)
                change.update(
                    action=action,
                    requirement=p.requirement,
                    proposed_by=p.proposed_by,
                    source=ACTIONS[action]["source"],
                )
                changes.append(change)
                if change.get("rows_changed") and action not in ("refresh_from_source", "lookup_owner"):
                    meta = manifest["assets"][req.asset_id]
                    version = str(meta.get("source_version") or "unversioned")
                    if "+remediated" not in version:
                        meta["source_version"] = version + "+remediated"
        for p in proposals:
            if p.action == "escalate":
                changes.append(
                    {
                        "action": "escalate",
                        "requirement": p.requirement,
                        "proposed_by": p.proposed_by,
                        "source": "human",
                        "rows_changed": 0,
                        "status": "needs_human",
                        "detail": p.rationale or "Needs a person; nothing was changed.",
                    }
                )
        log = manifest.setdefault("remediation_log", [])
        stamp = self.clock().isoformat()
        log += [
            {"at": stamp, **{k: c[k] for k in ("action", "requirement", "detail", "rows_changed", "source")}}
            for c in changes
        ]
        (work_dir / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True))
        return changes

    def _load(self, work_dir: Path, manifest: dict, asset_id: str) -> tuple[dict, Path, list[str], list[dict]]:
        meta = manifest["assets"].setdefault(asset_id, {})
        if not meta.get("file"):
            meta["file"] = self.source_manifest["assets"][asset_id]["file"]
        path = work_dir / meta["file"]
        header, rows = read_csv(path) if path.exists() else ([], [])
        return meta, path, header, rows

    def _quarantine(self, work_dir: Path, file: str, header: list[str], rows: list[dict], reason: str) -> None:
        if not rows:
            return
        qdir = work_dir / "quarantine"
        qdir.mkdir(exist_ok=True)
        path = qdir / file
        existing = read_csv(path)[1] if path.exists() else []
        write_csv(path, header + ["quarantine_reason"], existing + [{**r, "quarantine_reason": reason} for r in rows])

    @staticmethod
    def _rule(req: AssetRequirement) -> QualityRule:
        return req.quality or QualityRule()

    def _index(self, req: AssetRequirement, rows: list[dict]) -> dict[str, dict]:
        rule = self._rule(req)
        return {row_key(rule, r, i): r for i, r in enumerate(rows)}

    # -- actions (each returns {"detail": ..., "rows_changed": n, "status": ...})
    def _refresh_from_source(self, work_dir, manifest, req, p):
        meta, path, _, old = self._load(work_dir, manifest, req.asset_id)
        src_meta, header, rows = self._source(req.asset_id)
        write_csv(path, header, rows)
        meta.update(
            source_version=src_meta["source_version"],
            source_event_time=src_meta["source_event_time"],
            ingested_at=self.clock().isoformat(),
        )
        meta.pop("export_settings", None)
        return {
            "detail": f"Replaced {len(old)} row(s) with {len(rows)} row(s) from source {src_meta['source_version']}"
            f" (as of {src_meta['source_event_time']}).",
            "rows_changed": len(rows),
            "status": "applied",
        }

    def _restore_columns(self, work_dir, manifest, req, p):
        meta, path, header, rows = self._load(work_dir, manifest, req.asset_id)
        _, src_header, src_rows = self._source(req.asset_id)
        cols = [c for c in p.values if c not in header and c in src_header]
        if not cols:
            return {
                "detail": "Nothing to restore (columns present or unknown to the source).",
                "rows_changed": 0,
                "status": "skipped",
            }
        index, keep, lost = self._index(req, src_rows), [], []
        rule = self._rule(req)
        for i, r in enumerate(rows):
            src = index.get(row_key(rule, r, i))
            if src is None:
                lost.append(r)
            else:
                keep.append({**r, **{c: src[c] for c in cols}})
        new_header = [c for c in src_header if c in header or c in cols] + [c for c in header if c not in src_header]
        write_csv(path, new_header, keep)
        self._quarantine(work_dir, meta["file"], header, lost, "no source row to restore columns from")
        return {
            "detail": f"Restored {', '.join(cols)} for {len(keep)} row(s) from the source; "
            f"{len(lost)} row(s) quarantined.",
            "rows_changed": len(keep),
            "status": "applied",
        }

    def _reextract_rows(self, work_dir, manifest, req, p):
        meta, path, header, rows = self._load(work_dir, manifest, req.asset_id)
        _, src_header, src_rows = self._source(req.asset_id)
        header = header or src_header
        rule = self._rule(req)
        have = {row_key(rule, r, i) for i, r in enumerate(rows)}

        def match(r):
            if p.column == "lane":
                return f"{r.get('origin_warehouse', '')}>{r.get('destination_warehouse', '')}" in p.values
            return r.get(p.column, "") in p.values

        added = [r for i, r in enumerate(src_rows) if match(r) and row_key(rule, r, i) not in have]
        write_csv(path, header, rows + added)
        return {
            "detail": f"Re-extracted {len(added)} row(s) for {p.column} {', '.join(p.values)} from the source.",
            "rows_changed": len(added),
            "status": "applied" if added else "skipped",
        }

    def _deduplicate(self, work_dir, manifest, req, p):
        meta, path, header, rows = self._load(work_dir, manifest, req.asset_id)
        rule = self._rule(req)
        _, _, src_rows = self._source(req.asset_id)
        src = self._index(req, src_rows)
        groups: dict[str, list[dict]] = {}
        for i, r in enumerate(rows):
            groups.setdefault(row_key(rule, r, i), []).append(r)
        out, dropped, replaced, lost = [], 0, 0, []
        for key, group in groups.items():
            if len(group) == 1:
                out.append(group[0])
            elif all(g == group[0] for g in group):
                out.append(group[0])
                dropped += len(group) - 1
            elif key in src:
                out.append({c: src[key].get(c, "") for c in header})
                replaced += 1
            else:
                lost += group
        write_csv(path, header, out)
        self._quarantine(work_dir, meta["file"], header, lost, "conflicting duplicates, no source row")
        return {
            "detail": f"Dropped {dropped} exact duplicate(s); resolved {replaced} conflicting duplicate key(s) "
            f"from the source; quarantined {len(lost)} row(s).",
            "rows_changed": dropped + replaced + len(lost),
            "status": "applied",
        }

    def _normalise_codes(self, work_dir, manifest, req, p):
        meta, path, header, rows = self._load(work_dir, manifest, req.asset_id)
        allowed = self._rule(req).allowed_values.get(p.column, [])
        n = 0
        for r in rows:
            value = r.get(p.column) or ""
            fixed = value.strip().lower()
            if value not in allowed and fixed in allowed:
                r[p.column] = fixed
                n += 1
        write_csv(path, header, rows)
        return {
            "detail": f"Normalised {n} code(s) in {p.column} (trim + lower-case to an allowed code).",
            "rows_changed": n,
            "status": "applied" if n else "skipped",
        }

    def _reformat_dates(self, work_dir, manifest, req, p):
        meta, path, header, rows = self._load(work_dir, manifest, req.asset_id)
        fmt = (meta.get("export_settings") or {}).get("date_format")
        if not fmt:
            return {
                "detail": "Skipped: the export declares no date format, so dates cannot be converted safely.",
                "rows_changed": 0,
                "status": "skipped",
            }
        n = 0
        for r in rows:
            value = (r.get(p.column) or "").strip()
            if value and not is_iso_date(value):
                try:
                    r[p.column] = datetime.strptime(value, fmt).date().isoformat()
                    n += 1
                except ValueError:
                    continue  # left for replace_bad_rows_from_source
        write_csv(path, header, rows)
        return {
            "detail": f"Converted {n} date(s) in {p.column} from the declared export format {fmt} to YYYY-MM-DD.",
            "rows_changed": n,
            "status": "applied" if n else "skipped",
        }

    def _replace_bad_rows_from_source(self, work_dir, manifest, req, p):
        meta, path, header, rows = self._load(work_dir, manifest, req.asset_id)
        rule = self._rule(req).model_copy(update={"unique_key": self._rule(req).unique_key, "scope": {}})
        _, _, src_rows = self._source(req.asset_id)
        src = self._index(req, src_rows)
        out, replaced, lost = [], 0, []
        for i, r in enumerate(rows):
            single = violations(rule.model_copy(update={"unique_key": []}), [r])
            if not single:
                out.append(r)
                continue
            key = row_key(rule, r, i)
            if key in src:
                out.append({c: src[key].get(c, "") for c in header})
                replaced += 1
            else:
                lost.append(r)
        write_csv(path, header, out)
        self._quarantine(work_dir, meta["file"], header, lost, "breaks a quality rule, no source row")
        return {
            "detail": f"Replaced {replaced} bad row(s) with the source rows of the same key; quarantined "
            f"{len(lost)} row(s).",
            "rows_changed": replaced + len(lost),
            "status": "applied" if replaced or lost else "skipped",
        }

    def _lookup_owner(self, work_dir, manifest, req, p):
        meta = manifest["assets"].setdefault(req.asset_id, {})
        entry = (self.registry.get("assets") or {}).get(req.asset_id) or {}
        filled = []
        for f in p.values:
            if not meta.get(f) and entry.get(f):
                meta[f] = entry[f]
                filled.append(f)
        if filled:
            meta.setdefault("provenance_notes", []).append(
                f"{', '.join(filled)} from steward registry {self.registry.get('registry_version')}"
            )
        missing = [f for f in p.values if not meta.get(f)]
        detail = (f"Filled {', '.join(filled)} from the steward registry." if filled else "Nothing filled.") + (
            f" Not in the registry: {', '.join(missing)} (needs a person)." if missing else ""
        )
        return {
            "detail": detail,
            "rows_changed": 0,
            "fields_changed": filled,
            "status": "applied" if filled else "needs_human",
        }


def make_working_copy(original: Path, work: Path) -> None:
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(original, work, ignore=shutil.ignore_patterns("fault.yaml"))
