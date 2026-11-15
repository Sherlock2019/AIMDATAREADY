"""Runs the labelled scenarios in examples/sample_requests.json against any HTTP client with .post(path, json=...).

Used by scripts/run_demo.py (in-process or against a running server) and by tests/test_api.py, so the README
examples, the evaluation numbers and the automated tests all come from the same file.
"""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from typing import Any

from app.settings import ROOT

SCENARIO_FILE = ROOT / "examples" / "sample_requests.json"


def load_scenarios(path: Path = SCENARIO_FILE) -> list[dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    defaults = data.get("defaults", {})

    def expand(value: Any) -> Any:
        if isinstance(value, str) and value.startswith("$") and value[1:] in defaults:
            return copy.deepcopy(defaults[value[1:]])
        if isinstance(value, dict):
            return {k: expand(v) for k, v in value.items()}
        return value

    return [{**s, "body": expand(s["body"])} for s in data["scenarios"]]


def compare(expected: dict[str, Any], status_code: int, body: dict[str, Any]) -> list[str]:
    """Return a list of mismatches (empty = scenario passed)."""
    problems = []
    want_status = expected.get("status_code", 200)
    if status_code != want_status:
        problems.append(f"status_code {status_code} != {want_status}")
    for key in ("decision", "task_id", "status", "retriever_called", "answerable"):
        if key in expected and body.get(key) != expected[key]:
            problems.append(f"{key} {body.get(key)!r} != {expected[key]!r}")
    if "error_code" in expected and (body.get("error") or {}).get("code") != expected["error_code"]:
        problems.append(f"error code {(body.get('error') or {}).get('code')!r} != {expected['error_code']!r}")
    hits = [h["doc_id"] for h in ((body.get("retrieval") or {}).get("hits") or [])]
    for doc in expected.get("hits_include", []):
        if doc not in hits:
            problems.append(f"expected hit {doc} missing (got {hits})")
    for doc in expected.get("hits_exclude", []):
        if doc in hits:
            problems.append(f"document {doc} must not be returned")
    return problems


def run_scenario(client: Any, scenario: dict[str, Any]) -> dict[str, Any]:
    start = time.perf_counter()
    response = client.post(scenario["endpoint"], json=scenario["body"])
    elapsed_ms = (time.perf_counter() - start) * 1000
    body = response.json()
    actual = body.get("decision") or body.get("status")
    return {
        "name": scenario["name"],
        "endpoint": scenario["endpoint"],
        "expected": scenario["expected"],
        "actual": actual,
        "status_code": response.status_code,
        "latency_ms": round(elapsed_ms, 2),
        "problems": compare(scenario["expected"], response.status_code, body),
        "body": body,
    }
