"""供需市场的 PostgreSQL 实现（SDD 0018 第十一节）。

一个实例对应一次请求的数据库会话；transaction 可嵌套，最外层提交、出错回滚。回的都是字典
（字段同表），由服务层决定给谁看哪些。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.knowbots.catalog import CATEGORIES
from app.infrastructure.models.knowbots import Knowbot, Publisher, PublisherLicense
from app.infrastructure.models.market import (
    Demand,
    DemandResponse,
    HouseRules,
    KnowledgeListing,
    MarketMessage,
    MarketReport,
    PublisherDocument,
    Trade,
    TradeRequestAlias,
    TradeReview,
)

# 要证照的类目
LICENSED = tuple(c.code for c in CATEGORIES if c.license)


def _row(obj: Any, *, skip: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        c.key: getattr(obj, c.key) for c in obj.__table__.columns if c.key not in skip
    }


def _id(value: str | uuid.UUID) -> uuid.UUID | None:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


class SqlMarketRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self._depth = 0

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        outer = self._depth == 0
        self._depth += 1
        try:
            yield
            if outer:
                await self.session.commit()
        except BaseException:
            if outer:
                await self.session.rollback()
            raise
        finally:
            self._depth -= 1

    # ---------- 发布主体与资质 ----------
    async def publisher_of(self, subject: str) -> dict[str, Any] | None:
        row = (
            await self.session.execute(
                select(Publisher).where(Publisher.owner_subject == subject)
            )
        ).scalar_one_or_none()
        return None if row is None else _row(row, skip=("id_number_hash",))

    async def get_publisher(self, publisher_id: str) -> dict[str, Any] | None:
        key = _id(publisher_id)
        row = await self.session.get(Publisher, key) if key else None
        return None if row is None else _row(row, skip=("id_number_hash",))

    async def publisher_with_id_hash(self, id_hash: str, *, other_than: str) -> bool:
        found = await self.session.execute(
            select(Publisher.id).where(
                Publisher.id_number_hash == id_hash,
                Publisher.owner_subject != other_than,
            )
        )
        return found.first() is not None

    async def save_publisher(
        self, subject: str, fields: dict[str, Any]
    ) -> dict[str, Any]:
        row = (
            await self.session.execute(
                select(Publisher).where(Publisher.owner_subject == subject)
            )
        ).scalar_one_or_none()
        if row is None:
            row = Publisher(id=uuid.uuid4(), owner_subject=subject, **fields)
            self.session.add(row)
        else:
            for key, value in fields.items():
                setattr(row, key, value)
        await self.session.flush()
        await self.session.refresh(row)
        return _row(row, skip=("id_number_hash",))

    async def review_publisher(self, publisher_id: str, **fields: Any) -> None:
        key = _id(publisher_id)
        await self.session.execute(
            update(Publisher).where(Publisher.id == key).values(**fields)
        )

    async def pending_publishers(self) -> list[dict[str, Any]]:
        rows = (
            await self.session.execute(
                select(Publisher)
                .where(Publisher.review_status == "pending")
                .order_by(Publisher.updated_at)
            )
        ).scalars()
        return [_row(r, skip=("id_number_hash",)) for r in rows]

    async def add_document(self, fields: dict[str, Any]) -> dict[str, Any]:
        row = PublisherDocument(id=uuid.uuid4(), **fields)
        self.session.add(row)
        await self.session.flush()
        await self.session.refresh(row)
        return _row(row, skip=("content",))

    async def documents_of(self, publisher_id: str) -> list[dict[str, Any]]:
        rows = (
            await self.session.execute(
                select(PublisherDocument)
                .where(PublisherDocument.publisher_id == _id(publisher_id))
                .order_by(PublisherDocument.created_at)
            )
        ).scalars()
        return [_row(r, skip=("content",)) for r in rows]

    async def get_document(self, document_id: str) -> dict[str, Any] | None:
        key = _id(document_id)
        row = await self.session.get(PublisherDocument, key) if key else None
        return None if row is None else _row(row)

    async def keep_documents_until(
        self, document_ids: Sequence[uuid.UUID], until: datetime
    ) -> None:
        if document_ids:
            await self.session.execute(
                update(PublisherDocument)
                .where(
                    PublisherDocument.id.in_(list(document_ids)),
                    PublisherDocument.purge_after.is_(None),
                )
                .values(purge_after=until)
            )

    async def purge_documents(self, now: datetime) -> int:
        result = await self.session.execute(
            update(PublisherDocument)
            .where(
                PublisherDocument.purge_after < now,
                PublisherDocument.content.is_not(None),
            )
            .values(content=None, purged_at=now)
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def add_license(self, fields: dict[str, Any]) -> dict[str, Any]:
        row = PublisherLicense(id=uuid.uuid4(), review_status="pending", **fields)
        self.session.add(row)
        await self.session.flush()
        await self.session.refresh(row)
        return _row(row)

    async def licenses_of(self, publisher_id: str) -> list[dict[str, Any]]:
        rows = (
            await self.session.execute(
                select(PublisherLicense)
                .where(PublisherLicense.publisher_id == _id(publisher_id))
                .order_by(PublisherLicense.created_at)
            )
        ).scalars()
        return [_row(r) for r in rows]

    async def get_license(self, license_id: str) -> dict[str, Any] | None:
        key = _id(license_id)
        row = await self.session.get(PublisherLicense, key) if key else None
        return None if row is None else _row(row)

    async def review_license(self, license_id: str, **fields: Any) -> None:
        await self.session.execute(
            update(PublisherLicense)
            .where(PublisherLicense.id == _id(license_id))
            .values(**fields)
        )

    async def pending_licenses(self) -> list[dict[str, Any]]:
        rows = (
            await self.session.execute(
                select(PublisherLicense)
                .where(PublisherLicense.review_status == "pending")
                .order_by(PublisherLicense.created_at)
            )
        ).scalars()
        return [_row(r) for r in rows]

    async def has_valid_license(
        self, publisher_id: uuid.UUID, category: str, today: date
    ) -> bool:
        found = await self.session.execute(
            select(PublisherLicense.id).where(
                PublisherLicense.publisher_id == publisher_id,
                PublisherLicense.category == category,
                PublisherLicense.review_status == "approved",
                or_(
                    PublisherLicense.valid_until.is_(None),
                    PublisherLicense.valid_until >= today,
                ),
            )
        )
        return found.first() is not None

    async def licenses_expiring(self, until: date) -> list[dict[str, Any]]:
        """审过的、到期日在 until 以前、还没提醒过的。"""
        rows = (
            await self.session.execute(
                select(PublisherLicense).where(
                    PublisherLicense.review_status == "approved",
                    PublisherLicense.valid_until.is_not(None),
                    PublisherLicense.valid_until <= until,
                    PublisherLicense.reminded_at.is_(None),
                )
            )
        ).scalars()
        return [_row(r) for r in rows]

    async def licenses_expired(self, today: date) -> list[dict[str, Any]]:
        rows = (
            await self.session.execute(
                select(PublisherLicense).where(
                    PublisherLicense.review_status == "approved",
                    PublisherLicense.valid_until < today,
                )
            )
        ).scalars()
        return [_row(r) for r in rows]

    # ---------- 专家条目 ----------
    async def get_knowbot(self, knowbot_id: str) -> dict[str, Any] | None:
        key = _id(knowbot_id)
        row = await self.session.get(Knowbot, key) if key else None
        return None if row is None else _row(row)

    async def knowbots_of(self, subject: str) -> list[dict[str, Any]]:
        rows = (
            await self.session.execute(
                select(Knowbot)
                .where(Knowbot.owner_subject == subject)
                .order_by(Knowbot.created_at, Knowbot.id)
            )
        ).scalars()
        return [_row(r) for r in rows]

    async def update_knowbot(self, knowbot_id: uuid.UUID, **fields: Any) -> None:
        await self.session.execute(
            update(Knowbot).where(Knowbot.id == knowbot_id).values(**fields)
        )

    async def pending_experts(self) -> list[dict[str, Any]]:
        rows = (
            await self.session.execute(
                select(Knowbot)
                .where(Knowbot.listing_status == "pending")
                .order_by(Knowbot.updated_at)
            )
        ).scalars()
        return [_row(r) for r in rows]

    async def public_experts_of(
        self, publisher_id: uuid.UUID, category: str
    ) -> list[dict[str, Any]]:
        rows = (
            await self.session.execute(
                select(Knowbot).where(
                    Knowbot.publisher_id == publisher_id,
                    Knowbot.category == category,
                    Knowbot.visibility == "public",
                )
            )
        ).scalars()
        return [_row(r) for r in rows]

    # ---------- 知识条目 ----------
    async def add_listing(self, fields: dict[str, Any]) -> dict[str, Any]:
        row = KnowledgeListing(id=uuid.uuid4(), **fields)
        self.session.add(row)
        await self.session.flush()
        await self.session.refresh(row)
        return _row(row)

    async def get_listing(self, listing_id: str) -> dict[str, Any] | None:
        key = _id(listing_id)
        row = await self.session.get(KnowledgeListing, key) if key else None
        return None if row is None else _row(row)

    async def update_listing(self, listing_id: uuid.UUID, **fields: Any) -> None:
        await self.session.execute(
            update(KnowledgeListing)
            .where(KnowledgeListing.id == listing_id)
            .values(updated_at=func.now(), **fields)
        )

    async def listings_of(self, subject: str) -> list[dict[str, Any]]:
        rows = (
            await self.session.execute(
                select(KnowledgeListing)
                .where(KnowledgeListing.seller_subject == subject)
                .order_by(KnowledgeListing.updated_at.desc())
            )
        ).scalars()
        return [_row(r) for r in rows]

    async def listed_knowledge(
        self,
        *,
        q: str | None,
        category: str | None,
        limit: int,
        offset: int,
        today: date,
    ) -> tuple[list[dict[str, Any]], int]:
        # 上架了，且卖家现在仍有资格：主体实名审过，类目要证照的还有有效证照
        # （Astra 评审 F06：不等每天扫表，失去资格的当下就不在市场上）
        where = [
            KnowledgeListing.status == "listed",
            select(Publisher.id)
            .where(
                Publisher.id == KnowledgeListing.publisher_id,
                Publisher.review_status == "approved",
                Publisher.real_name_verified.is_(True),
            )
            .exists(),
            or_(
                KnowledgeListing.category.not_in(LICENSED),
                select(PublisherLicense.id)
                .where(
                    PublisherLicense.publisher_id == KnowledgeListing.publisher_id,
                    PublisherLicense.category == KnowledgeListing.category,
                    PublisherLicense.review_status == "approved",
                    or_(
                        PublisherLicense.valid_until.is_(None),
                        PublisherLicense.valid_until >= today,
                    ),
                )
                .exists(),
            ),
        ]
        if q:
            like = f"%{q}%"
            where.append(
                or_(
                    KnowledgeListing.title.ilike(like),
                    KnowledgeListing.description.ilike(like),
                )
            )
        if category:
            where.append(
                or_(
                    KnowledgeListing.category == category,
                    KnowledgeListing.category.like(f"{category}.%"),
                )
            )
        total = (
            await self.session.execute(
                select(func.count()).select_from(KnowledgeListing).where(*where)
            )
        ).scalar_one()
        rows = (
            await self.session.execute(
                select(KnowledgeListing)
                .where(*where)
                .order_by(KnowledgeListing.updated_at.desc(), KnowledgeListing.id)
                .limit(limit)
                .offset(offset)
            )
        ).scalars()
        return [_row(r) for r in rows], int(total)

    async def pending_listings(self) -> list[dict[str, Any]]:
        rows = (
            await self.session.execute(
                select(KnowledgeListing)
                .where(KnowledgeListing.status == "pending")
                .order_by(KnowledgeListing.updated_at)
            )
        ).scalars()
        return [_row(r) for r in rows]

    async def listed_by_publisher(
        self, publisher_id: uuid.UUID, category: str
    ) -> list[dict[str, Any]]:
        rows = (
            await self.session.execute(
                select(KnowledgeListing).where(
                    KnowledgeListing.publisher_id == publisher_id,
                    KnowledgeListing.category == category,
                    KnowledgeListing.status == "listed",
                )
            )
        ).scalars()
        return [_row(r) for r in rows]

    # ---------- 需求与响应 ----------
    async def add_demand(self, fields: dict[str, Any]) -> dict[str, Any]:
        row = Demand(id=uuid.uuid4(), **fields)
        self.session.add(row)
        await self.session.flush()
        await self.session.refresh(row)
        return _row(row)

    async def get_demand(self, demand_id: str) -> dict[str, Any] | None:
        key = _id(demand_id)
        row = await self.session.get(Demand, key) if key else None
        return None if row is None else _row(row)

    async def update_demand(self, demand_id: uuid.UUID, **fields: Any) -> None:
        await self.session.execute(
            update(Demand)
            .where(Demand.id == demand_id)
            .values(updated_at=func.now(), **fields)
        )

    async def demands_of(self, subject: str) -> list[dict[str, Any]]:
        rows = (
            await self.session.execute(
                select(Demand)
                .where(Demand.buyer_subject == subject)
                .order_by(Demand.created_at.desc())
            )
        ).scalars()
        return [_row(r) for r in rows]

    async def open_demands(
        self,
        *,
        kind: str | None,
        q: str | None,
        today: date,
        limit: int,
        offset: int,
    ) -> tuple[list[dict[str, Any]], int]:
        where = [Demand.status == "open", Demand.deadline >= today]
        if kind:
            where.append(Demand.kind == kind)
        if q:
            like = f"%{q}%"
            where.append(or_(Demand.title.ilike(like), Demand.problem.ilike(like)))
        total = (
            await self.session.execute(
                select(func.count()).select_from(Demand).where(*where)
            )
        ).scalar_one()
        rows = (
            await self.session.execute(
                select(Demand)
                .where(*where)
                .order_by(Demand.created_at.desc(), Demand.id)
                .limit(limit)
                .offset(offset)
            )
        ).scalars()
        return [_row(r) for r in rows], int(total)

    async def add_response(self, fields: dict[str, Any]) -> dict[str, Any]:
        row = DemandResponse(id=uuid.uuid4(), **fields)
        self.session.add(row)
        await self.session.flush()
        await self.session.refresh(row)
        return _row(row)

    async def get_response(self, response_id: str) -> dict[str, Any] | None:
        key = _id(response_id)
        row = await self.session.get(DemandResponse, key) if key else None
        return None if row is None else _row(row)

    async def responses_to(self, demand_id: uuid.UUID) -> list[dict[str, Any]]:
        rows = (
            await self.session.execute(
                select(DemandResponse)
                .where(DemandResponse.demand_id == demand_id)
                .order_by(DemandResponse.created_at)
            )
        ).scalars()
        return [_row(r) for r in rows]

    async def responses_of(self, subject: str) -> list[dict[str, Any]]:
        rows = (
            await self.session.execute(
                select(DemandResponse)
                .where(DemandResponse.seller_subject == subject)
                .order_by(DemandResponse.created_at.desc())
            )
        ).scalars()
        return [_row(r) for r in rows]

    async def update_response(self, response_id: uuid.UUID, **fields: Any) -> None:
        await self.session.execute(
            update(DemandResponse)
            .where(DemandResponse.id == response_id)
            .values(**fields)
        )

    async def close_other_responses(
        self, demand_id: uuid.UUID, chosen: uuid.UUID
    ) -> None:
        await self.session.execute(
            update(DemandResponse)
            .where(
                DemandResponse.demand_id == demand_id,
                DemandResponse.id != chosen,
                DemandResponse.status == "open",
            )
            .values(status="not_chosen")
        )

    # ---------- 交易与评价 ----------
    async def add_trade(self, fields: dict[str, Any]) -> dict[str, Any]:
        row = Trade(**({"id": uuid.uuid4()} | fields))
        self.session.add(row)
        await self.session.flush()
        await self.session.refresh(row)
        return _row(row)

    async def start_trade(self, fields: dict[str, Any]) -> dict[str, Any]:
        """落一笔带幂等键的成交；同一买家同一个键已经有了就回那一笔（并发也只有一笔）。"""
        await self.session.execute(
            pg_insert(Trade)
            .values(id=uuid.uuid4(), **fields)
            .on_conflict_do_nothing(
                index_elements=[Trade.buyer_subject, Trade.request_key],
                index_where=Trade.request_key.is_not(None),
            )
        )
        found = await self.trade_by_request(
            fields["buyer_subject"], fields["request_key"]
        )
        assert found is not None
        return found

    async def trade_by_request(
        self, buyer: str, request_key: str
    ) -> dict[str, Any] | None:
        row = (
            await self.session.execute(
                select(Trade).where(
                    Trade.buyer_subject == buyer, Trade.request_key == request_key
                )
            )
        ).scalar_one_or_none()
        if row is None:  # 换键接手时记下的新键
            row = (
                await self.session.execute(
                    select(Trade)
                    .join(TradeRequestAlias, TradeRequestAlias.trade_id == Trade.id)
                    .where(
                        TradeRequestAlias.buyer_subject == buyer,
                        TradeRequestAlias.request_key == request_key,
                    )
                )
            ).scalar_one_or_none()
        return None if row is None else _row(row)

    async def add_request_alias(
        self, buyer: str, request_key: str, trade_id: uuid.UUID
    ) -> None:
        await self.session.execute(
            pg_insert(TradeRequestAlias)
            .values(buyer_subject=buyer, request_key=request_key, trade_id=trade_id)
            .on_conflict_do_nothing()
        )

    async def copying_trade(
        self, buyer: str, request_digest: str
    ) -> dict[str, Any] | None:
        """这个买家同样参数、还没复制完的那一笔（换了幂等键重来时接着做）。"""
        row = (
            await self.session.execute(
                select(Trade)
                .where(
                    Trade.buyer_subject == buyer,
                    Trade.request_digest == request_digest,
                    Trade.status == "copying",
                )
                .order_by(Trade.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
        return None if row is None else _row(row)

    async def update_trade(self, trade_id: uuid.UUID, **fields: Any) -> None:
        await self.session.execute(
            update(Trade).where(Trade.id == trade_id).values(**fields)
        )

    async def get_trade(
        self, trade_id: str | uuid.UUID, *, for_update: bool = False
    ) -> dict[str, Any] | None:
        key = _id(trade_id)
        if key is None:
            return None
        row = await self.session.get(
            Trade, key, with_for_update=for_update, populate_existing=for_update
        )
        return None if row is None else _row(row)

    async def trades(
        self,
        *,
        buyer: str | None = None,
        seller: str | None = None,
        limit: int = 200,
        offset: int = 0,
        copying: bool = False,
    ) -> list[dict[str, Any]]:
        where = [] if copying else [Trade.status != "copying"]
        if buyer is not None:
            where.append(Trade.buyer_subject == buyer)
        if seller is not None:
            where.append(Trade.seller_subject == seller)
        rows = (
            await self.session.execute(
                select(Trade)
                .where(*where)
                .order_by(Trade.created_at.desc(), Trade.id)
                .limit(limit)
                .offset(offset)
            )
        ).scalars()
        return [_row(r) for r in rows]

    async def bought_without_resale(self, buyer: str) -> set[str]:
        """这个人买来、不许转卖的知识的内容摘要（上架时不许拿它们再卖）。"""
        trades = (
            await self.session.execute(
                select(Trade.listing_ref, Trade.planned).where(
                    Trade.buyer_subject == buyer, Trade.kind == "knowledge"
                )
            )
        ).all()
        keys = {k for k in (_id(ref) for ref, _ in trades) if k is not None}
        if not keys:
            return set()
        allowed = {
            r.id
            for r in (
                await self.session.execute(
                    select(KnowledgeListing).where(
                        KnowledgeListing.id.in_(keys),
                        KnowledgeListing.resale_allowed.is_(True),
                    )
                )
            ).scalars()
        }
        # 按要复制的几份算：复制到一半的也算买了（Astra 评审 F07）
        return {
            str(c.get("sha256"))
            for ref, planned in trades
            if _id(ref) not in allowed
            for c in planned or []
        }

    async def add_review(self, fields: dict[str, Any]) -> dict[str, Any]:
        row = TradeReview(**fields)
        self.session.add(row)
        await self.session.flush()
        await self.session.refresh(row)
        return _row(row)

    async def get_review(self, trade_id: uuid.UUID) -> dict[str, Any] | None:
        row = await self.session.get(TradeReview, trade_id)
        return None if row is None else _row(row)

    async def update_review(self, trade_id: uuid.UUID, **fields: Any) -> None:
        await self.session.execute(
            update(TradeReview).where(TradeReview.trade_id == trade_id).values(**fields)
        )

    async def reviews_for(self, listing_prefix: str) -> list[dict[str, Any]]:
        """一个条目的评价：知识按条目编号；专家按「专家编号@」开头（各版本合起来）。"""
        rows = (
            await self.session.execute(
                select(TradeReview, Trade)
                .join(Trade, Trade.id == TradeReview.trade_id)
                .where(
                    or_(
                        Trade.listing_ref == listing_prefix,
                        Trade.listing_ref.like(f"{listing_prefix}@%"),
                    )
                )
                .order_by(TradeReview.created_at.desc())
            )
        ).all()
        return [
            {**_row(review), "listing_ref": trade.listing_ref} for review, trade in rows
        ]

    # ---------- 留言 ----------
    async def add_message(self, fields: dict[str, Any]) -> dict[str, Any]:
        row = MarketMessage(id=uuid.uuid4(), **fields)
        self.session.add(row)
        await self.session.flush()
        await self.session.refresh(row)
        return _row(row)

    async def messages(
        self, thread_kind: str, thread_id: uuid.UUID
    ) -> list[dict[str, Any]]:
        rows = (
            await self.session.execute(
                select(MarketMessage)
                .where(
                    MarketMessage.thread_kind == thread_kind,
                    MarketMessage.thread_id == thread_id,
                    MarketMessage.hidden.is_(False),
                )
                .order_by(MarketMessage.created_at, MarketMessage.id)
            )
        ).scalars()
        return [_row(r) for r in rows]

    async def get_message(self, message_id: str) -> dict[str, Any] | None:
        key = _id(message_id)
        row = await self.session.get(MarketMessage, key) if key else None
        return None if row is None else _row(row)

    async def hide_message(self, message_id: uuid.UUID) -> None:
        await self.session.execute(
            update(MarketMessage)
            .where(MarketMessage.id == message_id)
            .values(hidden=True)
        )

    # ---------- 驻场规则、举报 ----------
    async def current_rules(self) -> dict[str, Any] | None:
        row = (
            await self.session.execute(
                select(HouseRules).order_by(HouseRules.version.desc()).limit(1)
            )
        ).scalar_one_or_none()
        return None if row is None else _row(row)

    async def add_rules(self, body: str, created_by: str) -> dict[str, Any]:
        current = await self.current_rules()
        row = HouseRules(
            version=(current["version"] if current else 0) + 1,
            body=body,
            created_by=created_by,
        )
        self.session.add(row)
        await self.session.flush()
        await self.session.refresh(row)
        return _row(row)

    async def add_report(self, fields: dict[str, Any]) -> dict[str, Any]:
        row = MarketReport(id=uuid.uuid4(), **fields)
        self.session.add(row)
        await self.session.flush()
        await self.session.refresh(row)
        return _row(row)

    async def get_report(self, report_id: str) -> dict[str, Any] | None:
        key = _id(report_id)
        row = await self.session.get(MarketReport, key) if key else None
        return None if row is None else _row(row)

    async def reports(self, status: str | None) -> list[dict[str, Any]]:
        query = select(MarketReport).order_by(MarketReport.created_at)
        if status:
            query = query.where(MarketReport.status == status)
        rows = (await self.session.execute(query)).scalars()
        return [_row(r) for r in rows]

    async def update_report(self, report_id: uuid.UUID, **fields: Any) -> None:
        await self.session.execute(
            update(MarketReport).where(MarketReport.id == report_id).values(**fields)
        )
