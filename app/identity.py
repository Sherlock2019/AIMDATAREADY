"""Where the user and calling-agent identities come from.

Demo mode: a "demo identity selector" picks a synthetic identity by NAME (e.g. {"user": "planner_all"}). Anybody can
send any name, so this is NOT authentication. Results are labelled authenticated=false, identity_mode=demo_selector.

Integration mode: identities must come from a trusted mechanism, for example
  * a JWT/OIDC access token validated by the API (issuer, audience, signature, expiry), giving the user id;
  * mTLS or a per-service credential identifying the calling agent (e.g. the Flowise Supervisor);
  * a token-exchange step so the agent acts on behalf of the user and both are visible.
That provider is NOT implemented in this PoC. NotConfiguredIdentityProvider returns no identity, so every readiness
check BLOCKS (fail closed) until a real provider is plugged in. Adding an API key to the demo does not change this.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from app.adapters.demo import DemoDirectory
from app.models import DemoIdentity, Principal

DEMO_IDENTITY_NOTICE = (
    "demo_selector: a synthetic identity chosen by name in the request. This is NOT authentication "
    "and must never be used outside a local demonstration."
)


class IdentityError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def resolve_demo_identity(
    selector: DemoIdentity | None, directory: DemoDirectory
) -> tuple[Principal | None, Principal | None]:
    """Map a demo selector to synthetic principals. No selector -> no identity (the engine will block)."""
    if selector is None:
        return None, None
    user = directory.users.get(selector.user)
    if user is None:
        raise IdentityError("unknown_demo_identity", f"unknown demo user selector {selector.user!r}")
    user_p = Principal(principal_id=user.user_id, kind="user", source="demo_selector", authenticated=False)
    agent_p = None
    if selector.agent is not None:
        agent = directory.agents.get(selector.agent)
        if agent is None:
            raise IdentityError("unknown_demo_identity", f"unknown demo agent selector {selector.agent!r}")
        agent_p = Principal(principal_id=agent.agent_id, kind="service", source="demo_selector", authenticated=False)
    return user_p, agent_p


class TrustedIdentityProvider(Protocol):
    """Interface for a real identity integration: derive principals from verified request credentials."""

    name: str

    def resolve(self, headers: Mapping[str, str]) -> tuple[Principal | None, Principal | None]: ...


class NotConfiguredIdentityProvider:
    """Integration-mode placeholder: no trusted provider is configured, so no identity is ever returned."""

    name = "not_configured"

    def resolve(self, headers: Mapping[str, str]) -> tuple[Principal | None, Principal | None]:
        return None, None
