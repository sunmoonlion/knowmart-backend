"""成交表的叫法跟上第四版；条目记下架原因（10-29）。

- `trade.copied` 改名 `granted`，状态 `copying` 改名 `granting`：v1 时买知识是复制一份，
  第四版是给买家建使用权（SDD 0043 第 4.3 节），旧叫法不留（decisions ⑰，所有者 10-29
  「都按你建议」）；状态的 check 约束随之重建。
- `knowledge_listing.delist_reason`：卖家交给 knowmart 的资料没了（`item_gone`）或不再
  出售（`item_not_for_sale`）时，用到它的待审、在架条目自动下架，原因写在这里给卖家看；
  卖家自己下架、改了重交时清空。原来是不让删在架条目里的资料，那道拦截随 v1 的 knowmart
  知识库一起没了。

downgrade 改回旧叫法、删掉这一列（记着的原因不恢复）。

Revision ID: 20261029_0011
Revises: 20261028_0010
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261029_0011"
down_revision = "20261028_0010"
branch_labels = None
depends_on = None

REASONS = "delist_reason IS NULL OR delist_reason IN ('item_gone', 'item_not_for_sale')"


def upgrade() -> None:
    op.alter_column("trade", "copied", new_column_name="granted")
    op.drop_constraint("ck_trade_status", "trade", type_="check")
    op.execute("UPDATE trade SET status = 'granting' WHERE status = 'copying'")
    op.create_check_constraint(
        "ck_trade_status", "trade", "status IN ('granting', 'done', 'failed')"
    )
    op.add_column("knowledge_listing", sa.Column("delist_reason", sa.String(32)))
    op.create_check_constraint(
        "ck_knowledge_listing_delist_reason", "knowledge_listing", REASONS
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_knowledge_listing_delist_reason", "knowledge_listing", type_="check"
    )
    op.drop_column("knowledge_listing", "delist_reason")
    op.drop_constraint("ck_trade_status", "trade", type_="check")
    op.execute("UPDATE trade SET status = 'copying' WHERE status = 'granting'")
    op.create_check_constraint(
        "ck_trade_status", "trade", "status IN ('copying', 'done', 'failed')"
    )
    op.alter_column("trade", "granted", new_column_name="copied")
