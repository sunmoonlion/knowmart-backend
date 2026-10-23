"""供需市场的接口（SDD 0018 第十一节）：web端给买卖双方，管理端给运营。

谁能看什么由 MarketService / MarketAdmin 判；这里只取会话里的人、转请求、包回信封。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.errors.exceptions import AppException
from app.application.services.market import MarketAdmin, MarketService
from app.bootstrap.knowbots import market_admin, market_service
from app.domain.market import (
    DOCUMENT_MAX_BYTES,
    BuyIn,
    DemandIn,
    ExpertListingIn,
    KnowledgeListingIn,
    LicenseIn,
    MessageIn,
    PublisherIn,
    ReplyIn,
    ReportDecision,
    ReportIn,
    ResponseIn,
    ReviewDecision,
    ReviewIn,
    RulesIn,
)
from app.domain.security import Principal
from app.infrastructure.storage.postgres import get_db_session
from app.interfaces.http.middleware.auth import (
    get_admin_current_user,
    get_web_current_user,
    get_web_knowbase_token,
    require_knowmart_admin,
)

CONTRACT_VERSION = 1
NO_STORE = {"Cache-Control": "no-store"}

web_router = APIRouter(prefix="/web/v1/knowmart/market", tags=["Market"])
admin_router = APIRouter(
    prefix="/admin/v1/knowmart/market",
    tags=["Market admin"],
    dependencies=[Depends(require_knowmart_admin)],
)


def _service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> MarketService:
    return market_service(session)


def _admin(session: Annotated[AsyncSession, Depends(get_db_session)]) -> MarketAdmin:
    return market_admin(session)


def _token(token: Annotated[str, Depends(get_web_knowbase_token)]):  # type: ignore[no-untyped-def]
    async def provide() -> str:
        return token

    return provide


Service = Annotated[MarketService, Depends(_service)]
Admin = Annotated[MarketAdmin, Depends(_admin)]
User = Annotated[Principal, Depends(get_web_current_user)]
Operator = Annotated[Principal, Depends(get_admin_current_user)]
Id = Annotated[str, Path(min_length=1, max_length=160)]
Page = Annotated[int, Query(ge=1, le=10000)]


def ok(body: Any, status_code: int = 200) -> JSONResponse:
    content = body if isinstance(body, dict) else {"items": body}
    return JSONResponse(
        {"contract_version": CONTRACT_VERSION, **content},
        status_code=status_code,
        headers=NO_STORE,
    )


# ======================= web端 =======================


@web_router.get("/rules")
async def rules(_: User, svc: Service) -> JSONResponse:
    """驻场规则（平台定）：当前这一版。"""
    return ok(await svc.rules())


@web_router.get("/publisher")
async def my_publisher(principal: User, svc: Service) -> JSONResponse:
    """我的发布主体：实名材料（只给本人看）、照片、资质。"""
    return ok(await svc.my_publisher(principal.subject))


@web_router.put("/publisher")
async def save_publisher(
    principal: User, body: PublisherIn, svc: Service
) -> JSONResponse:
    """入驻或改资料：改了就重新审。"""
    return ok(await svc.save_publisher(principal.subject, body))


@web_router.post("/publisher/documents", status_code=201)
async def add_document(
    principal: User,
    request: Request,
    svc: Service,
    kind: Annotated[str, Query(max_length=32)],
    content_type: Annotated[str, Header(alias="Content-Type")] = "",
) -> JSONResponse:
    """传一张证件照片（JPEG、PNG 或 PDF，不超过 5 MB）。

    请求体就是文件本身；只给运营看。
    """
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > DOCUMENT_MAX_BYTES:
            raise AppException(
                code="document_too_large", status_code=413, msg="too large"
            )
        chunks.append(chunk)
    found = await svc.add_document(
        principal.subject, kind, content_type, b"".join(chunks)
    )
    return ok({"document": found}, status_code=201)


@web_router.post("/publisher/licenses", status_code=201)
async def add_license(principal: User, body: LicenseIn, svc: Service) -> JSONResponse:
    return ok(
        {"license": await svc.add_license(principal.subject, body)}, status_code=201
    )


@web_router.put("/experts/{knowbot_id}/listing")
async def expert_listing(
    principal: User, body: ExpertListingIn, svc: Service, knowbot_id: Id
) -> JSONResponse:
    """专家条目：自用，或申请公开（要资质，运营审过才公开）；每次请的价格。"""
    return ok(
        {"listing": await svc.set_expert_listing(principal.subject, knowbot_id, body)}
    )


@web_router.get("/experts/{knowbot_id}/market")
async def expert_market(_: User, svc: Service, knowbot_id: Id) -> JSONResponse:
    """专家详情页上的市场部分：价格、评价。"""
    return ok(await svc.expert_market(knowbot_id))


@web_router.get("/knowledge")
async def knowledge_market(
    _: User,
    svc: Service,
    q: Annotated[str | None, Query(max_length=100)] = None,
    category: Annotated[str | None, Query(max_length=64, pattern=r"^[a-z_.]+$")] = None,
    page: Page = 1,
) -> JSONResponse:
    """市场里的知识：上架的，按类目、关键词找。"""
    return ok(await svc.knowledge_market(q=q, category=category, page=page))


@web_router.get("/knowledge/mine")
async def my_listings(principal: User, svc: Service) -> JSONResponse:
    return ok(await svc.my_listings(principal.subject))


@web_router.post("/knowledge", status_code=201)
async def create_listing(
    principal: User,
    body: KnowledgeListingIn,
    svc: Service,
    token: Annotated[Any, Depends(_token)],
) -> JSONResponse:
    """写一条知识条目（草稿）：资料从自己的 knowmart 知识库里选。"""
    found = await svc.create_listing(principal.subject, body, token)
    return ok({"listing": found}, status_code=201)


@web_router.get("/knowledge/{listing_id}")
async def listing(principal: User, svc: Service, listing_id: Id) -> JSONResponse:
    return ok({"listing": await svc.listing(principal.subject, listing_id)})


@web_router.put("/knowledge/{listing_id}")
async def update_listing(
    principal: User,
    body: KnowledgeListingIn,
    svc: Service,
    listing_id: Id,
    token: Annotated[Any, Depends(_token)],
) -> JSONResponse:
    found = await svc.update_listing(principal.subject, listing_id, body, token)
    return ok({"listing": found})


@web_router.post("/knowledge/{listing_id}/submit")
async def submit_listing(principal: User, svc: Service, listing_id: Id) -> JSONResponse:
    """交运营审核。"""
    return ok({"listing": await svc.submit_listing(principal.subject, listing_id)})


@web_router.post("/knowledge/{listing_id}/delist")
async def delist_listing(principal: User, svc: Service, listing_id: Id) -> JSONResponse:
    """下架：买家那份照旧在。"""
    return ok({"listing": await svc.delist_listing(principal.subject, listing_id)})


@web_router.post("/knowledge/{listing_id}/buy", status_code=201)
async def buy(
    principal: User, body: BuyIn, svc: Service, listing_id: Id
) -> JSONResponse:
    """买知识：复制一份进你选的知识库；本期标价、记成交，不收钱。"""
    found = await svc.buy(principal.subject, listing_id, body)
    return ok({"trade": found}, status_code=201)


@web_router.get("/demands")
async def demand_hall(
    principal: User,
    svc: Service,
    kind: Literal["expert", "knowledge"] | None = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    page: Page = 1,
) -> JSONResponse:
    """需求大厅：还在收响应的。"""
    return ok(
        await svc.demand_hall(kind=kind, q=q, page=page, viewer=principal.subject)
    )


@web_router.get("/demands/mine")
async def my_demands(principal: User, svc: Service) -> JSONResponse:
    return ok(await svc.my_demands(principal.subject))


@web_router.post("/demands", status_code=201)
async def create_demand(principal: User, body: DemandIn, svc: Service) -> JSONResponse:
    found = await svc.create_demand(principal.subject, body)
    return ok({"demand": found}, status_code=201)


@web_router.get("/demands/{demand_id}")
async def demand(principal: User, svc: Service, demand_id: Id) -> JSONResponse:
    """一条需求：响应只有需求方与那位卖家看得到，别人只看到有几个。"""
    return ok({"demand": await svc.demand(principal.subject, demand_id)})


@web_router.post("/demands/{demand_id}/close")
async def close_demand(principal: User, svc: Service, demand_id: Id) -> JSONResponse:
    return ok({"demand": await svc.close_demand(principal.subject, demand_id)})


@web_router.post("/demands/{demand_id}/responses", status_code=201)
async def respond(
    principal: User, body: ResponseIn, svc: Service, demand_id: Id
) -> JSONResponse:
    found = await svc.respond(principal.subject, demand_id, body)
    return ok({"response": found}, status_code=201)


@web_router.post("/demands/{demand_id}/choose/{response_id}")
async def choose(
    principal: User, svc: Service, demand_id: Id, response_id: Id
) -> JSONResponse:
    """买家选一家：别的响应结束。"""
    return ok(
        {"demand": await svc.choose_response(principal.subject, demand_id, response_id)}
    )


@web_router.get("/responses/mine")
async def my_responses(principal: User, svc: Service) -> JSONResponse:
    return ok(await svc.my_responses(principal.subject))


@web_router.post("/responses/{response_id}/withdraw")
async def withdraw(principal: User, svc: Service, response_id: Id) -> JSONResponse:
    return ok({"response": await svc.withdraw_response(principal.subject, response_id)})


@web_router.get("/trades")
async def my_trades(
    principal: User, svc: Service, role: Literal["buyer", "seller"] = "buyer"
) -> JSONResponse:
    """我买到的（buyer）、我卖出的（seller）。"""
    return ok(await svc.my_trades(principal.subject, role))


@web_router.post("/trades/{trade_id}/review", status_code=201)
async def review(
    principal: User, body: ReviewIn, svc: Service, trade_id: Id
) -> JSONResponse:
    found = await svc.review(principal.subject, trade_id, body)
    return ok({"review": found}, status_code=201)


@web_router.post("/trades/{trade_id}/reply")
async def reply(
    principal: User, body: ReplyIn, svc: Service, trade_id: Id
) -> JSONResponse:
    return ok({"review": await svc.reply(principal.subject, trade_id, body)})


@web_router.get("/messages/{thread_kind}/{thread_id}")
async def messages(
    principal: User,
    svc: Service,
    thread_kind: Literal["demand", "response"],
    thread_id: Id,
) -> JSONResponse:
    return ok(await svc.messages(principal.subject, thread_kind, thread_id))


@web_router.post("/messages/{thread_kind}/{thread_id}", status_code=201)
async def post_message(
    principal: User,
    body: MessageIn,
    svc: Service,
    thread_kind: Literal["demand", "response"],
    thread_id: Id,
) -> JSONResponse:
    """平台内留言，不显示联系方式。"""
    found = await svc.post_message(principal.subject, thread_kind, thread_id, body)
    return ok({"message": found}, status_code=201)


@web_router.post("/reports", status_code=201)
async def report(principal: User, body: ReportIn, svc: Service) -> JSONResponse:
    """举报：运营核实后下架。"""
    return ok(await svc.report(principal.subject, body), status_code=201)


# ======================= 管理端 =======================


class TakeDownIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["expert", "knowledge", "demand", "response", "message"]
    target_id: str = Field(min_length=1, max_length=160)
    note: str = Field(min_length=1, max_length=2000)


@admin_router.get("/queue")
async def queue(admin: Admin) -> JSONResponse:
    """等审核的：发布主体、资质、知识条目、申请公开的专家（带标出的禁止类词）。"""
    return ok(await admin.queue())


@admin_router.get("/documents/{document_id}")
async def document(admin: Admin, document_id: Id) -> Response:
    """证件照片原件：只给运营看，不缓存。"""
    found = await admin.document(document_id)
    return Response(
        content=found["content"],
        media_type=found["media_type"],
        headers={**NO_STORE, "Content-Disposition": "inline"},
    )


@admin_router.post("/publishers/{publisher_id}/decision", status_code=204)
async def decide_publisher(
    admin: Admin, operator: Operator, body: ReviewDecision, publisher_id: Id
) -> Response:
    await admin.decide_publisher(publisher_id, body, operator.subject)
    return Response(status_code=204, headers=NO_STORE)


@admin_router.post("/licenses/{license_id}/decision", status_code=204)
async def decide_license(
    admin: Admin, operator: Operator, body: ReviewDecision, license_id: Id
) -> Response:
    await admin.decide_license(license_id, body, operator.subject)
    return Response(status_code=204, headers=NO_STORE)


@admin_router.post("/knowledge/{listing_id}/decision", status_code=204)
async def decide_listing(
    admin: Admin, operator: Operator, body: ReviewDecision, listing_id: Id
) -> Response:
    await admin.decide_listing(listing_id, body, operator.subject)
    return Response(status_code=204, headers=NO_STORE)


@admin_router.post("/experts/{knowbot_id}/decision", status_code=204)
async def decide_expert(
    admin: Admin, operator: Operator, body: ReviewDecision, knowbot_id: Id
) -> Response:
    await admin.decide_expert(knowbot_id, body, operator.subject)
    return Response(status_code=204, headers=NO_STORE)


@admin_router.post("/takedown", status_code=204)
async def take_down(admin: Admin, operator: Operator, body: TakeDownIn) -> Response:
    """平台下架：知识买家那份照旧在；专家下架后不能再请，在跑的跑完。"""
    await admin.take_down(body.kind, body.target_id, body.note, operator.subject)
    return Response(status_code=204, headers=NO_STORE)


@admin_router.get("/reports")
async def reports(
    admin: Admin, status: Literal["open", "upheld", "dismissed"] | None = "open"
) -> JSONResponse:
    return ok(await admin.reports(status))


@admin_router.post("/reports/{report_id}/decision", status_code=204)
async def decide_report(
    admin: Admin, operator: Operator, body: ReportDecision, report_id: Id
) -> Response:
    await admin.decide_report(report_id, body, operator.subject)
    return Response(status_code=204, headers=NO_STORE)


@admin_router.get("/rules")
async def current_rules(admin: Admin) -> JSONResponse:
    return ok(await admin.rules())


@admin_router.put("/rules")
async def publish_rules(
    admin: Admin, operator: Operator, body: RulesIn
) -> JSONResponse:
    """改驻场规则：出一个新版本，旧的留着。"""
    return ok(await admin.publish_rules(body.body, operator.subject))


@admin_router.get("/trades")
async def trades(admin: Admin, page: Page = 1) -> JSONResponse:
    return ok(await admin.trades(page))


@admin_router.post("/sweep")
async def sweep(admin: Admin) -> JSONResponse:
    """手动跑一次资质到期与照片清理（定时任务每天也跑）。"""
    return ok(await admin.sweep())
