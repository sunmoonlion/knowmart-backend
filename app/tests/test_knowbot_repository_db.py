"""分身存放在真 PostgreSQL 上：真迁移、真仓储、触发器、并发交付（一次性 schema）。"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from app.application.errors.exceptions import ConcurrencyConflictError, NotFoundError
from app.application.services.knowbot_service import KnowbotService
from app.infrastructure.repositories.knowbots import SqlKnowbotRepository
from app.infrastructure.storage.postgres import make_session_factory
from tests.knowbot_fakes import manifest

ROOT = Path(__file__).resolve().parents[1]
OWNER = "8b1f5c1e-0000-4000-8000-000000000001"


def migrate(connection):
    with Operations.context(MigrationContext.configure(connection)):
        for path in sorted((ROOT / "alembic/versions").glob("20*.py")):
            spec = importlib.util.spec_from_file_location(path.stem, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.upgrade()


@pytest_asyncio.fixture
async def db():
    url = os.environ.get("DELIVERY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set DELIVERY_TEST_DATABASE_URL to a disposable *_tests database")
    parsed = make_url(url)
    assert parsed.database and parsed.database.endswith("_tests")
    schema = "knowbot_test_" + uuid.uuid4().hex
    engine = create_async_engine(
        parsed.set(drivername="postgresql+asyncpg"),
        connect_args={"server_settings": {"search_path": schema + ",public"}},
    )
    try:
        async with engine.begin() as c:
            await c.execute(text(f'CREATE SCHEMA "{schema}"'))
            await c.execute(
                text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA public')
            )
            await c.run_sync(migrate)
        yield make_session_factory(engine)
    finally:
        async with engine.begin() as c:
            await c.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


async def deliver(db, delivery: str, doc: dict, ref: str = "lab-bot-1"):
    async with db() as session:
        return await KnowbotService(SqlKnowbotRepository(session)).deliver(
            delivery_id=delivery, owner_subject=OWNER, knowbot_ref=ref, manifest=doc
        )


@pytest.mark.asyncio
async def test_delivery_summon_list_and_stop_round_trip(db) -> None:
    first = await deliver(db, "delivery-0001", manifest())
    assert not first.replayed
    assert (await deliver(db, "delivery-0001", manifest())).replayed
    await deliver(db, "delivery-0002", manifest("2", name="第二版"))
    with pytest.raises(ConcurrencyConflictError):
        await deliver(db, "delivery-0003", manifest("2", tagline="改"))
    async with db() as session:
        svc = KnowbotService(SqlKnowbotRepository(session))
        found = await svc.summon(
            owner_subject=OWNER,
            knowbot_id=str(first.knowbot_id),
            version="1",
            runtime="knowbench",
        )
        assert found.version.digest == first.digest
        assert found.version.manifest == manifest()  # 规范化后与交付的一致
        [bot] = await svc.list_for_owner(OWNER)
        assert bot.current_version == "2" and bot.name == "第二版"
        assert [v.version for v in bot.versions] == ["1", "2"]
        await svc.stop(owner_subject=OWNER, knowbot_id=str(bot.id), version="1")
    async with db() as session:
        svc = KnowbotService(SqlKnowbotRepository(session))
        with pytest.raises(NotFoundError):
            await svc.summon(
                owner_subject=OWNER,
                knowbot_id=str(first.knowbot_id),
                version="1",
                runtime="knowbench",
            )


@pytest.mark.asyncio
async def test_database_refuses_to_change_or_delete_a_delivered_version(db) -> None:
    await deliver(db, "delivery-0001", manifest())
    for statement in (
        "UPDATE knowbot_version SET digest = 'sha256:forged'",
        "UPDATE knowbot_version SET manifest = '{}'::jsonb",
        "DELETE FROM knowbot_version",
    ):
        async with db() as session:
            with pytest.raises(DBAPIError, match="never"):
                async with session.begin():
                    await session.execute(text(statement))
    async with db() as session, session.begin():
        await session.execute(text("UPDATE knowbot_version SET status = 'stopped'"))


@pytest.mark.asyncio
async def test_concurrent_deliveries_of_one_knowbot_never_duplicate_it(db) -> None:
    results = await asyncio.gather(
        deliver(db, "delivery-0001", manifest("1")),
        deliver(db, "delivery-0002", manifest("2")),
        return_exceptions=True,
    )
    assert not [r for r in results if isinstance(r, BaseException)]
    async with db() as session:
        count = (await session.execute(text("SELECT count(*) FROM knowbot"))).scalar()
        versions = (
            await session.execute(text("SELECT count(*) FROM knowbot_version"))
        ).scalar()
    assert count == 1 and versions == 2
