"""分身的接口（SDD 0027 §3.2）。

内部（服务身份）：给 knowlab 交付、给工作台取清单。网页（会话）：「我的分身」。
所有者一律是 Casdoor 用户 id：内部接口由调用方从它自己已认证的会话里取来，放进
路径或请求体，浏览器给不了；网页接口取当前会话。不是你的和不存在一样，都是找不到。
"""

from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, Path
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.errors.exceptions import BadRequestError, NotFoundError
from app.application.services.knowbot_service import KnowbotService, version_view
from app.application.services.market import MarketService
from app.bootstrap.knowbots import knowbot_service, market_service
from app.domain.cross_app import clean_origin, known_apps, return_url
from app.domain.security import BrowserSession, Principal
from app.infrastructure.security.relation_auth import (
    ServiceCaller,
    require_deliver_service,
    require_summon_service,
)
from app.infrastructure.storage.postgres import get_db_session
from app.interfaces.http.internal.host_guard import internal_host_only
from app.interfaces.http.middleware.auth import (
    get_web_browser_session,
    get_web_current_user,
)
from core.config import Settings, get_settings

CONTRACT_VERSION = 1
NO_STORE = {"Cache-Control": "no-store"}

internal_router = APIRouter(
    prefix="/internal/v1/knowmart",
    tags=["Knowbots (internal)"],
    dependencies=[Depends(internal_host_only)],  # 公网地址来的当没有（审读第 1 条）
)
web_router = APIRouter(prefix="/web/v1/knowbots", tags=["Knowbots"])


def service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> KnowbotService:
    return knowbot_service(session)


def knowbot_settings() -> Settings:
    """测试用依赖覆盖换配置。"""
    return get_settings()


Service = Annotated[KnowbotService, Depends(service)]


def market_for_request(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> MarketService:
    return market_service(session)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeliveryIn(Strict):
    delivery_id: str = Field(min_length=8, max_length=128)
    owner_subject: str = Field(min_length=1, max_length=512)
    knowbot_ref: str = Field(min_length=1, max_length=128)
    manifest: dict[str, Any]
    # knowlab 运营审核定了公开（SDD 0037 第十八之二节）：
    # 资质过的进上架审核，不过的留自用
    request_public: bool = False


class SummonReturnIn(Strict):
    source: str = Field(alias="from", min_length=1, max_length=64)
    ref: str | None = Field(default=None, max_length=256)


@internal_router.post("/deliveries")
async def deliver(
    _: Annotated[ServiceCaller, Depends(require_deliver_service)],
    body: DeliveryIn,
    svc: Service,
    market: Annotated[MarketService, Depends(market_for_request)],
) -> JSONResponse:
    """knowlab 交付一个测试通过的版本。

    幂等：同一交付单号重发，内容一样回 200，不一样回 409。版本号按清单里的记（knowlab
    测试通过时定，可以有空号）。带了申请公开的，回这一位的上架状态。
    """
    result = await svc.deliver(
        delivery_id=body.delivery_id,
        owner_subject=body.owner_subject,
        knowbot_ref=body.knowbot_ref,
        manifest=body.manifest,
    )
    content: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "knowbot_id": str(result.knowbot_id),
        "version": result.version,
        "digest": result.digest,
        "replayed": result.replayed,
    }
    if body.request_public:
        content["listing_status"] = await market.ask_public_on_delivery(
            body.owner_subject, str(result.knowbot_id)
        )
    return JSONResponse(
        status_code=200 if result.replayed else 201,
        content=content,
        headers=NO_STORE,
    )


@internal_router.post(
    "/owners/{owner_subject}/knowbots/{knowbot_id}/versions/{version}/stop",
    status_code=204,
)
async def stop_delivered_version(
    _: Annotated[ServiceCaller, Depends(require_deliver_service)],
    svc: Service,
    owner_subject: Annotated[str, Path(min_length=1, max_length=512)],
    knowbot_id: Annotated[str, Path(max_length=64)],
    version: Annotated[str, Path(max_length=16)],
) -> Response:
    """knowlab 停用某一版（那边用户或运营停的，SDD 0037 第十八之二节）。

    服务关系同交付；只认这个所有者的，别人的与没有的都是 404；停过的再停照样 204。
    """
    await svc.stop(
        owner_subject=owner_subject,
        knowbot_id=knowbot_id,
        version=version,
        by_knowlab=True,
    )
    return Response(status_code=204, headers=NO_STORE)


class UnlistedIn(Strict):
    versions: list[dict[str, Any]] = Field(max_length=200)


@internal_router.put(
    "/owners/{owner_subject}/sources/{knowbot_ref}/unlisted", status_code=204
)
async def set_unlisted(
    _: Annotated[ServiceCaller, Depends(require_deliver_service)],
    body: UnlistedIn,
    svc: Service,
    owner_subject: Annotated[str, Path(min_length=1, max_length=512)],
    knowbot_ref: Annotated[str, Path(min_length=1, max_length=128)],
) -> Response:
    """knowlab 交来这个创意没上架那几版的结局（用户不要 / 审核没过，SDD 0037 第三节）。

    整份替换，重发安全；服务关系同交付。详情页拿它写明空号那一版。
    """
    await svc.set_unlisted(
        owner_subject=owner_subject, knowbot_ref=knowbot_ref, versions=body.versions
    )
    return Response(status_code=204, headers=NO_STORE)


def _split_ref(ref: str) -> tuple[str, str]:
    knowbot_id, sep, version = ref.partition("@")
    if not sep or not knowbot_id or not version:
        raise NotFoundError("No such knowbot", code="knowbot_not_found")
    return knowbot_id, version


@internal_router.get("/owners/{owner_subject}/knowbots/{knowbot_ref}")
async def summon(
    caller: Annotated[ServiceCaller, Depends(require_summon_service)],
    svc: Service,
    owner_subject: Annotated[str, Path(min_length=1, max_length=512)],
    knowbot_ref: Annotated[str, Path(min_length=3, max_length=160)],
) -> JSONResponse:
    """运行方「请」：这个所有者的这一版清单原文与摘要。

    别人的、停用的、没有的、运行方不是这个调用方的，都是 404。
    """
    knowbot_id, version = _split_ref(knowbot_ref)
    found = await svc.summon(
        owner_subject=owner_subject,
        knowbot_id=knowbot_id,
        version=version,
        runtime=caller.caller,
    )
    return JSONResponse(
        content={
            "contract_version": CONTRACT_VERSION,
            "knowbot_id": str(found.knowbot_id),
            "name": found.name,
            "tagline": found.tagline,
            "version": found.version.version,
            "digest": found.version.digest,
            "manifest": found.version.manifest,
        },
        headers=NO_STORE,
    )


@web_router.get("")
async def my_knowbots(
    principal: Annotated[Principal, Depends(get_web_current_user)], svc: Service
) -> JSONResponse:
    """我的分身：只有当前用户自己的。不给清单原文。"""
    bots = await svc.list_for_owner(principal.subject)
    return JSONResponse(
        content={
            "contract_version": CONTRACT_VERSION,
            "knowbots": [
                {
                    "knowbot_id": str(b.id),
                    "name": b.name,
                    "tagline": b.tagline,
                    "current_version": b.current_version,
                    "versions": [version_view(v) for v in b.versions],
                }
                for b in bots
            ],
        },
        headers=NO_STORE,
    )


@web_router.post("/{knowbot_id}/versions/{version}/stop", status_code=204)
async def stop_version(
    session: Annotated[BrowserSession, Depends(get_web_browser_session)],
    svc: Service,
    knowbot_id: Annotated[str, Path(max_length=64)],
    version: Annotated[str, Path(max_length=16)],
) -> Response:
    """停用某一版：之后请不了；已经请去在跑的不受影响。

    knowlab 交来的专家回 409 `stop_in_knowlab`：到 knowlab 的「专家」里停
    （SDD 0037 第六节）。
    """
    await svc.stop(
        owner_subject=session.principal.subject, knowbot_id=knowbot_id, version=version
    )
    return Response(status_code=204, headers=NO_STORE)


def _with_knowbot(url: str, knowbot: str) -> str:
    parts = urlsplit(url)
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k != "knowbot"
    ]
    query.append(("knowbot", knowbot))
    return urlunsplit(parts._replace(query=urlencode(query)))


@web_router.post("/{knowbot_id}/versions/{version}/summon-return")
async def summon_return(
    session: Annotated[BrowserSession, Depends(get_web_browser_session)],
    body: SummonReturnIn,
    svc: Service,
    market: Annotated[MarketService, Depends(market_for_request)],
    settings: Annotated[Settings, Depends(knowbot_settings)],
    knowbot_id: Annotated[str, Path(max_length=64)],
    version: Annotated[str, Path(max_length=16)],
) -> JSONResponse:
    """「请这位」：核验是自己的、未停用的、运行方是来源应用的这一版，带上
    knowbot=<分身>@<版本> 回去。

    回跳地址按 from 只从本应用配置里查，链接里带的任何地址都不用（跨应用跳转规则）。
    对方收到后还会按自己的会话向这里的内部接口再核一次，伪造参数只会得到「找不到」。
    """
    sources = settings.cross_app_sources()
    origin = clean_origin(body.source, body.ref, known_apps=known_apps(sources))
    target = return_url(sources.get(origin.app) if origin else None, origin)
    if origin is None or target is None:
        raise BadRequestError(
            "There is no registered place to return to", code="no_return_target"
        )
    # 从哪个运行方来，就只能选运行方是它的分身
    found = await svc.summon(
        owner_subject=session.principal.subject,
        knowbot_id=knowbot_id,
        version=version,
        runtime=origin.app,
    )
    # 请的是别人公开的专家：记一笔成交（供需市场，本期不收钱）
    await market.record_summon(
        buyer=session.principal.subject,
        knowbot_id=str(found.knowbot_id),
        version=found.version.version,
    )
    return JSONResponse(
        content={
            "contract_version": CONTRACT_VERSION,
            "return_url": _with_knowbot(
                target, f"{found.knowbot_id}@{found.version.version}"
            ),
        },
        headers=NO_STORE,
    )
