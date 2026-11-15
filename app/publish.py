"""Certified data store: a data set is published only when the readiness gate says READY for a task.

    .data/pipeline/published/<asset>/v<N>/<asset>.csv    the certified rows (immutable)
    .data/pipeline/published/<asset>/v<N>/manifest.json  evaluation id, tasks it is certified for, contract, fixes, hash
    .data/pipeline/published/<asset>/current.json        pointer to the live version

Agents and the RAG index read ONLY published versions. Identical content is never published twice (same hash: the new
task is added to `certified_for`). BLOCKED data is never published.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any


class Publisher:
    def __init__(self, root: Path):
        self.root = Path(root)

    def current(self, asset: str) -> dict | None:
        path = self.root / asset / "current.json"
        return json.loads(path.read_text()) if path.exists() else None

    def path(self, asset: str) -> Path | None:
        cur = self.current(asset)
        return self.root / asset / f"v{cur['version']}" / f"{asset}.csv" if cur else None

    def publish(self, asset: str, src: Path, meta: dict[str, Any]) -> tuple[dict, bool]:
        """Returns (manifest, is_new_version)."""
        digest = hashlib.sha256(src.read_bytes()).hexdigest()[:16]
        cur = self.current(asset)
        if cur and cur["sha256"] == digest:
            if meta["task"] not in cur["certified_for"]:
                cur["certified_for"].append(meta["task"])
                self._write(asset, cur)
            return cur, False
        version = (cur["version"] + 1) if cur else 1
        folder = self.root / asset / f"v{version}"
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, folder / f"{asset}.csv")
        manifest = {"asset": asset, "version": version, "sha256": digest, "certified_for": [meta.pop("task")],
                    "file": f"published/{asset}/v{version}/{asset}.csv", **meta}  # fmt: skip
        self._write(asset, manifest)
        return manifest, True

    def _write(self, asset: str, manifest: dict) -> None:
        folder = self.root / asset / f"v{manifest['version']}"
        (folder / "manifest.json").write_text(json.dumps(manifest, indent=2))
        (self.root / asset / "current.json").write_text(json.dumps(manifest, indent=2))

    def list(self) -> list[dict]:
        return [m for m in (self.current(p.name) for p in sorted(self.root.glob("*")) if p.is_dir()) if m]

    def clear(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)
