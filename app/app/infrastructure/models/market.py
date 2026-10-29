"""供需市场的表（SDD 0018 第十一节）。

知识条目、需求、响应、交易记录、评价、留言、驻场规则、举报、发布主体的证件照片与变更
历史、小店。专家条目在 knowbot 表上（knowbots.py）。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.models.base import Base, TimestampMixin


def _now() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PublisherDocument(Base):
    """实名与证照的照片：只给运营审核看；审过后留一年清掉内容（第 9 小节第 4 项）。"""

    __tablename__ = "publisher_document"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    publisher_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("publisher.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    media_type: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[bytes | None] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = _now()
    purge_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KnowledgeListing(TimestampMixin, Base):
    __tablename__ = "knowledge_listing"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    seller_subject: Mapped[str] = mapped_column(String(512), nullable=False)
    publisher_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("publisher.id", ondelete="RESTRICT")
    )
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    sample: Mapped[str] = mapped_column(Text, nullable=False, default="")
    rights_statement: Mapped[str] = mapped_column(Text, nullable=False)
    resale_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    review_note: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[str | None] = mapped_column(String(512))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 资料没了（item_gone）或不再出售（item_not_for_sale）时自动下架的原因，给卖家看
    delist_reason: Mapped[str | None] = mapped_column(String(32))


class Demand(TimestampMixin, Base):
    __tablename__ = "demand"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    buyer_subject: Mapped[str] = mapped_column(String(512), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    problem: Mapped[str] = mapped_column(Text, nullable=False)
    data_wanted: Mapped[str] = mapped_column(Text, nullable=False, default="")
    deadline: Mapped[date] = mapped_column(Date, nullable=False)
    budget: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    chosen_response_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # 买家勾了「公开到我的小店」（默认不勾）：勾了才在小店页上显示
    shop_public: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class DemandResponse(Base):
    __tablename__ = "demand_response"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    demand_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("demand.id", ondelete="CASCADE"), nullable=False
    )
    seller_subject: Mapped[str] = mapped_column(String(512), nullable=False)
    listing_kind: Mapped[str | None] = mapped_column(String(16))
    listing_ref: Mapped[str | None] = mapped_column(String(128))
    proposal: Mapped[str] = mapped_column(Text, nullable=False)
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    days: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    created_at: Mapped[datetime] = _now()


class Trade(Base):
    __tablename__ = "trade"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    buyer_subject: Mapped[str] = mapped_column(String(512), nullable=False)
    seller_subject: Mapped[str] = mapped_column(String(512), nullable=False)
    listing_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    # v1 时买知识复制进的应用；user-data/v2 不分区，新成交为空，旧的留作历史
    target_app: Mapped[str | None] = mapped_column(String(16))
    granted: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    demand_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("demand.id", ondelete="SET NULL")
    )
    # 买知识：办理中（granting）/ 成交；幂等键与参数摘要；要建使用权的几份
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="done")
    request_key: Mapped[str | None] = mapped_column(String(64))
    request_digest: Mapped[str | None] = mapped_column(String(64))
    planned: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    failure: Mapped[str | None] = mapped_column(String(64))
    # 成交那一刻卖家写的能不能转卖（Fable 评审 P2-16）
    resale_allowed: Mapped[bool | None] = mapped_column(Boolean)
    # 成交那一刻卖的发布主体（名称、个人或非个人）：主体后来改了，这笔照旧标当时的
    # （SDD 0018 第十一节第 7 之二小节）
    seller_publisher: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _now()


class TradeRequestAlias(Base):
    """换了键接手同一笔时记下的新键（Fable 评审 P2-7）。"""

    __tablename__ = "trade_request_alias"

    buyer_subject: Mapped[str] = mapped_column(String(512), primary_key=True)
    request_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    trade_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trade.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = _now()


class TradeReview(Base):
    __tablename__ = "trade_review"

    trade_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trade.id", ondelete="CASCADE"), primary_key=True
    )
    rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reply: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MarketMessage(Base):
    __tablename__ = "market_message"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    thread_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    thread_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    author_subject: Mapped[str] = mapped_column(String(512), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = _now()


class HouseRules(Base):
    __tablename__ = "house_rules"

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = _now()


class MarketReport(Base):
    __tablename__ = "market_report"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    reporter_subject: Mapped[str] = mapped_column(String(512), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    note: Mapped[str | None] = mapped_column(Text)
    handled_by: Mapped[str | None] = mapped_column(String(512))
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()


class PublisherChange(Base):
    """发布主体的变更历史：一店一主体，主体可改，改了要重审、留历史（SDD 0018 第十一节
    第 7 之二小节）。只记对外的名称与个人 / 非个人，不记实名材料。"""

    __tablename__ = "publisher_change"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    publisher_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("publisher.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = _now()


class Shop(TimestampMixin, Base):
    """每个用户一家小店（SDD 0018 第十一节第 7 之二小节；数据拓扑第十一节）。

    处置状态是持久的：停店（suspended）期间公开列表、条目发布、审核转公开、下单、请
    专家都不过；恢复不动被单独下架的条目，停店也不撤销历史订单。
    """

    __tablename__ = "shop"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    owner_subject: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    status_note: Mapped[str | None] = mapped_column(Text)
    status_by: Mapped[str | None] = mapped_column(String(512))
    status_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("status IN ('open', 'suspended')", name="ck_shop_status"),
    )


class ShopEvent(Base):
    """小店的留痕：改店名、停店、恢复，谁、什么时候、为什么。"""

    __tablename__ = "shop_event"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    shop_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shop.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    old_name: Mapped[str | None] = mapped_column(String(40))
    new_name: Mapped[str | None] = mapped_column(String(40))
    note: Mapped[str | None] = mapped_column(Text)
    by_subject: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = _now()

    __table_args__ = (
        CheckConstraint(
            "kind IN ('renamed', 'suspended', 'restored')", name="ck_shop_event_kind"
        ),
    )
