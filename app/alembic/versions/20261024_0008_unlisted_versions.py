"""knowlab 测试通过、没上架的那几版的结局（SDD 0037 第三节）。

专家版本号在 knowlab 测试通过时定，用户不要、或运营审核没过的那一版不上架，所以这里的版本号
可能有空号；详情页写明空号那一版的结局。knowlab 按版本逐条交来（Astra 评审 F08）。只记结局，
不记理由（理由在 knowlab）。

每一版另记能不能给别人看、给别人请（public_allowed）：knowlab 运营逐版审成「公开」的才是真，
审成自用的那一版即使专家整体公开也只给主人（Fable 评审 P2-2）。这之前交付的照原来的规矩
（看专家整体），回填为真。

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
    op.add_column(
        "knowbot_version",
        sa.Column(
            "public_allowed", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
    )
    op.alter_column("knowbot_version", "public_allowed", server_default=sa.false())


def downgrade() -> None:
    op.drop_column("knowbot_version", "public_allowed")
    op.drop_table("knowbot_unlisted_version")
