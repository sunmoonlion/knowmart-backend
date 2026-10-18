"""分身与它的版本（SDD 0027）。版本只插不改；停用只改状态。"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.models.base import Base, TimestampMixin


class Knowbot(TimestampMixin, Base):
    __tablename__ = "knowbot"

    # 由应用生成（模板不装 uuid-ossp）
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)

    owner_subject: Mapped[str] = mapped_column(String(512), nullable=False)
    # knowlab 那边这个分身的稳定引用；同一个引用永远是同一个分身
    source_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    tagline: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    current_version: Mapped[str | None] = mapped_column(String(16))
    # 专家目录（SDD 0032）：类目、可见范围（自用 / 公开）、发布主体（公开才有）
    category: Mapped[str] = mapped_column(
        String(64), nullable=False, default="finance.securities_advisory"
    )
    visibility: Mapped[str] = mapped_column(
        String(16), nullable=False, default="private"
    )
    publisher_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("publisher.id", ondelete="RESTRICT")
    )

    __table_args__ = (
        UniqueConstraint("source_ref", name="uq_knowbot_source_ref"),
        Index("ix_knowbot_owner_subject", "owner_subject"),
        CheckConstraint(
            "visibility IN ('private', 'public')", name="ck_knowbot_visibility"
        ),
        CheckConstraint(
            "visibility = 'private' OR publisher_id IS NOT NULL",
            name="ck_knowbot_public_has_publisher",
        ),
    )


class Publisher(TimestampMixin, Base):
    """发布主体：个人或非个人（SDD 0032，照腾讯 WorkBuddy 开放平台）。

    现在不开放入驻。"""

    __tablename__ = "publisher"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    real_name_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    credit_code: Mapped[str | None] = mapped_column(String(32))

    __table_args__ = (
        CheckConstraint(
            "kind IN ('individual', 'organization')", name="ck_publisher_kind"
        ),
    )


class PublisherLicense(Base):
    """发布主体按类目的资质；审核通过、未过期才算数。现在不开放上传。"""

    __tablename__ = "publisher_license"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    publisher_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("publisher.id", ondelete="RESTRICT"),
        nullable=False,
    )
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    license_name: Mapped[str] = mapped_column(String(200), nullable=False)
    license_no: Mapped[str] = mapped_column(String(100), nullable=False)
    valid_until: Mapped[date | None] = mapped_column(Date)
    review_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(512))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "review_status IN ('pending', 'approved', 'rejected')",
            name="ck_publisher_license_review_status",
        ),
        Index("ix_publisher_license_publisher_category", "publisher_id", "category"),
    )


class KnowbotVersion(Base):
    __tablename__ = "knowbot_version"

    knowbot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowbot.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    version: Mapped[str] = mapped_column(String(16), primary_key=True)
    digest: Mapped[str] = mapped_column(String(80), nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    delivery_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    delivered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("delivery_id", name="uq_knowbot_version_delivery_id"),
        CheckConstraint(
            "status IN ('active', 'stopped')", name="ck_knowbot_version_status"
        ),
    )
