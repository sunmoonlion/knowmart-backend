"""Exact version reads, delivery replay and public scope in PostgreSQL."""

import pytest

from app.application.errors.exceptions import ConcurrencyConflictError
from app.application.services.expert_catalog import ExpertCatalog
from app.application.services.knowbot_service import KnowbotService
from app.infrastructure.repositories.knowbots import SqlKnowbotRepository
from tests.test_knowbot_repository_db import OWNER, AllShareable, deliver, manifest
from tests.test_knowbot_repository_db import db as database  # noqa: F401


@pytest.fixture
def db(database):  # noqa: F811
    return database


@pytest.mark.asyncio
async def test_owner_reading_exact_version_gets_selected_version_title(db):
    first = await deliver(
        db, "version-001", manifest("1", name="季报核对", tagline="交付单季核对表")
    )
    await deliver(
        db, "version-002", manifest("2", name="年度汇总", tagline="交付全年汇总表")
    )
    async with db() as session:
        repo = SqlKnowbotRepository(session)
        detail = await ExpertCatalog(repo).detail(OWNER, str(first.knowbot_id), "1")
        selected = await KnowbotService(repo).summon(
            owner_subject=OWNER,
            knowbot_id=str(first.knowbot_id),
            version="1",
            runtime="knowbench",
        )
        assert detail["version"] == selected.version.version == "1"
        assert detail["name"] == selected.name == "季报核对"
        assert detail["tagline"] == selected.tagline == "交付单季核对表"
        assert selected.version.manifest["name"] == "季报核对"


@pytest.mark.asyncio
async def test_delivery_replay_after_next_category(db):
    async def submit(version, category):
        async with db() as session:
            return await KnowbotService(
                SqlKnowbotRepository(session), bindings=AllShareable()
            ).deliver(
                delivery_id=f"category-{version}",
                owner_subject=OWNER,
                knowbot_ref="knowlab-reviewed",
                manifest=manifest(version),
                category=category,
            )

    await submit("1", "tools.office")
    assert (await submit("1", "tools.office")).replayed
    await submit("2", "tools.calculator")
    assert (await submit("1", "tools.office")).replayed
    with pytest.raises(ConcurrencyConflictError) as failure:
        await submit("1", "tools.calculator")
    assert failure.value.code == "delivery_id_reused"


@pytest.mark.asyncio
async def test_binding_observation_after_owner_withdraws_public_scope(db):
    first = await deliver(db, "scope-001", manifest(), "knowlab-reviewed", public=True)

    class BindingUpdate:
        async def binding_states(self, items):
            async with db() as writer:
                await KnowbotService(SqlKnowbotRepository(writer)).withdraw_public(
                    owner_subject=OWNER, knowbot_id=str(first.knowbot_id), version="1"
                )
            return [{"state": "live", "shareable": False} for _ in items]

    async with db() as reader:
        observed = await KnowbotService(
            SqlKnowbotRepository(reader), bindings=BindingUpdate()
        ).check_bindings()
        assert observed["held"] == 0
    async with db() as reader:
        svc = KnowbotService(SqlKnowbotRepository(reader), bindings=AllShareable())
        assert (await svc.check_bindings())["checked"] == 0
        [version] = await svc.source_versions(OWNER, "knowlab-reviewed")
        assert not version["public_allowed"]
        assert (version["status"], version["held_reason"]) == ("active", None)
