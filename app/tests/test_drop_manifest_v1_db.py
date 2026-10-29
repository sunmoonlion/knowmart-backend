"""迁移 20261028_0010（decisions ⑰）：清单 v1 的版本删掉，只剩 v1 的专家连同空号结局
一起删，还有剩的当前版本修到剩下最高；订单历史不动。真 PostgreSQL、一次性 schema。"""

from __future__ import annotations

import importlib.util
import json
import os
import uuid

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from tests.knowbot_fakes import manifest
from tests.test_knowbot_repository_db import OWNER, ROOT

TARGET = "20261028_0010_drop_manifest_v1"
OLD_ONLY = uuid.UUID(int=1)
MIXED = uuid.UUID(int=2)
NEW_ONLY = uuid.UUID(int=3)


def _module(path):  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(connection, *, target: bool, down: bool = False) -> None:
    """target=False：跑到这一版之前；target=True：只跑这一版（down 跑 downgrade）。"""
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
    schema = "knowbot_v1_drop_" + uuid.uuid4().hex
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


def v1(version: str, name: str = "旧专家") -> dict:
    doc = {k: v for k, v in manifest(version, name=name).items() if k != "bindings"}
    return {**doc, "schema": "knowbot-manifest/v1", "data_bindings": []}


async def _bot(c, bot: uuid.UUID, ref: str, current: str, versions: list) -> None:  # type: ignore[no-untyped-def]
    await c.execute(
        text(
            "INSERT INTO knowbot (id, owner_subject, source_ref, name, current_version)"
            " VALUES (:id, :owner, :ref, :name, :current)"
        ),
        {"id": bot, "owner": OWNER, "ref": ref, "name": "x", "current": current},
    )
    for version, doc in versions:
        await c.execute(
            text(
                "INSERT INTO knowbot_version"
                " (knowbot_id, version, digest, manifest, delivery_id)"
                " VALUES (:id, :v, 'sha256:x', CAST(:m AS jsonb), :d)"
            ),
            {"id": bot, "v": version, "m": json.dumps(doc), "d": f"{ref}-{version}"},
        )


@pytest.mark.asyncio
async def test_v1_versions_and_v1_only_experts_are_dropped(engine) -> None:
    async with engine.begin() as c:
        await _bot(c, OLD_ONLY, "lab-old", "2", [("1", v1("1")), ("2", v1("2"))])
        # 混着的：第 1、10 版是 v1，第 2、3 版是 v2；当前是第 10 版（按数值比）
        await _bot(
            c,
            MIXED,
            "lab-mixed",
            "10",
            [
                ("1", v1("1")),
                ("2", manifest("2", name="二")),
                ("3", manifest("3", name="三", tagline="第三版")),
                ("10", v1("10")),
            ],
        )
        await _bot(c, NEW_ONLY, "lab-new", "1", [("1", manifest("1", name="新"))])
        for ref, version in (("lab-old", "3"), ("lab-mixed", "4"), ("lab-none", "1")):
            await c.execute(
                text(
                    "INSERT INTO knowbot_unlisted_version"
                    " (source_ref, version, owner_subject, outcome)"
                    " VALUES (:ref, :v, :owner, 'declined')"
                ),
                {"ref": ref, "v": version, "owner": OWNER},
            )
        await c.execute(
            text(
                "INSERT INTO trade (id, kind, buyer_subject, seller_subject,"
                " listing_ref, title, status)"
                " VALUES (:id, 'expert', 'b', :owner, :ref, '请专家', 'done')"
            ),
            {"id": uuid.uuid4(), "owner": OWNER, "ref": f"{OLD_ONLY}@1"},
        )
        await c.run_sync(lambda conn: _run(conn, target=True))

    async with engine.connect() as c:
        bots = {
            r.id: (r.current_version, r.name, r.tagline)
            for r in await c.execute(
                text("SELECT id, current_version, name, tagline FROM knowbot")
            )
        }
        assert bots == {MIXED: ("3", "三", "第三版"), NEW_ONLY: ("1", "x", "")}
        versions = sorted(
            (str(r.knowbot_id), r.version)
            for r in await c.execute(
                text("SELECT knowbot_id, version FROM knowbot_version")
            )
        )
        assert versions == [(str(MIXED), "2"), (str(MIXED), "3"), (str(NEW_ONLY), "1")]
        unlisted = sorted(
            r.source_ref
            for r in await c.execute(
                text("SELECT source_ref FROM knowbot_unlisted_version")
            )
        )
        # 只删一版不剩的专家的；还有剩的、本来就没交付过的（v2 首版没上架）都留着
        assert unlisted == ["lab-mixed", "lab-none"]
        # 订单历史不动
        assert (await c.execute(text("SELECT count(*) FROM trade"))).scalar() == 1

    # 「不许删」的触发器开回来了
    with pytest.raises(DBAPIError, match="never deleted"):
        async with engine.begin() as c:
            await c.execute(text("DELETE FROM knowbot_version"))

    async with engine.begin() as c:  # downgrade 不恢复删掉的行，只是能跑
        await c.run_sync(lambda conn: _run(conn, target=True, down=True))
