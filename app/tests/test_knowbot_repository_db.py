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


@pytest.mark.asyncio
async def test_catalog_visibility_in_sql(db) -> None:
    """专家目录（SDD 0032）：自己的都看得到；别人的公开专家要开关与该类目有效资质。"""
    from datetime import date

    mine = await deliver(db, "delivery-0101", manifest(), ref="lab-mine")
    other = "9c2f0000-0000-4000-8000-000000000002"
    async with db() as session:
        theirs = await KnowbotService(SqlKnowbotRepository(session)).deliver(
            delivery_id="delivery-0102",
            owner_subject=other,
            knowbot_ref="lab-theirs",
            manifest=manifest(name="别人的公开专家"),
        )
        # 公开必须有发布主体（数据库约束兜底）
        with pytest.raises(DBAPIError):
            async with session.begin_nested():
                await session.execute(
                    text("update knowbot set visibility = 'public' where id = :i"),
                    {"i": theirs.knowbot_id},
                )
        publisher = uuid.uuid4()
        await session.execute(
            text(
                "insert into publisher (id, kind, name, real_name_verified,"
                " review_status) values (:i, 'organization', '某某证券', true,"
                " 'approved')"
            ),
            {"i": publisher},
        )
        # 未分类的不能公开（审读第 6 条，数据库约束兜底）
        with pytest.raises(DBAPIError):
            async with session.begin_nested():
                await session.execute(
                    text(
                        "update knowbot set visibility = 'public', publisher_id = :p "
                        "where id = :i"
                    ),
                    {"p": publisher, "i": theirs.knowbot_id},
                )
        await session.execute(
            text(
                "update knowbot set visibility = 'public', publisher_id = :p, "
                "category = 'finance.securities_advisory' where id = :i"
            ),
            {"p": publisher, "i": theirs.knowbot_id},
        )
        await session.execute(
            text(
                "insert into publisher_license (id, publisher_id, category, "
                "license_name, license_no, valid_until, review_status) values "
                "(:i, :p, 'finance.securities_advisory', '经营证券期货业务许可证', "
                "'000000001', :until, 'approved')"
            ),
            {"i": uuid.uuid4(), "p": publisher, "until": date(2026, 12, 31)},
        )
        await session.commit()
    async with db() as session:
        repo = SqlKnowbotRepository(session)
        closed = await repo.list_visible(
            OWNER, public_market=False, today=date(2026, 10, 18)
        )
        assert [b.id for b in closed] == [mine.knowbot_id]
        assert closed[0].visibility == "private"
        assert closed[0].category == "uncategorized"  # 没选类目
        opened = await repo.list_visible(
            OWNER, public_market=True, today=date(2026, 10, 18)
        )
        assert {b.id for b in opened} == {mine.knowbot_id, theirs.knowbot_id}
        expired = await repo.list_visible(
            OWNER, public_market=True, today=date(2027, 1, 1)
        )
        assert [b.id for b in expired] == [mine.knowbot_id]
        found = await repo.licensed_publisher(
            publisher, "finance.securities_advisory", today=date(2026, 10, 18)
        )
        assert found is not None and found.name == "某某证券"
        assert found.licenses[0]["valid_until"] == "2026-12-31"
        assert (
            await repo.licensed_publisher(
                publisher, "finance.public_fund", today=date(2026, 10, 18)
            )
            is None
        )
        # 有证照也要主体审过：改了资料回到待审，就不公开、请不了（Astra 评审 F06）
        await session.execute(
            text("update publisher set review_status = 'pending' where id = :i"),
            {"i": publisher},
        )
        assert (
            await repo.licensed_publisher(
                publisher, "finance.securities_advisory", today=date(2026, 10, 18)
            )
            is None
        )
        hidden = await repo.list_visible(
            OWNER, public_market=True, today=date(2026, 10, 18)
        )
        assert [b.id for b in hidden] == [mine.knowbot_id]


def _step(name: str, direction: str):
    """只跑一条迁移的 upgrade 或 downgrade。"""

    def run(connection) -> None:
        path = ROOT / "alembic/versions" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(path.stem, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with Operations.context(MigrationContext.configure(connection)):
            getattr(module, direction)()

    return run


@pytest.mark.asyncio
async def test_experts_from_before_0006_become_uncategorized(db) -> None:
    """0005 给已有的专家都写了投资咨询类，没人选过：0006 改成未分类（审读第 6 条）。"""
    bot = await deliver(db, "delivery-0201", manifest(), ref="lab-old")
    name = "20261018_0006_uncategorized"
    async with db() as session, session.begin():
        connection = await session.connection()
        await connection.run_sync(_step(name, "downgrade"))
        row = await session.execute(
            text("select category from knowbot where id = :i"), {"i": bot.knowbot_id}
        )
        assert row.scalar_one() == "finance.securities_advisory"
        await connection.run_sync(_step(name, "upgrade"))
    async with db() as session:
        row = await session.execute(
            text("select category from knowbot where id = :i"), {"i": bot.knowbot_id}
        )
        assert row.scalar_one() == "uncategorized"


@pytest.mark.asyncio
async def test_unlisted_outcomes_on_the_real_database(db) -> None:
    """SDD 0037 第三节：没上架那几版的结局整份替换、只认主人，结局只有两种（库挡）。"""
    async with db() as session:
        svc = KnowbotService(SqlKnowbotRepository(session))
        await svc.set_unlisted(
            owner_subject=OWNER,
            knowbot_ref="knowlab-r1",
            versions=[{"version": "1", "outcome": "declined"}],
        )
        await svc.set_unlisted(
            owner_subject=OWNER,
            knowbot_ref="knowlab-r1",
            versions=[
                {"version": "1", "outcome": "rejected"},
                {"version": "2", "outcome": "declined"},
            ],
        )
    async with db() as session:
        repo = SqlKnowbotRepository(session)
        assert await repo.unlisted_of("knowlab-r1", OWNER) == {
            "1": "rejected",
            "2": "declined",
        }
        assert await repo.unlisted_of("knowlab-r1", "someone-else") == {}
    with pytest.raises(DBAPIError):
        async with db() as session, session.begin():
            await session.execute(
                text("UPDATE knowbot_unlisted_version SET outcome = 'stopped'")
            )
