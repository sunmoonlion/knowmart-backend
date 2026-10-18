"""专家目录（SDD 0032）：专家加类目、可见范围、发布主体；发布主体与资质两张骨架表。

现在一律自用、不开放入驻与资质上传；公开要「主体有这个类目审核通过、未过期的资质」且平台开关打开。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision = "20261018_0005"
down_revision = "20261016_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "publisher",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),  # 由应用生成
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column(
            "real_name_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        # 非个人主体的统一社会信用代码
        sa.Column("credit_code", sa.String(32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('individual', 'organization')", name="ck_publisher_kind"
        ),
    )
    op.create_table(
        "publisher_license",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "publisher_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("publisher.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("license_name", sa.String(200), nullable=False),
        sa.Column("license_no", sa.String(100), nullable=False),
        sa.Column("valid_until", sa.Date(), nullable=True),
        sa.Column(
            "review_status",
            sa.String(16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("reviewed_by", sa.String(512), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "review_status IN ('pending', 'approved', 'rejected')",
            name="ck_publisher_license_review_status",
        ),
    )
    op.create_index(
        "ix_publisher_license_publisher_category",
        "publisher_license",
        ["publisher_id", "category"],
    )
    op.add_column(
        "knowbot",
        sa.Column(
            "category",
            sa.String(64),
            nullable=False,
            server_default="finance.securities_advisory",
        ),
    )
    op.add_column(
        "knowbot",
        sa.Column(
            "visibility", sa.String(16), nullable=False, server_default="private"
        ),
    )
    op.add_column(
        "knowbot",
        sa.Column(
            "publisher_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("publisher.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_knowbot_visibility", "knowbot", "visibility IN ('private', 'public')"
    )
    # 公开必须有发布主体
    op.create_check_constraint(
        "ck_knowbot_public_has_publisher",
        "knowbot",
        "visibility = 'private' OR publisher_id IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint("ck_knowbot_public_has_publisher", "knowbot", type_="check")
    op.drop_constraint("ck_knowbot_visibility", "knowbot", type_="check")
    op.drop_column("knowbot", "publisher_id")
    op.drop_column("knowbot", "visibility")
    op.drop_column("knowbot", "category")
    op.drop_index(
        "ix_publisher_license_publisher_category", table_name="publisher_license"
    )
    op.drop_table("publisher_license")
    op.drop_table("publisher")
