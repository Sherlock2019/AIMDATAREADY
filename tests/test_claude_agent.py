"""Claude agent wiring, tested with a STUB client (no network, no cost).

The stub stands in for client.beta.messages.tool_runner(...): it records the request parameters and calls the
agent's tools the way the real runner would. What is proven: the request uses claude-opus-5-5 with adaptive thinking
and refusal fallbacks; tool outputs reach the model; proposals go through the allow-list; invalid ones are rejected;
nothing is applied by Claude. What is NOT proven: how the real model behaves (no credentials on this machine).
"""

import json
from types import SimpleNamespace

import pytest

from app.adapters.dataset import DatasetAdapter
from app.adapters.demo import DemoAdapter
from app.claude_agent import MODEL, propose_with_claude
from app.dataset_lab import DEFAULT_CONTEXT, DatasetLab
from app.identity import resolve_demo_identity
from app.models import DemoIdentity
from app.settings import ROOT
from tests.conftest import NOW


class StubRunner:
    def __init__(self, tools, script):
        self.tools, self.script, self.outputs = {t.name: t for t in tools}, script, {}

    def until_done(self):
        for name, args in self.script:
            self.outputs.setdefault(name, []).append(self.tools[name].call(args))
        text = SimpleNamespace(type="text", text="Inventory has a duplicate batch; I propose deduplicate.")
        return SimpleNamespace(
            stop_reason="end_turn", content=[text], model=MODEL, usage=SimpleNamespace(input_tokens=10, output_tokens=5)
        )


class StubClient:
    def __init__(self, script):
        self.script, self.calls = script, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(tool_runner=self._runner))

    def _runner(self, **kwargs):
        self.calls.append(kwargs)
        self.runner = StubRunner(kwargs["tools"], self.script)
        return self.runner


@pytest.fixture
def setup(tmp_path):
    authz = DemoAdapter.from_dir(ROOT / "catalog")
    lab = DatasetLab(ROOT / "datasets", tmp_path, authz, lambda: NOW, None)
    u, a = resolve_demo_identity(DemoIdentity(user="planner_all", agent="scinsight_supervisor"), authz.directory)
    return lab, u, a


def test_claude_proposals_are_validated_and_not_applied(setup):
    lab, u, a = setup
    case = "04_duplicate_rows"
    result = lab.evaluate(case, u, a, "demo_selector", "working")
    script = [
        ("get_failures", {}),
        ("inspect_rows", {"requirement": "inventory", "limit": 3}),
        ("list_actions", {}),
        ("propose_fix", {"requirement": "inventory", "action": "deduplicate", "rationale": "duplicate batches"}),
        ("propose_fix", {"requirement": "inventory", "action": "drop_table", "rationale": "bad idea"}),
    ]
    client = StubClient(script)
    out = propose_with_claude(
        title="dupes",
        result=result,
        profile=lab.profile,
        context=DEFAULT_CONTEXT,
        adapter=DatasetAdapter(lab.work_root / case, lab.authz),
        client=client,
    )
    req = client.calls[0]
    assert req["model"] == "claude-opus-5-5" and req["thinking"] == {"type": "adaptive"}
    assert req["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in req["betas"]
    failures = json.loads(client.runner.outputs["get_failures"][0])
    assert failures["decision"] == "BLOCKED" and any(f["check"] == "quality" for f in failures["failures"])
    rows = json.loads(client.runner.outputs["inspect_rows"][0])["rows"]
    assert any(r["batch_id"] == "C-001-1" for r in rows)
    assert [p["action"] for p in out["proposals"]] == ["deduplicate"] and out["proposals"][0]["proposed_by"] == "claude"
    assert out["rejected"][0]["action"] == "drop_table" and "summary" in out
    # Claude only proposed: the working copy is still BLOCKED until a person applies the proposal.
    assert lab.evaluate(case, u, a, "demo_selector", "working").decision.value == "BLOCKED"
    applied = lab.apply(case, out["proposals"], u, a, "demo_selector")
    assert applied["after"]["decision"] == "READY"


def test_inspect_rows_refuses_requirements_without_access(setup):
    lab, _, a = setup
    u2, a2 = resolve_demo_identity(DemoIdentity(user="planner_ab", agent="scinsight_supervisor"), lab.authz.directory)
    case = "02_missing_warehouse_c"
    result = lab.evaluate(case, u2, a2, "demo_selector", "working")
    client = StubClient([("inspect_rows", {"requirement": "inventory"})])
    propose_with_claude(
        title="x",
        result=result,
        profile=lab.profile,
        context=DEFAULT_CONTEXT,
        adapter=DatasetAdapter(lab.work_root / case, lab.authz),
        client=client,
    )
    assert "access not granted" in client.runner.outputs["inspect_rows"][0]


def test_without_credentials_claude_is_reported_unavailable(setup, monkeypatch, tmp_path):
    lab, u, a = setup
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    result = lab.evaluate("04_duplicate_rows", u, a, "demo_selector", "working")
    out = propose_with_claude(
        title="x",
        result=result,
        profile=lab.profile,
        context=DEFAULT_CONTEXT,
        adapter=DatasetAdapter(lab.work_root / "04_duplicate_rows", lab.authz),
    )
    assert out["available"] is False and "ANTHROPIC_API_KEY" in out["message"]
