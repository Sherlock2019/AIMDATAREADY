"""Generate the SYNTHETIC datasets for the faulty-data demo (deterministic: same files every run).

    .venv/bin/python datasets/build_datasets.py

Writes
  datasets/source_of_record/   the clean, current extract of the (fictional) system of record
  datasets/steward_registry.yaml   governed list of data owners (used to fill missing ownership, never invented)
  datasets/cases/00_clean/     the dataset as it should be
  datasets/cases/01..10_*/     the same dataset with ONE injected fault each, plus fault.yaml describing it

fault.yaml is for humans and tests only. The readiness engine never reads it: it must find the fault in the data.
"""

from __future__ import annotations

import copy
import csv
import shutil
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
PRODUCTS = [("SKU-001", "001"), ("SKU-002", "002"), ("SUN-SPF50-100", "SUN")]
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
INV_HEADER = ["product_id", "warehouse_id", "batch_id", "quantity", "uom", "status", "expiry_date"]
FC_HEADER = ["product_id", "warehouse_id", "week_start", "forecast_units", "model_version"]
LT_HEADER = ["origin_warehouse", "destination_warehouse", "mode", "lead_time_days", "cost_per_unit_eur"]
SPECIAL_STATUS = {
    ("SKU-002", "WH-B", 2): "blocked",
    ("SUN-SPF50-100", "WH-C", 1): "reserved",
    ("SKU-001", "WH-B", 2): "quality_inspection",
}

SOURCE_META = {
    "sap_inventory_snapshot": {
        "file": "inventory.csv",
        "business_owner": "Demo Inventory Control (fictional)",
        "technical_owner": "Demo Data Platform team (fictional)",
        "source_name": "S4HANA-SYNTHETIC / MARD+MCHB extract",
        "source_version": "SAP-SYN-2026-11-14",
        "source_event_time": "2026-11-14T06:00:00+00:00",
        "ingested_at": "2026-11-14T06:30:00+00:00",
        "contract_ref": "contracts/inventory_snapshot@1.2 (demo reference)",
        "known_limitations": ["Synthetic data; quantities are illustrative."],
    },
    "demand_forecast_weekly": {
        "file": "demand_forecast.csv",
        "business_owner": "Demo Demand Planning (fictional)",
        "technical_owner": "Demo Data Science team (fictional)",
        "source_name": "demo-forecast-job",
        "source_version": "FCST-2026-W46",
        "source_event_time": "2026-11-13T00:00:00+00:00",
        "ingested_at": "2026-11-13T01:00:00+00:00",
        "known_limitations": ["Baseline forecast; promotions are not modelled."],
    },
    "transfer_lead_times": {
        "file": "transfer_lead_times.csv",
        "business_owner": "Demo Logistics (fictional)",
        "technical_owner": "Demo Logistics IT (fictional)",
        "source_name": "demo-tm-lanes",
        "source_version": "LANES-2026-11",
        "source_event_time": "2026-11-01T00:00:00+00:00",
        "ingested_at": "2026-11-01T02:00:00+00:00",
        "known_limitations": ["Planned lead times, not measured transit times."],
    },
}


def inventory_rows() -> list[dict]:
    rows = []
    for pi, (sku, short) in enumerate(PRODUCTS):
        for wi, wh in enumerate(WAREHOUSES):
            for n in (1, 2):
                rows.append(
                    {
                        "product_id": sku,
                        "warehouse_id": wh,
                        "batch_id": f"{wh[-1]}-{short}-{n}",
                        "quantity": str(400 + 150 * ((pi * 7 + wi * 3 + n) % 9)),
                        "uom": "EA",
                        "status": SPECIAL_STATUS.get((sku, wh, n), "released"),
                        "expiry_date": f"2027-{1 + (pi * 3 + wi + n) % 12:02d}-28",
                    }
                )
    return rows


def forecast_rows() -> list[dict]:
    return [
        {
            "product_id": sku,
            "warehouse_id": wh,
            "week_start": week,
            "forecast_units": str(300 + 40 * ((pi + wi * 2 + k) % 7)),
            "model_version": "baseline-1.0",
        }
        for pi, (sku, _) in enumerate(PRODUCTS)
        for wi, wh in enumerate(WAREHOUSES)
        for k, week in enumerate(WEEKS)
    ]


def lane_rows() -> list[dict]:
    return [
        {
            "origin_warehouse": o,
            "destination_warehouse": d,
            "mode": "road",
            "lead_time_days": str(days),
            "cost_per_unit_eur": f"{0.25 + 0.05 * days:.2f}",
        }
        for o, d, days in LANES
    ]


def write_csv(path: Path, header: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_dataset(folder: Path, data: dict, meta: dict, title: str, fault: dict | None = None) -> None:
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    for name, (header, rows) in data.items():
        write_csv(folder / name, header, rows)
    manifest = {
        "dataset": folder.name,
        "title": title,
        "synthetic": True,
        "notice": "SYNTHETIC demo data. Not connected to SAP or any production system.",
        "assets": meta,
    }
    (folder / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True))
    if fault:
        (folder / "fault.yaml").write_text(yaml.safe_dump(fault, sort_keys=False, allow_unicode=True))


def clean() -> tuple[dict, dict]:
    data = {
        "inventory.csv": (list(INV_HEADER), inventory_rows()),
        "demand_forecast.csv": (list(FC_HEADER), forecast_rows()),
        "transfer_lead_times.csv": (list(LT_HEADER), lane_rows()),
    }
    return data, copy.deepcopy(SOURCE_META)


def row(rows: list[dict], batch: str) -> dict:
    return next(r for r in rows if r["batch_id"] == batch)


def build_cases() -> list[tuple[str, str, dict, dict, dict]]:
    """(folder, title, data, meta, fault) for each case."""
    cases = []

    def case(folder, title, mutate, injected, detected, fix):
        data, meta = clean()
        mutate(data, meta)
        fault = {"title": title, "injected_fault": injected, "expected_detection": detected, "expected_fix": fix}
        cases.append((folder, title, data, meta, fault))

    inv = lambda d: d["inventory.csv"][1]  # noqa: E731

    def stale(d, m):
        for r in inv(d):
            r["quantity"] = str(int(r["quantity"]) - 50)  # older stock position
        m["sap_inventory_snapshot"].update(
            source_version="SAP-SYN-2026-11-11",
            source_event_time="2026-11-11T06:00:00+00:00",
            ingested_at="2026-11-14T07:30:00+00:00",
        )

    case(
        "01_stale_inventory",
        "Stale inventory snapshot (re-ingested today)",
        stale,
        "Inventory is the 2026-11-11 snapshot, re-loaded 30 minutes ago.",
        ["inventory:freshness"],
        ["refresh_from_source"],
    )

    def drop_wh_c(d, m):
        d["inventory.csv"] = (d["inventory.csv"][0], [r for r in inv(d) if r["warehouse_id"] != "WH-C"])

    case(
        "02_missing_warehouse_c",
        "Warehouse C rows missing",
        drop_wh_c,
        "The load dropped every WH-C inventory row.",
        ["inventory:coverage"],
        ["reextract_rows"],
    )

    def drop_col(d, m):
        d["inventory.csv"] = ([c for c in INV_HEADER if c != "expiry_date"], inv(d))

    case(
        "03_missing_column",
        "expiry_date column missing",
        drop_col,
        "The inventory extract lost its expiry_date column.",
        ["inventory:schema", "inventory:quality"],
        ["restore_columns"],
    )

    def dupes(d, m):
        rows = inv(d)
        rows.append(dict(row(rows, "A-001-1")))  # exact duplicate
        conflict = dict(row(rows, "C-001-1"))
        conflict["quantity"] = "9999"  # same batch, different quantity
        rows.append(conflict)

    case(
        "04_duplicate_rows",
        "Duplicate batch rows",
        dupes,
        "Batch A-001-1 appears twice; batch C-001-1 appears twice with different quantities.",
        ["inventory:quality"],
        ["deduplicate"],
    )

    def negative(d, m):
        rows = inv(d)
        row(rows, "A-001-2")["quantity"] = "-120"
        row(rows, "C-001-1")["quantity"] = "-40"

    case(
        "05_negative_quantity",
        "Negative stock quantities",
        negative,
        "Two batches have negative quantities.",
        ["inventory:quality"],
        ["replace_bad_rows_from_source"],
    )

    def nulls(d, m):
        rows = inv(d)
        row(rows, "A-001-1")["status"] = ""
        row(rows, "C-001-2")["quantity"] = ""

    case(
        "06_null_values",
        "Empty required values",
        nulls,
        "One batch has no status, another has no quantity.",
        ["inventory:quality"],
        ["replace_bad_rows_from_source"],
    )

    def codes(d, m):
        rows = inv(d)
        row(rows, "A-001-1")["status"] = "Released"
        row(rows, "A-001-2")["status"] = " released "
        row(rows, "C-001-1")["status"] = "ok"

    case(
        "07_invalid_status_codes",
        "Non-standard status codes",
        codes,
        "Status codes 'Released' and ' released ' (fixable by normalising) and 'ok' (unknown).",
        ["inventory:quality"],
        ["normalise_codes", "replace_bad_rows_from_source"],
    )

    def dates(d, m):
        for r in inv(d):
            if r["product_id"] == "SKU-001" and r["warehouse_id"] in ("WH-A", "WH-C"):
                y, mo, day = r["expiry_date"].split("-")
                r["expiry_date"] = f"{day}/{mo}/{y}"
        m["sap_inventory_snapshot"]["export_settings"] = {"date_format": "%d/%m/%Y"}

    case(
        "08_bad_date_format",
        "Expiry dates in DD/MM/YYYY",
        dates,
        "SKU-001 expiry dates in WH-A and WH-C were exported as DD/MM/YYYY (the export declares this format).",
        ["inventory:quality"],
        ["reformat_dates"],
    )

    def lane(d, m):
        header, rows = d["transfer_lead_times.csv"]
        d["transfer_lead_times.csv"] = (
            header,
            [r for r in rows if (r["origin_warehouse"], r["destination_warehouse"]) != ("WH-A", "WH-C")],
        )

    case(
        "09_missing_lane",
        "Lead time WH-A > WH-C missing",
        lane,
        "The lane table has no WH-A > WH-C row.",
        ["transfer_lead_times:coverage"],
        ["reextract_rows"],
    )

    def owner(d, m):
        m["sap_inventory_snapshot"]["business_owner"] = None
        m["sap_inventory_snapshot"]["technical_owner"] = None

    case(
        "10_missing_owner",
        "Inventory owners not recorded",
        owner,
        "The inventory record has no business or technical owner.",
        ["inventory:provenance"],
        ["lookup_owner"],
    )
    return cases


def main() -> None:
    data, meta = clean()
    write_dataset(HERE / "source_of_record", data, meta, "Source of record (clean, current extract)")
    registry = {
        "registry_version": "steward-registry-2026-11-01",
        "notice": "Synthetic governed owner registry.",
        "assets": {
            a: {"business_owner": m["business_owner"], "technical_owner": m["technical_owner"]}
            for a, m in SOURCE_META.items()
        },
    }
    (HERE / "steward_registry.yaml").write_text(yaml.safe_dump(registry, sort_keys=False))
    cases_dir = HERE / "cases"
    data, meta = clean()
    write_dataset(
        cases_dir / "00_clean",
        data,
        meta,
        "Clean dataset (control case)",
        {
            "title": "Clean dataset (control case)",
            "injected_fault": "none",
            "expected_detection": [],
            "expected_fix": [],
        },
    )
    for folder, title, data, meta, fault in build_cases():
        write_dataset(cases_dir / folder, data, meta, title, fault)
    print(f"wrote source_of_record, steward_registry.yaml and {len(build_cases()) + 1} cases in {cases_dir}")


if __name__ == "__main__":
    main()
