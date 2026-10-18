"""没选类目的专家记「未分类」，不默认成「证券 / 期货投资咨询」（所有者 2026-10-18 采纳审读第 6 条）。

0005 给已有的专家都写了默认的投资咨询类，没有一位是谁选过的：都改成未分类。类目以后在 knowlab
交付时由运营选。未分类的专家不能公开（公开要发布主体在该类目有资质，未分类没有类目可审）。
"""

from __future__ import annotations

from alembic import op

revision = "20261018_0006"
down_revision = "20261018_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("knowbot", "category", server_default="uncategorized")
    op.execute(
        "UPDATE knowbot SET category = 'uncategorized'"
        " WHERE category = 'finance.securities_advisory' AND visibility = 'private'"
    )
    op.create_check_constraint(
        "ck_knowbot_public_has_category",
        "knowbot",
        "visibility = 'private' OR category <> 'uncategorized'",
    )


def downgrade() -> None:
    op.drop_constraint("ck_knowbot_public_has_category", "knowbot", type_="check")
    op.execute(
        "UPDATE knowbot SET category = 'finance.securities_advisory'"
        " WHERE category = 'uncategorized'"
    )
    op.alter_column("knowbot", "category", server_default="finance.securities_advisory")
