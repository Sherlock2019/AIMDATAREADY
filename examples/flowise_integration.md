# Calling taskDataReadiness from Flowise

Flowise must not be *asked* to check readiness in a prompt. The check is a **Custom Function node (code)** followed
by a **Condition node**:

```
existing Supervisor classifies the task (LLM + deterministic guard)
  → Condition "route" ─ warehouse ─→ Custom Function "Data readiness" ─→ Condition "Data ready?"
                                                                           ├─ yes → existing specialist
                                                                           └─ no  → Direct Reply (reason) and stop
```

## The HTTP request the node sends

```http
POST {tdr_api_url}/readiness/check
Content-Type: application/json

{
  "task_id": "warehouse_transfer",
  "context": {
    "product_id": "SUN-SPF50-100",
    "origin_warehouse": "WH-A",
    "destination_warehouse": "WH-C",
    "planning_period": "2026-11-16/2026-11-29"
  },
  "demo_identity": {"user": "planner_all", "agent": "scinsight_supervisor"}
}
```

`demo_identity` exists only in demo mode and is **not authentication**. In a real integration the user identity must
come from a verified credential (for example a token exchanged on the user's behalf and validated by the service).
The JSON body cannot carry it: the API rejects `user_id` or `roles` fields with HTTP 422.

Response handling, all in code:

| Response | Node output | Flow |
| --- | --- | --- |
| 200 + `READY` | `tdr_ready = "yes"` | specialist runs |
| 200 + `READY_WITH_WARNINGS` | `tdr_ready = "yes"`, warnings in `tdr_message` | specialist runs (profile allowed it) |
| 200 + `BLOCKED` | `tdr_ready = "no"`, reasons in `tdr_message` | Direct Reply, stop |
| 4xx / 5xx, timeout (5 s), network error, unexpected body | `tdr_ready = "no"` | Direct Reply, stop (**fail closed**) |

The full node code is [integrations/supplychain/readiness_node.js](../integrations/supplychain/readiness_node.js).

## For the SCInsight Supervisor (generated copy)

```bash
.venv/bin/python integrations/supplychain/build_readiness_supervisor.py
# writes integrations/supplychain/generated/SCInsight-Cosmetics-Supervisor-Agentflow-v1.readiness.json
# the original export in ~/Supplychain-AI-agent is read, never written
```

Test the gate code without Flowise (Node 18+, service running on 8460):

```bash
F=integrations/supplychain/generated/SCInsight-Cosmetics-Supervisor-Agentflow-v1.readiness.json
node integrations/supplychain/run_node_check.cjs $F http://127.0.0.1:8460 planner_all \
  "Should we move SUN-SPF50-100 from warehouse A to warehouse C for 2026-11-16/2026-11-29?"
# {"tdr_ready":"yes","tdr_message":"","tdr_evaluation_id":"tdr-..."}
node integrations/supplychain/run_node_check.cjs $F http://127.0.0.1:8460 planner_ab \
  "Should we move SUN-SPF50-100 from warehouse A to warehouse C for 2026-11-16/2026-11-29?"
# {"tdr_ready":"no","tdr_message":"The warehouse analysis was not run because the data is not ready ...WH-C...", ...}
```

Flowise Variables used by the node: `tdr_api_url` and `tdr_demo_user` (demo only). The copy has **not** been
imported into Flowise. Import steps, network reachability (Flowise runs in Docker; `start.sh` binds 127.0.0.1) and
known mismatches are in [docs/supplychain_integration.md](../docs/supplychain_integration.md).

## Other Flowise paths

A gate on the Supervisor does not protect tools that the Chatflow's Tool Agent can call directly (SAP tools, the
pgvector Retriever Tool). Remove those tools from the Tool Agent or gate them too, and rely on the data services to
enforce authorisation.
