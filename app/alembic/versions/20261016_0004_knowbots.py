"""分身与版本（SDD 0027 P2）：版本只插不改，数据库触发器兜底；停用只改状态。"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision = "20261016_0004"
down_revision = "20260911_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowbot",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),  # 由应用生成
        sa.Column("owner_subject", sa.String(512), nullable=False),
        sa.Column("source_ref", sa.String(128), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("tagline", sa.String(200), nullable=False, server_default=""),
        sa.Column("current_version", sa.String(16), nullable=True),
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
        sa.UniqueConstraint("source_ref", name="uq_knowbot_source_ref"),
    )
    op.create_index("ix_knowbot_owner_subject", "knowbot", ["owner_subject"])
    op.create_table(
        "knowbot_version",
        sa.Column(
            "knowbot_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("knowbot.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("version", sa.String(16), primary_key=True),
        sa.Column("digest", sa.String(80), nullable=False),
        sa.Column("manifest", pg.JSONB(), nullable=False),
        sa.Column("delivery_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column(
            "delivered_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("delivery_id", name="uq_knowbot_version_delivery_id"),
        sa.CheckConstraint(
            "status IN ('active', 'stopped')", name="ck_knowbot_version_status"
        ),
    )
    # 版本只插不改：除了 status 什么都不许改，也不许删
    # （应用代码出错也改不动已交付的版本）
    op.execute(
        """
        CREATE FUNCTION knowbot_version_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'knowbot versions are never deleted';
          END IF;
          IF NEW.knowbot_id <> OLD.knowbot_id OR NEW.version <> OLD.version
             OR NEW.digest <> OLD.digest OR NEW.manifest <> OLD.manifest
             OR NEW.delivery_id <> OLD.delivery_id OR NEW.delivered_at <>
             OLD.delivered_at THEN
            RAISE EXCEPTION 'knowbot versions never change; only status may';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER knowbot_version_immutable
        BEFORE UPDATE OR DELETE ON knowbot_version
        FOR EACH ROW EXECUTE FUNCTION knowbot_version_immutable()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS knowbot_version_immutable ON knowbot_version")
    op.execute("DROP FUNCTION IF EXISTS knowbot_version_immutable()")
    op.drop_table("knowbot_version")
    op.drop_index("ix_knowbot_owner_subject", table_name="knowbot")
    op.drop_table("knowbot")
