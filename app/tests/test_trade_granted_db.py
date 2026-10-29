"""迁移 20261029_0011：成交表 copied/copying 改名 granted/granting，条目加下架原因；
旧行跟着改，downgrade 改回。真 PostgreSQL、一次性 schema。"""

from __future__ import annotations

import json
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from tests.test_drop_manifest_v1_db import _module
from tests.test_knowbot_repository_db import ROOT

TARGET = "20261029_0011_trade_granted_delist_reason"


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
    schema = "trade_granted_" + uuid.uuid4().hex
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


async def test_old_rows_follow_the_rename_and_downgrade_restores(engine) -> None:  # type: ignore[no-untyped-def]
    held = [{"item_id": "d1", "name": "行业数据.xlsx"}]
    async with engine.begin() as c:
        for status in ("copying", "done"):
            await c.execute(
                text(
                    "INSERT INTO trade (id, kind, buyer_subject, seller_subject,"
                    " listing_ref, title, copied, status)"
                    " VALUES (:id, 'knowledge', 'b', 's', 'l', '买知识',"
                    " CAST(:held AS jsonb), :status)"
                ),
                {"id": uuid.uuid4(), "held": json.dumps(held), "status": status},
            )
        await c.run_sync(lambda conn: _run(conn, target=True))

    async with engine.connect() as c:
        rows = (
            await c.execute(text("SELECT status, granted FROM trade ORDER BY status"))
        ).all()
        assert [(r.status, r.granted) for r in rows] == [
            ("done", held),
            ("granting", held),
        ]
        cols = {
            r.column_name
            for r in await c.execute(
                text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name = 'knowledge_listing'"
                    " AND table_schema = current_schema()"
                )
            )
        }
        assert "delist_reason" in cols

    with pytest.raises(DBAPIError):  # 旧状态写不进了
        async with engine.begin() as c:
            await c.execute(text("UPDATE trade SET status = 'copying'"))

    async with engine.begin() as c:
        await c.run_sync(lambda conn: _run(conn, target=True, down=True))
    async with engine.connect() as c:
        rows = (
            await c.execute(text("SELECT status, copied FROM trade ORDER BY status"))
        ).all()
        assert [r.status for r in rows] == ["copying", "done"]
