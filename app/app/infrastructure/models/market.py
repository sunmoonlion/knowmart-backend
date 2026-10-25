"""供需市场的表（SDD 0018 第十一节）。

知识条目、需求、响应、交易记录、评价、留言、驻场规则、举报、发布主体的证件照片。
专家条目在 knowbot 表上（knowbots.py）。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
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
    target_app: Mapped[str | None] = mapped_column(String(16))
    copied: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    demand_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("demand.id", ondelete="SET NULL")
    )
    # 买知识：复制中 / 成交；这次操作的幂等键与参数摘要；要复制的几份（Astra 评审 F07）
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="done")
    request_key: Mapped[str | None] = mapped_column(String(64))
    request_digest: Mapped[str | None] = mapped_column(String(64))
    planned: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    failure: Mapped[str | None] = mapped_column(String(64))
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
