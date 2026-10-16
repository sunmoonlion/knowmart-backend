from __future__ import annotations

import pytest

from app.application.errors.exceptions import (
    BadRequestError,
    ConcurrencyConflictError,
    NotFoundError,
)
from app.application.services.knowbot_service import KnowbotService
from tests.knowbot_fakes import MemoryKnowbots, manifest

OWNER = "8b1f5c1e-0000-4000-8000-000000000001"
OTHER = "8b1f5c1e-0000-4000-8000-000000000002"


def service() -> tuple[KnowbotService, MemoryKnowbots]:
    repo = MemoryKnowbots()
    return KnowbotService(repo), repo


async def deliver(svc: KnowbotService, delivery: str, doc: dict, owner: str = OWNER):
    return await svc.deliver(
        delivery_id=delivery, owner_subject=owner, knowbot_ref="lab-bot-1", manifest=doc
    )


@pytest.mark.asyncio
async def test_first_delivery_creates_and_a_replay_returns_the_same() -> None:
    svc, repo = service()
    first = await deliver(svc, "delivery-0001", manifest())
    assert not first.replayed and first.version == "1"
    again = await deliver(svc, "delivery-0001", manifest())
    assert again.replayed and again.knowbot_id == first.knowbot_id
    assert again.digest == first.digest
    assert len(repo.versions) == 1


@pytest.mark.asyncio
async def test_reused_delivery_id_with_other_content_conflicts() -> None:
    svc, repo = service()
    await deliver(svc, "delivery-0001", manifest())
    with pytest.raises(ConcurrencyConflictError) as error:
        await deliver(svc, "delivery-0001", manifest(tagline="变了"))
    assert error.value.code == "delivery_id_reused"


@pytest.mark.asyncio
async def test_a_delivered_version_never_changes() -> None:
    svc, repo = service()
    await deliver(svc, "delivery-0001", manifest())
    for doc in (manifest(), manifest(tagline="变了")):
        with pytest.raises(ConcurrencyConflictError) as error:
            await deliver(svc, "delivery-0002", doc)
        assert error.value.code == "knowbot_version_exists"
    assert repo.rollbacks == 2 and len(repo.versions) == 1


@pytest.mark.asyncio
async def test_another_owner_cannot_take_over_a_knowbot() -> None:
    svc, repo = service()
    await deliver(svc, "delivery-0001", manifest())
    with pytest.raises(ConcurrencyConflictError) as error:
        await deliver(svc, "delivery-0002", manifest("2"), owner=OTHER)
    assert error.value.code == "knowbot_owner_mismatch"


@pytest.mark.asyncio
async def test_current_version_only_moves_forward() -> None:
    svc, repo = service()
    await deliver(svc, "delivery-0002", manifest("2", name="第二版"))
    await deliver(svc, "delivery-0001", manifest("1", name="第一版"))
    [bot] = await svc.list_for_owner(OWNER)
    assert bot.current_version == "2" and bot.name == "第二版"
    assert [v.version for v in bot.versions] == ["1", "2"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields",
    [
        {"delivery_id": "short"},
        {"owner_subject": "has space"},
        {"knowbot_ref": "bad/ref"},
        {"manifest": {"schema": "nope"}},
    ],
)
async def test_bad_deliveries_are_rejected_before_any_write(fields: dict) -> None:
    svc, repo = service()
    args = {
        "delivery_id": "delivery-0001",
        "owner_subject": OWNER,
        "knowbot_ref": "lab-bot-1",
        "manifest": manifest(),
    } | fields
    with pytest.raises(BadRequestError):
        await svc.deliver(**args)
    assert repo.bots == {} and repo.commits == 0


@pytest.mark.asyncio
async def test_summon_returns_only_the_owners_active_version() -> None:
    svc, repo = service()
    result = await deliver(svc, "delivery-0001", manifest())
    bot_id = str(result.knowbot_id)
    found = await svc.summon(owner_subject=OWNER, knowbot_id=bot_id, version="1")
    assert found.version.digest == result.digest
    assert found.version.manifest["pack"]["pack_id"] == "fin-forecast"
    for owner, knowbot, version in [
        (OTHER, bot_id, "1"),
        (OWNER, bot_id, "2"),
        (OWNER, "not-a-uuid", "1"),
        (OWNER, "00000000-0000-4000-8000-000000000000", "1"),
        (OWNER, bot_id, "1; drop"),
        ("", bot_id, "1"),
    ]:
        with pytest.raises(NotFoundError):
            await svc.summon(owner_subject=owner, knowbot_id=knowbot, version=version)


@pytest.mark.asyncio
async def test_stopped_version_cannot_be_summoned_and_others_cannot_stop() -> None:
    svc, repo = service()
    result = await deliver(svc, "delivery-0001", manifest())
    bot_id = str(result.knowbot_id)
    with pytest.raises(NotFoundError):
        await svc.stop(owner_subject=OTHER, knowbot_id=bot_id, version="1")
    await svc.stop(owner_subject=OWNER, knowbot_id=bot_id, version="1")
    with pytest.raises(NotFoundError):
        await svc.summon(owner_subject=OWNER, knowbot_id=bot_id, version="1")
    [bot] = await svc.list_for_owner(OWNER)
    assert bot.versions[0].status == "stopped"


@pytest.mark.asyncio
async def test_list_shows_only_the_owners_knowbots() -> None:
    svc, repo = service()
    await deliver(svc, "delivery-0001", manifest())
    await svc.deliver(
        delivery_id="delivery-0002",
        owner_subject=OTHER,
        knowbot_ref="lab-bot-2",
        manifest=manifest(),
    )
    assert [b.source_ref for b in await svc.list_for_owner(OWNER)] == ["lab-bot-1"]
    assert await svc.list_for_owner("bad owner") == []
