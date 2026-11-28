"""加部分唯一索引 trade(buyer_subject, listing_ref) WHERE kind='expert'。

同一用户请同一位专家的同一版只记一次成交，防止并发多记。

Revision ID: 20261101_0014
Revises: 20261030_0013
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261101_0014"
down_revision = "20261030_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("trade", sa.Column("duplicate_of", sa.Uuid(), nullable=True))
    op.execute("""WITH ranked AS (
        SELECT id, first_value(id) OVER (PARTITION BY buyer_subject, listing_ref
            ORDER BY created_at, id) AS keeper FROM trade WHERE kind = 'expert'
    ) UPDATE trade t SET duplicate_of=r.keeper FROM ranked r
        WHERE t.id=r.id AND t.id<>r.keeper""")
    op.create_index(
        "ix_trade_buyer_expert_ref",
        "trade",
        ["buyer_subject", "listing_ref"],
        unique=True,
        postgresql_where=sa.text("kind = 'expert' AND duplicate_of IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_trade_buyer_expert_ref", table_name="trade")
    op.execute("ALTER TABLE trade DROP COLUMN IF EXISTS duplicate_of")
