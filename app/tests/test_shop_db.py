"""小店在真 PostgreSQL 上（SDD 0018 第十一节第 7 之二小节、0043 第 4.3 节、
数据拓扑第十一节）：每个用户一家店、店名留痕；首页只列有东西可看的店；小店详情页只放
别人看得到的；停店挡新的公开交易、不撤历史订单；需求勾了才上小店、卖家响应了才见到
店；后台核资料还在不在。"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import text

from app.application.errors.exceptions import (
    BadRequestError,
    ConcurrencyConflictError,
    NotFoundError,
    ServiceUnavailableError,
)
from app.application.services.expert_catalog import CatalogQuery, ExpertCatalog
from app.application.services.knowbot_service import KnowbotService
from app.application.services.market import MarketAdmin, MarketService
from app.domain.market import (
    BuyIn,
    DemandIn,
    ExpertListingIn,
    PublisherIn,
    ResponseIn,
    ReviewDecision,
    ShopDecision,
    ShopIn,
)
from app.infrastructure.repositories.knowbots import SqlKnowbotRepository
from app.infrastructure.repositories.market import SqlMarketRepository
from tests.knowbot_fakes import manifest
from tests.test_knowbot_repository_db import db, deliver  # noqa: F401
from tests.test_market_db import (
    BUYER,
    NOW,
    OPS,
    OTHER,
    SELLER,
    Vault,
    World,
    approved_publisher,
    knowledge,
    token,
)

PLATFORM_CODE = "91110000MA01ABCD2X"


class CheckingVault(Vault):
    """再加上后台的成批核（knowbase 待加的 market-offers/check 的替身）。"""

    def __init__(self) -> None:
        super().__init__()
        self.asked: list[list[tuple[str, str]]] = []
        self.down = False

    async def offer_states(self, items: Any) -> dict[tuple[str, str], str]:
        if self.down:
            raise ServiceUnavailableError("knowbase down", code="knowbase_unavailable")
        self.asked.append(list(items))
        out = {}
        for seller, material in items:
            if material in self.gone or material not in self.offered:
                out[(seller, material)] = "gone"
            elif material in self.lowered or not self.offered[material]["sellable"]:
                out[(seller, material)] = "not_for_sale"
            else:
                out[(seller, material)] = "offered"
        return out


class ShopWorld(World):
    def svc(self, session) -> MarketService:  # type: ignore[no-untyped-def]
        return MarketService(
            SqlMarketRepository(session),
            SqlKnowbotRepository(session),
            self.vault,
            id_hash_key=self.id_hash_key,
            public_market=True,
            platform_credit_code=PLATFORM_CODE,
            now=lambda: self.now,
        )

    def admin(self, session) -> MarketAdmin:  # type: ignore[no-untyped-def]
        return MarketAdmin(
            SqlMarketRepository(session), vault=self.vault, now=lambda: self.now
        )


@pytest.fixture
def world(db):  # noqa: F811
    return ShopWorld(db, CheckingVault())


async def listed_knowledge(world: World, **over: Any) -> str:
    async def work(svc: MarketService, admin: MarketAdmin) -> str:
        made = await svc.create_listing(SELLER, knowledge(**over), token)
        await svc.submit_listing(SELLER, made["listing_id"])
        await admin.decide_listing(
            made["listing_id"], ReviewDecision(approve=True), OPS
        )
        return made["listing_id"]

    return await world.run(work)


def demand(**over: Any) -> DemandIn:
    body: dict[str, Any] = {
        "kind": "knowledge",
        "title": "要 2024 年的行业数据",
        "problem": "做行业对比",
        "deadline": "2026-12-31",
    }
    return DemandIn.model_validate(body | over)


async def public_expert(world: World, db) -> str:  # type: ignore[no-untyped-def]  # noqa: F811
    bot = (await deliver(db, "delivery-0201", manifest(), public=True)).knowbot_id
    async with db() as session:
        await session.execute(
            text("update knowbot set category = 'tools.office' where id = :i"),
            {"i": bot},
        )
        await session.commit()

    async def work(svc: MarketService, admin: MarketAdmin) -> None:
        await svc.set_expert_listing(
            SELLER, str(bot), ExpertListingIn(visibility="public", price=Decimal("5"))
        )
        await admin.decide_expert(str(bot), ReviewDecision(approve=True), OPS)

    await world.run(work)
    return str(bot)


# ---------- 每个用户一家店 ----------


async def test_a_shop_is_named_after_the_nickname_and_renames_are_kept(
    world: ShopWorld,
    db,  # noqa: F811
) -> None:
    async with db() as session:
        await session.execute(
            text(
                "insert into auth_user (id, issuer, subject, display_name, roles,"
                " scopes) values (gen_random_uuid(), 'i', :s, '张三', '[]', '[]')"
            ),
            {"s": SELLER},
        )
        await session.commit()

    async def work(svc: MarketService, admin: MarketAdmin) -> None:
        mine = await svc.my_shop(SELLER)
        assert mine["shop"]["name"] == "张三" and mine["shop"]["status"] == "open"
        assert mine["shop"]["listed"] is False  # 没东西在架：不进首页列表
        assert (await svc.my_shop(OTHER))["shop"]["name"] == "小店"  # 没昵称
        with pytest.raises(BadRequestError) as refused:  # 驻场规则禁止词
            await svc.rename_shop(SELLER, ShopIn(name="张三荐股小店"))
        assert refused.value.code == "shop_name_forbidden"
        with pytest.raises(BadRequestError):
            await svc.rename_shop(SELLER, ShopIn(name="推荐好数据"))
        with pytest.raises(PydanticValidationError):
            ShopIn(name="长" * 41)
        renamed = await svc.rename_shop(SELLER, ShopIn(name="张三的数据铺"))
        assert renamed["shop"]["name"] == "张三的数据铺"
        assert [
            (e["kind"], e["old_name"], e["new_name"]) for e in renamed["history"]
        ] == [("renamed", "张三", "张三的数据铺")]
        # 一个用户只有一家
        again = await svc.ensure_shop(SELLER)
        assert str(again["id"]) == renamed["shop"]["shop_id"]

    await world.run(work)


async def test_a_publisher_change_is_kept_and_trades_name_the_publisher_then(
    world: ShopWorld,
) -> None:
    await approved_publisher(world)
    listing = await listed_knowledge(world)

    async def work(svc: MarketService, admin: MarketAdmin) -> None:
        await svc.buy(BUYER, listing, BuyIn(request_id="buy-00000001"))
        # 改主体：要重审，留历史；成交照旧标当时卖的主体
        await svc.save_publisher(
            SELLER,
            PublisherIn(
                kind="individual",
                name="张三数据",
                real_name="张三",
                id_number="11010519491231002X",
            ),
        )
        [queued] = (await admin.queue())["publishers"]
        assert [h["name"] for h in queued["history"]] == ["张三工作室", "张三数据"]
        [trade] = await svc.my_trades(BUYER, "buyer")
        assert trade["seller_publisher"] == {"name": "张三工作室", "kind": "individual"}

    await world.run(work)


# ---------- 首页：小店列表 ----------


async def test_the_home_lists_only_shops_with_something_on_show(
    world: ShopWorld,
    db,  # noqa: F811
) -> None:
    async def home(svc: MarketService, admin: MarketAdmin) -> list[str]:
        return [
            s["name"]
            for s in (await svc.shops(q=None, category=None, sort="time", page=1))[
                "items"
            ]
        ]

    await approved_publisher(world)

    async def drafts(svc: MarketService, admin: MarketAdmin) -> None:
        await svc.create_listing(SELLER, knowledge(), token)  # 草稿不算
        await svc.create_demand(OTHER, demand())  # 没勾公开到小店不算

    await world.run(drafts)
    assert await world.run(home) == []

    await listed_knowledge(world)

    async def rename(svc: MarketService, admin: MarketAdmin) -> None:
        await svc.rename_shop(SELLER, ShopIn(name="甲店"))
        await svc.rename_shop(BUYER, ShopIn(name="乙店"))
        await svc.create_demand(BUYER, demand(public_to_shop=True))

    await world.run(rename)
    assert await world.run(home) == ["乙店", "甲店"]  # 按最近一次改动

    async def found(svc: MarketService, admin: MarketAdmin) -> None:
        page = await svc.shops(q=None, category=None, sort="name", page=1)
        assert [s["name"] for s in page["items"]] == ["乙店", "甲店"]
        first = next(s for s in page["items"] if s["name"] == "甲店")
        assert (first["knowledge"], first["experts"], first["demands"]) == (1, 0, 0)
        assert first["publisher"]["name"] == "张三工作室"
        assert [c["code"] for c in first["categories"]] == ["tools.lookup"]
        by_name = await svc.shops(q="甲", category=None, sort="time", page=1)
        assert [s["name"] for s in by_name["items"]] == ["甲店"]
        by_group = await svc.shops(q=None, category="tools", sort="time", page=1)
        assert [s["name"] for s in by_group["items"]] == ["甲店"]  # 需求没有类目
        assert (await svc.my_shop(SELLER))["shop"]["listed"] is True
        assert (await svc.my_shop(OTHER))["shop"]["listed"] is False

    await world.run(found)


async def test_the_platform_shop_is_marked_by_its_publisher(world: ShopWorld) -> None:
    async def work(svc: MarketService, admin: MarketAdmin) -> None:
        mine = await svc.save_publisher(
            SELLER,
            PublisherIn(
                kind="organization",
                name="日月数据科技有限公司",
                credit_code=PLATFORM_CODE.lower(),
                legal_person="王五",
                contact_name="赵六",
            ),
        )
        shop = await svc.ensure_shop(SELLER)
        assert (await svc.shop(BUYER, str(shop["id"])))["shop"]["platform"] is False
        await admin.decide_publisher(
            mine["publisher"]["publisher_id"], ReviewDecision(approve=True), OPS
        )
        page = await svc.shop(BUYER, str(shop["id"]))
        assert page["shop"]["platform"] is True
        assert page["shop"]["publisher"]["name"] == "日月数据科技有限公司"
        other = await svc.ensure_shop(OTHER)
        assert (await svc.shop(BUYER, str(other["id"])))["shop"]["platform"] is False

    await world.run(work)


# ---------- 小店详情页（对外）----------


async def test_a_shop_page_shows_only_what_others_may_see(
    world: ShopWorld,
    db,  # noqa: F811
) -> None:
    await approved_publisher(world)
    listing = await listed_knowledge(world)
    bot = await public_expert(world, db)

    async def work(svc: MarketService, admin: MarketAdmin) -> None:
        await svc.create_listing(SELLER, knowledge(title="还在写的"), token)
        shown = await svc.create_demand(SELLER, demand(public_to_shop=True))
        await svc.create_demand(SELLER, demand(title="不公开的需求"))
        shop = await svc.ensure_shop(SELLER)
        page = await svc.shop(BUYER, str(shop["id"]))
        assert page["shop"]["mine"] is False and page["shop"]["status"] == "open"
        assert [x["listing_id"] for x in page["knowledge"]] == [listing]
        assert "item_id" not in page["knowledge"][0]["items"][0]  # 对外不给资料编号
        assert [(e["knowbot_id"], e["version"]) for e in page["experts"]] == [
            (bot, "1")
        ]
        assert [d["demand_id"] for d in page["demands"]] == [shown["demand_id"]]
        assert page["shop"]["publisher"]["real_name_verified"] is True
        # 从条目、专家详情能落到这家店
        detail = await svc.listing(BUYER, listing)
        assert detail["shop"]["shop_id"] == str(shop["id"])
        assert (await svc.expert_market(bot))["shop"]["shop_id"] == str(shop["id"])
        with pytest.raises(NotFoundError):
            await svc.shop(BUYER, "00000000-0000-4000-8000-000000000000")

    await world.run(work)


async def test_a_demand_shows_its_shop_only_to_those_who_respond(
    world: ShopWorld,
) -> None:
    async def work(svc: MarketService, admin: MarketAdmin) -> None:
        made = await svc.create_demand(BUYER, demand())
        assert made["mine"] is True
        hall = await svc.demand_hall(kind=None, q=None, page=1, viewer=SELLER)
        assert "shop" not in hall["items"][0]  # 大厅里匿名
        assert (await svc.demand(SELLER, made["demand_id"]))["shop"] is None
        await svc.respond(
            SELLER, made["demand_id"], ResponseIn(proposal="我有 2024 年的")
        )
        buyer_shop = await svc.ensure_shop(BUYER)
        seller_shop = await svc.ensure_shop(SELLER)
        seen = await svc.demand(SELLER, made["demand_id"])
        assert seen["shop"]["shop_id"] == str(buyer_shop["id"])
        mine = await svc.demand(BUYER, made["demand_id"])
        assert [r["shop"]["shop_id"] for r in mine["responses"]] == [
            str(seller_shop["id"])
        ]
        assert (await svc.demand(OTHER, made["demand_id"]))["shop"] is None

    await world.run(work)


# ---------- 处置状态 ----------


async def test_a_suspended_shop_stops_new_public_dealings_and_keeps_old_trades(
    world: ShopWorld,
    db,  # noqa: F811
) -> None:
    await approved_publisher(world)
    listing = await listed_knowledge(world)
    other_listing = await listed_knowledge(world, title="另一份")
    bot = await public_expert(world, db)

    async def before(svc: MarketService, admin: MarketAdmin) -> str:
        await svc.buy(BUYER, listing, BuyIn(request_id="buy-00000001"))
        await svc.create_demand(SELLER, demand(public_to_shop=True))
        draft = await svc.create_listing(SELLER, knowledge(title="草稿"), token)
        await svc.submit_listing(SELLER, draft["listing_id"])  # 在审
        # 运营单独下架一条
        await admin.take_down("knowledge", other_listing, "侵权", OPS)
        shop = await svc.ensure_shop(SELLER)
        await admin.suspend_shop(str(shop["id"]), ShopDecision(note="屡次违规"), OPS)
        with pytest.raises(ConcurrencyConflictError):  # 已经停着
            await admin.suspend_shop(str(shop["id"]), ShopDecision(note="再停"), OPS)
        return draft["listing_id"]

    pending = await world.run(before)

    async def blocked(svc: MarketService, admin: MarketAdmin) -> None:
        home = await svc.shops(q=None, category=None, sort="time", page=1)
        assert home["items"] == []
        assert (await svc.knowledge_market(q=None, category=None, page=1))[
            "items"
        ] == []
        assert (await svc.demand_hall(kind=None, q=None, page=1, viewer=BUYER))[
            "items"
        ] == []
        with pytest.raises(NotFoundError):
            await svc.listing(BUYER, listing)
        with pytest.raises(ConcurrencyConflictError) as refused:
            await svc.buy(OTHER, listing, BuyIn(request_id="buy-00000002"))
        assert refused.value.code == "listing_unavailable"
        for attempt in (
            svc.create_demand(SELLER, demand()),
            svc.set_expert_listing(
                SELLER, bot, ExpertListingIn(visibility="public", price=None)
            ),
        ):
            with pytest.raises(ConcurrencyConflictError) as refused:
                await attempt
            assert refused.value.code == "shop_suspended"
        with pytest.raises(ConcurrencyConflictError) as refused:
            await admin.decide_listing(pending, ReviewDecision(approve=True), OPS)
        assert refused.value.code == "shop_suspended"
        assert await svc.ask_public_on_delivery(SELLER, bot) == "public"
        page = await svc.shop(BUYER, str((await svc.ensure_shop(SELLER))["id"]))
        assert page["shop"]["status"] == "suspended" and page["knowledge"] == []
        mine = await svc.my_shop(SELLER)
        assert mine["shop"]["status_note"] == "屡次违规"
        assert mine["shop"]["listed"] is False
        # 历史订单不撤
        [trade] = await svc.my_trades(BUYER, "buyer")
        assert trade["status"] == "done" and trade["listing_state"] == "off"

    await world.run(blocked)
    async with db() as session:
        knowbots = KnowbotService(SqlKnowbotRepository(session), public_market=True)
        with pytest.raises(NotFoundError):  # 停店期间别人请不了
            await knowbots.summon(
                owner_subject=BUYER, knowbot_id=bot, version="1", runtime="knowbench"
            )
        catalog = ExpertCatalog(SqlKnowbotRepository(session), public_market=True)
        assert (await catalog.list(BUYER, CatalogQuery()))["items"] == []
        own = await knowbots.summon(  # 自己的照样能请
            owner_subject=SELLER, knowbot_id=bot, version="1", runtime="knowbench"
        )
        assert own.version.version == "1"

    async def restored(svc: MarketService, admin: MarketAdmin) -> None:
        shop = await svc.ensure_shop(SELLER)
        await admin.restore_shop(str(shop["id"]), ShopDecision(note="整改完"), OPS)
        found = await svc.knowledge_market(q=None, category=None, page=1)
        # 恢复不动被单独下架的那一条
        assert [x["listing_id"] for x in found["items"]] == [listing]
        [trade] = await svc.my_trades(BUYER, "buyer")
        assert trade["listing_state"] == "on"
        assert trade["shop"]["shop_id"] == str(shop["id"])
        [row] = await admin.shops(q=None, status=None, page=1)
        assert [e["kind"] for e in row["history"]] == ["restored", "suspended"]
        assert row["owner_subject"] == SELLER

    await world.run(restored)
    async with db() as session:
        knowbots = KnowbotService(SqlKnowbotRepository(session), public_market=True)
        found = await knowbots.summon(
            owner_subject=BUYER, knowbot_id=bot, version="1", runtime="knowbench"
        )
        assert found.version.version == "1"


async def test_a_suspended_shop_cannot_answer_or_be_answered(world: ShopWorld) -> None:
    async def work(svc: MarketService, admin: MarketAdmin) -> None:
        theirs = await svc.create_demand(BUYER, demand())
        mine = await svc.create_demand(SELLER, demand())
        shop = await svc.ensure_shop(SELLER)
        await admin.suspend_shop(str(shop["id"]), ShopDecision(note="违规"), OPS)
        with pytest.raises(ConcurrencyConflictError) as refused:
            await svc.respond(SELLER, theirs["demand_id"], ResponseIn(proposal="x"))
        assert refused.value.code == "shop_suspended"
        with pytest.raises(ConcurrencyConflictError) as refused:
            await svc.respond(BUYER, mine["demand_id"], ResponseIn(proposal="x"))
        assert refused.value.code == "demand_closed"

    await world.run(work)


# ---------- 后台核资料还在不在 ----------


async def test_listings_are_checked_in_the_background(world: ShopWorld) -> None:
    """资料在 knowbase 没了或不再出售：不等卖家打开「我发布的」，后台定时核到就下架、
    写明原因；knowbase 连不上这一轮不核（「待你决定」第 7 条）。"""
    await approved_publisher(world)
    gone = await listed_knowledge(world, items=[{"item_id": "d1"}])
    lowered = await listed_knowledge(world, title="样张", items=[{"item_id": "d2"}])

    async def check(svc: MarketService, admin: MarketAdmin) -> dict[str, Any]:
        return await admin.check_offers()

    assert await world.run(check) == {"checked": 2, "delisted": 0, "skipped": None}
    assert world.vault.asked == [[(SELLER, "d1"), (SELLER, "d2")]]
    world.vault.down = True
    world.vault.gone.add("d1")
    assert (await world.run(check))["skipped"] == "knowbase_unavailable"
    world.vault.down = False
    world.vault.offered["d2"]["sellable"] = False
    assert await world.run(check) == {"checked": 2, "delisted": 2, "skipped": None}

    async def mine(svc: MarketService, admin: MarketAdmin) -> dict[str, Any]:
        return {x["listing_id"]: x for x in await svc.my_listings(SELLER)}

    found = await world.run(mine)
    assert (found[gone]["status"], found[gone]["delist_reason"]) == (
        "delisted",
        "item_gone",
    )
    assert found[lowered]["delist_reason"] == "item_not_for_sale"
    assert await world.run(check) == {"checked": 0, "delisted": 0, "skipped": None}


async def test_without_knowbase_the_background_check_is_skipped(db) -> None:  # noqa: F811
    async with db() as session:
        admin = MarketAdmin(SqlMarketRepository(session), now=lambda: NOW)
        assert await admin.check_offers() == {
            "checked": 0,
            "delisted": 0,
            "skipped": "knowbase_unconfigured",
        }
