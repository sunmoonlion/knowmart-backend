"""knowbase 资料库客户端（契约 user-data/v2，SDD 0043 第 4.3 节）：本人列 knowmart 栏、
成交以服务身份建使用权。"""

from __future__ import annotations

import json

import httpx
import pytest

from app.application.errors.exceptions import (
    BadGatewayError,
    ConcurrencyConflictError,
    NotFoundError,
    ServiceUnavailableError,
)
from app.infrastructure.external.knowbase_user_data import (
    KnowbaseLibrary,
    KnowbaseRefused,
    KnowbaseVault,
    material_view,
)

URL = "https://knowbase.internal"
PREFIX = "KNOWBASE_USER_DATA_PURCHASE_SERVICE_"


def environ(**changes: str) -> dict[str, str]:
    return {
        "KNOWBASE_INTERNAL_URL": URL,
        PREFIX + "CLIENT_ID": "purchase-client",
        PREFIX + "CLIENT_SECRET": "secret",
        PREFIX + "SCOPE": "knowbase:user-data-purchase",
        PREFIX
        + "DISCOVERY_URL": "https://casdoor.internal/.well-known/openid-configuration",
        PREFIX + "BACKCHANNEL_ENDPOINT": "https://casdoor.internal",
    } | changes


class FakeOidc:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def exchange_client_credentials(self, **values: str) -> dict[str, object]:
        self.calls.append(values["scope"])
        return {"access_token": f"svc-{values['scope']}", "expires_in": 300}


def client(answer, **env: str) -> tuple[KnowbaseLibrary, list[httpx.Request], FakeOidc]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answer(request)

    oidc = FakeOidc()
    return (
        KnowbaseLibrary(
            environ(**env), transport=httpx.MockTransport(handler), oidc_client=oidc
        ),
        seen,
        oidc,
    )


def entry(
    material_id: str = "m1",
    *,
    mine: bool = True,
    license_: str = "sale",
    deleted: bool = False,
) -> dict[str, object]:
    """knowbase 书架条目（ShelfEntryRead）里小店用得到的几样。"""
    return {
        "category": "uploaded" if mine else "purchased",
        "material": {
            "material_id": material_id,
            "name": "茅台年报.pdf",
            "mine": mine,
            "license": license_,
            "deleted": deleted,
        },
        "holding": {"basis": "upload", "license": license_, "version_no": None},
        "version": {
            "version_no": 2,
            "sha256": "ab" * 32,
            "size_bytes": 1024,
            "media_type": "application/pdf",
        },
    }


async def test_the_column_is_read_with_the_users_token():
    kb, seen, oidc = client(lambda r: httpx.Response(200, json=[entry()]))
    assert await kb.column("user-tok") == [entry()]
    request = seen[0]
    assert request.method == "GET"
    assert request.url.path == "/api/internal/v2/user-data/columns/knowmart"
    assert request.headers["authorization"] == "Bearer user-tok"
    assert oidc.calls == []  # 用户的事不用服务身份


async def test_purchases_use_the_purchase_relation_and_cache_the_token():
    kb, seen, oidc = client(
        lambda r: httpx.Response(
            201,
            json={
                "material_id": "m1",
                "buyer": "b",
                "version_no": 2,
                "license": "self",
            },
        )
    )
    for n in range(2):
        got = await kb.purchase(
            seller="s", material_id="m1", buyer="b", transaction_ref=f"tx:{n}"
        )
    assert got["version_no"] == 2
    assert oidc.calls == ["knowbase:user-data-purchase"]
    assert seen[0].url.path == "/api/internal/v2/user-data/purchases"
    assert seen[0].headers["authorization"] == "Bearer svc-knowbase:user-data-purchase"
    assert json.loads(seen[0].content) == {
        "seller": "s",
        "material_id": "m1",
        "buyer": "b",
        "transaction_ref": "tx:0",
        "resale_allowed": False,
    }


async def test_refusals_keep_knowbases_code_and_failures_are_unavailable():
    kb, _, _ = client(
        lambda r: httpx.Response(403, json={"code": "column_denied", "message": "x"})
    )
    with pytest.raises(KnowbaseRefused) as refused:
        await kb.column("user-tok")
    assert (refused.value.status, refused.value.code) == (403, "column_denied")
    kb, _, _ = client(lambda r: httpx.Response(503, json={}))
    with pytest.raises(ServiceUnavailableError) as down:
        await kb.column("user-tok")
    assert down.value.code == "knowbase_unavailable"

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    kb, _, _ = client(unreachable)
    with pytest.raises(ServiceUnavailableError):
        await kb.column("user-tok")


@pytest.mark.parametrize(
    "env",
    [
        {"KNOWBASE_INTERNAL_URL": ""},
        {"KNOWBASE_INTERNAL_URL": "http://knowbase.internal"},  # 只走 https
    ],
)
async def test_without_a_knowbase_address_nothing_is_sent(env):
    kb, seen, _ = client(lambda r: httpx.Response(200, json=[]), **env)
    with pytest.raises(ServiceUnavailableError) as caught:
        await kb.column("user-tok")
    assert caught.value.code == "knowbase_unconfigured" and seen == []


async def test_a_missing_purchase_relation_is_unconfigured():
    kb, seen, _ = client(
        lambda r: httpx.Response(201, json={}),
        **{PREFIX + "SCOPE": "knowbase:something-else"},
    )
    with pytest.raises(ServiceUnavailableError) as caught:
        await kb.purchase(seller="s", material_id="m", buyer="b", transaction_ref="t")
    assert caught.value.code == "knowbase_unconfigured" and seen == []


def test_only_own_live_material_licensed_for_sale_is_sellable():
    view = material_view(entry())
    assert view == {
        "material_id": "m1",
        "name": "茅台年报.pdf",
        "version_no": 2,
        "media_type": "application/pdf",
        "size_bytes": 1024,
        "sha256": "ab" * 32,
        "license": "sale",
        "mine": True,
        "sellable": True,
    }
    assert not material_view(entry(license_="self"))["sellable"]
    assert not material_view(entry(mine=False))["sellable"]  # 买来的不能再卖
    assert not material_view(entry(deleted=True))["sellable"]


async def test_the_vault_turns_knowbase_refusals_into_market_errors():
    vault = KnowbaseVault(
        client(lambda r: httpx.Response(404, json={"code": "not_found"}))[0]
    )
    with pytest.raises(NotFoundError) as gone:
        await vault.purchase(
            seller="s", material_id="m", buyer="b", transaction_ref="t"
        )
    assert gone.value.code == "item_gone"
    vault = KnowbaseVault(
        client(lambda r: httpx.Response(409, json={"code": "not_for_sale"}))[0]
    )
    with pytest.raises(ConcurrencyConflictError) as lowered:
        await vault.purchase(
            seller="s", material_id="m", buyer="b", transaction_ref="t"
        )
    assert lowered.value.code == "item_not_for_sale"
    vault = KnowbaseVault(
        client(lambda r: httpx.Response(403, json={"code": "column_denied"}))[0]
    )
    with pytest.raises(BadGatewayError):
        await vault.materials("user-tok")
    vault = KnowbaseVault(
        client(lambda r: httpx.Response(200, json=[entry(), entry("m2", mine=False)]))[
            0
        ]
    )
    assert [m["sellable"] for m in await vault.materials("user-tok")] == [True, False]


class TradingAuth:
    def __init__(self) -> None:
        self.calls: list[tuple[str | None, str]] = []

    async def user_token(self, session_id: str | None, *, audience: str) -> str:
        self.calls.append((session_id, audience))
        return "for-knowbase"


async def test_the_web_dependency_trades_this_sessions_token(monkeypatch):
    from app.interfaces.http.middleware import auth as middleware
    from core.config import Settings

    trading = TradingAuth()
    monkeypatch.setattr(middleware, "web_auth_service", trading)
    monkeypatch.setattr(
        middleware,
        "get_settings",
        lambda: Settings(_env_file=None, knowbase_user_audience="knowbase-client"),
    )
    token = await middleware.get_web_knowbase_token(session=None, session_id="sid")
    assert token == "for-knowbase"
    assert trading.calls == [("sid", "knowbase-client")]
    monkeypatch.setattr(middleware, "get_settings", lambda: Settings(_env_file=None))
    with pytest.raises(ServiceUnavailableError) as caught:
        await middleware.get_web_knowbase_token(session=None, session_id="sid")
    assert caught.value.code == "knowbase_unconfigured"
