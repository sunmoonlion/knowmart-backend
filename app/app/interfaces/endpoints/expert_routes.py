"""专家目录的网页接口（SDD 0032）：首页列表、类目、详情。

只取当前会话的用户；谁看得到哪个专家由 ExpertCatalog 判。看不到的回「找不到」。
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.expert_catalog import CatalogQuery, ExpertCatalog
from app.bootstrap.knowbots import expert_catalog
from app.domain.security import Principal
from app.infrastructure.storage.postgres import get_db_session
from app.interfaces.http.middleware.auth import get_web_current_user

CONTRACT_VERSION = 1
NO_STORE = {"Cache-Control": "no-store"}

router = APIRouter(prefix="/web/v1/knowmart", tags=["Expert catalog"])


def catalog(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ExpertCatalog:
    return expert_catalog(session)


Catalog = Annotated[ExpertCatalog, Depends(catalog)]
User = Annotated[Principal, Depends(get_web_current_user)]


@router.get("/experts")
async def experts(
    principal: User,
    svc: Catalog,
    category: Annotated[str | None, Query(max_length=64, pattern=r"^[a-z_.]+$")] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    data: Literal["all", "own", "user"] = "all",
    updated: Literal["any", "7d", "30d"] = "any",
    sort: Literal["updated", "name"] = "updated",
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
) -> JSONResponse:
    """首页：当前用户看得到的专家，按筛选条件筛过、排过、分好页；附类目与数量。"""
    found = await svc.list(
        principal.subject,
        CatalogQuery(
            category=category, q=q, data=data, updated=updated, sort=sort, page=page
        ),
    )
    return JSONResponse(
        content={"contract_version": CONTRACT_VERSION, **found}, headers=NO_STORE
    )


@router.get("/categories")
async def categories(principal: User, svc: Catalog) -> JSONResponse:
    """类目树：只列当前用户看得到专家的类目，带数量。"""
    return JSONResponse(
        content={
            "contract_version": CONTRACT_VERSION,
            "categories": await svc.categories(principal.subject),
        },
        headers=NO_STORE,
    )


@router.get("/experts/{knowbot_id}")
async def expert(
    principal: User,
    svc: Catalog,
    knowbot_id: Annotated[str, Path(max_length=64)],
    version: Annotated[str | None, Query(max_length=16)] = None,
) -> JSONResponse:
    """详情：能做什么、需要的数据、交回什么、版本记录；公开专家另有发布主体。"""
    found = await svc.detail(principal.subject, knowbot_id, version)
    return JSONResponse(
        content={"contract_version": CONTRACT_VERSION, "expert": found},
        headers=NO_STORE,
    )
