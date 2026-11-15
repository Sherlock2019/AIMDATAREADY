"""Shared fixtures. Every test uses a FIXED clock so results never depend on today's date."""

from __future__ import annotations

import os
from datetime import UTC, datetime

os.environ.setdefault("TDR_RAG_EMBEDDING", "hashing")  # no model download in tests (deterministic, offline)

import pytest
from fastapi.testclient import TestClient

from app.adapters.demo import DemoAdapter
from app.audit import MemoryAuditSink
from app.engine import ReadinessEngine
from app.identity import resolve_demo_identity
from app.main import create_app
from app.models import DemoIdentity
from app.profiles import load_profiles
from app.settings import ROOT, Settings

NOW = datetime(2026, 11, 14, 8, 0, tzinfo=UTC)
TRANSFER_CONTEXT = {
    "product_id": "SKU-001",
    "origin_warehouse": "WH-A",
    "destination_warehouse": "WH-C",
    "planning_period": "2026-11-16/2026-11-29",
}


@pytest.fixture
def profiles():
    return load_profiles(ROOT / "profiles")


@pytest.fixture
def adapter():
    return DemoAdapter.from_dir(ROOT / "catalog")


@pytest.fixture
def audit():
    return MemoryAuditSink()


@pytest.fixture
def engine(profiles, adapter, audit):
    return ReadinessEngine(profiles, adapter, clock=lambda: NOW, audit=audit)


@pytest.fixture
def principals(adapter):
    def make(user: str = "planner_all", agent: str | None = "scinsight_supervisor"):
        return resolve_demo_identity(DemoIdentity(user=user, agent=agent), adapter.directory)

    return make


@pytest.fixture
def client(audit):
    return TestClient(create_app(Settings(audit_path=None), clock=lambda: NOW, audit=audit))


@pytest.fixture
def integration_client():
    return TestClient(create_app(Settings(mode="integration", audit_path=None), clock=lambda: NOW))
