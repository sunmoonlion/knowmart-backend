from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

import app.interfaces.http.middleware.auth as auth_middleware
from app.application.services.knowbot_service import KnowbotService
from app.infrastructure.security.relation_auth import (
    ServiceCaller,
    require_deliver_service,
    require_summon_service,
)
from app.interfaces.endpoints import knowbot_routes
from app.main import app, settings
from tests.knowbot_fakes import MemoryKnowbots, manifest
from tests.test_auth_routes_security import FakeAuthService, session

CSRF = "csrf-token-with-at-least-thirty-two-characters"
ORIGIN = "http://localhost:3000"
OWNER = "user-123"  # 会话里的用户（tests/test_auth_routes_security.session）


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch):
    repo = MemoryKnowbots()
    svc = KnowbotService(repo)
    fake_auth = FakeAuthService({"member": session("web", "profile:read")})
    monkeypatch.setattr(auth_middleware, "web_auth_service", fake_auth)
    app.dependency_overrides[knowbot_routes.service] = lambda: svc
    app.dependency_overrides[knowbot_routes.market_for_request] = lambda: SUMMONS
    SUMMONS.calls.clear()
    app.dependency_overrides[require_deliver_service] = lambda: _caller("knowlab")
    app.dependency_overrides[require_summon_service] = lambda: _caller("knowbench")
    app.dependency_overrides[knowbot_routes.knowbot_settings] = lambda: (
        settings.model_copy(
            update={
                "cross_app_sources_json": (
                    '{"knowbench": {"return_url": '
                    '"https://knowbench.example.test/zh-CN/workbench?ref={ref}"}}'
                )
            }
        )
    )
    yield svc, repo
    app.dependency_overrides.clear()


class Summons:
    """市场的替身：「请这位」时记一笔成交（请别人公开的专家才记，SDD 0018）。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def record_summon(self, **kw):  # type: ignore[no-untyped-def]
        self.calls.append(kw)


SUMMONS = Summons()


def _caller(name: str) -> ServiceCaller:
    return ServiceCaller(caller=name, principal=None)  # type: ignore[arg-type]


def client(base_url: str = "http://127.0.0.1") -> httpx.AsyncClient:
    # 内部接口只认集群内与回环地址（审读第 1 条）
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base_url)


async def _deliver(c: httpx.AsyncClient, delivery: str, owner: str = OWNER, **kw):
    return await c.post(
        "/api/internal/v1/knowmart/deliveries",
        json={
            "delivery_id": delivery,
            "owner_subject": owner,
            "knowbot_ref": kw.pop("ref", "lab-bot-1"),
            "manifest": manifest(**kw),
        },
    )


@pytest.mark.asyncio
async def test_delivery_is_201_then_200_on_replay_and_409_on_change(wired) -> None:
    async with client() as c:
        first = await _deliver(c, "delivery-0001")
        assert first.status_code == 201 and first.json()["replayed"] is False
        again = await _deliver(c, "delivery-0001")
        assert again.status_code == 200 and again.json()["replayed"] is True
        changed = await _deliver(c, "delivery-0001", tagline="变了")
        assert changed.status_code == 409
        bad = await c.post(
            "/api/internal/v1/knowmart/deliveries",
            json={
                "delivery_id": "delivery-0009",
                "owner_subject": OWNER,
                "knowbot_ref": "x",
                "manifest": {"schema": "nope"},
            },
        )
        assert bad.status_code == 400


@pytest.mark.asyncio
async def test_summon_is_owner_scoped_and_hides_others(wired) -> None:
    async with client() as c:
        knowbot = (await _deliver(c, "delivery-0001")).json()["knowbot_id"]
        ok = await c.get(
            f"/api/internal/v1/knowmart/owners/{OWNER}/knowbots/{knowbot}@1"
        )
        assert ok.status_code == 200
        body = ok.json()
        assert body["manifest"]["pack"]["pack_id"] == "fin-forecast"
        assert body["digest"].startswith("sha256:")
        assert ok.headers["cache-control"] == "no-store"
        for path in (
            f"/api/internal/v1/knowmart/owners/someone-else/knowbots/{knowbot}@1",
            f"/api/internal/v1/knowmart/owners/{OWNER}/knowbots/{knowbot}@2",
            f"/api/internal/v1/knowmart/owners/{OWNER}/knowbots/{knowbot}",
        ):
            assert (await c.get(path)).status_code == 404
        # 运行方不是这个调用方的分身，同样拿不到
        app.dependency_overrides[require_summon_service] = lambda: _caller("another")
        mine = f"/api/internal/v1/knowmart/owners/{OWNER}/knowbots/{knowbot}@1"
        assert (await c.get(mine)).status_code == 404


@pytest.mark.asyncio
async def test_internal_routes_require_their_own_relation() -> None:
    app.dependency_overrides.clear()
    async with client() as c:
        no_token = await c.post(
            "/api/internal/v1/knowmart/deliveries",
            json={
                "delivery_id": "delivery-0001",
                "owner_subject": OWNER,
                "knowbot_ref": "x",
                "manifest": manifest(),
            },
        )
        assert no_token.status_code == 401
        no_token = await c.get("/api/internal/v1/knowmart/owners/u/knowbots/x@1")
        assert no_token.status_code == 401
    # 公网地址来的：当没有这个接口，连令牌都不看
    async with client("http://testserver") as c:
        hidden = await c.get("/api/internal/v1/knowmart/owners/u/knowbots/x@1")
        assert hidden.status_code == 404


@pytest.mark.asyncio
async def test_my_knowbots_lists_only_the_session_owner(wired) -> None:
    async with client() as c:
        await _deliver(c, "delivery-0001")
        await _deliver(c, "delivery-0002", owner="someone-else", ref="lab-bot-2")
        assert (await c.get("/api/web/v1/knowbots")).status_code == 401
        c.cookies.set("sunmoonai_knowmart_web_sid", "member")
        listed = (await c.get("/api/web/v1/knowbots")).json()["knowbots"]
        assert [k["name"] for k in listed] == ["财务分析与预测"]
        assert "manifest" not in listed[0]
        assert listed[0]["versions"][0]["status"] == "active"
        assert listed[0]["versions"][0]["runtime"] == "knowbench"


@pytest.mark.asyncio
async def test_stop_needs_csrf_and_blocks_new_summons(wired) -> None:
    async with client() as c:
        knowbot = (await _deliver(c, "delivery-0001")).json()["knowbot_id"]
        c.cookies.set("sunmoonai_knowmart_web_sid", "member")
        url = f"/api/web/v1/knowbots/{knowbot}/versions/1/stop"
        assert (await c.post(url)).status_code == 403
        stopped = await c.post(url, headers={"Origin": ORIGIN, "X-CSRF-Token": CSRF})
        assert stopped.status_code == 204
        summon = f"/api/internal/v1/knowmart/owners/{OWNER}/knowbots/{knowbot}@1"
        assert (await c.get(summon)).status_code == 404


@pytest.mark.asyncio
async def test_summon_return_goes_back_to_the_registered_place(wired) -> None:
    async with client() as c:
        knowbot = (await _deliver(c, "delivery-0001")).json()["knowbot_id"]
        c.cookies.set("sunmoonai_knowmart_web_sid", "member")
        url = f"/api/web/v1/knowbots/{knowbot}/versions/1/summon-return"
        headers = {"Origin": ORIGIN, "X-CSRF-Token": CSRF}
        ok = await c.post(
            url, json={"from": "knowbench", "ref": "task-42"}, headers=headers
        )
        assert ok.status_code == 200
        target = urlsplit(ok.json()["return_url"])
        assert target.netloc == "knowbench.example.test"
        assert target.path == "/zh-CN/workbench"
        assert parse_qs(target.query) == {
            "ref": ["task-42"],
            "knowbot": [f"{knowbot}@1"],
        }
        # 交给市场判是不是请别人的（是才记成交）
        assert SUMMONS.calls == [
            {"buyer": OWNER, "knowbot_id": knowbot, "version": "1"}
        ]
        unknown = await c.post(url, json={"from": "evil", "ref": "x"}, headers=headers)
        assert unknown.status_code == 400
        assert (await c.post(url, json={"from": "knowbench"})).status_code == 403
        other = f"/api/web/v1/knowbots/{knowbot}/versions/9/summon-return"
        missing = await c.post(other, json={"from": "knowbench"}, headers=headers)
        assert missing.status_code == 404
