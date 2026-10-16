from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

import app.interfaces.http.web.interactions as interaction_routes
from app.application.ports import SourceResolution
from app.application.services.web_interaction import (
    REFERENCE_ACTION_ID,
    REFERENCE_EVENT_IDS,
    REFERENCE_EVIDENCE_ID,
    REFERENCE_RUN_ID,
    ReferenceWebInteractionAdapter,
    UnavailableWebInteractionAdapter,
    get_web_interaction_port,
)
from app.domain.security import Principal
from app.interfaces.http.middleware.auth import get_web_current_user
from app.main import app
from core.config import Settings


def principal() -> Principal:
    now = datetime.now(UTC)
    return Principal(
        actor_type="user",
        subject="web-user",
        issuer="https://identity.example.test",
        app="knowmart",
        surface="web",
        audience="knowmart-web-client",
        actor_id=uuid.UUID("00000000-0000-4000-8000-000000000001"),
        roles=("member",),
        scopes=frozenset({"profile:read"}),
        authenticated_at=now,
        expires_at=now + timedelta(minutes=10),
        policy_version="knowmart-web-v2",
    )


@pytest.fixture
def reference_app(monkeypatch: pytest.MonkeyPatch):
    config = Settings(_env_file=None, env="test", reference_interaction_enabled=True)

    async def principal_override() -> Principal:
        return principal()

    async def interaction_override():
        return ReferenceWebInteractionAdapter()

    app.dependency_overrides[get_web_current_user] = principal_override
    app.dependency_overrides[get_web_interaction_port] = interaction_override
    monkeypatch.setattr(interaction_routes, "get_settings", lambda: config)
    yield
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_snapshot_action_sse_and_citation_contract(reference_app: None) -> None:
    del reference_app
    prefix = "/api/web/v1"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    ) as client:
        snapshot = await client.get(f"{prefix}/runs/{REFERENCE_RUN_ID}")
        assert snapshot.status_code == 200
        assert snapshot.json()["status"] == "running"

        stream = await client.get(f"{prefix}/runs/{REFERENCE_RUN_ID}/events")
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        assert stream.headers["x-accel-buffering"] == "no"
        assert f"id: {REFERENCE_EVENT_IDS[1]}" in stream.text
        assert '"type":"citation"' in stream.text

        action = await client.post(
            f"{prefix}/runs/{REFERENCE_RUN_ID}/actions",
            json={
                "contract_version": 1,
                "action_id": str(REFERENCE_ACTION_ID),
                "value": "confirm",
            },
        )
        assert action.status_code == 200
        assert action.json()["status"] == "succeeded"
        assert action.json()["citations"][0]["source_href"] == (
            f"{prefix}/citations/{REFERENCE_EVIDENCE_ID}/source"
        )

        source = await client.get(
            f"{prefix}/citations/{REFERENCE_EVIDENCE_ID}/source"
        )
        assert source.status_code == 302
        assert source.headers["location"] == (
            f"{prefix}/reference/sources/{REFERENCE_EVIDENCE_ID}"
        )
        resolved = await client.get(source.headers["location"])
        assert resolved.status_code == 200


@pytest.mark.asyncio
async def test_cursor_and_payload_errors_are_stable(reference_app: None) -> None:
    del reference_app
    path = f"/api/web/v1/runs/{REFERENCE_RUN_ID}"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        conflict = await client.get(
            f"{path}/events",
            headers={"Last-Event-ID": str(REFERENCE_EVENT_IDS[0])},
            params={"last_event_id": str(REFERENCE_EVENT_IDS[1])},
        )
        assert conflict.status_code == 400
        assert conflict.json()["error"]["code"] == "cursor_invalid"

        expired = await client.get(
            f"{path}/events",
            params={"last_event_id": "00000000-0000-5000-8000-000000000099"},
        )
        assert expired.status_code == 409
        assert expired.json()["error"]["code"] == "cursor_expired"

        invalid = await client.post(
            f"{path}/actions", json={"contract_version": 1, "extra": True}
        )
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_default_provider_fails_closed() -> None:
    async def principal_override() -> Principal:
        return principal()

    async def interaction_override():
        return UnavailableWebInteractionAdapter()

    app.dependency_overrides[get_web_current_user] = principal_override
    app.dependency_overrides[get_web_interaction_port] = interaction_override
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.get(
                f"/api/web/v1/runs/{REFERENCE_RUN_ID}"
            )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "provider_unavailable"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_citation_rejects_protocol_relative_target() -> None:
    class UnsafeAdapter(ReferenceWebInteractionAdapter):
        async def resolve_citation_source(self, evidence_id, context):
            del evidence_id, context
            return SourceResolution(location="//attacker.example.test/source")

    async def principal_override() -> Principal:
        return principal()

    async def interaction_override():
        return UnsafeAdapter()

    app.dependency_overrides[get_web_current_user] = principal_override
    app.dependency_overrides[get_web_interaction_port] = interaction_override
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.get(
                f"/api/web/v1/citations/{REFERENCE_EVIDENCE_ID}/source"
            )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "source_invalid"
    finally:
        app.dependency_overrides.clear()
