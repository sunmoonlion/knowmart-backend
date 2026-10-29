"""迁移 20261029_0012：小店、需求「公开到我的小店」、主体变更历史、成交标当时的主体；
已有的用户各补一家店（店名取昵称，没有或含禁止词的叫「小店」），downgrade 删掉。
真 PostgreSQL、一次性 schema。"""

from __future__ import annotations

import os
import uuid
from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from tests.test_drop_manifest_v1_db import _module
from tests.test_knowbot_repository_db import ROOT

TARGET = "20261029_0012_shop"


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
    schema = "shop_" + uuid.uuid4().hex
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


async def test_existing_users_get_a_shop_and_downgrade_removes_it(engine) -> None:  # type: ignore[no-untyped-def]
    async with engine.begin() as c:
        for subject, nickname in (("a", "张三"), ("b", "李四推荐"), ("c", None)):
            await c.execute(
                text(
                    "INSERT INTO demand (id, buyer_subject, kind, title, problem,"
                    " data_wanted, deadline, status)"
                    " VALUES (:id, :s, 'expert', 't', 'p', '', :d, 'open')"
                ),
                {"id": uuid.uuid4(), "s": subject, "d": date(2099, 1, 1)},
            )
            if nickname is not None:
                await c.execute(
                    text(
                        "INSERT INTO auth_user (id, issuer, subject, display_name,"
                        " roles, scopes) VALUES (gen_random_uuid(), 'i', :s, :n,"
                        " '[]', '[]')"
                    ),
                    {"s": subject, "n": nickname},
                )
        await c.run_sync(lambda conn: _run(conn, target=True))

    async with engine.connect() as c:
        shops = (
            await c.execute(
                text("SELECT owner_subject, name, status FROM shop ORDER BY 1")
            )
        ).all()
        assert [tuple(r) for r in shops] == [
            ("a", "张三", "open"),
            ("b", "小店", "open"),  # 昵称含禁止词
            ("c", "小店", "open"),  # 没有昵称
        ]
        public = (
            await c.execute(text("SELECT DISTINCT shop_public FROM demand"))
        ).all()
        assert [r[0] for r in public] == [False]  # 旧需求都算没勾

    async with engine.begin() as c:
        await c.run_sync(lambda conn: _run(conn, target=True, down=True))
    async with engine.connect() as c:
        tables = {
            r[0]
            for r in await c.execute(
                text(
                    "SELECT table_name FROM information_schema.tables"
                    " WHERE table_schema = current_schema()"
                )
            )
        }
        assert not {"shop", "shop_event", "publisher_change"} & tables
