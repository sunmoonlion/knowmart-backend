from __future__ import annotations

import time

import pytest

from app.application.errors.exceptions import (
    ForbiddenError,
    ServiceUnavailableError,
    UnauthorizedError,
)
from app.infrastructure.security.relation_auth import (
    RelationServiceVerifier,
    relation_binding,
)
from app.main import settings

ENV = {
    "SERVICE_AUTH_SUMMON_AUDIENCE": "sunmoon-investment-knowmart-summon",
    "SERVICE_AUTH_SUMMON_SUBJECT_ALLOWLIST": (
        "admin/sunmoonai-investment-knowmart-summon"
    ),
    "SERVICE_AUTH_SUMMON_REQUIRED_SCOPE": "knowmart:summon",
}


class FakeOidc:
    def __init__(self, **claims: object) -> None:
        now = int(time.time())
        self.claims = {
            "iss": "https://identity.example.test",
            "sub": "admin/sunmoonai-investment-knowmart-summon",
            "scope": "knowmart:summon",
            "iat": now,
            "exp": now + 900,
        } | claims
        self.audience: str | None = None

    async def verify_access_token(self, encoded: str, *, audience: str) -> dict:
        self.audience = audience
        return dict(self.claims)


def verifier(oidc: FakeOidc, env: dict | None = None) -> RelationServiceVerifier:
    return RelationServiceVerifier(
        "summon",
        settings=settings,
        oidc_client=oidc,
        environ=ENV if env is None else env,
    )


def test_binding_needs_all_three_and_the_matching_scope() -> None:
    assert relation_binding("summon", ENV) is not None
    assert relation_binding("deliver", ENV) is None
    assert (
        relation_binding(
            "summon", ENV | {"SERVICE_AUTH_SUMMON_REQUIRED_SCOPE": "knowmart:deliver"}
        )
        is None
    )
    assert (
        relation_binding("summon", ENV | {"SERVICE_AUTH_SUMMON_SUBJECT_ALLOWLIST": " "})
        is None
    )


@pytest.mark.asyncio
async def test_bound_subject_with_scope_is_accepted_with_the_callers_audience() -> None:
    oidc = FakeOidc()
    principal = await verifier(oidc).verify("token")
    assert oidc.audience == "sunmoon-investment-knowmart-summon"
    assert principal.actor_type == "service" and principal.surface == "internal"
    assert principal.scopes == frozenset({"knowmart:summon"})


@pytest.mark.asyncio
async def test_unconfigured_relation_is_unavailable() -> None:
    with pytest.raises(ServiceUnavailableError):
        await verifier(FakeOidc(), env={}).verify("token")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("claims", "error"),
    [
        ({"sub": "admin/sunmoonai-knowlab-knowmart-deliver"}, ForbiddenError),
        ({"scope": "openid"}, ForbiddenError),
        ({"scope": ""}, ForbiddenError),
        ({"scope": 7}, UnauthorizedError),
        ({"iat": "soon"}, UnauthorizedError),
        ({"iss": ""}, UnauthorizedError),
    ],
)
async def test_wrong_subject_scope_or_claims_are_rejected(claims: dict, error) -> None:
    with pytest.raises(error):
        await verifier(FakeOidc(**claims)).verify("token")


@pytest.mark.asyncio
async def test_empty_or_oversized_tokens_are_rejected() -> None:
    for token in ("", "x" * 16385):
        with pytest.raises(UnauthorizedError):
            await verifier(FakeOidc()).verify(token)
