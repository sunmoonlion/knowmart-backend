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
    op.create_index(
        "ix_trade_buyer_expert_ref",
        "trade",
        ["buyer_subject", "listing_ref"],
        unique=True,
        postgresql_where=sa.text("kind = 'expert'"),
    )


def downgrade() -> None:
    op.drop_index("ix_trade_buyer_expert_ref", table_name="trade")
