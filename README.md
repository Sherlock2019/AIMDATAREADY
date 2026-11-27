# DataReady (taskDataReadiness)

A preflight check that answers one question before an AI agent acts: **is the data this task needs fit and
permitted to use, right now, for this user?**

## What it is

A small local service (Python + FastAPI) that sits in front of an AI application's data access. It does two things:

1. **Identify the task.** Which business task is being asked for, for example "recommend a stock transfer"?
2. **Check data readiness.** For each data set the task needs, it checks whether the data is available, current,
   complete, traceable and authorised for this user, this agent and this purpose.

It answers `READY`, `READY_WITH_WARNINGS` or `BLOCKED`, with each check's result, plain-language reasons and an
evaluation id written to an audit log. When data is faulty, it proposes fixes (from rules, or optionally from
Claude). A person approves the fixes, then the data is checked again.

It is a **local demo with synthetic data** (simulated SAP extracts). The identity selector is not authentication,
and a `READY` result does not replace access control in the database, API or retriever.

## The problem it tries to solve

AI agents answer from whatever data they are given. They rarely check whether that data is fit for the job:

| Without a readiness check | With DataReady |
| --- | --- |
| The agent uses a 3-day-old inventory snapshot because it was re-loaded today | `BLOCKED`: source data is 74 h old, the task allows 24 h |
| Warehouse C rows are missing, and the agent recommends a transfer anyway | `BLOCKED`: coverage check finds no data for WH-C |
| A planner gets data for a warehouse they are not allowed to see, through the agent's wider access | `BLOCKED`: user and agent are checked separately |
| A revoked or draft policy document is used to answer a question | Only approved, valid documents the role may read are searched |
| Nobody can say later why the agent's answer was trusted | Every decision has an evaluation id, the checks and the evidence |

## How it works

```
User request ─► 1 Identify task ─► 2 Load the task's readiness profile (what data, how fresh, who may see it)
             ─► 3 Run checks: access · freshness · schema · coverage · quality · provenance · documents
             ─► BLOCKED: explain, propose fixes, a person approves, re-check
             ─► READY:   retrieve only the authorised rows and columns, then run the agent
```

* **Task profiles** (YAML in `profiles/` and `pipeline/tasks.yaml`) say what each task needs and its thresholds.
* **Catalog and contracts** (ODCS files in `pipeline/odcs/`) describe each data set: owner, source, schema, rules.
* **Checks** (`app/checks/`) are deterministic: no LLM decides readiness, so the same input gives the same answer.
* **Fixes** never invent values. They come from the source of record, the steward registry or a lossless transform,
  and are applied to a working copy.
* **Lineage and retrieval**: runs emit OpenLineage events, and document search (Chroma) filters by permission inside
  the query.

## Quick start

Linux or WSL with Python 3.11+.

```bash
./start.sh dashboard    # installs on first run, then open http://127.0.0.1:8460/dashboard
./start.sh test         # tests + lint
./start.sh help         # all commands
```

In the dashboard, pick a role and a task, choose a data set, run the checks, then propose and apply fixes.

## What next

### 1. Test it

```bash
./start.sh test         # unit + API tests, lint
./start.sh scenarios    # 36 labelled readiness scenarios: expected vs actual
./start.sh demo         # 11 faulty data sets: detect → fix → re-check, in the terminal
.venv/bin/python scripts/evaluate.py   # labelled evaluation → docs/evaluation_report.md
```

Then try your own cases in the dashboard or the API docs (<http://127.0.0.1:8460/docs>).

### 2. Integrate it in any AI pipeline

The gate is one HTTP call, so it fits any pipeline (Flowise, LangGraph, LangChain, a custom agent or a plain
backend). Call it **before** the agent, retriever or data service runs:

```bash
curl -s -X POST http://127.0.0.1:8460/readiness/check -H 'Content-Type: application/json' -d '{
  "task_id": "warehouse_transfer",
  "context": {"product_id":"SKU-001","origin_warehouse":"WH-A","destination_warehouse":"WH-C",
              "planning_period":"2026-11-16/2026-11-29"},
  "demo_identity": {"user":"planner_all","agent":"scinsight_supervisor"}}'
```

* `READY` (or an allowed `READY_WITH_WARNINGS`): continue to the agent.
* `BLOCKED`: stop, show the `reasons` to the user, and log the `evaluation_id`.

Steps for your own pipeline:

1. **Add a gate step** where the request is routed. In Flowise that is a Custom Function node plus a Condition node
   (see [examples/flowise_integration.md](examples/flowise_integration.md)). In LangGraph it is a node or a check in
   the `route()` function. In code it is one HTTP call.
2. **Describe your tasks** as readiness profiles: copy a file in `profiles/` and set the data sets, freshness,
   coverage and access each task needs.
3. **Connect your data** by writing an adapter: subclass `ReadinessAdapter` in
   [app/adapters/base.py](app/adapters/base.py) (catalog, schema, coverage, authorisation for your database, SAP,
   catalog tool or vector store).
4. **Reuse your router** if you have one: pass its task id instead of using the demo keyword router.

### 3. Before production

* Replace the demo identity with a real identity provider (verified user and agent tokens).
* Enforce the same access rules in the database, API and retriever; this gate is a preflight, not the only barrier.
* Move audit and lineage to a durable store, and agree real thresholds with the data owners.

## More

* Full reference (endpoints, scenarios, adding profiles and adapters, limitations): [docs/reference.md](docs/reference.md)
* Architecture: [docs/architecture.md](docs/architecture.md)
* Evaluation results: [docs/evaluation_report.md](docs/evaluation_report.md)
