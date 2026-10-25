"""knowmart 知识库（SDD 0042 第八节）。

网页经后端、凭用户令牌转给 knowbase 的 knowmart 分区。
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.application.errors.exceptions import ServiceUnavailableError, UnauthorizedError
from app.infrastructure.external.knowbase_user_data import Content, KnowbaseRefused
from app.interfaces.endpoints import library_routes as library
from app.interfaces.errors.exception_handlers import register_exception_handlers

ITEM = {
    "item_id": "i1",
    "app": "knowmart",
    "name": "行业数据 2024.xlsx",
    "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "size_bytes": 6,
    "sha256": "ab" * 32,
    "origin": "upload",
    "origin_ref": None,
    "created_at": "2026-10-23T12:00:00Z",
}


class FakeKnowbase:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.refuse: KnowbaseRefused | None = None

    def _maybe_refuse(self) -> None:
        if self.refuse is not None:
            raise self.refuse

    async def items(self, token: str) -> list[dict]:
        self.calls.append(("items", token))
        self._maybe_refuse()
        return [ITEM]

    async def upload(self, token: str, name: str, content: bytes) -> dict:
        self.calls.append(("upload", token, name, content))
        self._maybe_refuse()
        return {**ITEM, "name": name, "size_bytes": len(content)}

    async def item(self, token: str, item_id: str) -> dict:
        self.calls.append(("item", token, item_id))
        self._maybe_refuse()
        return ITEM

    async def download(self, token: str, item_id: str) -> Content:
        self.calls.append(("download", token, item_id))
        return Content(
            data=b"PK\x03\x04", media_type=ITEM["media_type"], sha256="ab" * 32
        )

    async def delete(self, token: str, item_id: str) -> None:
        self.calls.append(("delete", token, item_id))
        self._maybe_refuse()


@pytest.fixture
def api():
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(library.router, prefix="/api")
    knowbase = FakeKnowbase()
    state: dict[str, object] = {"token": "user-token"}

    def token() -> str:
        if isinstance(state["token"], Exception):
            raise state["token"]
        return str(state["token"])

    app.dependency_overrides[library.get_web_knowbase_token] = token
    app.dependency_overrides[library.get_knowbase] = lambda: knowbase
    app.dependency_overrides[library.get_web_current_user] = lambda: SimpleNamespace(
        subject="seller-1"
    )
    listed: set[tuple[str, str]] = set()
    state["listed"] = listed

    async def check(seller: str, item_id: str) -> bool:
        return (seller, item_id) in listed

    app.dependency_overrides[library.get_listed_check] = lambda: check
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://mart"
    )
    return client, knowbase, state


BASE = "/api/web/v1/knowmart/library"


async def test_listing_uploading_downloading_and_deleting(api):
    client, knowbase, _ = api
    body = (await client.get(BASE)).json()
    assert [i["name"] for i in body["items"]] == ["行业数据 2024.xlsx"]
    assert "origin_ref" not in body["items"][0] and "app" not in body["items"][0]
    created = await client.post(BASE, params={"name": "样张.csv"}, content=b"a,b\n")
    assert created.status_code == 201 and created.json()["item"]["name"] == "样张.csv"
    got = await client.get(f"{BASE}/i1/content")
    assert got.content == b"PK\x03\x04"
    assert got.headers["content-disposition"].startswith(
        "attachment; filename*=UTF-8''"
    )
    assert (await client.delete(f"{BASE}/i1")).status_code == 204
    assert [c[0] for c in knowbase.calls] == [
        "items",
        "upload",
        "item",
        "download",
        "delete",
    ]


async def test_oversized_uploads_stop_before_knowbase(api, monkeypatch):
    client, knowbase, _ = api
    monkeypatch.setattr(library, "MAX_UPLOAD_BYTES", 4)
    refused = await client.post(BASE, params={"name": "a.csv"}, content=b"12345")
    assert (refused.status_code, refused.json()["code"]) == (413, "too_large")
    assert knowbase.calls == []


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [(409, "in_use", 409), (404, "item_not_found", 404), (418, "odd", 502)],
)
async def test_knowbase_refusals_keep_their_code(api, status, code, expected):
    client, knowbase, _ = api
    knowbase.refuse = KnowbaseRefused(status, code)
    answer = await client.delete(f"{BASE}/i1")
    assert (answer.status_code, answer.json()["code"]) == (expected, code)


async def test_sign_in_again_and_unconfigured_reach_the_page(api):
    client, _, state = api
    state["token"] = UnauthorizedError("Sign in again", code="reauth_required")
    answer = await client.get(BASE)
    assert (answer.status_code, answer.json()["code"]) == (401, "reauth_required")
    state["token"] = ServiceUnavailableError("x", code="knowbase_unconfigured")
    answer = await client.get(BASE)
    assert (answer.status_code, answer.json()["code"]) == (503, "knowbase_unconfigured")


async def test_what_is_up_for_sale_is_delisted_before_it_is_deleted(api) -> None:
    """待审、在架的条目里还有这份：不让删，先下架（Fable 评审 P1-2）。"""
    client, knowbase, state = api
    state["listed"].add(("seller-1", "i1"))  # type: ignore[union-attr]
    refused = await client.delete("/api/web/v1/knowmart/library/i1")
    assert (refused.status_code, refused.json()["code"]) == (409, "item_listed")
    assert not any(c[0] == "delete" for c in knowbase.calls)
