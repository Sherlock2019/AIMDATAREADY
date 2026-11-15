"""Integration with the SCInsight Supervisor Agentflow export (~/Supplychain-AI-agent).

What is proven here:
  * generating the patched copy never modifies the original export;
  * in the copy, the Warehouse specialist is reachable ONLY through the gate's "ready" output (graph test), so a
    blocked request cannot invoke it;
  * the gate's JavaScript, run outside Flowise with Node, stops on BLOCKED, on missing identity and when the
    readiness service is down, and continues on READY (against a real uvicorn process of this service).
What is NOT proven: that Flowise imports or executes the copy (it was not imported into Flowise).
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

import httpx
import pytest

from app.settings import ROOT
from integrations.supplychain.build_readiness_supervisor import (
    DEFAULT_SOURCE,
    GATE_COND,
    GATE_FN,
    GATE_STOP,
    build,
    sha256,
)

pytestmark = pytest.mark.skipif(not DEFAULT_SOURCE.exists(), reason="SCInsight Supervisor export not found")
HARNESS = ROOT / "integrations" / "supplychain" / "run_node_check.cjs"


@pytest.fixture(scope="module")
def patched(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("flow") / "supervisor.readiness.json"
    before = sha256(DEFAULT_SOURCE)
    build(DEFAULT_SOURCE, out)
    assert sha256(DEFAULT_SOURCE) == before
    return out


def reachable(flow: dict, start: str, skip_handle: str | None = None) -> set[str]:
    nxt: dict[str, list[str]] = {}
    for e in flow["edges"]:
        if e["sourceHandle"] != skip_handle:
            nxt.setdefault(e["source"], []).append(e["target"])
    seen, todo = {start}, deque([start])
    while todo:
        for t in nxt.get(todo.popleft(), []):
            if t not in seen:
                seen.add(t)
                todo.append(t)
    return seen


def test_original_export_is_untouched_and_copy_differs(patched):
    assert patched.read_bytes() != DEFAULT_SOURCE.read_bytes()


def test_warehouse_specialist_only_reachable_through_ready_output(patched):
    flow = json.loads(patched.read_text())
    original = json.loads(DEFAULT_SOURCE.read_text())
    warehouse = next(
        n["id"]
        for n in flow["nodes"]
        if "Warehouse-Agentflow" in str(n["data"]["inputs"].get("executeFlowSelectedFlow"))
    )
    into = [e for e in flow["edges"] if e["target"] == warehouse]
    assert [(e["source"], e["sourceHandle"]) for e in into] == [(GATE_COND, f"{GATE_COND}-output-0")]
    assert any(e["source"] == GATE_COND and e["target"] == GATE_STOP for e in flow["edges"])
    assert warehouse in reachable(flow, "startAgentflow_0")
    assert warehouse not in reachable(flow, "startAgentflow_0", skip_handle=f"{GATE_COND}-output-0")
    # every other edge of the original flow is still there
    kept = {(e["source"], e["sourceHandle"], e["target"]) for e in flow["edges"]}
    removed = [e for e in original["edges"] if (e["source"], e["sourceHandle"], e["target"]) not in kept]
    assert [e["target"] for e in removed] == [warehouse]
    start = next(n for n in flow["nodes"] if n["id"] == "startAgentflow_0")
    assert {"tdr_ready", "tdr_message", "tdr_evaluation_id"} <= {
        s["key"] for s in start["data"]["inputs"]["startState"]
    }
    fn = next(n for n in flow["nodes"] if n["id"] == GATE_FN)
    assert "fail closed" in fn["data"]["inputs"]["customFunctionJavascriptFunction"].lower()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live_api():
    port = _free_port()
    env = {**os.environ, "TDR_MODE": "demo", "TDR_AUDIT_PATH": ""}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if httpx.get(url + "/health", timeout=0.5).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        else:
            pytest.fail("readiness API did not start")
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def gate(patched: Path, url: str, user: str, question: str, as_of: str = "") -> dict:
    out = subprocess.run(
        ["node", str(HARNESS), str(patched), url, user, question, as_of],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(out.stdout)


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
QUESTION = "Should we move SUN-SPF50-100 from warehouse A to warehouse C for 2026-11-16/2026-11-29?"


@needs_node
def test_gate_continues_on_ready(patched, live_api):
    r = gate(patched, live_api, "planner_all", QUESTION)
    assert r["tdr_ready"] == "yes" and r["tdr_evaluation_id"].startswith("tdr-")


@needs_node
def test_gate_stops_when_blocked(patched, live_api):
    r = gate(patched, live_api, "planner_ab", QUESTION)
    assert r["tdr_ready"] == "no" and "WH-C" in r["tdr_message"]


@needs_node
def test_gate_stops_without_identity_or_context(patched, live_api):
    assert gate(patched, live_api, "-", QUESTION)["tdr_ready"] == "no"
    r = gate(patched, live_api, "planner_all", "Should we transfer SUN-SPF50-100 to Warehouse B?")
    assert r["tdr_ready"] == "no" and "origin_warehouse" in r["tdr_message"]


@needs_node
def test_gate_default_period_from_snapshot_date(patched, live_api):
    question = "Should we move SUN-SPF50-100 from warehouse A to warehouse C?"
    assert gate(patched, live_api, "planner_all", question, as_of="2026-11-16")["tdr_ready"] == "yes"


@needs_node
def test_gate_fails_closed_when_service_is_down(patched):
    r = gate(patched, f"http://127.0.0.1:{_free_port()}", "planner_all", QUESTION)
    assert r["tdr_ready"] == "no" and "fail closed" in r["tdr_message"]
