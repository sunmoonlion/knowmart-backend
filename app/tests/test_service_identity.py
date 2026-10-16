from __future__ import annotations

import time

import pytest

from app.application.errors.exceptions import ForbiddenError
from app.infrastructure.security.service_identity import ServiceIdentityVerifier
from core.config import Settings


class FakeOidcClient:
    def __init__(self, claims: dict[str, object]) -> None:
        self.claims = claims
        self.audience: str | None = None

    async def verify_access_token(
        self, _encoded: str, *, audience: str
    ) -> dict[str, object]:
        self.audience = audience
        return self.claims


def settings() -> Settings:
    return Settings(
        _env_file=None,
        casdoor_endpoint="https://identity.example.test",
        service_auth_audience="knowbase-internal",
        service_auth_subject_bindings_json=(
            '{"research-agent-worker":["knowbase:retrieve","profile:read"]}'
        ),
    )


def claims(**overrides: object) -> dict[str, object]:
    now = int(time.time())
    result: dict[str, object] = {
        "iss": "https://identity.example.test",
        "sub": "research-agent-worker",
        "aud": "knowbase-internal",
        "iat": now,
        "exp": now + 300,
        "scope": "knowbase:retrieve",
    }
    result.update(overrides)
    return result


@pytest.mark.asyncio
async def test_service_identity_enforces_subject_and_scope_binding() -> None:
    fake = FakeOidcClient(claims())
    verifier = ServiceIdentityVerifier(settings(), oidc_client=fake)  # type: ignore[arg-type]
    principal = await verifier.verify(
        "signed-token", required_scopes=frozenset({"knowbase:retrieve"})
    )
    assert principal.actor_type == "service"
    assert principal.surface == "internal"
    assert principal.subject == "research-agent-worker"
    assert fake.audience == "knowbase-internal"


@pytest.mark.asyncio
async def test_service_identity_rejects_unbound_or_escalated_scope() -> None:
    unbound = ServiceIdentityVerifier(  # type: ignore[arg-type]
        settings(), oidc_client=FakeOidcClient(claims(sub="unknown-worker"))
    )
    with pytest.raises(ForbiddenError) as error:
        await unbound.verify("signed-token", required_scopes=frozenset())
    assert error.value.code == "service_subject_unbound"

    escalated = ServiceIdentityVerifier(  # type: ignore[arg-type]
        settings(),
        oidc_client=FakeOidcClient(claims(scope="knowbase:retrieve knowbase:admin")),
    )
    with pytest.raises(ForbiddenError) as error:
        await escalated.verify(
            "signed-token", required_scopes=frozenset({"knowbase:retrieve"})
        )
    assert error.value.code == "service_scope_denied"
