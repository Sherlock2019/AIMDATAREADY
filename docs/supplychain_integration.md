# Integration with ~/Supplychain-AI-agent (SCInsight)

Inspected on 2026-11-14. Branch `feature/knowledge-platform`, last commit `cf00016`, working tree clean. No AGENTS.md
exists in the repository. **No file in that repository was modified.** Its git status was clean before and after,
and the SHA-256 of the Supervisor export was identical before and after generating the patched copy.

## What the project is

| Item | Finding |
| --- | --- |
| Entry point | One FastAPI app, `backend/app/main.py`; SCInsight routes are an `APIRouter` mounted at `/scinsight` (`backend/scinsight/app.py`). Docker container `aurelia-scm-api-1` on port 8000. |
| Other services | MCP server `backend/app/mcp_server.py` (container port 8002), LangGraph runtime (8123), Flowise (127.0.0.1:3000). |
| How a user asks | Flowise Chatflow `SCInsight-Cosmetics-Chatflow-v1` (Tool Agent), or the Supervisor Agentflow directly, or the LangGraph mirror (`backend/scinsight/lg/supervisor.py`). |
| Task identification | Supervisor Agentflow node "3 · Understand the request (LLM)": Qwen with prompt `flowise/prompts/supervisor_classifier.md` returns one of `factory_planning, procurement, warehouse, inventory_optimizer, logistics, quality, outcome_learning, sap_write_request, out_of_scope`. Then "4 · Safety guard" (deterministic JS) forces `sap_write_request` / `out_of_scope` on regex matches. |
| Routing | "5 · Route to a specialist" (Condition node) → Execute Flow nodes for the 7 specialist Agentflows; `sap_write_request` → "Stop · SAP change refused"; else "Stop · out of scope". |
| Flowise exports | `flowise/agentflows/*.json`, `flowise/chatflows/*.json`, `flowise/tools/*.json`. They are build outputs of `flowise/scinsight/build-scinsight.cjs` and imported by `flowise/scinsight/deploy.py` via `flowise/scinsight/manifest.json` (placeholders such as `__FLOW:<name>__` are bound at import). |
| Data | **Synthetic.** `sap_mode = "synthetic"` (`backend/scinsight/settings.py`; "live requires an authorised SAP adapter; not implemented"). Fixtures in `backend/scinsight/fixtures.py`, snapshot `SAP-SYN-...` as of 2026-10-21, labelled "not real SAP data". SAP is read-only (write verbs → 405; `SAP_WRITE_ENABLED=true` refuses to start). |
| Authentication | Every SCInsight route resolves a `Principal` from `X-API-Key`/Bearer (hashed keys in the `users` table). Tenant and roles come only from the key; a mismatching `X-Tenant` header → 403 (`backend/scinsight/security.py`). |
| Data-access checks | Tenant filter on every query; role checks (`READ_ROLES`, review authority per domain); snapshot freshness: `Service.snapshot()` returns 409 if older than `SCI_SNAPSHOT_MAX_AGE_DAYS` (2). **But** the "today" used is `SCI_BUSINESS_DATE` or, by default, the snapshot date itself, so age is 0 unless that variable is set. **No per-user warehouse (row) scope**: any reader in the tenant sees all warehouses. |
| RAG | (a) `POST /scinsight/api/v1/rag/search` filters by tenant, caller roles and validity dates before ranking, and returns citations. (b) Flowise Document Store "SCInsight policy RAG DB" (pgvector), used by the Chatflow's Retriever Tool and by a Retriever node in each specialist. Its content is filtered **at indexing** to tenant `scinsight-cosmetics` documents readable by `agent_service` (`deploy.py rag_documents()`), but queries apply **no per-user role filter and no validity-date filter**. |
| How tools call SAP data | Flowise Custom Tools and Custom Function nodes call the SCInsight API with `$vars.sci_api_url` and the shared `$vars.sci_agent_key` (role `agent_service`). LangGraph uses `SCI_AGENT_KEY` from the environment. |
| Tests | `tests/scinsight/` (API scenarios, security, Flowise e2e, LangGraph mirror, RAG tuning); `make test` → `.venv/bin/python -m pytest`; `make lint` → ruff. Not run as part of this work: nothing in that repository changed. |
| Docker | `docker-compose.yml` profiles core/localdb/local-llm/observability; Flowise has `HTTP_DENY_LIST` for SSRF protection. |

### Identity gap

The end user's identity does not reach SCInsight from Flowise. The Supervisor's "2 · Check access" node calls
`/api/v1/context/validate` with the shared agent key, so SCInsight sees the *agent*, not the person. Per-user
decisions (row scope, purpose) are therefore impossible on this path today. That includes per-user decisions by this
readiness service. The generated gate passes a **demo** user name from a Flowise Variable to show the decision
logic; that is not a solution.

## Integration map

```
User (Flowise chat)
  → Chatflow Tool Agent ─────────────────────────────────────────────┐ (direct tools, see table below)
  → ChatflowTool "SC Agent Manager" → Supervisor Agentflow
      → 2 · Check access  (/context/validate, agent key; snapshot freshness)
      → 3 · Understand the request (LLM)  → 4 · Safety guard (JS)          ← existing task identification
      → 5 · Route to a specialist
          ├─ warehouse ─→ [5a · Data readiness → Data ready?]           ← PROPOSED readiness check (generated copy)
          │                    ├─ yes → 6 · Specialist · Warehouse Agentflow → /optimizer/warehouse-balancing (API key)
          │                    └─ no  → Stop · data not ready
          ├─ other specialists (no readiness profile yet → unchanged)
          └─ sap_write_request / out_of_scope → Stop
```

Stage 1 is reused, not duplicated: the gate only runs on the branch the Supervisor already chose
(`from_supervisor_intent("warehouse") → warehouse_transfer`).

## Paths that reach data without passing the Supervisor (need protection)

| Path | What it reaches | Existing control | Gap |
| --- | --- | --- | --- |
| Chatflow Tool Agent → `customTool_0..3` (SAP inventory, production orders, purchase orders, BOM) | SCInsight SAP read endpoints | API key (agent), tenant scoping, snapshot age | LLM chooses the call; no readiness gate; agent identity only |
| Chatflow Tool Agent → `retrieverTool_0` | Flowise pgvector RAG DB directly | Index-time filter (tenant + agent-readable) | No per-user or validity filtering at query time |
| Chatflow Tool Agent → `customTool_10` search policy | `/rag/search` | Tenant, roles, validity in the API | Agent roles, not the user's |
| Each specialist Agentflow's Retriever node | Flowise pgvector RAG DB | Same as above | Same as above |
| Specialist Agentflows invoked directly (Flowise prediction API) | Their API calls | Flowise API key assigned at import (`deploy.py`: `isPublic: False`, `apikeyid`); each specialist has its own "Check access" node | Skips the Supervisor's routing and guard |
| SCInsight REST API called directly | All SCInsight data | API key → tenant/roles | No readiness gate (and none needed if the API itself enforces) |
| MCP server `/mcp` (Aurelia tools, e.g. `warehouse_stock_balance`, `search_governed_knowledge`) | Aurelia demo data | `MCP_API_KEYS`; **if that variable is empty, the middleware lets every request through** (`mcp_server.py`, `if ... not self.keys`) | Unauthenticated when unconfigured |
| LangGraph mirror (`backend/scinsight/lg/supervisor.py`) | Same SCInsight API | Agent key from env | No readiness gate; integration point is `route()` |

A prompt telling the LLM to "check readiness first" would not protect any of these paths. Protection must come from
(1) code in the flow (the generated gate), (2) **removing direct data tools from the Chatflow Tool Agent** or putting
them behind the same gate, and (3) the SCInsight API and the RAG store enforcing per-user authorisation themselves
once a verified user identity is propagated.

## What was implemented

1. A standalone service (this repository) that runs without SCInsight, Flowise, SAP or an LLM.
2. `integrations/supplychain/build_readiness_supervisor.py`: reads the Supervisor export **read-only** and writes a
   clearly named copy `integrations/supplychain/generated/SCInsight-Cosmetics-Supervisor-Agentflow-v1.readiness.json`.
   It adds 4 nodes and 3 state keys and rewires one edge (26 → 29 edges). The original is preserved.
3. `integrations/supplychain/readiness_node.js`: the gate's code (fail closed, 5 s timeout).
4. Tests (`tests/test_supplychain_integration.py`, 7 tests, all passed):
   * the source export is unchanged by generation;
   * in the copy, the Warehouse specialist is reachable **only** through the gate's "ready" output. Without that
     edge it is unreachable from Start, so a blocked request cannot invoke it;
   * the gate JS, run by Node outside Flowise (`run_node_check.cjs`) against a real uvicorn process of this
     service, returns `yes` on READY and `no` on BLOCKED (WH-C out of scope), on no identity, on missing origin
     warehouse, and when the service is down.

**Not done / not verified:** the copy was **not imported into Flowise** and was not run inside Flowise. The running
Flowise and SCInsight containers were not touched. The LangGraph mirror and the Chatflow were not changed.

## How to try the copy in Flowise (manual, needs your go-ahead on the running instance)

1. Start this service where the Flowise container can reach it. Flowise runs in Docker: either run the service on
   the Docker network, or bind it to an address reachable as `host.docker.internal` and confirm that
   `HTTP_DENY_LIST` in `docker-compose.yml` does not block that address. `start.sh` binds 127.0.0.1 only, which a
   container cannot reach.
2. Add Flowise Variables `tdr_api_url` (e.g. `http://host.docker.internal:8460`) and, for the demo only,
   `tdr_demo_user` (`planner_all` or `planner_ab`).
3. Import under a **new name** so the original stays live. The copy still contains the `__FLOW:...__`,
   `__QWEN_...__` and `__FLOWISE_...__` placeholders, which `deploy.py` binds. Either add a manifest entry named
   e.g. `SCInsight-Cosmetics-Supervisor-Agentflow-v1-readiness` pointing at the copy (that edits the SCInsight
   repository), or bind the placeholders by hand to the ids of the existing imported flows and credentials.
4. Ask: "Should we move SUN-SPF50-100 from warehouse A to warehouse C?". With `planner_ab` the reply should be
   "The warehouse analysis was not run because the data is not ready ... WH-C".

Known mismatches to resolve first:

* **The demo catalog does not describe SCInsight's data.** Its timestamps are synthetic and set for the demo clock
  (2026-11-14), while the SCInsight snapshot is as of 2026-10-21. A real SCInsight adapter must supply those facts.
* **Different task shape.** SCInsight's warehouse balancing chooses the donor warehouse itself, but the
  `warehouse_transfer` profile requires an explicit origin. Questions such as "Should we transfer sunscreen to
  Warehouse B?" are therefore BLOCKED for missing `origin_warehouse`. A `warehouse_balancing` profile (all
  warehouses in scope, no origin) would match SCInsight better.
* **No planning period in typical questions.** The gate defaults it to 14 days from the Supervisor's snapshot date
  (`$flow.state.as_of`). That is a documented integration default, not a business rule.
* **Warnings are not shown.** On `READY_WITH_WARNINGS` the gate continues and keeps the warning text in
  `$flow.state.tdr_message`; the copy does not yet show it to the user.

## Permanent integration (recommended order)

1. Propagate a verified user identity into flows and the API (the largest gap).
2. Move the gate into `flowise/scinsight/build-scinsight.cjs` (the source of the exports) and into the LangGraph
   `route()`. For example, before `specialist_warehouse`, call `/readiness/check` and route to a stop node unless
   `READY`.
3. Remove `retrieverTool_0` and the direct SAP tools from the Chatflow Tool Agent, or gate them; keep the governed
   `/rag/search` tool.
4. Add per-user warehouse scope in the SCInsight API (or PostgreSQL row-level security) so enforcement does not
   depend on any preflight.
5. Set `MCP_API_KEYS` everywhere, or make the MCP middleware refuse to start without keys.

## Readiness service unavailable

The gate returns `tdr_ready = "no"` with "The data readiness service is unavailable ... (fail closed)". The
Warehouse specialist does not run. This is the intended default for protected data.
