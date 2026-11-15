"""One-time migration: pipeline/catalog.yaml + pipeline/contracts.yaml -> pipeline/odcs/<asset>.odcs.yaml.

After the migration the ODCS files are the single source of truth (the service reads them). Each file is validated
against the official ODCS v3.1.0 JSON Schema and converted back to prove nothing was lost.

    .venv/bin/python scripts/export_odcs.py            # writes pipeline/odcs/*.odcs.yaml
    .venv/bin/python scripts/export_odcs.py --check    # validate the existing ODCS files only
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.odcs import OdcsCatalog, dump, from_odcs, to_odcs, validate  # noqa: E402

OUT = ROOT / "pipeline" / "odcs"


def main() -> int:
    if "--check" in sys.argv:
        store = OdcsCatalog(OUT)
        bad = {a: e for a, e in store.errors.items() if e}
        for asset, errors in bad.items():
            print(f"INVALID {asset}: " + "; ".join(errors[:5]))
        print(f"{len(store.catalog)} ODCS file(s), {len(bad)} invalid")
        return 1 if bad else 0
    catalog = yaml.safe_load((ROOT / "pipeline" / "catalog.yaml").read_text())["assets"]
    contracts = yaml.safe_load((ROOT / "pipeline" / "contracts.yaml").read_text())["contracts"]
    failed = 0
    for asset in catalog:
        doc = to_odcs(asset, catalog[asset], contracts[asset])
        errors = validate(doc)
        back = from_odcs(yaml.safe_load(dump(asset, doc)))
        lossless = back == (asset, catalog[asset], contracts[asset])
        (OUT / f"{asset}.odcs.yaml").write_text(dump(asset, doc))
        failed += bool(errors) or not lossless
        print(f"{asset:22s} schema {'valid' if not errors else 'INVALID: ' + '; '.join(errors[:3])} | "
              f"round trip {'lossless' if lossless else 'LOSSY'}")  # fmt: skip
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
