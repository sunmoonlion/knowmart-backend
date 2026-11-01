"""迁移 20261030_0013：在售专家所绑数据失效时转「待处理」（0043 第 4.3 节）。版本状态多
一种 held、带原因；只有待处理的带原因；downgrade 把待处理的改回 active 再删列。
真 PostgreSQL、一次性 schema。"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from tests.test_drop_manifest_v1_db import _module
from tests.test_knowbot_repository_db import ROOT

TARGET = "20261030_0013_version_held"


def _run(connection, *, target: bool, down: bool = False) -> None:  # type: ignore[no-untyped-def]
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with Operations.context(MigrationContext.configure(connection)):
        for path in sorted((ROOT / "alembic/versions").glob("20*.py")):
            if path.stem > TARGET or (path.stem == TARGET) != target:
                continue
            module = _module(path)
            module.downgrade() if down else module.upgrade()


@pytest_asyncio.fixture
async def engine():
    url = os.environ.get("DELIVERY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set DELIVERY_TEST_DATABASE_URL to a disposable *_tests database")
    parsed = make_url(url)
    assert parsed.database and parsed.database.endswith("_tests")
    schema = "held_" + uuid.uuid4().hex
    made = create_async_engine(
        parsed.set(drivername="postgresql+asyncpg"),
        connect_args={"server_settings": {"search_path": schema + ",public"}},
    )
    try:
        async with made.begin() as c:
            await c.execute(text(f'CREATE SCHEMA "{schema}"'))
            await c.execute(
                text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA public')
            )
            await c.run_sync(lambda conn: _run(conn, target=False))
        yield made
    finally:
        async with made.begin() as c:
            await c.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await made.dispose()


async def test_held_versions_carry_a_reason_and_downgrade_back(engine) -> None:
    bot = uuid.uuid4()
    async with engine.begin() as c:
        await c.execute(
            text(
                "INSERT INTO knowbot (id, owner_subject, source_ref, name, tagline,"
                " category, visibility, listing_status, created_at, updated_at)"
                " VALUES (:b, 'u-1', 'knowlab-r1', '财务分析与预测', '',"
                " 'uncategorized', 'private', 'self', now(), now())"
            ),
            {"b": bot},
        )
        await c.execute(
            text(
                "INSERT INTO knowbot_version (knowbot_id, version, digest, manifest,"
                " delivery_id, status, public_allowed) VALUES (:b, '1', 'sha256:x',"
                " '{}', 'knowlab-expert-1', 'active', true)"
            ),
            {"b": bot},
        )
        await c.run_sync(lambda conn: _run(conn, target=True))
    async with engine.begin() as c:
        await c.execute(
            text(
                "UPDATE knowbot_version SET status = 'held',"
                " held_reason = 'binding_deleted', held_at = now()"
            )
        )
    with pytest.raises(IntegrityError):  # 待处理的必须带原因
        async with engine.begin() as c:
            await c.execute(text("UPDATE knowbot_version SET held_reason = NULL"))
    async with engine.begin() as c:
        await c.run_sync(lambda conn: _run(conn, target=True, down=True))
    async with engine.begin() as c:
        status = (
            await c.execute(text("SELECT status FROM knowbot_version"))
        ).scalar_one()
        assert status == "active"
