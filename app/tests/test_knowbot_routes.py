from __future__ import annotations

import uuid
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

import app.interfaces.http.middleware.auth as auth_middleware
from app.application.services.knowbot_service import KnowbotService
from app.infrastructure.security.relation_auth import (
    ServiceCaller,
    require_catalog_service,
    require_deliver_service,
    require_demand_service,
    require_summon_service,
)
from app.interfaces.endpoints import knowbot_routes
from app.main import app, settings
from tests.knowbot_fakes import AllShareable, MemoryKnowbots, manifest
from tests.test_auth_routes_security import FakeAuthService, session

CSRF = "csrf-token-with-at-least-thirty-two-characters"
ORIGIN = "http://localhost:3000"
OWNER = "user-123"  # 会话里的用户（tests/test_auth_routes_security.session）


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch):
    repo = MemoryKnowbots()
    svc = KnowbotService(repo, bindings=AllShareable())
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

    async def ask_public_on_delivery(self, subject: str, knowbot_id: str) -> str:
        self.calls.append({"ask_public": knowbot_id, "subject": subject})
        return "pending"

    async def withdraw_public_on_delivery(self, subject: str, knowbot_id: str) -> None:
        self.calls.append({"withdraw_public": knowbot_id, "subject": subject})


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
async def test_delivery_may_ask_to_go_public(wired) -> None:
    async with client() as c:
        plain = (await _deliver(c, "delivery-0001")).json()
        assert "listing_status" not in plain and SUMMONS.calls == []
        asked = await c.post(
            "/api/internal/v1/knowmart/deliveries",
            json={
                "delivery_id": "delivery-0002",
                "owner_subject": OWNER,
                "knowbot_ref": "lab-bot-1",
                "manifest": manifest(version="2"),
                "request_public": True,
            },
        )
        assert asked.status_code == 201
        assert asked.json()["listing_status"] == "pending"
        assert asked.json()["version"] == "2"
        assert SUMMONS.calls == [{"ask_public": plain["knowbot_id"], "subject": OWNER}]


@pytest.mark.asyncio
async def test_delivery_carries_a_category_and_refuses_unlicensed_bindings(
    wired,
) -> None:
    svc, repo = wired
    async with client() as c:
        body = {
            "delivery_id": "delivery-0001",
            "owner_subject": OWNER,
            "knowbot_ref": "lab-bot-1",
            "manifest": manifest(),
            "category": "tools.lookup",
        }
        bad = await c.post(
            "/api/internal/v1/knowmart/deliveries",
            json={**body, "category": "uncategorized"},
        )
        assert bad.status_code == 422
        svc.bindings.states[(OWNER, "m-1", 2)] = {
            "state": "live",
            "license": "self",
            "shareable": False,
        }
        refused = await c.post(
            "/api/internal/v1/knowmart/deliveries",
            json={**body, "request_public": True},
        )
        assert (refused.status_code, refused.json()["code"]) == (
            409,
            "binding_not_shareable",
        )
        made = await c.post("/api/internal/v1/knowmart/deliveries", json=body)
        assert made.status_code == 201, made.text
    [bot] = await repo.list_for_owner(OWNER)
    assert bot.category == "tools.lookup"


@pytest.mark.asyncio
async def test_knowlab_reads_back_how_its_versions_stand(wired) -> None:
    """knowlab 取回交来的各版：待处理的带原因（0043 第 4.3 节）。"""
    svc, repo = wired
    async with client() as c:
        made = await c.post(
            "/api/internal/v1/knowmart/deliveries",
            json={
                "delivery_id": "knowlab-expert-1",
                "owner_subject": OWNER,
                "knowbot_ref": "knowlab-r1",
                "manifest": manifest(),
                "request_public": True,
            },
        )
        bot = uuid.UUID(made.json()["knowbot_id"])
        await repo.hold_version(bot, "1", "binding_deleted", datetime.now(UTC))
        url = f"/api/internal/v1/knowmart/owners/{OWNER}/sources/knowlab-r1/versions"
        got = await c.get(url)
        assert got.json()["versions"] == [
            {
                "version": "1",
                "status": "held",
                "held_reason": "binding_deleted",
                "public_allowed": True,
            }
        ]
        other = await c.get(
            "/api/internal/v1/knowmart/owners/someone/sources/knowlab-r1/versions"
        )
        assert other.json()["versions"] == []
        assert (
            await c.get(
                f"/api/internal/v1/knowmart/owners/{OWNER}/sources/lab-1/versions"
            )
        ).status_code == 404


@pytest.mark.asyncio
async def test_knowlab_stops_a_version_of_its_owner(wired) -> None:
    async with client() as c:
        native = (await _deliver(c, "delivery-0002")).json()["knowbot_id"]
        knowbot = (await _deliver(c, "delivery-0001", ref="knowlab-r1")).json()[
            "knowbot_id"
        ]
        # 不是 knowlab 交来的：knowlab 停不了（Kimi 评审第 1 条）
        url = f"/api/internal/v1/knowmart/owners/{OWNER}/knowbots/{native}"
        assert (await c.post(url + "/versions/1/stop")).status_code == 404
        base = f"/api/internal/v1/knowmart/owners/{{owner}}/knowbots/{knowbot}"
        others = await c.post(base.format(owner="someone-else") + "/versions/1/stop")
        assert others.status_code == 404
        missing = await c.post(base.format(owner=OWNER) + "/versions/9/stop")
        assert missing.status_code == 404
        for _ in range(2):  # 停过的再停照样 204
            stopped = await c.post(base.format(owner=OWNER) + "/versions/1/stop")
            assert stopped.status_code == 204
        summon = f"/api/internal/v1/knowmart/owners/{OWNER}/knowbots/{knowbot}@1"
        assert (await c.get(summon)).status_code == 404


@pytest.mark.asyncio
async def test_knowlab_withdraws_public_without_stopping_version(wired) -> None:
    async with client() as c:
        made = await c.post(
            "/api/internal/v1/knowmart/deliveries",
            json={
                "delivery_id": "delivery-0010",
                "owner_subject": OWNER,
                "knowbot_ref": "knowlab-r2",
                "manifest": manifest(),
                "request_public": True,
            },
        )
        knowbot = made.json()["knowbot_id"]
        base = f"/api/internal/v1/knowmart/owners/{{owner}}/knowbots/{knowbot}"
        assert (
            await c.post(
                base.format(owner="someone-else") + "/versions/1/withdraw-public"
            )
        ).status_code == 404
        for _ in range(2):
            res = await c.post(base.format(owner=OWNER) + "/versions/1/withdraw-public")
            assert res.status_code == 204
        summon = f"/api/internal/v1/knowmart/owners/{OWNER}/knowbots/{knowbot}@1"
        assert (await c.get(summon)).status_code == 200


@pytest.mark.asyncio
async def test_knowlab_sends_what_became_of_unlisted_versions(wired) -> None:
    _, repo = wired
    url = f"/api/internal/v1/knowmart/owners/{OWNER}/sources/knowlab-r1/unlisted"
    async with client() as c:
        ok = await c.put(url + "/1", json={"outcome": "rejected"})
        assert ok.status_code == 204
        assert await repo.unlisted_of("knowlab-r1", OWNER) == {"1": "rejected"}
        bad = await c.put(url + "/1", json={"outcome": "x"})
        assert bad.status_code == 400


@pytest.mark.asyncio
async def test_knowlab_experts_are_not_stopped_here(wired) -> None:
    async with client() as c:
        knowbot = (await _deliver(c, "delivery-0001", ref="knowlab-r1")).json()[
            "knowbot_id"
        ]
        c.cookies.set("sunmoonai_knowmart_web_sid", "member")
        url = f"/api/web/v1/knowbots/{knowbot}/versions/1/stop"
        refused = await c.post(url, headers={"Origin": ORIGIN, "X-CSRF-Token": CSRF})
        assert (
            refused.status_code == 409 and refused.json()["code"] == "stop_in_knowlab"
        )


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
        no_token = await c.post(
            "/api/internal/v1/knowmart/owners/u/knowbots/x/versions/1/stop"
        )
        assert no_token.status_code == 401
        no_token = await c.post(
            "/api/internal/v1/knowmart/owners/u/knowbots/x/versions/1/withdraw-public"
        )
        assert no_token.status_code == 401
        no_token = await c.put(
            "/api/internal/v1/knowmart/owners/u/sources/x/unlisted/1",
            json={"outcome": "declined"},
        )
        assert no_token.status_code == 401
    # 公网地址来的：当没有这个接口，连令牌都不看
    async with client("http://testserver") as c:
        hidden = await c.get("/api/internal/v1/knowmart/owners/u/knowbots/x@1")
        assert hidden.status_code == 404


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


class FakeCatalog:
    async def runtime_catalog(self, owner: str, runtime: str):  # type: ignore[no-untyped-def]
        assert (owner, runtime) == (OWNER, "knowbench")
        return [{"knowbot_id": "k1", "version": "2", "owned": False}]


class FakeMarket(Summons):
    async def expert_terms(self, buyer, entries):  # type: ignore[no-untyped-def]
        return [{**e, "price": "5.00", "free": False} for e in entries]

    async def create_demand(self, subject, body):  # type: ignore[no-untyped-def]
        self.calls.append({"demand": body.title, "subject": subject})
        return {"demand_id": "d1", "title": body.title}


@pytest.mark.asyncio
async def test_find_expert_catalog_and_demands_for_the_user(wired) -> None:
    """运行方替用户「找专家」、替他发需求（请专家方案设计第 4.9 节）：按关系认调用方，
    记在这个用户名下。"""
    market = FakeMarket()
    app.dependency_overrides[knowbot_routes.catalog_for_request] = FakeCatalog
    app.dependency_overrides[knowbot_routes.market_for_request] = lambda: market
    app.dependency_overrides[require_catalog_service] = lambda: _caller("knowbench")
    app.dependency_overrides[require_demand_service] = lambda: _caller("knowbench")
    base = f"/api/internal/v1/knowmart/owners/{OWNER}"
    async with client() as c:
        got = await c.get(f"{base}/catalog")
        assert got.status_code == 200
        assert got.json()["experts"] == [
            {
                "knowbot_id": "k1",
                "version": "2",
                "owned": False,
                "price": "5.00",
                "free": False,
            }
        ]
        made = await c.post(
            f"{base}/demands",
            json={
                "kind": "expert",
                "title": "每季度对比电池公司毛利率",
                "problem": "按固定口径对比几家电池公司的毛利率并预测下季",
                "deadline": "2099-01-01",
            },
        )
        assert made.status_code == 201
        assert market.calls == [
            {"demand": "每季度对比电池公司毛利率", "subject": OWNER}
        ]
    async with client("http://testserver") as c:  # 公网来的当没有
        assert (await c.get(f"{base}/catalog")).status_code == 404
