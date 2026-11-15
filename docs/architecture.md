# Architecture

## Components

```
            trusted caller (Flowise Supervisor, LangGraph, a UI) ── identity: demo selector (demo) | trusted provider (planned)
                                  │
            ┌─────────────────────▼─────────────────────┐
            │ Stage 1  app/task_router.py                │  explicit id · supervisor intent · demo keyword rules
            │          → task_id | clarification_required │  (a routing hint, never a permission)
            └─────────────────────┬─────────────────────┘
                                  │ task_id + context
            ┌─────────────────────▼─────────────────────┐
            │ Stage 2  app/engine.py                     │  profile (profiles/*.yaml, validated on load)
            │   context → per asset: catalog · access ·  │◄── adapter (app/adapters): catalog, schema, coverage,
            │   freshness · schema · coverage ·          │    timestamps, documents, authorize()
            │   provenance · documents                   │
            │   → READY | READY_WITH_WARNINGS | BLOCKED  │──► audit (app/audit.py)
            └─────────────────────┬─────────────────────┘
                                  │ only if not BLOCKED
            ┌─────────────────────▼─────────────────────┐
            │ consuming service (specialist / retriever)  │  MUST enforce authorisation itself and
            │                                             │  re-check state at use time
            └─────────────────────────────────────────────┘
```

## Decision rules

1. Any **mandatory** check with `FAIL` or `ERROR` (cannot be evaluated) → `BLOCKED`.
2. Otherwise, any `WARN` or a failed **optional** requirement → `READY_WITH_WARNINGS` if the profile sets
   `allow_warnings: true`, else `BLOCKED`.
3. Otherwise → `READY`.

Unknown task → HTTP 404 with `decision: BLOCKED`; nothing is evaluated and no other profile is substituted. Missing
or invalid task context → `BLOCKED` before any asset is touched. No user identity, no agent identity (when the
profile requires one), unknown authorisation or an unreachable authorisation service → `BLOCKED`.

When access to an asset is not granted, its other checks are `SKIPPED`. The response then carries no asset id,
source name, version or owner for that asset, and documents the caller may not read are counted but never named.

## Freshness: event time vs. ingestion time

`source_event_time` is when the data was true at the source (e.g. a snapshot "as of"); `ingested_at` is when the
copy arrived. Age is always computed from the event time. If it is missing, the profile chooses `block` or `warn`;
the ingestion time is never used instead. A timestamp in the future (beyond 5 minutes of clock skew) fails. All
timestamps are timezone-aware, and "now" comes from an injectable clock (`TDR_CLOCK`, or a lambda in tests).

## RAG checkpoints

| When | What must happen | In this PoC |
| --- | --- | --- |
| Before indexing | Record source id, version, classification, approval state, validity dates and access metadata for every document; refuse documents without them. | Represented in the demo catalog's `documents` records. Ingestion itself is not built. (SCInsight's own ingestion already enforces front matter, see the integration doc.) |
| Preflight (this service) | Are there approved, valid, versioned documents this user and agent may use for this purpose? | `documents` check, `policy_document_qa` profile. |
| During retrieval | Filter by permission *inside* the store query (metadata filter / row-level security), so unauthorised chunks are never loaded. | `DemoProtectedPolicyRetriever` (demo, keyword scoring) filters status, validity, user and agent roles before scoring. |
| After retrieval | Is the authorised evidence sufficient to answer? Otherwise say "not found". | `post_retrieval_evidence_check` (demo thresholds). |

## Structured data

Row and column restrictions belong where the rows live: PostgreSQL row-level security policies, a view per role,
or a protected API that derives scope from the verified caller (as the SCInsight API derives the tenant from the
API key). The readiness `access` check with `row_scope_context_keys` only *predicts* that the user may see the
requested warehouses, so it can stop early with a clear reason.

## Time of check vs. time of use

A source can change after the preflight (snapshot replaced, document revoked, permission removed). Two answers:

* **Recheck at use time.** `protected.guarded_call()` runs the evaluation immediately before the protected call and
  calls it only if not `BLOCKED`. `/demo/policy/search` uses it.
* **The consuming service enforces its own rules** at the moment it returns data. The SCInsight API, for example,
  re-checks the SAP snapshot age on every call (`Service.snapshot()` returns HTTP 409 when it is too old).

## When the readiness service is unavailable

For protected data the caller must treat "no answer" as `BLOCKED`. The generated Flowise gate does this (timeout
5 s; any network error, non-2xx status or unexpected body → stop). The policy is the same everywhere: unknown is not
allowed.

## Identity

* Demo: `demo_identity` picks a synthetic user and agent by name → `authenticated: false`. Not authentication.
* Integration (planned): a `TrustedIdentityProvider` derives the user from a validated token and the agent from a
  service credential. In `TDR_MODE=integration` only the placeholder `NotConfiguredIdentityProvider` exists, so every
  check is `BLOCKED`. Headers such as `X-User-Id` are ignored. A user-supplied JSON identity is rejected (HTTP 422).

## Audit

Each evaluation writes one JSON line with the evaluation id, time, task, profile version, decision, each check's
status, asset references, adapter mode, identity mode, a pseudonymous user reference (SHA-256 prefix), the agent id
and the validated task context. Question text, prompts, raw rows and secrets are never written.

Future work for production: append-only, tamper-evident storage; a retention period agreed with legal and
compliance; access control on the log; correlation with the downstream service's own audit trail (SCInsight already
keeps a hash-chained audit table).
