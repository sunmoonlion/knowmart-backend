"""每个用户一家小店；需求「公开到我的小店」；发布主体变更历史；成交标当时的主体（10-29）。

SDD 0018 第十一节第 7 之二小节、0043 第 4.3 节、数据拓扑第十一节：

- `shop`：一个用户一家，店名默认取昵称、可改；处置状态（open / suspended）是持久的，
  停店期间公开列表、条目发布、审核转公开、下单、请专家都核它。
- `shop_event`：改店名、停店、恢复的留痕。
- `demand.shop_public`：发需求时勾「公开到我的小店」，默认不勾（旧需求都算没勾）。
- `publisher_change`：一店一主体，主体可改要重审、留变更历史（只记名称与个人或
  非个人）。
- `trade.seller_publisher`：成交那一刻卖的主体；旧成交为空。

已有的用户（有专家、条目、需求、响应、主体或成交的）各补一家店，店名取登录时记下的昵称，
没有或含禁止词的叫「小店」。downgrade 删掉这些表与列。

Revision ID: 20261029_0012
Revises: 20261029_0011
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261029_0012"
down_revision = "20261029_0011"
branch_labels = None
depends_on = None

# 同 app/domain/market.py 的 SHOP_NAME_WORDS（迁移不引应用代码，抄一份）
WORDS = (
    "荐股",
    "目标价",
    "评级",
    "买入",
    "卖出",
    "增持",
    "减持",
    "稳赚",
    "保本",
    "收益承诺",
    "保证收益",
    "翻倍",
    "推荐",
)

BACKFILL = """
INSERT INTO shop (id, owner_subject, name, status, created_at, updated_at)
SELECT gen_random_uuid(), s.subject,
       CASE WHEN n.name IS NULL OR n.name ~ :words THEN '小店' ELSE n.name END,
       'open', now(), now()
FROM (
    SELECT owner_subject AS subject FROM knowbot
    UNION SELECT seller_subject FROM knowledge_listing
    UNION SELECT buyer_subject FROM demand
    UNION SELECT seller_subject FROM demand_response
    UNION SELECT owner_subject FROM publisher WHERE owner_subject IS NOT NULL
    UNION SELECT buyer_subject FROM trade
    UNION SELECT seller_subject FROM trade
) AS s
LEFT JOIN (
    SELECT subject,
           min(NULLIF(btrim(left(coalesce(display_name, username, ''), 40)), ''))
               AS name
    FROM auth_user GROUP BY subject
) AS n ON n.subject = s.subject
ON CONFLICT (owner_subject) DO NOTHING
"""


def upgrade() -> None:
    op.create_table(
        "shop",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_subject", sa.String(512), nullable=False),
        sa.Column("name", sa.String(40), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("status_note", sa.Text()),
        sa.Column("status_by", sa.String(512)),
        sa.Column("status_at", sa.DateTime(timezone=True)),
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
        sa.UniqueConstraint("owner_subject", name="uq_shop_owner_subject"),
        sa.CheckConstraint("status IN ('open', 'suspended')", name="ck_shop_status"),
    )
    op.create_table(
        "shop_event",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "shop_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("shop.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("old_name", sa.String(40)),
        sa.Column("new_name", sa.String(40)),
        sa.Column("note", sa.Text()),
        sa.Column("by_subject", sa.String(512), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('renamed', 'suspended', 'restored')", name="ck_shop_event_kind"
        ),
    )
    op.create_index("ix_shop_event_shop", "shop_event", ["shop_id", "created_at"])
    op.create_table(
        "publisher_change",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "publisher_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("publisher.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_publisher_change_publisher", "publisher_change", ["publisher_id"]
    )
    op.add_column(
        "demand",
        sa.Column(
            "shop_public", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "trade", sa.Column("seller_publisher", postgresql.JSONB(), nullable=True)
    )
    op.get_bind().execute(sa.text(BACKFILL), {"words": "|".join(WORDS)})


def downgrade() -> None:
    op.drop_column("trade", "seller_publisher")
    op.drop_column("demand", "shop_public")
    op.drop_index("ix_publisher_change_publisher", table_name="publisher_change")
    op.drop_table("publisher_change")
    op.drop_index("ix_shop_event_shop", table_name="shop_event")
    op.drop_table("shop_event")
    op.drop_table("shop")
