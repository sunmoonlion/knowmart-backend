"""供需市场在真 PostgreSQL 上（SDD 0018 第十一节）：发布主体与资质、专家与知识两种供给、
需求与响应、成交与评价、留言、举报、定时活、驻场规则。"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import text

from app.application.errors.exceptions import (
    AppException,
    BadRequestError,
    ConcurrencyConflictError,
    NotFoundError,
    ServiceUnavailableError,
)
from app.application.services.knowbot_service import KnowbotService
from app.application.services.market import (
    MarketAdmin,
    MarketService,
    listed_with_item,
)
from app.domain.market import (
    BuyIn,
    DemandIn,
    ExpertListingIn,
    KnowledgeListingIn,
    LicenseIn,
    MessageIn,
    OfferIn,
    PublisherIn,
    ReplyIn,
    ReportDecision,
    ReportIn,
    ResponseIn,
    ReviewDecision,
    ReviewIn,
    RevokeIn,
    hash_id,
)
from app.infrastructure.repositories.knowbots import SqlKnowbotRepository
from app.infrastructure.repositories.market import SqlMarketRepository
from tests.knowbot_fakes import manifest
from tests.test_knowbot_repository_db import OWNER, db, deliver  # noqa: F401

SELLER = OWNER
BUYER = "8b1f5c1e-0000-4000-8000-000000000002"
OTHER = "8b1f5c1e-0000-4000-8000-000000000003"
OPS = "ops-1"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 100
NOW = datetime(2026, 10, 23, 12, tzinfo=UTC)


class Vault:
    """knowbase 的替身：卖家 knowmart 知识库里的资料，成交时复制给买家。"""

    def __init__(self) -> None:
        self.items = {
            "d1": {
                "name": "行业数据 2024.xlsx",
                "media_type": "application/vnd.ms-excel",
                "size_bytes": 10,
                "sha256": "a" * 64,
            },
            "d2": {
                "name": "样张.csv",
                "media_type": "text/csv",
                "size_bytes": 4,
                "sha256": "b" * 64,
            },
        }
        self.purchases: list[dict[str, Any]] = []
        self.copies: dict[str, str] = {}  # 成交编号 → 复制出来的那份
        self.fail_on: list[str] = []  # 复制这几份时依次失败一次
        self.gone: set[str] = set()  # 卖家删了的

    async def item(self, user_token: str, item_id: str) -> dict[str, Any]:
        if item_id not in self.items:
            raise NotFoundError("no", code="item_not_found")
        return self.items[item_id]

    async def purchase(self, **kw: Any) -> dict[str, Any]:
        """同 knowbase：按成交编号幂等，同一个编号重发回已复制的那份。"""
        if kw["seller_item_id"] in self.fail_on:
            self.fail_on.remove(kw["seller_item_id"])
            raise ServiceUnavailableError("knowbase down", code="knowbase_unavailable")
        if kw["seller_item_id"] in self.gone:
            raise NotFoundError("gone", code="item_gone")
        source = self.items[kw["seller_item_id"]]
        if kw["transaction_ref"] not in self.copies:
            self.purchases.append(kw)
            self.copies[kw["transaction_ref"]] = f"copy-{len(self.purchases)}"
        return {
            "item_id": self.copies[kw["transaction_ref"]],
            "name": source["name"],
            "sha256": source["sha256"],
        }


async def token() -> str:
    return "user-token"


class World:
    def __init__(self, db, vault: Vault) -> None:  # noqa: F811
        self.db = db
        self.vault = vault
        self.now = NOW
        self.id_hash_key = "test-only-id-hash-key"

    def svc(self, session) -> MarketService:  # type: ignore[no-untyped-def]
        return MarketService(
            SqlMarketRepository(session),
            SqlKnowbotRepository(session),
            self.vault,
            id_hash_key=self.id_hash_key,
            now=lambda: self.now,
        )

    def admin(self, session) -> MarketAdmin:  # type: ignore[no-untyped-def]
        return MarketAdmin(SqlMarketRepository(session), now=lambda: self.now)

    async def run(self, work):  # type: ignore[no-untyped-def]
        async with self.db() as session:
            return await work(self.svc(session), self.admin(session))


@pytest.fixture
def world(db):  # noqa: F811
    return World(db, Vault())


async def approved_publisher(world: World, subject: str = SELLER) -> str:
    async def work(svc: MarketService, admin: MarketAdmin):
        mine = await svc.save_publisher(
            subject,
            PublisherIn(
                kind="individual",
                name="张三工作室",
                real_name="张三",
                id_number="11010519491231002X",
            ),
        )
        await svc.add_document(subject, "id_front", "image/png", PNG)
        await admin.decide_publisher(
            mine["publisher"]["publisher_id"], ReviewDecision(approve=True), OPS
        )
        return mine["publisher"]["publisher_id"]

    return await world.run(work)


# ---------- 发布主体与资质 ----------


async def test_a_publisher_is_reviewed_before_it_counts(world: World) -> None:
    async def work(svc: MarketService, admin: MarketAdmin):
        mine = await svc.save_publisher(
            SELLER,
            PublisherIn(
                kind="individual",
                name="张三工作室",
                real_name="张三",
                id_number="11010519491231002x",
            ),
        )
        p = mine["publisher"]
        assert (
            p["review_status"] == "pending"
            and p["id_number_masked"] == "110***********002X"
        )
        assert "id_number_hash" not in p and p["real_name_verified"] is False
        # 同一个身份证号别的账号不能再用
        with pytest.raises(ConcurrencyConflictError):
            await svc.save_publisher(
                OTHER,
                PublisherIn(
                    kind="individual",
                    name="冒用",
                    real_name="李四",
                    id_number="11010519491231002X",
                ),
            )
        with pytest.raises(BadRequestError):
            await svc.add_document(SELLER, "id_front", "image/gif", PNG)
        doc = await svc.add_document(SELLER, "id_front", "image/png", PNG)
        queue = await admin.queue()
        assert [x["publisher_id"] for x in queue["publishers"]] == [p["publisher_id"]]
        assert (
            queue["publishers"][0]["documents"][0]["document_id"] == doc["document_id"]
        )
        assert (await admin.document(doc["document_id"]))["content"] == PNG
        with pytest.raises(PydanticValidationError):  # 不通过要写理由
            ReviewDecision(approve=False)
        await admin.decide_publisher(
            p["publisher_id"], ReviewDecision(approve=True), OPS
        )
        after = (await svc.my_publisher(SELLER))["publisher"]
        assert (
            after["review_status"] == "approved" and after["real_name_verified"] is True
        )
        # 要证照的类目：传证照、审过才算
        license_doc = await svc.add_document(
            SELLER, "license", "application/pdf", b"%PDF-1"
        )
        lic = await svc.add_license(
            SELLER,
            LicenseIn(
                category="finance.securities_advisory",
                license_name="经营证券期货业务许可证",
                license_no="000001",
                valid_until=date(2027, 1, 1),
                document_id=license_doc["document_id"],
            ),
        )
        assert lic["review_status"] == "pending"
        await admin.decide_license(lic["license_id"], ReviewDecision(approve=True), OPS)
        with pytest.raises(BadRequestError):  # 不要证照的类目不收证照
            await svc.add_license(
                SELLER,
                LicenseIn(
                    category="tools.office",
                    license_name="x",
                    license_no="1",
                    document_id=license_doc["document_id"],
                ),
            )

    await world.run(work)


# ---------- 专家条目 ----------


async def test_an_expert_goes_public_after_review_and_others_can_ask_it(
    world: World,
    db,  # noqa: F811
) -> None:
    bot = (await deliver(db, "delivery-0101", manifest(), public=True)).knowbot_id
    async with db() as session:
        await session.execute(
            text("update knowbot set category = :c where id = :i"),
            {"c": "tools.office", "i": bot},
        )
        await session.commit()

    async def no_publisher(svc: MarketService, admin: MarketAdmin):
        with pytest.raises(BadRequestError) as refused:
            await svc.set_expert_listing(
                SELLER,
                str(bot),
                ExpertListingIn(visibility="public", price=Decimal("9.9")),
            )
        assert refused.value.code == "publisher_required"

    await world.run(no_publisher)
    await approved_publisher(world)

    async def go_public(svc: MarketService, admin: MarketAdmin):
        assert [e["knowbot_id"] for e in await svc.my_experts(SELLER)] == [str(bot)]
        listing = await svc.set_expert_listing(
            SELLER,
            str(bot),
            ExpertListingIn(visibility="public", price=Decimal("9.90")),
        )
        assert (listing["listing_status"], listing["visibility"], listing["price"]) == (
            "pending",
            "private",
            "9.90",
        )
        assert [e["knowbot_id"] for e in (await admin.queue())["experts"]] == [str(bot)]
        await admin.decide_expert(str(bot), ReviewDecision(approve=True), OPS)

    await world.run(go_public)
    async with db() as session:
        knowbots = KnowbotService(SqlKnowbotRepository(session), public_market=True)
        found = await knowbots.summon(
            owner_subject=BUYER, knowbot_id=str(bot), version="1", runtime="knowbench"
        )
        assert found.version.version == "1"
        closed = KnowbotService(SqlKnowbotRepository(session), public_market=False)
        with pytest.raises(NotFoundError):  # 平台没开公开：别人请不了
            await closed.summon(
                owner_subject=BUYER,
                knowbot_id=str(bot),
                version="1",
                runtime="knowbench",
            )

    async def trade(svc: MarketService, admin: MarketAdmin):
        assert (
            await svc.record_summon(buyer=SELLER, knowbot_id=str(bot), version="1")
            is None
        )
        made = await svc.record_summon(buyer=BUYER, knowbot_id=str(bot), version="1")
        assert made and made["price"] == "9.90" and made["listing_ref"] == f"{bot}@1"
        await svc.review(BUYER, made["trade_id"], ReviewIn(rating=4, text="好用"))
        with pytest.raises(ConcurrencyConflictError):
            await svc.review(BUYER, made["trade_id"], ReviewIn(rating=5))
        await svc.reply(SELLER, made["trade_id"], ReplyIn(reply="谢谢"))
        market = await svc.expert_market(str(bot))
        assert market["rating"] == {"count": 1, "average": 4.0}
        assert market["reviews"][0]["reply"] == "谢谢"
        # 平台下架：不再公开
        await admin.take_down("expert", f"{bot}@1", "被举报核实", OPS)

    await world.run(trade)
    async with db() as session:
        knowbots = KnowbotService(SqlKnowbotRepository(session), public_market=True)
        with pytest.raises(NotFoundError):
            await knowbots.summon(
                owner_subject=BUYER,
                knowbot_id=str(bot),
                version="1",
                runtime="knowbench",
            )


async def test_knowlab_asks_public_on_delivery_only_qualified_go_to_review(
    world: World,
    db,  # noqa: F811
) -> None:
    bot = (await deliver(db, "delivery-0201", manifest())).knowbot_id
    async with db() as session:
        await session.execute(
            text("update knowbot set category = :c where id = :i"),
            {"c": "tools.office", "i": bot},
        )
        await session.commit()

    async def ask(svc: MarketService, admin: MarketAdmin) -> str:
        return await svc.ask_public_on_delivery(SELLER, str(bot))

    assert await world.run(ask) == "not_qualified"  # 没有发布主体：留自用，照实回
    await approved_publisher(world)
    assert await world.run(ask) == "pending"
    assert await world.run(ask) == "pending"

    async def others(svc: MarketService, admin: MarketAdmin) -> None:
        with pytest.raises(NotFoundError):
            await svc.ask_public_on_delivery(OTHER, str(bot))
        assert [e["knowbot_id"] for e in (await admin.queue())["experts"]] == [str(bot)]

    await world.run(others)


# ---------- 知识条目、需求与响应 ----------


def knowledge(**over: Any) -> KnowledgeListingIn:
    body: dict[str, Any] = {
        "title": "2024 行业数据",
        "category": "tools.lookup",
        "description": "一份表，覆盖 2024 年各月",
        "items": [{"item_id": "d1"}],
        "sample": "月份,销量\n1,10",
        "rights_statement": "我自己整理，有权出售",
        "price": "50",
    }
    return KnowledgeListingIn.model_validate(body | over)


async def test_an_expert_without_a_category_gets_one_when_going_public(
    world: World,
    db,  # noqa: F811
) -> None:
    """knowlab 交来的专家起初没有类目：交付时照实回 category_needed；卖家申请公开时选
    类目；被平台下架的点「只自用」也不冲掉下架（Fable 评审 P2-1、P2-10）。"""
    bot = (await deliver(db, "delivery-0401", manifest(), ref="knowlab-r4")).knowbot_id
    await approved_publisher(world)

    async def ask(svc: MarketService, admin: MarketAdmin) -> str:
        return await svc.ask_public_on_delivery(SELLER, str(bot))

    assert await world.run(ask) == "category_needed"

    async def go_public(svc: MarketService, admin: MarketAdmin) -> None:
        with pytest.raises(BadRequestError) as refused:
            await svc.set_expert_listing(
                SELLER, str(bot), ExpertListingIn(visibility="public")
            )
        assert refused.value.code == "category_invalid"
        listing = await svc.set_expert_listing(
            SELLER,
            str(bot),
            ExpertListingIn(visibility="public", category="tools.office"),
        )
        assert listing["listing_status"] == "pending"
        mine = {e["knowbot_id"]: e for e in await svc.my_experts(SELLER)}
        assert mine[str(bot)]["category"]["code"] == "tools.office"
        await admin.take_down("expert", f"{bot}@1", "被举报核实", OPS)
        kept = await svc.set_expert_listing(
            SELLER, str(bot), ExpertListingIn(visibility="private")
        )
        assert kept["listing_status"] == "taken_down"

    await world.run(go_public)


async def test_knowledge_is_reviewed_listed_bought_and_not_resold(world: World) -> None:
    async def draft(svc: MarketService, admin: MarketAdmin):
        made = await svc.create_listing(SELLER, knowledge(), token)
        assert made["status"] == "draft" and made["items"][0]["item_id"] == "d1"
        with pytest.raises(BadRequestError) as refused:
            await svc.submit_listing(SELLER, made["listing_id"])
        assert refused.value.code == "publisher_required"
        return made["listing_id"]

    listing_id = await world.run(draft)
    await approved_publisher(world)

    async def review(svc: MarketService, admin: MarketAdmin):
        assert (await svc.submit_listing(SELLER, listing_id))["status"] == "pending"
        with pytest.raises(NotFoundError):  # 审过之前别人看不到
            await svc.listing(BUYER, listing_id)
        queue = await admin.queue()
        assert [k["listing_id"] for k in queue["knowledge"]] == [listing_id]
        await admin.decide_listing(listing_id, ReviewDecision(approve=True), OPS)
        market = await svc.knowledge_market(q="行业", category="tools", page=1)
        assert [i["listing_id"] for i in market["items"]] == [listing_id]
        assert (
            "item_id" not in market["items"][0]["items"][0]
        )  # 买家看不到卖家的资料编号

    await world.run(review)

    async def buy(svc: MarketService, admin: MarketAdmin):
        with pytest.raises(BadRequestError):
            await svc.buy(
                SELLER, listing_id, BuyIn(request_id="buy-0001", target_app="knowbench")
            )
        trade = await svc.buy(
            BUYER, listing_id, BuyIn(request_id="buy-0002", target_app="knowbench")
        )
        assert (trade["kind"], trade["price"], trade["target_app"]) == (
            "knowledge",
            "50.00",
            "knowbench",
        )
        assert [c["item_id"] for c in trade["copied"]] == ["copy-1"]
        assert (
            world.vault.purchases[0]["buyer"] == BUYER
            and world.vault.purchases[0]["buyer_app"] == "knowbench"
        )
        assert [t["trade_id"] for t in await svc.my_trades(BUYER, "buyer")] == [
            trade["trade_id"]
        ]
        assert [t["trade_id"] for t in await svc.my_trades(SELLER, "seller")] == [
            trade["trade_id"]
        ]
        # 买来、不许转卖的：拿同样的内容再上架不行
        with pytest.raises(BadRequestError) as refused:
            await svc.create_listing(BUYER, knowledge(), token)
        assert refused.value.code == "resale_not_allowed"
        # 复制时带上「不许转卖」；卖家事后改勾也不影响已买的（按成交那一刻，P2-16）
        assert world.vault.purchases[0]["no_resale"] is True
        await svc.repo.update_listing(listing_id, resale_allowed=True)
        with pytest.raises(BadRequestError):
            await svc.create_listing(BUYER, knowledge(), token)
        # 资料自己带着标记（下载了再传的同样内容），按标记拦
        world.vault.items["d3"] = {**world.vault.items["d2"], "no_resale": True}
        with pytest.raises(BadRequestError) as flagged:
            await svc.create_listing(BUYER, knowledge(items=[{"item_id": "d3"}]), token)
        assert flagged.value.code == "resale_not_allowed"
        # 卖家下架：买家那份照旧在（成交记录还在）
        assert (await svc.delist_listing(SELLER, listing_id))["status"] == "delisted"
        assert len(await svc.my_trades(BUYER, "buyer")) == 1

    await world.run(buy)


async def _edit_publisher(world: World) -> None:
    """卖家改了主体资料：回到待审、不算实名。"""

    async def work(svc: MarketService, admin: MarketAdmin) -> None:
        await svc.save_publisher(
            SELLER,
            PublisherIn(
                kind="individual",
                name="张三工作室（改名）",
                real_name="张三",
                id_number="11010519491231002X",
            ),
        )

    await world.run(work)


async def _reapprove(world: World, publisher_id: str) -> None:
    async def work(svc: MarketService, admin: MarketAdmin) -> None:
        await admin.decide_publisher(publisher_id, ReviewDecision(approve=True), OPS)

    await world.run(work)


async def test_knowledge_needs_a_qualified_seller_when_approved_and_bought(
    world: World,
) -> None:
    """资格不只在提交时核：运营批准、市场列出、买的那一刻都现核（Astra 评审 F06）。"""
    publisher_id = await approved_publisher(world)

    async def submit(svc: MarketService, admin: MarketAdmin) -> str:
        made = await svc.create_listing(SELLER, knowledge(), token)
        await svc.submit_listing(SELLER, made["listing_id"])
        return made["listing_id"]

    listing_id = await world.run(submit)
    await _edit_publisher(world)

    async def approve(svc: MarketService, admin: MarketAdmin) -> None:
        await admin.decide_listing(listing_id, ReviewDecision(approve=True), OPS)

    with pytest.raises(ConcurrencyConflictError) as refused:
        await world.run(approve)
    assert refused.value.code == "publisher_not_qualified"
    await _reapprove(world, publisher_id)
    await world.run(approve)
    await _edit_publisher(world)

    async def unqualified(svc: MarketService, admin: MarketAdmin) -> None:
        market = await svc.knowledge_market(q=None, category=None, page=1)
        assert market["items"] == [] and market["total"] == 0
        with pytest.raises(ConcurrencyConflictError) as refused:
            await svc.buy(
                BUYER, listing_id, BuyIn(request_id="buy-0003", target_app="knowbench")
            )
        assert refused.value.code == "listing_unavailable"
        assert world.vault.purchases == []

    await world.run(unqualified)
    await _reapprove(world, publisher_id)

    async def again(svc: MarketService, admin: MarketAdmin) -> None:
        market = await svc.knowledge_market(q=None, category=None, page=1)
        assert [i["listing_id"] for i in market["items"]] == [listing_id]
        await svc.buy(
            BUYER, listing_id, BuyIn(request_id="buy-0004", target_app="knowbench")
        )

    await world.run(again)


async def test_a_licensed_expert_still_needs_an_approved_publisher(
    world: World,
    db,  # noqa: F811
) -> None:
    """有证照的类目也要主体审过；证照过期不等每天扫表就请不了（Astra 评审 F06）。"""
    bot = (
        await deliver(db, "delivery-0301", manifest(), ref="lab-bot-3", public=True)
    ).knowbot_id
    async with db() as session:
        await session.execute(
            text("update knowbot set category = :c where id = :i"),
            {"c": "finance.securities_advisory", "i": bot},
        )
        await session.commit()
    publisher_id = await approved_publisher(world)

    async def go_public(svc: MarketService, admin: MarketAdmin) -> None:
        doc = await svc.add_document(SELLER, "license", "application/pdf", b"%PDF-1")
        lic = await svc.add_license(
            SELLER,
            LicenseIn(
                category="finance.securities_advisory",
                license_name="许可证",
                license_no="9",
                valid_until=date(2026, 12, 31),
                document_id=doc["document_id"],
            ),
        )
        await admin.decide_license(lic["license_id"], ReviewDecision(approve=True), OPS)
        await svc.set_expert_listing(
            SELLER, str(bot), ExpertListingIn(visibility="public")
        )

    await world.run(go_public)
    await _edit_publisher(world)

    async def approve(svc: MarketService, admin: MarketAdmin) -> None:
        await admin.decide_expert(str(bot), ReviewDecision(approve=True), OPS)

    with pytest.raises(ConcurrencyConflictError) as refused:
        await world.run(approve)
    assert refused.value.code == "publisher_not_qualified"
    await _reapprove(world, publisher_id)
    await world.run(approve)

    async def summon(at: datetime) -> None:
        async with db() as session:
            await KnowbotService(
                SqlKnowbotRepository(session), public_market=True, now=lambda: at
            ).summon(
                owner_subject=BUYER,
                knowbot_id=str(bot),
                version="1",
                runtime="knowbench",
            )

    await summon(NOW)
    await _edit_publisher(world)
    with pytest.raises(NotFoundError):
        await summon(NOW)
    await _reapprove(world, publisher_id)
    await summon(NOW)
    with pytest.raises(NotFoundError):  # 证照过期，扫表还没跑
        await summon(datetime(2027, 1, 2, tzinfo=UTC))


async def test_buying_resumes_where_it_stopped(world: World) -> None:
    """买知识可恢复（Astra 评审 F07）：第二份没复制成，重来接着做那一笔，副本不多出来；
    同一个键换了参数拒；换了键同样参数接着做没复制完的；并发重来也只有一笔。"""
    await approved_publisher(world)

    async def listed(svc: MarketService, admin: MarketAdmin) -> str:
        made = await svc.create_listing(
            SELLER, knowledge(items=[{"item_id": "d1"}, {"item_id": "d2"}]), token
        )
        await svc.submit_listing(SELLER, made["listing_id"])
        await admin.decide_listing(
            made["listing_id"], ReviewDecision(approve=True), OPS
        )
        return made["listing_id"]

    listing_id = await world.run(listed)
    body = BuyIn(request_id="retry-0001", target_app="knowlab")
    world.vault.fail_on = ["d2"]

    async def buy(svc: MarketService, admin: MarketAdmin) -> dict[str, Any]:
        return await svc.buy(BUYER, listing_id, body)

    with pytest.raises(ServiceUnavailableError):
        await world.run(buy)

    async def halfway(svc: MarketService, admin: MarketAdmin) -> None:
        assert await svc.my_trades(BUYER, "buyer") == []  # 没复制完不算成交
        [trade] = await admin.trades(1)
        assert trade["status"] == "copying" and len(trade["copied"]) == 1
        # 复制到一半的也算买了：不许转卖照样挡
        with pytest.raises(BadRequestError) as refused:
            await svc.create_listing(BUYER, knowledge(), token)
        assert refused.value.code == "resale_not_allowed"
        with pytest.raises(ConcurrencyConflictError) as reused:
            await svc.buy(
                BUYER,
                listing_id,
                BuyIn(request_id="retry-0001", target_app="knowbench"),
            )
        assert reused.value.code == "request_reused"

    await world.run(halfway)
    done = await world.run(buy)
    assert done["status"] == "done" and [c["item_id"] for c in done["copied"]] == [
        "copy-1",
        "copy-2",
    ]
    assert len(world.vault.purchases) == 2
    again = await world.run(buy)  # 回执丢了再发：同一笔
    assert again["trade_id"] == done["trade_id"]

    # 换了键、同样参数：上一笔没复制完就接着做那一笔
    world.vault.fail_on = ["d1"]
    second = BuyIn(request_id="retry-0002", target_app="knowbench")

    async def buy_second(svc: MarketService, admin: MarketAdmin) -> dict[str, Any]:
        return await svc.buy(BUYER, listing_id, second)

    with pytest.raises(ServiceUnavailableError):
        await world.run(buy_second)
    second = BuyIn(request_id="retry-0003", target_app="knowbench")
    resumed = await world.run(buy_second)
    assert resumed["status"] == "done" and len(world.vault.purchases) == 4
    # 接手时用的新键也记在这笔上：用它再发，回的还是这一笔，不再买一遍（Fable P2-7）
    again = await world.run(buy_second)
    assert again["trade_id"] == resumed["trade_id"] and len(world.vault.purchases) == 4

    # 并发重来：一笔
    third = BuyIn(request_id="retry-0004", target_app="knowbench")

    async def buy_third(svc: MarketService, admin: MarketAdmin) -> dict[str, Any]:
        return await svc.buy(BUYER, listing_id, third)

    got = await asyncio.gather(world.run(buy_third), world.run(buy_third))
    assert got[0]["trade_id"] == got[1]["trade_id"]
    assert len(world.vault.purchases) == 6

    async def tally(svc: MarketService, admin: MarketAdmin) -> None:
        assert len(await svc.my_trades(BUYER, "buyer")) == 3

    await world.run(tally)


async def test_a_seller_cannot_delete_what_is_up_and_a_lost_copy_ends_the_trade(
    world: World,
) -> None:
    """在架条目里的资料不让删；卖家那份还是没了，这笔记失败、照实说，可以再买
    （Fable 评审 P1-2）。"""
    await approved_publisher(world)

    async def listed(svc: MarketService, admin: MarketAdmin) -> str:
        made = await svc.create_listing(
            SELLER, knowledge(items=[{"item_id": "d1"}, {"item_id": "d2"}]), token
        )
        assert not await listed_with_item(svc.repo, SELLER, "d2")  # 草稿不算
        await svc.submit_listing(SELLER, made["listing_id"])
        await admin.decide_listing(
            made["listing_id"], ReviewDecision(approve=True), OPS
        )
        assert await listed_with_item(svc.repo, SELLER, "d2")
        assert not await listed_with_item(svc.repo, OTHER, "d2")
        return made["listing_id"]

    listing_id = await world.run(listed)
    world.vault.gone = {"d2"}

    async def buy(svc: MarketService, admin: MarketAdmin) -> dict[str, Any]:
        return await svc.buy(
            BUYER, listing_id, BuyIn(request_id="gone-0001", target_app="knowbench")
        )

    failed = await world.run(buy)
    assert (failed["status"], failed["failure"]) == ("failed", "item_gone")
    assert [c["item_id"] for c in failed["copied"]] == ["copy-1"]  # 已复制的照旧归他
    again = await world.run(buy)  # 同键重来：还是那一笔，不再接手
    assert again["trade_id"] == failed["trade_id"] and again["status"] == "failed"

    async def mine(svc: MarketService, admin: MarketAdmin) -> None:
        [shown] = await svc.my_trades(BUYER, "buyer")
        assert shown["status"] == "failed"

    await world.run(mine)
    world.vault.gone = set()

    async def rebuy(svc: MarketService, admin: MarketAdmin) -> dict[str, Any]:
        return await svc.buy(
            BUYER, listing_id, BuyIn(request_id="gone-0002", target_app="knowbench")
        )

    fresh = await world.run(rebuy)  # 失败的不再被捞回，换键另起一笔
    assert fresh["trade_id"] != failed["trade_id"] and fresh["status"] == "done"


async def test_an_approved_publisher_or_license_can_be_revoked(
    world: World,
    db,  # noqa: F811
) -> None:
    """撤销审过的主体、证照：即刻生效，公开的专家改回自用、知识下架（Fable P2-8）。"""
    bot = (
        await deliver(db, "delivery-0501", manifest(), ref="lab-bot-5", public=True)
    ).knowbot_id
    async with db() as session:
        await session.execute(
            text("update knowbot set category = :c where id = :i"),
            {"c": "finance.securities_advisory", "i": bot},
        )
        await session.commit()
    publisher_id = await approved_publisher(world)

    async def go_up(svc: MarketService, admin: MarketAdmin) -> tuple[str, str]:
        doc = await svc.add_document(SELLER, "license", "application/pdf", b"%PDF-1")
        lic = await svc.add_license(
            SELLER,
            LicenseIn(
                category="finance.securities_advisory",
                license_name="许可证",
                license_no="9",
                valid_until=None,
                document_id=doc["document_id"],
            ),
        )
        await admin.decide_license(lic["license_id"], ReviewDecision(approve=True), OPS)
        await svc.set_expert_listing(
            SELLER, str(bot), ExpertListingIn(visibility="public")
        )
        await admin.decide_expert(str(bot), ReviewDecision(approve=True), OPS)
        made = await svc.create_listing(SELLER, knowledge(), token)
        await svc.submit_listing(SELLER, made["listing_id"])
        await admin.decide_listing(
            made["listing_id"], ReviewDecision(approve=True), OPS
        )
        [shown] = (await admin.queue())["approved_publishers"]
        assert [x["license_id"] for x in shown["licenses"]] == [lic["license_id"]]
        return lic["license_id"], made["listing_id"]

    license_id, listing_id = await world.run(go_up)

    async def revoke_license(svc: MarketService, admin: MarketAdmin) -> None:
        await admin.revoke_license(license_id, RevokeIn(note="证照伪造"), OPS)
        found = await svc.repo.get_knowbot(str(bot))
        assert (found["visibility"], found["listing_status"]) == ("private", "self")
        assert "证照伪造" in found["listing_review_note"]
        listing = await svc.repo.get_listing(listing_id)
        assert listing["status"] == "listed"  # 不要证照的类目不受影响
        with pytest.raises(NotFoundError):  # 撤过的不能再撤
            await admin.revoke_license(license_id, RevokeIn(note="再来"), OPS)

    await world.run(revoke_license)

    async def revoke_publisher(svc: MarketService, admin: MarketAdmin) -> None:
        await admin.revoke_publisher(publisher_id, RevokeIn(note="身份证伪造"), OPS)
        listing = await svc.repo.get_listing(listing_id)
        assert listing["status"] == "delisted"
        mine = await svc.my_publisher(SELLER)
        assert mine["publisher"]["review_status"] == "revoked"
        with pytest.raises(BadRequestError) as refused:
            await svc.submit_listing(SELLER, listing_id)
        assert refused.value.code == "publisher_not_approved"
        assert (await admin.queue())["approved_publishers"] == []

    await world.run(revoke_publisher)


async def test_a_chosen_demand_can_be_settled_or_closed(
    world: World,
    db,  # noqa: F811
) -> None:
    """选定之后：补附条目再成交、请专家记在需求上、或买家直接关（Fable P2-9）。"""
    await approved_publisher(world)
    bot = (
        await deliver(db, "delivery-0601", manifest(), ref="lab-bot-6", public=True)
    ).knowbot_id

    def demand_in(kind: str, title: str) -> DemandIn:
        return DemandIn(
            kind=kind,
            title=title,
            problem="做行业对比",
            deadline=date(2026, 11, 30),
            budget=Decimal("80"),
        )

    async def flow(svc: MarketService, admin: MarketAdmin) -> None:
        # 先谈好、做好上架再附：补附条目，买家照它买，需求关掉
        want = await svc.create_demand(BUYER, demand_in("knowledge", "要一份数据"))
        offer = await svc.respond(
            SELLER, want["demand_id"], ResponseIn(proposal="我来整理")
        )
        await svc.choose_response(BUYER, want["demand_id"], offer["response_id"])
        listing = await svc.create_listing(SELLER, knowledge(), token)
        await svc.submit_listing(SELLER, listing["listing_id"])
        await admin.decide_listing(
            listing["listing_id"], ReviewDecision(approve=True), OPS
        )
        with pytest.raises(NotFoundError):  # 只有那位卖家能附
            await svc.attach_offer(
                OTHER,
                offer["response_id"],
                OfferIn(listing_kind="knowledge", listing_ref=listing["listing_id"]),
            )
        await svc.attach_offer(
            SELLER,
            offer["response_id"],
            OfferIn(listing_kind="knowledge", listing_ref=listing["listing_id"]),
        )
        await svc.buy(
            BUYER,
            listing["listing_id"],
            BuyIn(
                request_id="settle-01",
                target_app="knowbench",
                demand_id=want["demand_id"],
            ),
        )
        assert (await svc.demand(BUYER, want["demand_id"]))["status"] == "closed"
        # 专家类：选中的那家附的是这位专家，请了就记在需求上、关掉
        ask = await svc.create_demand(BUYER, demand_in("expert", "要一位专家"))
        pitch = await svc.respond(
            SELLER,
            ask["demand_id"],
            ResponseIn(listing_kind="expert", listing_ref=f"{bot}@1", proposal="请它"),
        )
        await svc.choose_response(BUYER, ask["demand_id"], pitch["response_id"])
        made = await svc.record_summon(buyer=BUYER, knowbot_id=str(bot), version="1")
        assert made is not None and made["demand_id"] == ask["demand_id"]
        assert (await svc.demand(BUYER, ask["demand_id"]))["status"] == "closed"
        # 选定了但不在这里成交：买家直接关
        idle = await svc.create_demand(BUYER, demand_in("knowledge", "不要了"))
        one = await svc.respond(SELLER, idle["demand_id"], ResponseIn(proposal="可以"))
        await svc.choose_response(BUYER, idle["demand_id"], one["response_id"])
        closed = await svc.close_demand(BUYER, idle["demand_id"])
        assert closed["status"] == "closed"

    await world.run(flow)


async def test_a_demand_gets_responses_and_the_buyer_chooses_one(world: World) -> None:
    await approved_publisher(world)

    async def flow(svc: MarketService, admin: MarketAdmin):
        listing = await svc.create_listing(SELLER, knowledge(), token)
        await svc.submit_listing(SELLER, listing["listing_id"])
        await admin.decide_listing(
            listing["listing_id"], ReviewDecision(approve=True), OPS
        )
        demand = await svc.create_demand(
            BUYER,
            DemandIn(
                kind="knowledge",
                title="要 2024 行业数据",
                problem="做行业对比",
                deadline=date(2026, 11, 30),
                budget=Decimal("80"),
            ),
        )
        assert [
            d["demand_id"]
            for d in (
                await svc.demand_hall(kind="knowledge", q=None, page=1, viewer=OTHER)
            )["items"]
        ] == [demand["demand_id"]]
        with pytest.raises(BadRequestError):
            await svc.respond(
                BUYER, demand["demand_id"], ResponseIn(proposal="自己响应自己")
            )
        offer = await svc.respond(
            SELLER,
            demand["demand_id"],
            ResponseIn(
                listing_kind="knowledge",
                listing_ref=listing["listing_id"],
                proposal="现成的",
                price=Decimal("50"),
            ),
        )
        other = await svc.respond(
            OTHER, demand["demand_id"], ResponseIn(proposal="我可以做一份", days=7)
        )
        with pytest.raises(ConcurrencyConflictError):
            await svc.respond(
                OTHER, demand["demand_id"], ResponseIn(proposal="再来一次")
            )
        # 响应只有需求方与那位卖家看得到；别人只看到有几个
        seen_by_other = await svc.demand(OTHER, demand["demand_id"])
        assert seen_by_other["response_count"] == 2
        assert [r["response_id"] for r in seen_by_other["responses"]] == [
            other["response_id"]
        ]
        assert len((await svc.demand(BUYER, demand["demand_id"]))["responses"]) == 2
        # 留言：响应下的只有两位能看能说
        await svc.post_message(
            BUYER, "response", offer["response_id"], MessageIn(body="能先看样张吗")
        )
        with pytest.raises(NotFoundError):
            await svc.messages(OTHER, "response", offer["response_id"])
        said = await svc.messages(SELLER, "response", offer["response_id"])
        assert [(m["role"], m["mine"]) for m in said] == [("buyer", False)]
        await svc.post_message(
            OTHER, "demand", demand["demand_id"], MessageIn(body="期限能延吗")
        )
        assert len(await svc.messages(BUYER, "demand", demand["demand_id"])) == 1
        # 选一家：别的结束
        chosen = await svc.choose_response(
            BUYER, demand["demand_id"], offer["response_id"]
        )
        assert chosen["status"] == "chosen"
        assert {r["response_id"]: r["status"] for r in chosen["responses"]} == {
            offer["response_id"]: "chosen",
            other["response_id"]: "not_chosen",
        }
        trade = await svc.buy(
            BUYER,
            listing["listing_id"],
            BuyIn(
                request_id="buy-0005",
                target_app="knowlab",
                demand_id=demand["demand_id"],
            ),
        )
        assert trade["demand_id"] == demand["demand_id"]
        assert (await svc.demand(BUYER, demand["demand_id"]))["status"] == "closed"
        mine = await svc.my_responses(SELLER)
        assert mine[0]["demand"]["title"] == "要 2024 行业数据"

    await world.run(flow)


# ---------- 举报、定时活、驻场规则 ----------


async def test_reports_take_things_down(world: World) -> None:
    await approved_publisher(world)

    async def flow(svc: MarketService, admin: MarketAdmin):
        listing = await svc.create_listing(
            SELLER, knowledge(description="稳赚不赔的目标价"), token
        )
        await svc.submit_listing(SELLER, listing["listing_id"])
        queue = await admin.queue()
        assert set(queue["knowledge"][0]["flagged"]) >= {"稳赚", "目标价"}
        await admin.decide_listing(
            listing["listing_id"], ReviewDecision(approve=True), OPS
        )
        report = await svc.report(
            BUYER,
            ReportIn(
                target_kind="knowledge",
                target_id=listing["listing_id"],
                reason="有收益承诺",
            ),
        )
        assert [r["report_id"] for r in await admin.reports("open")] == [
            report["report_id"]
        ]
        await admin.decide_report(
            report["report_id"], ReportDecision(uphold=True, note="核实"), OPS
        )
        with pytest.raises(NotFoundError):
            await svc.listing(BUYER, listing["listing_id"])
        assert (await svc.listing(SELLER, listing["listing_id"]))[
            "status"
        ] == "taken_down"

    await world.run(flow)


async def test_licenses_expire_and_photos_are_purged(world: World, db) -> None:  # noqa: F811
    bot = (await deliver(db, "delivery-0201", manifest(), ref="lab-bot-2")).knowbot_id
    async with db() as session:
        await session.execute(
            text("update knowbot set category = :c where id = :i"),
            {"c": "finance.securities_advisory", "i": bot},
        )
        await session.commit()
    await approved_publisher(world)

    async def license_and_list(svc: MarketService, admin: MarketAdmin):
        doc = await svc.add_document(SELLER, "license", "application/pdf", b"%PDF-1")
        lic = await svc.add_license(
            SELLER,
            LicenseIn(
                category="finance.securities_advisory",
                license_name="许可证",
                license_no="9",
                valid_until=date(2026, 11, 10),
                document_id=doc["document_id"],
            ),
        )
        await admin.decide_license(lic["license_id"], ReviewDecision(approve=True), OPS)
        await svc.set_expert_listing(
            SELLER, str(bot), ExpertListingIn(visibility="public")
        )
        await admin.decide_expert(str(bot), ReviewDecision(approve=True), OPS)
        assert (await admin.sweep())["reminded"] == 1  # 30 天内到期：提醒
        assert (await admin.sweep())["reminded"] == 0  # 提醒过不再提醒
        lics = (await svc.my_publisher(SELLER))["licenses"]
        assert lics[0]["expiring"] is True

    await world.run(license_and_list)
    world.now = NOW + timedelta(days=30)

    async def expired(svc: MarketService, admin: MarketAdmin):
        result = await admin.sweep()
        assert result["expired"] == 1
        found = await svc.repo.get_knowbot(str(bot))
        assert (found["visibility"], found["listing_status"]) == ("private", "self")

    await world.run(expired)
    world.now = NOW + timedelta(days=400)

    async def purged(svc: MarketService, admin: MarketAdmin):
        assert (await admin.sweep())["purged"] >= 1
        docs = (await svc.my_publisher(SELLER))["documents"]
        assert all(d["purged"] for d in docs if d["kind"] != "license")

    await world.run(purged)


async def test_house_rules_have_versions(world: World) -> None:
    async def flow(svc: MarketService, admin: MarketAdmin):
        first = await svc.rules()
        assert first["version"] == 0 and first["body"].startswith("# 驻场规则")
        await admin.publish_rules("# 驻场规则（第 2 版）\n\n改了一条。", OPS)
        assert (await svc.rules())["version"] == 1

    await world.run(flow)


async def test_id_numbers_are_kept_as_a_keyed_digest(world: World, db) -> None:  # noqa: F811
    """身份证号只存遮罩与带钥匙的摘要（Kimi 评审第 5 条）；没配钥匙个人入驻不了。"""
    await approved_publisher(world)
    async with db() as session:
        stored = (
            await session.execute(
                text("select id_number_hash from publisher where kind = 'individual'")
            )
        ).scalar_one()
    assert stored == hash_id("11010519491231002X", world.id_hash_key)
    assert stored != hashlib.sha256(b"11010519491231002X").hexdigest()
    world.id_hash_key = ""

    async def unconfigured(svc: MarketService, admin: MarketAdmin) -> None:
        await svc.save_publisher(
            OTHER,
            PublisherIn(
                kind="individual",
                name="李四",
                real_name="李四",
                id_number="11010519491231002X",
            ),
        )

    with pytest.raises(AppException) as refused:
        await world.run(unconfigured)
    assert refused.value.code == "publisher_unconfigured"


def test_id_numbers_are_checked() -> None:
    with pytest.raises(PydanticValidationError):
        PublisherIn(
            kind="individual", name="x", real_name="x", id_number="12345678901234567A"
        )
    with pytest.raises(PydanticValidationError):
        PublisherIn(
            kind="organization", name="某公司", credit_code="91110000000000000X"
        )
