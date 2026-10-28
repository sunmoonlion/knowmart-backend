"""删清单 v1 交来的专家版本（decisions ⑰：不留旧版兼容）。

10-28 起 knowmart 只收 `knowbot-manifest/v2`（逻辑 + 作者挑选的数据）；v1 的版本
（数据需求声明、knowlab 数据集绑定）没人能再请、再跑，连同只剩 v1 版本的专家一起删：

- `knowbot_version`：`manifest->>'schema'` 不是 `knowbot-manifest/v2` 的行删掉；
- `knowbot`：一版都不剩的删掉，它在 `knowbot_unlisted_version` 里的空号结局一并删；
  还有剩的，`current_version` 修到剩下最高的那一版，名字与一句话跟着那一版（同交付时
  `set_current` 的做法）；
- 订单历史不动：`trade.listing_ref`、`demand_response.listing_ref`、评价、举报照旧留着。

版本表的「不许删」触发器只在删 v1 那一句前后停开一次（同一个事务里）。

downgrade 不恢复删掉的行（数据已不在），只是空操作：表结构没变。

Revision ID: 20261028_0010
Revises: 20261025_0009
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261028_0010"
down_revision = "20261025_0009"
branch_labels = None
depends_on = None

V2 = "knowbot-manifest/v2"


def upgrade() -> None:
    conn = op.get_bind()
    params = {"v2": V2}
    # 只剩 v1 版本的专家：先删它们的空号结局（按 knowlab 的引用对上）
    conn.execute(
        sa.text(
            """
            DELETE FROM knowbot_unlisted_version u
            USING knowbot k
            WHERE u.source_ref = k.source_ref
              AND NOT EXISTS (
                SELECT 1 FROM knowbot_version v
                WHERE v.knowbot_id = k.id AND v.manifest->>'schema' = :v2
              )
            """
        ),
        params,
    )
    # 版本表有「只插不改、不许删」的触发器（20261016_0004）；只在这次删 v1 时停一下，
    # 同一个事务里删完就开回来
    conn.execute(
        sa.text("ALTER TABLE knowbot_version DISABLE TRIGGER knowbot_version_immutable")
    )
    conn.execute(
        sa.text(
            "DELETE FROM knowbot_version WHERE manifest->>'schema' IS DISTINCT FROM :v2"
        ),
        params,
    )
    conn.execute(
        sa.text("ALTER TABLE knowbot_version ENABLE TRIGGER knowbot_version_immutable")
    )
    conn.execute(
        sa.text(
            """
            DELETE FROM knowbot k
            WHERE NOT EXISTS (SELECT 1 FROM knowbot_version v WHERE v.knowbot_id = k.id)
            """
        )
    )
    # 还有剩的：当前版本修到剩下最高的那一版（版本号是数字串，按数值比）
    conn.execute(
        sa.text(
            """
            UPDATE knowbot k
            SET current_version = top.version,
                name = top.manifest->>'name',
                tagline = coalesce(top.manifest->>'tagline', '')
            FROM (
                SELECT DISTINCT ON (knowbot_id) knowbot_id, version, manifest
                FROM knowbot_version
                ORDER BY knowbot_id, version::int DESC
            ) top
            WHERE top.knowbot_id = k.id
              AND k.current_version IS DISTINCT FROM top.version
            """
        )
    )


def downgrade() -> None:
    """删掉的 v1 版本与专家不恢复；表结构没变，这里不做事。"""
