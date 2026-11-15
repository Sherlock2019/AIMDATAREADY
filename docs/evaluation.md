# Evaluation

Everything below was executed on 2026-11-14 on a 12-thread WSL2 machine (Python 3.12.3, Node 24.22.1). Nothing is
estimated. Re-run with the commands shown.

## Definitions

* **Unsafe pass:** a case that should be BLOCKED but is allowed (READY or READY_WITH_WARNINGS). This is the error
  that matters most.
* **False block:** a case that should be allowed but is BLOCKED. Costly in usability, safe in effect.
* **Labelled scenario:** a request plus the outcome a reviewer decided is correct *before* running it, stored in
  [examples/sample_requests.json](../examples/sample_requests.json).

## Labelled scenarios

36 scenarios, run in-process (`scripts/run_demo.py`) and against the live server
(`scripts/run_demo.py --base-url http://127.0.0.1:8460`). They also run in pytest as `test_labelled_scenarios`.

| Group | Count | Expected outcomes |
| --- | --- | --- |
| Task identification | 5 | 2 identified, 3 clarification_required (two-task question, unsupported question, unknown explicit id) |
| Warehouse transfer readiness | 21 | 2 READY, 2 READY_WITH_WARNINGS, 17 BLOCKED |
| Unknown task / identity in body | 2 | BLOCKED (HTTP 404 / 422) |
| Policy preflight + demo protected retriever | 6 | 4 READY (one of them READY but not answerable), 2 BLOCKED (one proves the retriever was not called) |
| Combined `/evaluate` | 2 | 1 READY, 1 BLOCKED (ambiguous) |

Of the 31 readiness decisions, 22 were labelled BLOCKED and 9 allowed (7 READY, 2 READY_WITH_WARNINGS).

**Result: 36/36 matched, in-process and against the live server. Unsafe passes: 0 of 22. False blocks: 0 of 9.**

## Automated tests

`.venv/bin/python -m pytest` → **131 passed** (updated 2026-11-14 after adding the faulty-dataset lab; the first
run of this document had 96 tests, with one Starlette `httpx` deprecation warning that no longer appears now that
`anthropic` brings in `httpx2`).

| File | Tests | Covers |
| --- | --- | --- |
| tests/test_task_router.py | 8 | transfer / policy recognised; two-task and unsupported → clarification; explicit unknown id; no task without profile; Supervisor intent reuse; context hints |
| tests/test_readiness.py | 33 | READY; missing asset; stale (and recent-ingestion trap); missing event time; schema; missing warehouse; schema-complete-but-not-covered; missing lane; row-scope denial with metadata hidden; unknown user; no identity; authz down; agent access; warnings allowed vs. not; optional asset; provenance block vs. warn; missing context; unknown profile audited; demo identity never authenticated; audit minimal and pseudonymous; policy documents counted not named; direct retrieval without preflight; blocked never calls downstream; use-time recheck; freshness unit rules; invalid profiles rejected; fixed vs. later clock |
| tests/test_api.py | 48 | health; identify; READY / BLOCKED; 4 invalid bodies → structured 422; identity fields in body rejected; unknown profile / scenario / identity; error shape outside gated paths; integration mode refuses demo selector and fails closed (spoofed headers ignored); `/evaluate` context sources; the 36 labelled scenarios |
| tests/test_supplychain_integration.py | 7 | original export unchanged; Warehouse specialist reachable only via the gate's "ready" output; gate JS via Node against a live uvicorn process: READY → yes, BLOCKED → no, no identity → no, missing origin → no, default period, service down → no |
| tests/test_datasets.py | 32 | 10 faulty + 1 clean case; each fault detected from the data (10); each fault corrected, re-checked READY, original files unchanged, change log present (10); fixes take source values; owner from registry; date reformat refused without a declared format; access problems escalated, not fixed; allow-list rejects 6 malformed proposals; dataset profile differs from the base profile only by quality rules; API check → propose → apply → re-check; run-all; dashboard served; unknown case / path traversal / forged action; lab absent outside demo mode |
| tests/test_claude_agent.py | 3 | **stub client** (no network, no cost): request uses claude-opus-5-5, adaptive thinking and refusal fallback; tool outputs reach the model; proposals pass the allow-list and an invalid one is rejected; Claude does not apply anything; `inspect_rows` refuses without access; reported unavailable without credentials |

Static checks: `ruff check .` → all checks passed; `ruff format --check .` → 46 files already formatted.

## Faulty-dataset lab

`./start.sh demo` (planner_all) → **11/11 as expected**. 10 faulty cases were BLOCKED before correction and READY
after; the clean control was READY with no fixes. For planner_ab (no access to WH-C), case 02 stays BLOCKED with two
"needs a person" escalations and no data changes. The Claude path has not been run against the real API: no
Anthropic credential exists on this machine.

Limits: each case injects one fault, chosen by the same author as the rules, so detection and correction are
demonstrated, not measured. Real data can hold several faults at once, and faults the rules do not anticipate.

## Access-bypass tests

| Path | Test | Result |
| --- | --- | --- |
| Supervisor path (generated SCInsight copy) | Graph reachability: Warehouse specialist unreachable from Start without the gate's "ready" edge | Passed |
| Supervisor path, gate code | Node-executed gate returns `no` for an out-of-scope user, no identity, and an unreachable service | Passed |
| Direct data path (demo retriever, no preflight) | Contractor, untrusted agent, no identity and authorisation-down all get zero documents; planner never gets the restricted finance document | Passed |
| Use-time change | READY preflight, then the source turns stale: `guarded_call` blocks and the downstream function is not called | Passed |
| Identity spoofing | `user_id` / `roles` in the body → 422; `X-User-Id` header in integration mode → BLOCKED | Passed |
| **SCInsight direct paths** (Chatflow SAP tools, pgvector Retriever Tool and nodes, direct specialist calls, MCP) | **Not tested**: that repository was not modified and the running stack was not touched | Listed as gaps in [supplychain_integration.md](supplychain_integration.md) |

## Latency

Method: `scripts/run_demo.py --repeat 20` times each `POST /readiness/check` on the client with
`time.perf_counter()` (25 readiness scenarios × 20 = 500 requests per run), then reports median, p95 and max. The
figures include HTTP handling, JSON (de)serialisation, the DemoAdapter, the checks and the audit write.

| Run | Median | p95 | Max |
| --- | --- | --- | --- |
| Live uvicorn on 127.0.0.1:8460 (httpx client, JSONL audit on) | 2.06 ms | 3.15 ms | 4.78 ms |
| In-process (FastAPI TestClient, in-memory audit) | 4.84 ms | 6.02 ms | 22.14 ms |

An earlier live run of the same command measured median 1.99 ms, p95 2.61 ms. The figures vary between runs. A real
adapter's network calls (catalog, authorisation, database) will dominate.

## Limits of this evaluation

* The data, identities and permissions are synthetic, and the same person wrote the scenarios and the code. A
  perfect score shows the rules do what was intended. It does not show the rules are the right ones.
* 36 scenarios cover each rule at least once, not combinations at scale or adversarial wording. The keyword router
  is easy to fool; only its clarification behaviour was tested.
* No real SAP, database, catalog, identity provider or Flowise runtime was involved. The Flowise gate was tested by
  running its JavaScript in Node, not inside Flowise.
* Freshness was tested with a fixed clock; behaviour under real clock skew between systems was not measured.
* Latency was measured on one machine under no concurrent load.
