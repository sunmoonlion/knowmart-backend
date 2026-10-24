"""供需市场（SDD 0018 第十一节，本期做）。

- 发布主体：按用户一条（个人 / 非个人），实名材料与证照照片存这里的私有表（不进知识库），
  运营审过才算实名；照片审过后留一年清掉（第 9 小节第 4 项）。资质加照片与审核说明。
- 专家条目：沿用 knowbot，加价格（每次请）与上架审核（自用 / 申请公开 / 公开 / 没过 / 下架）。
- 知识条目、需求、响应、交易记录、留言、评价、驻场规则版本、举报：新表。
  知识的内容不在这里，在 knowbase 的 knowmart 分区（SDD 0042 第八节）；本期标价、记成交，
  不接真实支付（第 9 小节第 2 项）。

Revision ID: 20261023_0007
Revises: 20261018_0006
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision = "20261023_0007"
down_revision = "20261018_0006"
branch_labels = None
depends_on = None


def _ts(name: str, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def _created() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )


def _updated() -> sa.Column:
    return sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )


def _review(prefix: str = "") -> list[sa.Column]:
    return [
        sa.Column(f"{prefix}review_note", sa.Text()),
        sa.Column(f"{prefix}reviewed_by", sa.String(512)),
        _ts(f"{prefix}reviewed_at"),
    ]


def upgrade() -> None:
    # ---- 发布主体与资质 ----
    op.add_column("publisher", sa.Column("owner_subject", sa.String(512)))
    op.create_index(
        "uq_publisher_owner_subject",
        "publisher",
        ["owner_subject"],
        unique=True,
        postgresql_where=sa.text("owner_subject IS NOT NULL"),
    )
    op.add_column(
        "publisher",
        sa.Column(
            "review_status", sa.String(16), nullable=False, server_default="approved"
        ),
    )
    for column in _review():
        op.add_column("publisher", column)
    op.add_column("publisher", sa.Column("real_name", sa.String(100)))
    # 身份证号只留遮住中间的样子与摘要（查重）；原号码只在照片里，给运营核
    op.add_column("publisher", sa.Column("id_number_masked", sa.String(32)))
    op.add_column("publisher", sa.Column("id_number_hash", sa.String(64)))
    op.add_column("publisher", sa.Column("legal_person", sa.String(100)))
    op.add_column("publisher", sa.Column("contact_name", sa.String(100)))
    op.create_check_constraint(
        "ck_publisher_review_status",
        "publisher",
        "review_status IN ('pending', 'approved', 'rejected')",
    )
    op.create_table(
        "publisher_document",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "publisher_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("publisher.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("media_type", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("content", sa.LargeBinary()),
        _created(),
        _ts("purge_after"),
        _ts("purged_at"),
        sa.CheckConstraint(
            "kind IN ('id_front', 'id_back', 'business_license', 'license')",
            name="ck_publisher_document_kind",
        ),
    )
    op.add_column(
        "publisher_license",
        sa.Column(
            "document_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("publisher_document.id", ondelete="SET NULL"),
        ),
    )
    op.add_column("publisher_license", sa.Column("review_note", sa.Text()))
    op.add_column("publisher_license", _ts("reminded_at"))

    # ---- 专家条目 ----
    op.add_column("knowbot", sa.Column("price", sa.Numeric(12, 2)))
    op.add_column(
        "knowbot",
        sa.Column(
            "listing_status", sa.String(16), nullable=False, server_default="self"
        ),
    )
    for column in _review("listing_"):
        op.add_column("knowbot", column)
    op.create_check_constraint(
        "ck_knowbot_listing_status",
        "knowbot",
        "listing_status IN ('self', 'pending', 'public', 'rejected', 'taken_down')",
    )

    # ---- 知识条目 ----
    op.create_table(
        "knowledge_listing",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("seller_subject", sa.String(512), nullable=False),
        sa.Column(
            "publisher_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("publisher.id", ondelete="RESTRICT"),
        ),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("items", pg.JSONB(), nullable=False),
        sa.Column("sample", sa.Text(), nullable=False, server_default=""),
        sa.Column("rights_statement", sa.Text(), nullable=False),
        sa.Column(
            "resale_allowed", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("price", sa.Numeric(12, 2)),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        *_review(),
        _created(),
        _updated(),
        sa.CheckConstraint(
            "status IN ('draft', 'pending', 'listed', 'rejected', 'delisted',"
            " 'taken_down')",
            name="ck_knowledge_listing_status",
        ),
        sa.CheckConstraint(
            "price IS NULL OR price >= 0", name="ck_knowledge_listing_price"
        ),
    )
    op.create_index(
        "ix_knowledge_listing_seller", "knowledge_listing", ["seller_subject"]
    )
    op.create_index("ix_knowledge_listing_status", "knowledge_listing", ["status"])

    # ---- 需求与响应 ----
    op.create_table(
        "demand",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("buyer_subject", sa.String(512), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("problem", sa.Text(), nullable=False),
        sa.Column("data_wanted", sa.Text(), nullable=False, server_default=""),
        sa.Column("deadline", sa.Date(), nullable=False),
        sa.Column("budget", sa.Numeric(12, 2)),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("chosen_response_id", pg.UUID(as_uuid=True)),
        _created(),
        _updated(),
        sa.CheckConstraint("kind IN ('expert', 'knowledge')", name="ck_demand_kind"),
        sa.CheckConstraint(
            "status IN ('open', 'chosen', 'closed', 'taken_down')",
            name="ck_demand_status",
        ),
    )
    op.create_index("ix_demand_status", "demand", ["status", "deadline"])
    op.create_table(
        "demand_response",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "demand_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("demand.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seller_subject", sa.String(512), nullable=False),
        # 附上自己已有的条目：专家写「专家编号@版本」，知识写知识条目编号；可以不附
        sa.Column("listing_kind", sa.String(16)),
        sa.Column("listing_ref", sa.String(128)),
        sa.Column("proposal", sa.Text(), nullable=False),
        sa.Column("price", sa.Numeric(12, 2)),
        sa.Column("days", sa.Integer()),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        _created(),
        sa.CheckConstraint(
            "status IN ('open', 'chosen', 'not_chosen', 'withdrawn', 'taken_down')",
            name="ck_demand_response_status",
        ),
        sa.CheckConstraint(
            "(listing_kind IS NULL AND listing_ref IS NULL)"
            " OR (listing_kind IN ('expert', 'knowledge') AND listing_ref IS NOT NULL)",
            name="ck_demand_response_listing",
        ),
        sa.UniqueConstraint(
            "demand_id", "seller_subject", name="uq_demand_response_seller"
        ),
    )

    # ---- 交易记录、评价 ----
    op.create_table(
        "trade",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("buyer_subject", sa.String(512), nullable=False),
        sa.Column("seller_subject", sa.String(512), nullable=False),
        sa.Column("listing_ref", sa.String(128), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("price", sa.Numeric(12, 2)),
        # 知识：复制进买家哪个知识库、复制出来的几份（卖家资料编号 → 买家资料编号与摘要）
        sa.Column("target_app", sa.String(16)),
        sa.Column("copied", pg.JSONB(), nullable=False, server_default="[]"),
        sa.Column(
            "demand_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("demand.id", ondelete="SET NULL"),
        ),
        # 买知识可恢复（Astra 评审 F07）：先落一笔「复制中」，记要复制的几份（planned）
        # 与买家这次操作的幂等键；逐份复制、逐份记进 copied，都复制完才算成交（done）
        sa.Column("status", sa.String(16), nullable=False, server_default="done"),
        sa.Column("request_key", sa.String(64)),
        sa.Column("request_digest", sa.String(64)),
        sa.Column("planned", pg.JSONB(), nullable=False, server_default="[]"),
        _created(),
        sa.CheckConstraint("kind IN ('expert', 'knowledge')", name="ck_trade_kind"),
        sa.CheckConstraint("status IN ('copying', 'done')", name="ck_trade_status"),
        sa.CheckConstraint(
            "target_app IS NULL OR target_app IN ('knowbench', 'knowlab')",
            name="ck_trade_target_app",
        ),
    )
    op.create_index("ix_trade_buyer", "trade", ["buyer_subject", "created_at"])
    op.create_index("ix_trade_seller", "trade", ["seller_subject", "created_at"])
    op.create_index("ix_trade_listing", "trade", ["listing_ref"])
    op.create_index(
        "uq_trade_request",
        "trade",
        ["buyer_subject", "request_key"],
        unique=True,
        postgresql_where=sa.text("request_key IS NOT NULL"),
    )
    op.create_table(
        "trade_review",
        sa.Column(
            "trade_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("trade.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("rating", sa.SmallInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("reply", sa.Text()),
        _created(),
        _ts("replied_at"),
        sa.CheckConstraint("rating BETWEEN 1 AND 5", name="ck_trade_review_rating"),
    )

    # ---- 留言、驻场规则、举报 ----
    op.create_table(
        "market_message",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("thread_kind", sa.String(16), nullable=False),
        sa.Column("thread_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("author_subject", sa.String(512), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("hidden", sa.Boolean(), nullable=False, server_default=sa.false()),
        _created(),
        sa.CheckConstraint(
            "thread_kind IN ('demand', 'response')", name="ck_market_message_thread"
        ),
    )
    op.create_index(
        "ix_market_message_thread",
        "market_message",
        ["thread_kind", "thread_id", "created_at"],
    )
    op.create_table(
        "house_rules",
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(512), nullable=False),
        _created(),
    )
    op.create_table(
        "market_report",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("reporter_subject", sa.String(512), nullable=False),
        sa.Column("target_kind", sa.String(16), nullable=False),
        sa.Column("target_id", sa.String(128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("note", sa.Text()),
        sa.Column("handled_by", sa.String(512)),
        _ts("handled_at"),
        _created(),
        sa.CheckConstraint(
            "target_kind IN ('expert', 'knowledge', 'demand', 'response', 'message')",
            name="ck_market_report_target",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'upheld', 'dismissed')", name="ck_market_report_status"
        ),
    )
    op.create_index("ix_market_report_status", "market_report", ["status"])


def downgrade() -> None:
    op.drop_index("ix_market_report_status", table_name="market_report")
    op.drop_table("market_report")
    op.drop_table("house_rules")
    op.drop_index("ix_market_message_thread", table_name="market_message")
    op.drop_table("market_message")
    op.drop_table("trade_review")
    op.drop_index("uq_trade_request", table_name="trade")
    op.drop_index("ix_trade_listing", table_name="trade")
    op.drop_index("ix_trade_seller", table_name="trade")
    op.drop_index("ix_trade_buyer", table_name="trade")
    op.drop_table("trade")
    op.drop_table("demand_response")
    op.drop_index("ix_demand_status", table_name="demand")
    op.drop_table("demand")
    op.drop_index("ix_knowledge_listing_status", table_name="knowledge_listing")
    op.drop_index("ix_knowledge_listing_seller", table_name="knowledge_listing")
    op.drop_table("knowledge_listing")
    op.drop_constraint("ck_knowbot_listing_status", "knowbot", type_="check")
    for column in ("listing_reviewed_at", "listing_reviewed_by", "listing_review_note"):
        op.drop_column("knowbot", column)
    op.drop_column("knowbot", "listing_status")
    op.drop_column("knowbot", "price")
    op.drop_column("publisher_license", "reminded_at")
    op.drop_column("publisher_license", "review_note")
    op.drop_column("publisher_license", "document_id")
    op.drop_table("publisher_document")
    op.drop_constraint("ck_publisher_review_status", "publisher", type_="check")
    for column in (
        "contact_name",
        "legal_person",
        "id_number_hash",
        "id_number_masked",
        "real_name",
        "reviewed_at",
        "reviewed_by",
        "review_note",
        "review_status",
    ):
        op.drop_column("publisher", column)
    op.drop_index("uq_publisher_owner_subject", table_name="publisher")
    op.drop_column("publisher", "owner_subject")
