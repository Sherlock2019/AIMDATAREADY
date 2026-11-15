"""Runtime settings from environment variables (see .env.example). No secrets are needed for the demo."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DEMO_CLOCK = "fixed:2026-11-14T08:00:00+00:00"


@dataclass(frozen=True)
class Settings:
    mode: str = "demo"  # demo | integration
    clock: str = DEFAULT_DEMO_CLOCK  # fixed:<ISO time with offset> | system
    profiles_dir: Path = ROOT / "profiles"
    catalog_dir: Path = ROOT / "catalog"
    audit_path: Path | None = ROOT / ".data" / "audit.jsonl"
    datasets_dir: Path = ROOT / "datasets"  # faulty-dataset lab: originals, source of record, profile
    datasets_work_dir: Path = ROOT / ".data" / "datasets"  # working copies that fixes are applied to
    pipeline_work_dir: Path = ROOT / ".data" / "pipeline"  # SAP pipeline staging + rounds (Flowise demo)

    @property
    def demo_mode(self) -> bool:
        return self.mode == "demo"

    @classmethod
    def from_env(cls) -> Settings:
        mode = os.environ.get("TDR_MODE", "demo").strip().lower()
        if mode not in {"demo", "integration"}:
            raise ValueError("TDR_MODE must be 'demo' or 'integration'")
        audit = os.environ.get("TDR_AUDIT_PATH", str(ROOT / ".data" / "audit.jsonl")).strip()
        return cls(
            mode=mode,
            clock=os.environ.get("TDR_CLOCK", DEFAULT_DEMO_CLOCK if mode == "demo" else "system").strip(),
            profiles_dir=Path(os.environ.get("TDR_PROFILES_DIR", ROOT / "profiles")),
            catalog_dir=Path(os.environ.get("TDR_CATALOG_DIR", ROOT / "catalog")),
            audit_path=Path(audit) if audit else None,
            datasets_dir=Path(os.environ.get("TDR_DATASETS_DIR", ROOT / "datasets")),
            datasets_work_dir=Path(os.environ.get("TDR_DATASETS_WORK_DIR", ROOT / ".data" / "datasets")),
        )


def make_clock(spec: str):
    """'system' -> real UTC time; 'fixed:2026-11-14T08:00:00+00:00' -> always that instant (repeatable demos)."""
    if spec == "system":
        return lambda: datetime.now(UTC)
    if spec.startswith("fixed:"):
        instant = datetime.fromisoformat(spec[len("fixed:") :])
        if instant.tzinfo is None:
            raise ValueError("TDR_CLOCK fixed time must include a timezone offset")
        return lambda: instant
    raise ValueError("TDR_CLOCK must be 'system' or 'fixed:<ISO-8601 time with offset>'")
