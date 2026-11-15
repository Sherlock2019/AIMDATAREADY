"""Run every labelled demo scenario and print expected vs actual, then latency figures.

    .venv/bin/python scripts/run_demo.py                          # in-process (no server needed)
    .venv/bin/python scripts/run_demo.py --base-url http://127.0.0.1:8460   # against a running server
    .venv/bin/python scripts/run_demo.py --repeat 20              # more samples for the latency figures

Exit code 1 if any scenario does not match its expected outcome.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.scenario_runner import load_scenarios, run_scenario  # noqa: E402


def make_client(base_url: str | None):
    if base_url:
        import httpx

        return httpx.Client(base_url=base_url, timeout=10)
    from fastapi.testclient import TestClient

    from app.audit import MemoryAuditSink
    from app.main import create_app
    from app.settings import Settings

    return TestClient(create_app(Settings(audit_path=None), audit=MemoryAuditSink()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--repeat", type=int, default=1, help="runs per scenario (latency samples)")
    args = parser.parse_args()
    client = make_client(args.base_url)
    scenarios = load_scenarios()
    failures, latencies = 0, []
    print(f"{'scenario':48} {'expected':22} {'actual':24} result")
    for sc in scenarios:
        runs = [run_scenario(client, sc) for _ in range(max(1, args.repeat))]
        latencies += [r["latency_ms"] for r in runs if sc["endpoint"] == "/readiness/check"]
        r = runs[0]
        want = sc["expected"].get("decision") or sc["expected"].get("status")
        ok = all(not x["problems"] for x in runs)
        failures += 0 if ok else 1
        print(
            f"{sc['name']:48} {str(want):22} {str(r['actual']) + ' (' + str(r['status_code']) + ')':24} "
            f"{'OK' if ok else 'MISMATCH: ' + '; '.join(r['problems'])}"
        )
    print(
        f"\n{len(scenarios) - failures}/{len(scenarios)} scenarios matched their expected outcome "
        f"({'in-process' if not args.base_url else args.base_url})."
    )
    if latencies:
        latencies.sort()
        p95 = latencies[max(0, int(round(0.95 * len(latencies))) - 1)]
        print(
            f"/readiness/check latency over {len(latencies)} requests: median {statistics.median(latencies):.2f} ms, "
            f"p95 {p95:.2f} ms, max {latencies[-1]:.2f} ms (client-measured, includes HTTP handling)."
        )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
