"""分身与它的版本（SDD 0027）。版本只插不改；停用只改状态。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
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

    __table_args__ = (
        UniqueConstraint("source_ref", name="uq_knowbot_source_ref"),
        Index("ix_knowbot_owner_subject", "owner_subject"),
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
