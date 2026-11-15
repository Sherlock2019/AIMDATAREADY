---
doc_id: POL-WH-020
version: "1.4"
title: Warehouse transfer policy
groups: [operations, management, knowledge_readers, knowledge_admins]
---
SIMULATED document for the demo. Fictional company rules.

## 1. Scope
This policy applies to stock transfers of finished cosmetics products between the company warehouses WH-A (Paris),
WH-B (Madrid) and WH-C (Milan).

## 2. When a transfer is allowed
A transfer may be proposed when the destination warehouse holds less than two weeks of forecast demand in usable
stock and the origin warehouse still holds at least three weeks of forecast demand after the transfer. Only released
batches that are not expired and have at least 180 days of remaining shelf life may be transferred.

## 3. Approval of transfers
Transfers up to 2,000 units are approved by the warehouse manager of the origin warehouse. Transfers above 2,000
units, or cross-border transfers of temperature-sensitive products, are approved by the supply-chain director. AI
agents may recommend a transfer but may never approve or execute it.

## 4. Lanes, lead time and cost
Transfers use the planned lanes in SAP TM. Road lead time is two to three days. A planned cost above 0.50 EUR per
unit requires sign-off by the logistics manager.

## 5. When transfers are not allowed
Batches in quality inspection, blocked batches and batches reserved for customer orders may not be transferred. No
transfers are planned during stock-count weeks.
