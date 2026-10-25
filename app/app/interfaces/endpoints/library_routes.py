"""knowmart 知识库（SDD 0042 第八节，D 类）：卖家放上架出售的资料。

真源在 knowbase 的 knowmart 分区；这里只转交，凭用户本人的令牌（会话里留的访问
令牌换来的），knowbase 按令牌认人、认分区。知识条目（供需市场）从这里选资料；卖出时
knowbase 复制一份给买家，卖家删了不影响已卖出的。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.errors.exceptions import AppException
from app.application.services.market import listed_with_item
from app.domain.security import Principal
from app.infrastructure.external.knowbase_user_data import (
    KnowbaseRefused,
    KnowbaseUserData,
)
from app.infrastructure.repositories.market import SqlMarketRepository
from app.infrastructure.storage.postgres import get_db_session
from app.interfaces.http.middleware.auth import (
    get_web_current_user,
    get_web_knowbase_token,
)

CONTRACT_VERSION = 1
NO_STORE = {"Cache-Control": "no-store"}
# 与 knowbase 一致：单份 50 MB；超了不收完就拒
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_client: KnowbaseUserData | None = None

router = APIRouter(prefix="/web/v1/knowmart", tags=["Knowledge base"])


def get_knowbase() -> KnowbaseUserData:
    global _client
    if _client is None:
        _client = KnowbaseUserData()
    return _client


def get_listed_check(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> Callable[[str, str], Awaitable[bool]]:
    """（卖家, 资料编号）→ 待审、在架的条目里还有没有这份。"""
    repo = SqlMarketRepository(session)
    return lambda seller, item_id: listed_with_item(repo, seller, item_id)


Token = Annotated[str, Depends(get_web_knowbase_token)]
Knowbase = Annotated[KnowbaseUserData, Depends(get_knowbase)]
User = Annotated[Principal, Depends(get_web_current_user)]
ListedCheck = Annotated[
    Callable[[str, str], Awaitable[bool]], Depends(get_listed_check)
]


def _refused(exc: KnowbaseRefused) -> AppException:
    # knowbase 的 code 原样给网页，网页按 code 说人话
    status = exc.status if exc.status in (401, 403, 404, 409, 413, 422) else 502
    return AppException(code=exc.code, status_code=status, msg=exc.code)


def item_view(raw: dict[str, Any]) -> dict[str, Any]:
    keep = ("item_id", "name", "media_type", "size_bytes", "sha256", "origin")
    return {**{k: raw.get(k) for k in keep}, "created_at": raw.get("created_at")}


@router.get("/library")
async def library_listing(token: Token, knowbase: Knowbase) -> JSONResponse:
    try:
        items = await knowbase.items(token)
    except KnowbaseRefused as exc:
        raise _refused(exc) from None
    return JSONResponse(
        {"contract_version": CONTRACT_VERSION, "items": [item_view(i) for i in items]},
        headers=NO_STORE,
    )


@router.post("/library", status_code=201)
async def library_upload(
    request: Request,
    token: Token,
    knowbase: Knowbase,
    name: Annotated[str, Query(min_length=1, max_length=200)],
) -> JSONResponse:
    """请求体就是文件本身。"""
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_UPLOAD_BYTES:
            raise AppException(code="too_large", status_code=413, msg="too_large")
        chunks.append(chunk)
    try:
        item = await knowbase.upload(token, name, b"".join(chunks))
    except KnowbaseRefused as exc:
        raise _refused(exc) from None
    return JSONResponse(
        {"contract_version": CONTRACT_VERSION, "item": item_view(item)},
        status_code=201,
        headers=NO_STORE,
    )


@router.get("/library/{item_id}/content")
async def library_download(item_id: str, token: Token, knowbase: Knowbase) -> Response:
    try:
        meta = await knowbase.item(token, item_id)
        content = await knowbase.download(token, item_id)
    except KnowbaseRefused as exc:
        raise _refused(exc) from None
    filename = quote(str(meta.get("name") or "download"), safe="")
    return Response(
        content=content.data,
        media_type=content.media_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
            "X-Content-SHA256": content.sha256,
            **NO_STORE,
        },
    )


@router.delete("/library/{item_id}", status_code=204)
async def library_delete(
    item_id: str,
    token: Token,
    knowbase: Knowbase,
    user: User,
    listed: ListedCheck,
) -> Response:
    """删了不影响已卖出的：买家那份是复制的。

    待审、在架的条目里还有这份就不让删，先下架那条：不然正在买的人只拿到半份
    （Fable 评审 P1-2）。
    """
    if await listed(user.subject, item_id):
        raise AppException(code="item_listed", status_code=409, msg="item_listed")
    try:
        await knowbase.delete(token, item_id)
    except KnowbaseRefused as exc:
        raise _refused(exc) from None
    return Response(status_code=204, headers=NO_STORE)
