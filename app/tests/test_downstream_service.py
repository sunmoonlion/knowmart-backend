from __future__ import annotations

import httpx
import pytest

from app.application.errors.exceptions import BadRequestError
from app.infrastructure.external.downstream_service import DownstreamServiceClient
from core.config import Settings


class FakeServiceOidc:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def exchange_client_credentials(self, **values: str):
        self.calls.append(values)
        return {"access_token": "server-only-token", "expires_in": 300}


def settings() -> Settings:
    return Settings(
        _env_file=None,
        env="test",
        downstream_base_url="http://research-backend:8000",
        downstream_client_id="knowmart-service",
        downstream_client_secret="test-only-secret",
        downstream_scope="research:runs:read",
        downstream_allowed_path_prefixes="/api/internal/v1",
    )


@pytest.mark.asyncio
async def test_downstream_uses_service_identity_and_route_allowlist() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"contract_version": 1})

    oidc = FakeServiceOidc()
    client = DownstreamServiceClient(
        settings(),
        oidc_client=oidc,  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
    )
    result = await client.request_json(
        "GET", "/api/internal/v1/runs/1", operation_id="op-downstream-1"
    )
    assert result == {"contract_version": 1}
    assert oidc.calls[0]["client_id"] == "knowmart-service"
    assert requests[0].headers["authorization"] == "Bearer server-only-token"
    assert requests[0].headers["x-operation-id"] == "op-downstream-1"

    with pytest.raises(BadRequestError, match="not allowed"):
        await client.request_json(
            "GET", "/api/admin/v1/secrets", operation_id="op-downstream-2"
        )


def test_downstream_configuration_is_complete_and_origin_only() -> None:
    with pytest.raises(ValueError, match="missing"):
        Settings(
            _env_file=None, downstream_base_url="http://research-backend:8000"
        ).require_downstream_identity()
    with pytest.raises(ValueError, match="origin"):
        Settings(
            _env_file=None,
            downstream_base_url="http://research-backend:8000/api",
            downstream_client_id="client",
            downstream_client_secret="secret",
            downstream_scope="scope",
        ).require_downstream_identity()
