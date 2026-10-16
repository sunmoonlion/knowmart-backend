from __future__ import annotations

import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey

from app.application.errors.exceptions import (
    ServiceUnavailableError,
    UnauthorizedError,
)
from app.infrastructure.security.oidc import OidcProviderClient
from core.config import Settings


def settings() -> Settings:
    return Settings(
        _env_file=None,
        env="production",
        casdoor_endpoint="https://identity.example.test",
        admin_casdoor_client_id="knowmart-admin-client",
        admin_casdoor_client_secret="admin-secret",
        admin_casdoor_redirect_uri="https://admin.example.test/api/auth/admin/callback",
        admin_frontend_base_url="https://admin.example.test",
        admin_frontend_allowed_origins="https://admin.example.test",
        web_casdoor_client_id="knowmart-web-client",
        web_casdoor_client_secret="web-secret",
        web_casdoor_redirect_uri="https://web.example.test/api/auth/web/callback",
        web_frontend_base_url="https://web.example.test",
        web_frontend_allowed_origins="https://web.example.test",
        allowed_hosts="admin.example.test,web.example.test",
    )


def token(key: RSAKey, audience: str, **overrides: object) -> str:
    now = int(time.time())
    claims: dict[str, object] = {
        "iss": "https://identity.example.test",
        "sub": "user-123",
        "aud": audience,
        "iat": now,
        "exp": now + 300,
        "nonce": "nonce-123",
    }
    claims.update(overrides)
    return jwt.encode(
        {"alg": "RS256", "kid": "test-key"},
        claims,
        key,
        algorithms=["RS256"],
    )


def client(surface: str, encoded: str, key: RSAKey) -> OidcProviderClient:
    config = settings()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": "https://identity.example.test",
                    "authorization_endpoint": (
                        "https://identity.example.test/login/oauth/authorize"
                    ),
                    "token_endpoint": (
                        "https://identity.example.test/api/login/oauth/access_token"
                    ),
                    "jwks_uri": "https://identity.example.test/.well-known/jwks",
                },
            )
        if request.url.path.endswith("/jwks"):
            return httpx.Response(200, json={"keys": [key.as_dict(private=False)]})
        if request.url.path.endswith("/access_token"):
            form = parse_qs(request.content.decode())
            assert form["code_verifier"] == ["verifier-123"]
            return httpx.Response(200, json={"id_token": encoded})
        raise AssertionError(f"unexpected request: {request.url}")

    return OidcProviderClient(
        config,
        config.browser_profile(surface),  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("surface", "audience"),
    [("admin", "knowmart-admin-client"), ("web", "knowmart-web-client")],
)
async def test_each_surface_verifies_its_own_audience(
    surface: str, audience: str
) -> None:
    key = RSAKey.generate_key(parameters={"kid": "test-key"})
    oidc = client(surface, token(key, audience), key)
    claims = await oidc.exchange_authorization_code(
        code="code", code_verifier="verifier-123", nonce="nonce-123"
    )
    assert claims["aud"] == audience


@pytest.mark.asyncio
async def test_admin_token_cannot_be_replayed_at_web_client() -> None:
    key = RSAKey.generate_key(parameters={"kid": "test-key"})
    with pytest.raises(UnauthorizedError) as error:
        oidc = client("web", token(key, "knowmart-admin-client"), key)
        await oidc.exchange_authorization_code(
            code="code", code_verifier="verifier-123", nonce="nonce-123"
        )
    assert error.value.code == "audience_mismatch"


@pytest.mark.asyncio
async def test_signup_is_web_only_and_uses_provider_signup_endpoint() -> None:
    key = RSAKey.generate_key(parameters={"kid": "test-key"})
    web_client = client("web", token(key, "knowmart-web-client"), key)
    url = await web_client.build_authorization_url(
        state="state", nonce="nonce-123", code_challenge="challenge", mode="signup"
    )
    assert urlsplit(url).path == "/signup/oauth/authorize"
    with pytest.raises(UnauthorizedError):
        admin_client = client("admin", token(key, "knowmart-admin-client"), key)
        await admin_client.build_authorization_url(
            state="state", nonce="nonce-123", code_challenge="challenge", mode="signup"
        )


@pytest.mark.asyncio
async def test_discovery_cannot_redirect_jwks_cross_origin() -> None:
    config = settings()

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "issuer": "https://identity.example.test",
                "authorization_endpoint": "https://identity.example.test/authorize",
                "token_endpoint": "https://identity.example.test/token",
                "jwks_uri": "https://attacker.example.test/jwks",
            },
        )

    oidc = OidcProviderClient(
        config,
        config.browser_profile("web"),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ServiceUnavailableError, match="cross-origin"):
        await oidc.get_metadata()


@pytest.mark.asyncio
async def test_client_credentials_use_explicit_service_identity() -> None:
    config = settings()
    observed: dict[str, list[str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": "https://identity.example.test",
                    "authorization_endpoint": "https://identity.example.test/authorize",
                    "token_endpoint": "https://identity.example.test/token",
                    "jwks_uri": "https://identity.example.test/jwks",
                },
            )
        observed.update(parse_qs(request.content.decode()))
        return httpx.Response(200, json={"access_token": "opaque", "expires_in": 60})

    oidc = OidcProviderClient(
        config,
        config.browser_profile("web"),
        transport=httpx.MockTransport(handler),
    )
    await oidc.exchange_client_credentials(
        scope="knowbase:read",
        client_id="service-client",
        client_secret="service-secret",
    )
    assert observed["client_id"] == ["service-client"]
    assert observed["scope"] == ["knowbase:read"]


@pytest.mark.asyncio
async def test_workload_access_token_requires_exact_audience_and_signature() -> None:
    key = RSAKey.generate_key(parameters={"kid": "test-key"})
    oidc = client("admin", token(key, "knowbase-internal"), key)
    claims = await oidc.verify_access_token(
        token(key, "knowbase-internal", scope="knowbase:retrieve"),
        audience="knowbase-internal",
    )
    assert claims["sub"] == "user-123"
    with pytest.raises(UnauthorizedError) as error:
        await oidc.verify_access_token(
            token(key, "wrong-audience"), audience="knowbase-internal"
        )
    assert error.value.code == "audience_mismatch"
