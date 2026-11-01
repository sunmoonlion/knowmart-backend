"""在售专家所绑数据失效时转「待处理」（0043 第 4.3 节；数据拓扑第十节第 5 条）。

`knowbot_version.status` 多一种 `held`（待处理）：knowlab 审成公开、在售的那一版，所绑
数据删了、禁用了、许可改低了，后台定时核到就转待处理，别人请不了，原因记在
`held_reason`（作者在小店里看得到）；数据好了自动回 `active`。旧数据都没有待处理。
downgrade 先把待处理的改回 active 再收约束、删列。

Revision ID: 20261030_0013
Revises: 20261029_0012
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261030_0013"
down_revision = "20261029_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("knowbot_version", sa.Column("held_reason", sa.String(32)))
    op.add_column("knowbot_version", sa.Column("held_at", sa.DateTime(timezone=True)))
    op.drop_constraint("ck_knowbot_version_status", "knowbot_version", type_="check")
    op.create_check_constraint(
        "ck_knowbot_version_status",
        "knowbot_version",
        "status IN ('active', 'held', 'stopped')",
    )
    op.create_check_constraint(
        "ck_knowbot_version_held_reason",
        "knowbot_version",
        "(status = 'held') = (held_reason IS NOT NULL)",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE knowbot_version SET status = 'active', held_reason = NULL,"
        " held_at = NULL WHERE status = 'held'"
    )
    op.drop_constraint(
        "ck_knowbot_version_held_reason", "knowbot_version", type_="check"
    )
    op.drop_constraint("ck_knowbot_version_status", "knowbot_version", type_="check")
    op.create_check_constraint(
        "ck_knowbot_version_status",
        "knowbot_version",
        "status IN ('active', 'stopped')",
    )
    op.drop_column("knowbot_version", "held_at")
    op.drop_column("knowbot_version", "held_reason")
