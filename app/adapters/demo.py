"""DemoAdapter: synthetic catalog + synthetic identities + named demo scenarios. Everything here is DEMO data.

* The catalog comes from catalog/demo_catalog.yaml.
* The "directory" of users and agents comes from catalog/demo_identities.yaml. Their permissions are demo values.
  This adapter does NOT enforce production ACLs: it only answers the readiness engine's questions so the decision
  logic can be demonstrated. The service that returns the data must still enforce authorisation itself.
* A demo scenario (catalog/demo_scenarios.yaml) patches the synthetic evidence to reproduce a problem: stale
  inventory, a missing warehouse, unavailable lead times, an authorisation service that does not answer, ...
"""

from __future__ import annotations

import copy
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.adapters.base import AdapterUnavailable, AuthzDecision, ReadinessAdapter
from app.catalog import Catalog, CatalogAsset, CatalogError, DocumentRecord, load_catalog, load_yaml
from app.models import Principal


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DemoUser(_Strict):
    user_id: str
    description: str = ""
    roles: list[str] = Field(default_factory=list)
    warehouse_scope: list[str] = Field(default_factory=list)
    purposes: list[str] = Field(default_factory=list)


class DemoAgent(_Strict):
    agent_id: str
    description: str = ""


class DemoDirectory(_Strict):
    notice: str
    users: dict[str, DemoUser]  # selector name -> synthetic user
    agents: dict[str, DemoAgent]  # selector name -> synthetic agent/service


class DemoScenario(_Strict):
    description: str
    asset_overrides: dict[str, dict] = Field(default_factory=dict)  # asset_id -> fields to replace (dicts are merged)
    remove_assets: list[str] = Field(default_factory=list)  # asset ids that are "not available"
    authorization_unavailable: bool = False  # the authorisation service does not answer
    catalog_unavailable: bool = False  # the catalog does not answer


def _merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class DemoAdapter(ReadinessAdapter):
    mode = "demo"

    def __init__(
        self,
        catalog: Catalog,
        directory: DemoDirectory,
        scenarios: dict[str, DemoScenario],
        scenario: str | None = None,
    ):
        self.catalog, self.directory, self.scenarios = catalog, directory, scenarios
        self.scenario_name = scenario
        self.scenario = scenarios[scenario] if scenario else DemoScenario(description="baseline")
        self._users_by_id = {u.user_id: u for u in directory.users.values()}
        self._agent_ids = {a.agent_id for a in directory.agents.values()}
        assets = {}
        for asset in catalog.assets:
            if asset.asset_id in self.scenario.remove_assets:
                continue
            patch = self.scenario.asset_overrides.get(asset.asset_id)
            if patch:
                merged = _merge(asset.model_dump(by_alias=True, mode="json"), patch)
                asset = CatalogAsset.model_validate(merged)
            assets[asset.asset_id] = asset
        self._assets = assets

    @classmethod
    def from_dir(cls, catalog_dir: Path) -> DemoAdapter:
        catalog_dir = Path(catalog_dir)
        catalog = load_catalog(catalog_dir / "demo_catalog.yaml")
        try:
            directory = DemoDirectory.model_validate(load_yaml(catalog_dir / "demo_identities.yaml"))
            raw = load_yaml(catalog_dir / "demo_scenarios.yaml").get("scenarios", {})
            scenarios = {name: DemoScenario.model_validate(body) for name, body in raw.items()}
        except ValueError as exc:  # pydantic ValidationError is a ValueError
            raise CatalogError(f"invalid demo identities or scenarios: {exc}") from exc
        return cls(catalog, directory, scenarios)

    def with_scenario(self, name: str | None) -> DemoAdapter:
        """A copy of this adapter with one named scenario applied (KeyError for an unknown name)."""
        if name is not None and name not in self.scenarios:
            raise KeyError(name)
        return DemoAdapter(self.catalog, self.directory, self.scenarios, name)

    # ------------------------------------------------------------------ catalog
    def get_asset(self, asset_id: str) -> CatalogAsset | None:
        if self.scenario.catalog_unavailable:
            raise AdapterUnavailable("demo catalog unavailable")
        return self._assets.get(asset_id)

    # ------------------------------------------------------------------ authorisation (DEMO permissions only)
    def authorize(
        self,
        principal: Principal,
        asset_id: str,
        purpose: str,
        scope: dict[str, list[str]],
        document: DocumentRecord | None = None,
    ) -> AuthzDecision:
        if self.scenario.authorization_unavailable:
            raise AdapterUnavailable("demo authorisation service unavailable")
        asset = self._assets.get(asset_id)
        if asset is None:
            return AuthzDecision(None, "the asset is unknown to the authorisation service")
        policy = asset.access

        if principal.kind == "service":
            if principal.principal_id not in self._agent_ids:
                return AuthzDecision(None, "the calling agent is unknown to the authorisation service")
            if principal.principal_id not in policy.agents:
                return AuthzDecision(False, "the calling agent is not permitted to use this data")
            return AuthzDecision(True, "agent permitted (demo policy)")

        user = self._users_by_id.get(principal.principal_id)
        if user is None:
            return AuthzDecision(None, "the user is unknown to the authorisation service")
        if not set(user.roles) & set(policy.roles):
            return AuthzDecision(False, "the user's role does not permit this data")
        if purpose not in policy.purposes or purpose not in user.purposes:
            return AuthzDecision(False, f"the purpose '{purpose}' is not permitted for this user and data")
        if document is not None:
            if not set(user.roles) & set(document.access_roles):
                return AuthzDecision(False, "the user's role does not permit this document")
            if purpose not in document.purposes:
                return AuthzDecision(False, f"the document may not be used for '{purpose}'")
        if policy.row_scope == "warehouse":
            requested = scope.get("warehouse", [])
            denied = tuple(w for w in requested if w not in user.warehouse_scope)
            if denied:
                return AuthzDecision(False, "the user is not permitted to see data for: " + ", ".join(denied), denied)
        return AuthzDecision(True, "user permitted (demo policy)")
