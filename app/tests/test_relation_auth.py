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
    relation_bindings,
)
from app.main import settings

ENV = {
    "SERVICE_AUTH_SUMMON_CALLERS": "investment",
    "SERVICE_AUTH_SUMMON_INVESTMENT_AUDIENCE": "sunmoon-investment-knowmart-summon",
    "SERVICE_AUTH_SUMMON_INVESTMENT_SUBJECT_ALLOWLIST": (
        "admin/sunmoonai-investment-knowmart-summon"
    ),
    "SERVICE_AUTH_SUMMON_INVESTMENT_REQUIRED_SCOPE": "knowmart:summon",
}


class FakeOidc:
    def __init__(
        self, aud: str = "sunmoon-investment-knowmart-summon", **claims: object
    ) -> None:
        now = int(time.time())
        self.claims = {
            "iss": "https://identity.example.test",
            "sub": "admin/sunmoonai-investment-knowmart-summon",
            "scope": "knowmart:summon",
            "iat": now,
            "exp": now + 900,
        } | claims
        self.aud = aud
        self.tried: list[str] = []

    async def verify_access_token(self, encoded: str, *, audience: str) -> dict:
        self.tried.append(audience)
        if audience != self.aud:
            raise UnauthorizedError("wrong audience", code="audience_mismatch")
        return dict(self.claims)


def verifier(oidc: FakeOidc, env: dict | None = None) -> RelationServiceVerifier:
    return RelationServiceVerifier(
        "summon",
        settings=settings,
        oidc_client=oidc,
        environ=ENV if env is None else env,
    )


def test_bindings_need_all_three_and_the_matching_scope() -> None:
    assert [b.caller for b in relation_bindings("summon", ENV)] == ["investment"]
    assert relation_bindings("deliver", ENV) == []
    bad_scope = ENV | {
        "SERVICE_AUTH_SUMMON_INVESTMENT_REQUIRED_SCOPE": "knowmart:deliver"
    }
    assert relation_bindings("summon", bad_scope) == []
    no_subject = ENV | {"SERVICE_AUTH_SUMMON_INVESTMENT_SUBJECT_ALLOWLIST": " "}
    assert relation_bindings("summon", no_subject) == []
    assert relation_bindings("summon", ENV | {"SERVICE_AUTH_SUMMON_CALLERS": ""}) == []


@pytest.mark.asyncio
async def test_bound_subject_with_scope_is_accepted_with_the_callers_audience() -> None:
    oidc = FakeOidc()
    caller = await verifier(oidc).verify("token")
    assert caller.caller == "investment"
    assert oidc.tried == ["sunmoon-investment-knowmart-summon"]
    assert caller.principal.actor_type == "service"
    assert caller.principal.scopes == frozenset({"knowmart:summon"})


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


TWO_CALLERS = ENV | {
    "SERVICE_AUTH_SUMMON_CALLERS": "investment,other-runtime",
    "SERVICE_AUTH_SUMMON_OTHER_RUNTIME_AUDIENCE": (
        "sunmoon-other-runtime-knowmart-summon"
    ),
    "SERVICE_AUTH_SUMMON_OTHER_RUNTIME_SUBJECT_ALLOWLIST": (
        "admin/sunmoonai-other-runtime-knowmart-summon"
    ),
    "SERVICE_AUTH_SUMMON_OTHER_RUNTIME_REQUIRED_SCOPE": "knowmart:summon",
}


@pytest.mark.asyncio
async def test_the_caller_is_recognised_among_several() -> None:
    oidc = FakeOidc(
        aud="sunmoon-other-runtime-knowmart-summon",
        sub="admin/sunmoonai-other-runtime-knowmart-summon",
    )
    caller = await verifier(oidc, env=TWO_CALLERS).verify("token")
    assert caller.caller == "other-runtime"


@pytest.mark.asyncio
async def test_a_caller_cannot_use_another_callers_subject() -> None:
    # investment 的受众配上另一个运行方的主体：主体不在 investment 的名单里
    oidc = FakeOidc(sub="admin/sunmoonai-other-runtime-knowmart-summon")
    with pytest.raises(ForbiddenError):
        await verifier(oidc, env=TWO_CALLERS).verify("token")


@pytest.mark.asyncio
async def test_unknown_audience_is_rejected() -> None:
    with pytest.raises(UnauthorizedError):
        await verifier(FakeOidc(aud="someone-else")).verify("token")
