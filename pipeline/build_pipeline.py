"""Generate the SIMULATED SAP systems of record (synthetic CSVs + manifest) for the pipeline demo.

.venv/bin/python pipeline/build_pipeline.py      -> pipeline/sap_source/, pipeline/steward_registry.yaml
"""

from __future__ import annotations

import csv
import shutil
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
OUT = HERE / "sap_source"
PRODUCTS = [
    ("SKU-001", "001", "Vitamin C serum 30 ml"),
    ("SKU-002", "002", "Hydrating cream 50 ml"),
    ("SUN-SPF50-100", "SUN", "Sunscreen SPF50 100 ml"),
]
WAREHOUSES = ["WH-A", "WH-B", "WH-C"]
WEEKS = ["2026-11-09", "2026-11-16", "2026-11-23", "2026-11-30", "2026-12-07"]
LANES = [
    ("WH-A", "WH-B", 3),
    ("WH-A", "WH-C", 2),
    ("WH-B", "WH-A", 3),
    ("WH-B", "WH-C", 2),
    ("WH-C", "WH-A", 2),
    ("WH-C", "WH-B", 2),
]


def stock():
    rows = []
    for pi, (sku, short, _) in enumerate(PRODUCTS):
        for wi, wh in enumerate(WAREHOUSES):
            for n in (1, 2):
                rows.append(
                    {
                        "product_id": sku,
                        "warehouse_id": wh,
                        "batch_id": f"{wh[-1]}-{short}-{n}",
                        "quantity": str(400 + 150 * ((pi * 7 + wi * 3 + n) % 9)),
                        "uom": "EA",
                        "status": "blocked" if (pi, wi, n) == (1, 1, 2) else "released",
                        "expiry_date": f"2027-{1 + (pi * 3 + wi + n) % 12:02d}-28",
                    }
                )
    return rows


def tables():
    mm = stock()
    ewm = [
        {
            "warehouse_id": r["warehouse_id"],
            "bin_id": f"{r['warehouse_id'][-1]}-0{i % 4 + 1}-{i % 3 + 1}",
            "product_id": r["product_id"],
            "batch_id": r["batch_id"],
            "quantity": r["quantity"],
            "bin_status": "blocked" if r["status"] == "blocked" else "available",
        }
        for i, r in enumerate(mm)
    ]
    tm = [
        {
            "origin_warehouse": o,
            "destination_warehouse": d,
            "mode": "road",
            "lead_time_days": str(days),
            "cost_per_unit_eur": f"{0.25 + 0.05 * days:.2f}",
        }
        for o, d, days in LANES
    ]
    ibp = [
        {
            "product_id": sku,
            "warehouse_id": wh,
            "week_start": w,
            "forecast_units": str(300 + 40 * ((pi + wi + k) % 7)),
            "model_version": "ibp-baseline-2.3",
        }
        for pi, (sku, _, _) in enumerate(PRODUCTS)
        for wi, wh in enumerate(WAREHOUSES)
        for k, w in enumerate(WEEKS)
    ]
    pp = [
        {
            "order_id": f"PP-{1001 + i}",
            "product_id": PRODUCTS[i % 3][0],
            "plant": "FAC-1",
            "quantity": str(2000 + 500 * i),
            "due_date": f"2026-11-{18 + 2 * i}",
            "status": "released" if i < 3 else "planned",
        }
        for i in range(6)
    ]
    sd = [
        {
            "order_id": f"SO-{88001 + i}",
            "product_id": PRODUCTS[i % 3][0],
            "warehouse_id": WAREHOUSES[i // 3],
            "quantity": str(150 + 25 * i),
            "requested_date": f"2026-11-{16 + i}",
            "status": "open",
        }
        for i in range(9)
    ]
    ariba = [
        {
            "po_id": f"PO-450000{i + 1}",
            "supplier_id": ["SUP-A", "SUP-C", "SUP-D", "SUP-E", "SUP-F", "SUP-B"][i],
            "material_id": ["PUMP-DISP-24", "BTL-GLS-30", "SER-BASE-VC", "CRT-SER-30", "TUB-SUN-100", "CAP-PUMP-OVR"][
                i
            ],
            "quantity": str(5000 + 1000 * i),
            "price_eur": f"{[0.42, 0.31, 38.0, 0.09, 0.18, 0.05][i]:.2f}",
            "due_date": f"2026-11-{20 + i}",
            "status": "confirmed" if i % 2 else "ordered",
        }
        for i in range(6)
    ]
    qm = [
        {
            "lot_id": f"IL-{i + 1:04d}",
            "product_id": r["product_id"],
            "batch_id": r["batch_id"],
            "result": "fail" if r["status"] == "blocked" else "pass",
            "usage_decision": "rejected" if r["status"] == "blocked" else "accepted",
            "decided_on": "2026-11-10",
        }
        for i, r in enumerate(mm)
    ]
    mdg = [
        {"product_id": sku, "description": name, "uom": "EA", "shelf_life_days": "720", "status": "active"}
        for sku, _, name in PRODUCTS
    ]
    docs = [  # supplier agreements and purchasing policy (document collection)
        {
            "doc_id": "AGR-SUP-A",
            "title": "Supplier agreement - Alpine Dispensing",
            "version": "2.0",
            "source_id": "ariba://contracts/AGR-SUP-A/v2.0",
            "status": "approved",
            "valid_from": "2026-01-01",
            "valid_to": "2027-12-31",
            "supplier_id": "SUP-A",
        },
        {
            "doc_id": "AGR-SUP-C",
            "title": "Supplier agreement - Nordic Glassworks",
            "version": "1.3",
            "source_id": "ariba://contracts/AGR-SUP-C/v1.3",
            "status": "approved",
            "valid_from": "2026-03-01",
            "valid_to": "2027-02-28",
            "supplier_id": "SUP-C",
        },
        {
            "doc_id": "POL-PURCH-001",
            "title": "Purchasing policy",
            "version": "4.1",
            "source_id": "ariba://policies/POL-PURCH-001/v4.1",
            "status": "approved",
            "valid_from": "2026-01-01",
            "valid_to": "",
            "supplier_id": "",
        },
        {
            "doc_id": "POL-WH-020",
            "title": "Warehouse transfer policy",
            "version": "1.4",
            "source_id": "dms://policies/POL-WH-020/v1.4",
            "status": "approved",
            "valid_from": "2026-03-01",
            "valid_to": "",
            "supplier_id": "",
        },
        {
            "doc_id": "AGR-SUP-A-V1",
            "title": "Supplier agreement - Alpine Dispensing (old)",
            "version": "1.0",
            "source_id": "ariba://contracts/AGR-SUP-A/v1.0",
            "status": "superseded",
            "valid_from": "2024-01-01",
            "valid_to": "2025-12-31",
            "supplier_id": "SUP-A",
        },
    ]
    return {
        "ariba_contracts": docs,
        "s4_mm_stock": mm,
        "ewm_bin_stock": ewm,
        "tm_lanes": tm,
        "ibp_demand_forecast": ibp,
        "s4_pp_orders": pp,
        "s4_sd_orders": sd,
        "ariba_pos": ariba,
        "qm_inspection_lots": qm,
        "mdg_material_master": mdg,
    }


META = {  # (unused owners), source version, source event time, ingestion time - owners live in catalog.yaml
    "ariba_contracts": ("", "", "ARIBA-CONTRACTS-2026-11-02", "2026-11-02T00:00", "2026-11-02T03:00"),
    "s4_mm_stock": (
        "Demo Inventory Control",
        "Demo SAP Basis team",
        "S4-MM-2026-11-14",
        "2026-11-14T06:00",
        "2026-11-14T06:20",
    ),
    "ewm_bin_stock": ("Demo Warehouse Ops", "Demo EWM team", "EWM-2026-11-14", "2026-11-14T07:00", "2026-11-14T07:05"),
    "tm_lanes": ("Demo Logistics", "Demo TM team", "TM-LANES-2026-11", "2026-11-01T00:00", "2026-11-01T02:00"),
    "ibp_demand_forecast": (
        "Demo Demand Planning",
        "Demo IBP team",
        "IBP-2026-W46",
        "2026-11-13T00:00",
        "2026-11-13T01:00",
    ),
    "s4_pp_orders": (
        "Demo Production Planning",
        "Demo SAP Basis team",
        "S4-PP-2026-11-14",
        "2026-11-14T05:00",
        "2026-11-14T05:10",
    ),
    "s4_sd_orders": (
        "Demo Sales Ops",
        "Demo SAP Basis team",
        "S4-SD-2026-11-14",
        "2026-11-14T06:30",
        "2026-11-14T06:35",
    ),
    "ariba_pos": ("Demo Procurement", "Demo Ariba team", "ARIBA-2026-11-13", "2026-11-13T20:00", "2026-11-13T20:30"),
    "qm_inspection_lots": ("Demo Quality", "Demo QM team", "QM-2026-11-14", "2026-11-14T04:00", "2026-11-14T04:10"),
    "mdg_material_master": (
        "Demo Master Data Office",
        "Demo MDG team",
        "MDG-2026-10-30",
        "2026-10-30T00:00",
        "2026-10-30T01:00",
    ),
}


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    assets = {}
    for asset, rows in tables().items():
        with (OUT / f"{asset}.csv").open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0]), lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        _, _, version, event, ingested = META[asset]
        assets[asset] = {  # dynamic extraction facts only; static catalog fields come from catalog.yaml
            "file": f"{asset}.csv",
            "source_name": f"SIMULATED {asset}",
            "source_version": version,
            "source_event_time": f"{event}:00+00:00",
            "ingested_at": f"{ingested}:00+00:00",
        }
    manifest = {
        "dataset": "sap_source",
        "synthetic": True,
        "notice": "SIMULATED SAP systems of record.",
        "assets": assets,
    }
    (OUT / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    import sys

    sys.path.insert(0, str(HERE.parent))
    from app.odcs import OdcsCatalog

    catalog = (
        OdcsCatalog(HERE / "odcs").catalog  # single source of truth
        if OdcsCatalog.exists(HERE / "odcs")
        else yaml.safe_load((HERE / "catalog.yaml").read_text())["assets"]
    )
    fields = ("business_owner", "technical_owner", "data_steward", "contact")
    registry = {  # governed owner registry: the only place missing ownership may be filled from
        "registry_version": "steward-registry-2026-11-01",
        "assets": {a: {f: catalog[a][f] for f in fields} for a in assets},
    }
    (HERE / "steward_registry.yaml").write_text(yaml.safe_dump(registry, sort_keys=False))
    print(f"wrote {len(assets)} simulated SAP tables to {OUT}")


if __name__ == "__main__":
    main()
