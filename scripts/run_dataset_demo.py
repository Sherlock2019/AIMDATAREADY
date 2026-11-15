"""Faulty-dataset demo: for each case, detect the fault, propose fixes (rules), apply them, re-check.

    .venv/bin/python scripts/run_dataset_demo.py                 # all 11 cases (1 clean + 10 faulty)
    .venv/bin/python scripts/run_dataset_demo.py --case 07       # one case, with every change explained
    .venv/bin/python scripts/run_dataset_demo.py --user planner_ab   # a user who may not see WH-C: nothing is "fixed"

Originals in datasets/cases/ are never modified; corrected copies are written to .data/datasets/<case>/.
Exit code 1 if a faulty case is not BLOCKED first or does not end READY (for planner_all).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.adapters.demo import DemoAdapter  # noqa: E402
from app.dataset_lab import DatasetLab  # noqa: E402
from app.identity import resolve_demo_identity  # noqa: E402
from app.models import DemoIdentity  # noqa: E402
from app.settings import ROOT, Settings, make_clock  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--case", help="case number or id, e.g. 07 or 07_invalid_status_codes")
    parser.add_argument("--user", default="planner_all", help="demo identity (planner_all, planner_ab, contractor)")
    args = parser.parse_args()
    settings = Settings.from_env()
    authz = DemoAdapter.from_dir(ROOT / "catalog")
    lab = DatasetLab(settings.datasets_dir, settings.datasets_work_dir, authz, make_clock(settings.clock), None)
    user, agent = resolve_demo_identity(DemoIdentity(user=args.user, agent="scinsight_supervisor"), authz.directory)
    ids = lab.case_ids()
    if args.case:
        ids = [i for i in ids if i == args.case or i.startswith(args.case.zfill(2) + "_")]
        if not ids:
            print(f"unknown case {args.case!r}; known: {', '.join(lab.case_ids())}")
            return 2
    bad = 0
    print(f"identity: {args.user} (DEMO selector, not authentication)  task: warehouse_transfer SKU-001 WH-A -> WH-C\n")
    print(f"{'case':30} {'before':9} {'detected':44} {'fixes applied':52} after")
    for cid in ids:
        r = lab.auto_correct(cid, user, agent, "demo_selector")
        detected = sorted(
            {
                f"{req}:{chk}"
                for req, cells in r["before"]["matrix"].items()
                for chk, cell in cells.items()
                if cell["status"] in ("FAIL", "ERROR")
            }
        )
        fixes = [c["action"] for p in r["passes"] for c in p["changes"]]
        before, after = r["before"]["decision"], r["after"]["decision"]
        print(f"{cid:30} {before:9} {', '.join(detected) or '-':44} {', '.join(fixes) or '-':52} {after}")
        if args.user == "planner_all" and (after != "READY" or (cid != "00_clean" and before != "BLOCKED")):
            bad += 1
        if len(ids) == 1:
            print(f"\ninjected fault: {r['case']['injected_fault']}")
            for reason in r["before"]["reasons"]:
                print(f"  engine said: {reason}")
            for n, p in enumerate(r["passes"], 1):
                for c in p["changes"]:
                    print(f"  pass {n}: {c['action']:30} {c['detail']}  [data from: {c['source']}]")
            for h in r["needs_human"]:
                print(f"  needs a person: {h['requirement']}: {h['rationale']}")
            print(f"  corrected copy: {settings.datasets_work_dir / cid}")
    print(f"\n{len(ids) - bad}/{len(ids)} cases ended as expected." if args.user == "planner_all" else "")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
