"""Build the Flowise Agentflow "Data-Readiness-Agentflow-v1" (Flowise 3.1.2 format) from node_templates.json.

    Start (form: role, faults)
      -> 1 Extract from SAP (simulated)  -> 2 Detect (readiness engine) -> Data ready?
           yes -> Reply: READY
           no  -> 3 Propose fixes (mock agent) -> Human approval -> proceed -> 4 Fix + re-check + loop -> Reply
                                                                -> reject  -> Reply: not applied
Every step calls the taskDataReadiness API in code (Custom Function nodes); no LLM, no prompt decides anything.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
NAME = "Data-Readiness-Agentflow-v1"
API = "http://127.0.0.1:8460"  # Flowise runs on the host network, so it reaches the local API directly

CALL = """const fetch = require('node-fetch')
const BASE = '__API__'
const call = async (path, body) => {
    try {
        const res = await fetch(BASE + path, { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body), timeout: 30000 })
        const data = await res.json()
        if (!res.ok) return { ok: false, text: 'Error: ' + ((data.error && data.error.message) || res.status) }
        return { ok: true, ...data }
    } catch (e) {
        return { ok: false, text: 'API unreachable at ' + BASE + ' (' + e.message + '). Run ./start.sh --bg' }
    }
}
"""
STEPS = {  # node key -> (label, input variables, body of the function, state keys it writes)
    "extract": (
        "1 · Extract from SAP (simulated)",
        ["faults"],
        "const r = await call('/pipeline/extract', { faults: String($faults || 'all') })\n"
        "return JSON.stringify({ extract: r.text })",
        ["extract"],
    ),
    "detect": (
        "2 · Data Harness check (detect)",
        ["role", "task"],
        "const r = await call('/pipeline/check', { role: String($role), task: String($task) })\n"
        "return JSON.stringify({ detect: r.text, table: r.table || '', ready: r.ok && r.ready ? 'yes' : 'no' })",
        ["detect", "table", "ready"],
    ),
    "propose": (
        "3 · Propose fixes (mock agent)",
        ["role", "task"],
        "const r = await call('/pipeline/propose', { role: String($role), task: String($task) })\n"
        "return JSON.stringify({ proposals: r.text })",
        ["proposals"],
    ),
    "apply": (
        "4 · Fix, re-check, loop",
        ["role", "task"],
        "const r = await call('/pipeline/apply', { role: String($role), task: String($task) })\n"
        "return JSON.stringify({ result: r.text })",
        ["result"],
    ),
}


def build() -> dict:
    tpl = json.loads((HERE / "node_templates.json").read_text())["nodes"]
    roles = yaml.safe_load((ROOT / "pipeline" / "roles.yaml").read_text())
    faults = yaml.safe_load((ROOT / "pipeline" / "pipeline.yaml").read_text())["faults"]
    role_keys = [f"{a['id']}:{r['id']}" for a in roles["apps"] for r in a["roles"]]
    tasks = [p["id"] for p in yaml.safe_load((ROOT / "pipeline" / "tasks.yaml").read_text())["profiles"]]
    nodes, edges, count = [], [], {}

    def node(kind: str, label: str, x: int, y: int, inputs: dict) -> dict:
        n = copy.deepcopy(tpl[kind])
        count[kind] = count.get(kind, -1) + 1
        nid, old = f"{kind}_{count[kind]}", n["id"]
        n["id"] = n["data"]["id"] = nid
        n["data"]["label"] = label
        n["position"] = n["positionAbsolute"] = {"x": x, "y": y}
        n["data"]["inputs"].update(inputs)
        for a in n["data"].get("outputAnchors", []):
            a["id"] = a["id"].replace(old, nid, 1)
        n["selected"] = n["data"]["selected"] = False
        nodes.append(n)
        return n

    def edge(src: dict, handle: str, dst: dict, label: str | None = None) -> None:
        data = {
            "sourceColor": src["data"]["color"],
            "targetColor": dst["data"]["color"],
            "isHumanInput": src["data"]["name"] == "humanInputAgentflow",
        }
        if label:
            data["edgeLabel"] = label
        edges.append(
            {
                "source": src["id"],
                "sourceHandle": handle,
                "target": dst["id"],
                "targetHandle": dst["id"],
                "data": data,
                "type": "agentFlow",
                "id": f"{src['id']}-{handle}-{dst['id']}-{dst['id']}",
            }
        )

    def step(key: str, x: int, y: int) -> dict:
        label, variables, body, keys = STEPS[key]
        return node(
            "customFunctionAgentflow",
            label,
            x,
            y,
            {
                "customFunctionInputVariables": [
                    {"variableName": v, "variableValue": "{{ $form." + v + " }}"} for v in variables
                ],
                "customFunctionJavascriptFunction": CALL.replace("__API__", API) + body,
                "customFunctionUpdateState": [{"key": k, "value": "{{ output." + k + " }}"} for k in keys],
            },
        )

    out = lambda n: f"{n['id']}-output-{n['data']['name']}"  # noqa: E731
    state = ["extract", "detect", "table", "ready", "proposals", "result"]
    start = node(
        "startAgentflow",
        "Start · choose role and faults",
        0,
        80,
        {
            "startInputType": "formInput",
            "formTitle": "Data readiness round",
            "formDescription": "Pick a task, a role from the supply-chain apps, and the faults in the SAP extraction.",
            "formInputTypes": [
                {
                    "type": "options",
                    "label": "Requested task (my_role = the first task of the role below; each task has one readiness profile)",
                    "name": "task",
                    "addOptions": [{"option": "my_role"}] + [{"option": t} for t in tasks],
                },
                {
                    "type": "options",
                    "label": "Role (all roles in ~/Supplychain-AI-agent)",
                    "name": "role",
                    "addOptions": [{"option": k} for k in role_keys],
                },
                {
                    "type": "options",
                    "label": "Faults in the SAP extraction",
                    "name": "faults",
                    "addOptions": [{"option": "all"}, {"option": "none"}] + [{"option": f["id"]} for f in faults],
                },
            ],
            "startEphemeralMemory": True,
            "startState": [{"key": k, "value": ""} for k in state],
            "startPersistState": False,
        },
    )
    extract, detect = step("extract", 340, 80), step("detect", 680, 80)
    ready = node(
        "conditionAgentflow",
        "Data ready?",
        1020,
        80,
        {
            "conditions": [
                {"type": "string", "value1": "{{ $flow.state.ready }}", "operation": "equal", "value2": "yes"}
            ]
        },
    )
    ok = node(
        "directReplyAgentflow",
        "Reply · READY",
        1300,
        -120,
        {"directReplyMessage": "{{ $flow.state.extract }}\n\n{{ $flow.state.detect }}\n\n{{ $flow.state.table }}"},
    )
    propose = step("propose", 1300, 140)
    approve = node(
        "humanInputAgentflow",
        "Human approval",
        1640,
        140,
        {
            "humanInputDescriptionType": "fixed",
            "humanInputEnableFeedback": False,
            "humanInputDescription": "{{ $flow.state.extract }}\n\n{{ $flow.state.detect }}\n\n{{ $flow.state.table }}"
            "\n\n{{ $flow.state.proposals }}\n\n**Approve?** Only the fixes your role may approve "
            "are applied; the rest wait for the right approver.",
        },
    )
    apply_ = step("apply", 1940, 60)
    done = node(
        "directReplyAgentflow", "Reply · round result", 2280, 60, {"directReplyMessage": "{{ $flow.state.result }}"}
    )
    rejected = node(
        "directReplyAgentflow",
        "Reply · not applied",
        1940,
        300,
        {"directReplyMessage": "Fixes not applied.\n\n{{ $flow.state.table }}"},
    )
    node(
        "stickyNoteAgentflow",
        "Sticky Note",
        340,
        260,
        {
            "note": "DEMO. SAP systems are SIMULATED (synthetic CSVs). The role is a demo selector, not authentication.\n"
            "1 Detect -> 2 Propose -> 3 Fix -> loop: each approved fix is recorded upstream, so the next run (round) "
            "extracts without that fault.\nAll steps call the taskDataReadiness API at " + API
        },
    )

    edge(start, out(start), extract)
    edge(extract, out(extract), detect)
    edge(detect, out(detect), ready)
    edge(ready, f"{ready['id']}-output-0", ok, "yes")
    edge(ready, f"{ready['id']}-output-1", propose, "no")
    edge(propose, out(propose), approve)
    edge(approve, f"{approve['id']}-output-0", apply_, "proceed")
    edge(approve, f"{approve['id']}-output-1", rejected, "reject")
    edge(apply_, out(apply_), done)
    return {"nodes": nodes, "edges": edges}


if __name__ == "__main__":
    path = HERE / f"{NAME}.json"
    path.write_text(json.dumps(build(), indent=1, ensure_ascii=False) + "\n")
    print("wrote", path)
