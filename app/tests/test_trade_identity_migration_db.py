"""在一次性 schema 中用 0013 允许的成交记录检查 0014 的升级行为。"""

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from tests.test_drop_manifest_v1_db import ROOT, _module
from tests.test_drop_manifest_v1_db import engine as engine


def migrate(connection, first, last):
    paths = sorted((ROOT / "alembic/versions").glob("20*.py"))
    with Operations.context(MigrationContext.configure(connection)):
        for path in paths:
            if first <= path.stem[:13] <= last:
                _module(path).upgrade()


async def test_upgrade_retains_and_marks_duplicate_trades(engine):
    async with engine.begin() as conn:
        await conn.run_sync(migrate, "20261028_0010", "20261030_0013")
        for _ in range(2):
            await conn.execute(
                text("""
                INSERT INTO trade(id,kind,buyer_subject,seller_subject,
                                  listing_ref,title)
                VALUES (gen_random_uuid(),'expert','buyer','author','expert@1','demo')
            """)
            )
    async with engine.begin() as conn:
        await conn.run_sync(migrate, "20261101_0014", "20261101_0014")
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM trade")) == 2
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM trade WHERE duplicate_of IS NULL")
            )
            == 1
        )
