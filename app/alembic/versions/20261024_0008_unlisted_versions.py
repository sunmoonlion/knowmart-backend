"""knowlab 测试通过、没上架的那几版的结局（SDD 0037 第三节）。

专家版本号在 knowlab 测试通过时定，用户不要、或运营审核没过的那一版不上架，所以这里的版本号
可能有空号；详情页写明空号那一版的结局。knowlab 每次有结局（用户不要、审核不过）或交付成功
时把整份名单交过来，这里整份替换。只记结局，不记理由（理由在 knowlab）。

Revision ID: 20261024_0008
Revises: 20261023_0007
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261024_0008"
down_revision = "20261023_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowbot_unlisted_version",
        sa.Column("source_ref", sa.String(128), primary_key=True),
        sa.Column("version", sa.String(16), primary_key=True),
        sa.Column("owner_subject", sa.String(512), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "outcome IN ('declined', 'rejected')",
            name="ck_knowbot_unlisted_version_outcome",
        ),
    )


def downgrade() -> None:
    op.drop_table("knowbot_unlisted_version")
