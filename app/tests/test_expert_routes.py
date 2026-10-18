"""专家目录的网页接口（SDD 0032）：要会话、只给看得到的、看不到回 404、参数校验。"""

from __future__ import annotations

import httpx
import pytest

import app.interfaces.http.middleware.auth as auth_middleware
from app.application.services.expert_catalog import ExpertCatalog
from app.application.services.knowbot_service import KnowbotService
from app.interfaces.endpoints import expert_routes
from app.main import app
from tests.knowbot_fakes import MemoryKnowbots, manifest
from tests.test_auth_routes_security import FakeAuthService, session

OWNER = "user-123"  # 会话里的用户（tests/test_auth_routes_security.session）


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch):
    memory = MemoryKnowbots()
    fake_auth = FakeAuthService({"member": session("web", "profile:read")})
    monkeypatch.setattr(auth_middleware, "web_auth_service", fake_auth)
    app.dependency_overrides[expert_routes.catalog] = lambda: ExpertCatalog(memory)
    yield memory
    app.dependency_overrides.clear()


def client(signed_in: bool = True) -> httpx.AsyncClient:
    c = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )
    if signed_in:
        c.cookies.set("sunmoonai_knowmart_web_sid", "member")
    return c


async def deliver(repo, owner: str, ref: str, **kw):
    return (
        await KnowbotService(repo).deliver(
            delivery_id=f"delivery-{ref}",
            owner_subject=owner,
            knowbot_ref=ref,
            manifest=manifest(**kw),
        )
    ).knowbot_id


@pytest.mark.asyncio
async def test_catalog_lists_only_my_experts_with_categories(repo) -> None:
    mine = await deliver(repo, OWNER, "mine", name="财务分析与预测")
    await deliver(repo, "someone-else", "theirs", name="别人的")
    async with client() as c:
        listed = await c.get("/api/web/v1/knowmart/experts")
        assert listed.status_code == 200
        assert listed.headers["cache-control"] == "no-store"
        body = listed.json()
        assert [i["name"] for i in body["items"]] == ["财务分析与预测"]
        assert body["total"] == 1 and body["page"] == 1
        assert body["categories"][0]["children"][0]["count"] == 1
        cats = await c.get("/api/web/v1/knowmart/categories")
        assert cats.json()["categories"][0]["group"] == "finance"
        detail = await c.get(f"/api/web/v1/knowmart/experts/{mine}")
        assert detail.status_code == 200
        assert detail.json()["expert"]["owned"] is True


@pytest.mark.asyncio
async def test_others_experts_are_not_found_and_bad_params_are_refused(repo) -> None:
    theirs = await deliver(repo, "someone-else", "theirs")
    async with client() as c:
        assert (
            await c.get(f"/api/web/v1/knowmart/experts/{theirs}")
        ).status_code == 404
        assert (
            await c.get("/api/web/v1/knowmart/experts/not-a-uuid")
        ).status_code == 404
        for query in ("data=everything", "sort=price", "page=0", "category=BAD!"):
            response = await c.get(f"/api/web/v1/knowmart/experts?{query}")
            assert response.status_code == 422, query


@pytest.mark.asyncio
async def test_catalog_needs_a_session(repo) -> None:
    async with client(signed_in=False) as c:
        assert (await c.get("/api/web/v1/knowmart/experts")).status_code == 401
        assert (await c.get("/api/web/v1/knowmart/categories")).status_code == 401
