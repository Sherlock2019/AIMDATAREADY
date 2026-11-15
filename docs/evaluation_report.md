# Evaluation report (generated)

Generated 2026-11-15 10:58 by `scripts/evaluate.py` (in-process, fixed demo clock 2026-11-14T08:00:00+00:00). Every number below was produced by this run; nothing is estimated.

Labels were set by hand before running. READY_WITH_WARNINGS counts as **allowed**. A 404/422 refusal on a gated endpoint counts as **blocked**.

## Decision quality

| Metric | Definition (denominator) | Result |
|---|---|---|
| Labelled decision scenarios | A: 31 API + B: 11 datasets + C: 35 Data Harness | 77 |
| **Unsafe pass count** | should block, engine allowed | **0** |
| Unsafe pass rate | unsafe passes ÷ scenarios labelled block | 0/54 = 0.0% |
| False block count | should allow, engine blocked | 0 |
| False block rate | false blocks ÷ scenarios labelled allow | 0/23 = 0.0% |
| Block detection rate | correctly blocked ÷ scenarios labelled block | 54/54 = 100.0% |
| Correct decision rate | correct ÷ all labelled decision scenarios | 77/77 = 100.0% |
| Task identification (separate) | matching ÷ identification scenarios | 5/5 = 100.0% |

## Access control (tested paths only)

| Metric | Result |
|---|---|
| Unauthorized data exposure (demo protected retriever, 4 identity/agent combinations) | 0 documents |
| Access bypass - Supervisor path (generated SCInsight Agentflow copy, graph test) | pass (Warehouse specialist reachable only through the gate's READY output) |
| Access bypass - direct retrieval without preflight | pass (0 documents returned without authorization) |
| Denied / unknown access scenarios blocked | 5/5 = 100.0% |
| SCInsight direct paths (Chatflow SAP tools, pgvector retriever, MCP) | **not tested** - repository not modified |

## Operations

| Metric | Definition | Result |
|---|---|---|
| Engine latency P50 | in-engine evaluation time, 389 evaluations | 2.683 ms |
| Engine latency P95 | same samples | 4.918 ms |
| Engine latency max | same samples | 7.704 ms |
| Check error count | checks that ended in ERROR (could not be evaluated), last run of each scenario | 16 |
| Unknown-result count | access checks with unknown authorization, last run of each scenario | 7 |

Latency excludes HTTP handling; it is measured inside the engine with the synthetic adapters.

## Most common reasons for blocking

| Check | Blocking occurrences |
|---|---|
| access | 34 |
| coverage | 11 |
| quality | 11 |
| freshness | 5 |
| catalog | 4 |
| provenance | 3 |
| schema | 2 |
| context | 1 |
| metadata | 1 |
| documents | 1 |

## Data-readiness metrics (authorized transfer, clean data)

| Asset | Metrics (only where evidence exists) |
|---|---|
| s4_mm | records=18, source_data_age_hours=2.0, ingestion_delay_hours=0.33, required_field_completeness_pct=100.0, task_coverage=2/2, schema_compliance=pass, contract_validation=pass, provenance_complete=True, catalog_and_contract_complete=True |
| ibp | records=45, source_data_age_hours=32.0, ingestion_delay_hours=1.0, required_field_completeness_pct=100.0, task_coverage=2/2, schema_compliance=pass, contract_validation=pass, provenance_complete=True, catalog_and_contract_complete=True |
| ariba | records=6, source_data_age_hours=12.0, ingestion_delay_hours=0.5, required_field_completeness_pct=100.0, schema_compliance=pass, contract_validation=pass, provenance_complete=True, catalog_and_contract_complete=True |
| tm | records=6, source_data_age_hours=320.0, ingestion_delay_hours=2.0, required_field_completeness_pct=100.0, task_coverage=1/1, schema_compliance=pass, contract_validation=pass, provenance_complete=True, catalog_and_contract_complete=True |

## Scenario results

| Set | Scenario | Expected | Engine | OK |
|---|---|---|---|---|
| A api | transfer_ready | allow | allow | ✓ |
| A api | transfer_stale_inventory | block | block | ✓ |
| A api | transfer_ingestion_time_only | block | block | ✓ |
| A api | transfer_missing_warehouse_c | block | block | ✓ |
| A api | transfer_user_not_scoped_to_wh_c | block | block | ✓ |
| A api | transfer_same_user_within_scope | allow | allow | ✓ |
| A api | transfer_lead_times_unavailable | block | block | ✓ |
| A api | transfer_lane_missing | block | block | ✓ |
| A api | transfer_authorization_unavailable | block | block | ✓ |
| A api | transfer_catalog_unavailable | block | block | ✓ |
| A api | transfer_no_identity | block | block | ✓ |
| A api | transfer_untrusted_agent | block | block | ✓ |
| A api | transfer_no_agent | block | block | ✓ |
| A api | transfer_wrong_role | block | block | ✓ |
| A api | transfer_missing_context | block | block | ✓ |
| A api | transfer_unknown_product | block | block | ✓ |
| A api | transfer_period_not_covered | block | block | ✓ |
| A api | transfer_missing_schema_field | block | block | ✓ |
| A api | transfer_inventory_missing_owner_blocks | block | block | ✓ |
| A api | transfer_forecast_missing_owner_warns | allow | allow | ✓ |
| A api | transfer_forecast_aging_warns | allow | allow | ✓ |
| A api | unknown_task_profile | block | block | ✓ |
| A api | identity_in_body_rejected | block | block | ✓ |
| A api | policy_ready | allow | allow | ✓ |
| A api | policy_contractor_blocked | block | block | ✓ |
| A api | policy_search_answerable | allow | allow | ✓ |
| A api | policy_search_no_evidence | allow | allow | ✓ |
| A api | policy_search_blocked_does_not_retrieve | block | block | ✓ |
| A api | policy_search_restricted_doc_visible_to_finance | allow | allow | ✓ |
| A api | evaluate_question_ready | allow | allow | ✓ |
| A api | evaluate_ambiguous_stops | block | block | ✓ |
| B dataset | 00_clean | allow | allow | ✓ |
| B dataset | 01_stale_inventory | block | block | ✓ |
| B dataset | 02_missing_warehouse_c | block | block | ✓ |
| B dataset | 03_missing_column | block | block | ✓ |
| B dataset | 04_duplicate_rows | block | block | ✓ |
| B dataset | 05_negative_quantity | block | block | ✓ |
| B dataset | 06_null_values | block | block | ✓ |
| B dataset | 07_invalid_status_codes | block | block | ✓ |
| B dataset | 08_bad_date_format | block | block | ✓ |
| B dataset | 09_missing_lane | block | block | ✓ |
| B dataset | 10_missing_owner | block | block | ✓ |
| C harness | authorized transfer, clean data | allow | allow | ✓ |
| C harness | open PO dates missing (optional asset -> warning) | allow | allow | ✓ |
| C harness | stale inventory (74 h, max 2 h) | block | block | ✓ |
| C harness | incompatible unit in inventory | block | block | ✓ |
| C harness | missing transfer lead time WH-A>WH-C | block | block | ✓ |
| C harness | denied row access to WH-C | block | block | ✓ |
| C harness | denied data-set access (no read) | block | block | ✓ |
| C harness | role cannot read the forecast | block | block | ✓ |
| C harness | unknown authorization (service down) | block | block | ✓ |
| C harness | agent service reading for itself | allow | allow | ✓ |
| C harness | denied column access (supplier prices) | block | block | ✓ |
| C harness | authorized replenishment with prices | allow | allow | ✓ |
| C harness | forecast, clean data | allow | allow | ✓ |
| C harness | catalog record incomplete (steward, contact) | block | block | ✓ |
| C harness | forecast horizon too short | block | block | ✓ |
| C harness | supplier risk, clean data | allow | allow | ✓ |
| C harness | empty QM usage decisions | block | block | ✓ |
| C harness | policy question, approved documents | allow | allow | ✓ |
| C harness | all supplier agreements expired | block | block | ✓ |
| C harness | weekly report, clean data | allow | allow | ✓ |
| C harness | missing warehouse (WH-C bin stock) | block | block | ✓ |
| C harness | warehouse manager: compare stock, clean data | allow | allow | ✓ |
| C harness | warehouse manager: compare stock, stale inventory (74 h, max 24 h) | block | block | ✓ |
| C harness | procurement manager: replenishment, clean data | allow | allow | ✓ |
| C harness | procurement manager: replenishment, duplicate POs | block | block | ✓ |
| C harness | quality manager: batch release, empty usage decisions | block | block | ✓ |
| C harness | production planner: production readiness, negative order qty | block | block | ✓ |
| C harness | logistics manager: plan shipments, clean data | allow | allow | ✓ |
| C harness | MLOps: evaluate forecast model, horizon too short | block | block | ✓ |
| C harness | SAP executor: record execution (no read access) | block | block | ✓ |
| C harness | director: weekly report, clean data | allow | allow | ✓ |
| C harness | planner WH-A/WH-B only: compare stock needs WH-C | block | block | ✓ |
| C harness | Aurelia demand: forecast needs MDG it may not read | block | block | ✓ |
| C harness | Aurelia warehouse: transfer policy needs documents it may not read | block | block | ✓ |
| C harness | knowledge viewer: transfer policy, approved documents | allow | allow | ✓ |

## Limits

* Synthetic data, demo identities and demo permissions; the same person wrote rules, data and labels. A perfect score shows the rules do what was intended, not that they are the right rules.
* Zero exposures/bypasses apply only to the tested paths. The SCInsight direct paths were not tested.
* Passing schema, completeness and freshness checks does not prove values are accurate (no reconciliation).
* Latency was measured on one machine, in-process, without concurrency or real connectors.
* RAG metrics beyond the demo retriever (citation coverage/support, relevance) are not measured.
