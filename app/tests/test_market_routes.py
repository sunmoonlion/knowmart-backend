"""供需市场的接口（SDD 0018 第十一节）：要会话、信封、状态码；管理端要运营。"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

import app.interfaces.http.middleware.auth as auth_middleware
from app.interfaces.endpoints import market_routes
from app.main import app
from tests.test_auth_routes_security import FakeAuthService, session
from tests.test_knowbot_routes import CSRF, ORIGIN


class FakeMarket:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def rules(self) -> dict[str, Any]:
        return {"version": 0, "body": "# 驻场规则", "created_at": None}

    async def knowledge_market(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("knowledge_market", kw))
        return {"items": [], "total": 0, "page": kw["page"], "page_size": 24}

    async def create_demand(self, subject: str, body: Any) -> dict[str, Any]:
        self.calls.append(("create_demand", subject, body.title))
        return {"demand_id": "d1", "title": body.title}

    async def my_trades(self, subject: str, role: str) -> list[dict[str, Any]]:
        self.calls.append(("my_trades", subject, role))
        return []

    async def materials(self, user_token: Any) -> list[dict[str, Any]]:
        self.calls.append(("materials", await user_token()))
        return [{"material_id": "m1", "name": "行业数据.csv", "sellable": True}]


class FakeAdmin:
    async def queue(self) -> dict[str, Any]:
        return {"publishers": [], "licenses": [], "knowledge": [], "experts": []}


@pytest.fixture
def market(monkeypatch: pytest.MonkeyPatch):
    fake = FakeMarket()
    fake_auth = FakeAuthService(
        {
            "member": session("web", "profile:read"),
            "operator": session("admin", "knowmart:admin"),
        }
    )
    monkeypatch.setattr(auth_middleware, "web_auth_service", fake_auth)
    monkeypatch.setattr(auth_middleware, "admin_auth_service", fake_auth, raising=False)
    app.dependency_overrides[market_routes._service] = lambda: fake
    app.dependency_overrides[market_routes._admin] = lambda: FakeAdmin()
    yield fake
    app.dependency_overrides.clear()


def client(cookie: str | None = "member", name: str = "sunmoonai_knowmart_web_sid"):
    c = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )
    if cookie:
        c.cookies.set(name, cookie)
    return c


BASE = "/api/web/v1/knowmart/market"


async def test_web_endpoints_need_a_session_and_answer_in_an_envelope(market) -> None:
    async with client(None) as c:
        assert (await c.get(f"{BASE}/rules")).status_code == 401
    async with client() as c:
        rules = await c.get(f"{BASE}/rules")
        assert rules.status_code == 200 and rules.headers["cache-control"] == "no-store"
        assert rules.json()["contract_version"] == 1 and rules.json()["version"] == 0
        listed = await c.get(f"{BASE}/knowledge", params={"q": "行业", "page": 2})
        assert listed.json()["page"] == 2
        assert market.calls[0] == (
            "knowledge_market",
            {"q": "行业", "category": None, "page": 2},
        )
        trades = await c.get(f"{BASE}/trades", params={"role": "seller"})
        assert trades.json()["items"] == []
        assert market.calls[-1] == ("my_trades", "user-123", "seller")
        headers = {"Origin": ORIGIN, "X-CSRF-Token": CSRF}
        bad = await c.post(
            f"{BASE}/demands", json={"kind": "expert", "title": "x"}, headers=headers
        )
        assert bad.status_code == 422
        made = await c.post(
            f"{BASE}/demands",
            json={
                "kind": "expert",
                "title": "要一位看财报的专家",
                "problem": "看三年财报",
                "deadline": "2099-01-01",
            },
            headers=headers,
        )
        assert made.status_code == 201 and made.json()["demand"]["demand_id"] == "d1"
        assert market.calls[-1] == ("create_demand", "user-123", "要一位看财报的专家")


async def test_admin_endpoints_are_not_for_web_users(market) -> None:
    async with client() as c:
        denied = await c.get("/api/admin/v1/knowmart/market/queue")
        assert denied.status_code in (401, 403)


async def test_materials_come_from_knowbase_with_the_users_token(market) -> None:
    """写知识条目选的资料：用户交给 knowmart 的（user-data/v2 的 knowmart 栏）。"""

    async def provide() -> str:
        return "knowbase-token"

    app.dependency_overrides[market_routes._token] = lambda: provide
    async with client() as c:
        got = await c.get(f"{BASE}/knowledge/materials")
        assert got.status_code == 200 and got.headers["cache-control"] == "no-store"
        assert [m["material_id"] for m in got.json()["items"]] == ["m1"]
        assert market.calls[-1] == ("materials", "knowbase-token")
        # 资料不分应用：买知识不再收「放进哪个应用」
        refused = await c.post(
            f"{BASE}/knowledge/l1/buy",
            json={"request_id": "buy-00001", "target_app": "knowbench"},
            headers={"Origin": ORIGIN, "X-CSRF-Token": CSRF},
        )
        assert refused.status_code == 422
