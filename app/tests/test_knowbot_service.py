from __future__ import annotations

import pytest

from app.application.errors.exceptions import (
    BadRequestError,
    ConcurrencyConflictError,
    NotFoundError,
    ServiceUnavailableError,
)
from app.application.services.knowbot_service import KnowbotService
from tests.knowbot_fakes import AllShareable, MemoryKnowbots, manifest

OWNER = "8b1f5c1e-0000-4000-8000-000000000001"
OTHER = "8b1f5c1e-0000-4000-8000-000000000002"


def service() -> tuple[KnowbotService, MemoryKnowbots]:
    repo = MemoryKnowbots()
    return KnowbotService(repo, bindings=AllShareable()), repo


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
    [bot] = await svc.repo.list_for_owner(OWNER)
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
    found = await svc.summon(
        owner_subject=OWNER, knowbot_id=bot_id, version="1", runtime="knowbench"
    )
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
            await svc.summon(
                owner_subject=owner,
                knowbot_id=knowbot,
                version=version,
                runtime="knowbench",
            )


@pytest.mark.asyncio
async def test_stopped_version_cannot_be_summoned_and_others_cannot_stop() -> None:
    svc, repo = service()
    result = await deliver(svc, "delivery-0001", manifest())
    bot_id = str(result.knowbot_id)
    with pytest.raises(NotFoundError):
        await svc.stop(owner_subject=OTHER, knowbot_id=bot_id, version="1")
    await svc.stop(owner_subject=OWNER, knowbot_id=bot_id, version="1")
    with pytest.raises(NotFoundError):
        await svc.summon(
            owner_subject=OWNER, knowbot_id=bot_id, version="1", runtime="knowbench"
        )
    [bot] = await svc.repo.list_for_owner(OWNER)
    assert bot.versions[0].status == "stopped"


@pytest.mark.asyncio
async def test_knowlab_experts_are_stopped_only_by_knowlab() -> None:
    """SDD 0037 第六节：knowlab 交来的专家，一生记在 knowlab；这里点停用回 409。"""
    svc, _ = service()
    result = await svc.deliver(
        delivery_id="knowlab-expert-1",
        owner_subject=OWNER,
        knowbot_ref="knowlab-r1",
        manifest=manifest(),
    )
    bot_id = str(result.knowbot_id)
    with pytest.raises(ConcurrencyConflictError) as refused:
        await svc.stop(owner_subject=OWNER, knowbot_id=bot_id, version="1")
    assert refused.value.code == "stop_in_knowlab"
    await svc.stop(owner_subject=OWNER, knowbot_id=bot_id, version="1", by_knowlab=True)
    [bot] = await svc.repo.list_for_owner(OWNER)
    assert bot.versions[0].status == "stopped"


@pytest.mark.asyncio
async def test_knowlab_cannot_stop_or_mark_what_it_did_not_deliver() -> None:
    """knowlab 的服务关系只管 knowlab 交来的：别的分身当没有（Kimi 评审第 1 条）。"""
    svc, _ = service()
    result = await deliver(svc, "delivery-0001", manifest())  # 不是 knowlab 交来的
    with pytest.raises(NotFoundError):
        await svc.stop(
            owner_subject=OWNER,
            knowbot_id=str(result.knowbot_id),
            version="1",
            by_knowlab=True,
        )
    with pytest.raises(BadRequestError):
        await svc.set_unlisted(
            owner_subject=OWNER, knowbot_ref="other-r1", version="1", outcome="declined"
        )


@pytest.mark.asyncio
async def test_unlisted_outcomes_are_written_per_version_and_checked() -> None:
    """按版本逐条写：各版本互不覆盖，先后乱序、重发都收敛（Astra 评审 F08）。"""
    svc, repo = service()
    for version, outcome in (("2", "rejected"), ("1", "declined"), ("2", "rejected")):
        await svc.set_unlisted(
            owner_subject=OWNER,
            knowbot_ref="knowlab-r1",
            version=version,
            outcome=outcome,
        )
    assert await repo.unlisted_of("knowlab-r1", OWNER) == {
        "1": "declined",
        "2": "rejected",
    }
    for version, outcome in (("0", "declined"), ("1", "stopped")):
        with pytest.raises(BadRequestError):
            await svc.set_unlisted(
                owner_subject=OWNER,
                knowbot_ref="knowlab-r1",
                version=version,
                outcome=outcome,
            )
    await svc.deliver(
        delivery_id="knowlab-expert-3",
        owner_subject=OWNER,
        knowbot_ref="knowlab-r1",
        manifest=manifest("3"),
    )
    with pytest.raises(ConcurrencyConflictError):  # 已交付过：只认那个主人
        await svc.set_unlisted(
            owner_subject=OTHER,
            knowbot_ref="knowlab-r1",
            version="1",
            outcome="declined",
        )


@pytest.mark.asyncio
async def test_summon_only_serves_the_callers_own_runtime() -> None:
    svc, repo = service()
    result = await deliver(svc, "delivery-0001", manifest())
    with pytest.raises(NotFoundError):
        await svc.summon(
            owner_subject=OWNER,
            knowbot_id=str(result.knowbot_id),
            version="1",
            runtime="another-runtime",
        )


# —— 收清单时硬查所绑数据、自用转公开、类目、在售转待处理（0043 第 4.3 节）——
BOUND = (OWNER, "m-1", 2)


async def _deliver(svc: KnowbotService, delivery: str, doc: dict, **kw):
    return await svc.deliver(
        delivery_id=delivery,
        owner_subject=OWNER,
        knowbot_ref="lab-bot-1",
        manifest=doc,
        **kw,
    )


@pytest.mark.asyncio
async def test_bindings_are_checked_hard_when_the_manifest_arrives() -> None:
    checker = AllShareable()
    repo = MemoryKnowbots()
    svc = KnowbotService(repo, bindings=checker)
    # 只许本人用的：交到自用可以，公开不行
    checker.states[BOUND] = {"state": "live", "license": "self", "shareable": False}
    with pytest.raises(ConcurrencyConflictError) as error:
        await _deliver(svc, "delivery-0001", manifest(), request_public=True)
    assert error.value.code == "binding_not_shareable"
    assert repo.bots == {}
    await _deliver(svc, "delivery-0001", manifest())
    # 删了的连自用也不收
    checker.states[BOUND] = {"state": "deleted", "license": "self", "shareable": False}
    with pytest.raises(ConcurrencyConflictError) as error:
        await _deliver(svc, "delivery-0002", manifest("2"))
    assert error.value.code == "binding_deleted"
    # knowbase 连不上、没配：带绑定的不收；不带绑定的照收
    checker.down = True
    with pytest.raises(ServiceUnavailableError):
        await _deliver(svc, "delivery-0002", manifest("2"))
    unconfigured = KnowbotService(MemoryKnowbots())
    with pytest.raises(ServiceUnavailableError) as error:
        await _deliver(unconfigured, "delivery-0003", manifest())
    assert error.value.code == "knowbase_unconfigured"
    await _deliver(unconfigured, "delivery-0003", manifest(bindings=[]))


@pytest.mark.asyncio
async def test_self_use_turns_public_after_a_recheck_and_category_is_kept() -> None:
    checker = AllShareable()
    repo = MemoryKnowbots()
    svc = KnowbotService(repo, bindings=checker)
    first = await _deliver(svc, "knowlab-expert-1", manifest())
    with pytest.raises(BadRequestError) as error:
        await _deliver(svc, "knowlab-expert-x", manifest(), category="nope")
    assert error.value.code == "category_invalid"
    checker.states[BOUND] = {"state": "live", "license": "self", "shareable": False}
    with pytest.raises(ConcurrencyConflictError) as error:
        await _deliver(svc, "knowlab-delivery-2", manifest(), request_public=True)
    assert error.value.code == "binding_not_shareable"
    checker.states.clear()
    turned = await _deliver(
        svc,
        "knowlab-delivery-2",
        manifest(),
        request_public=True,
        category="tools.lookup",
    )
    assert turned.knowbot_id == first.knowbot_id and not turned.replayed
    [bot] = await repo.list_for_owner(OWNER)
    assert bot.versions[0].public_allowed and bot.category == "tools.lookup"
    # knowlab 超时重试同一次转公开：当重放，不撞「版本已交过」
    again = await _deliver(svc, "knowlab-delivery-2", manifest(), request_public=True)
    assert again.replayed and again.knowbot_id == first.knowbot_id
    retried = await _deliver(svc, "knowlab-delivery-9", manifest(), request_public=True)
    assert retried.replayed
    # 硬查带着冻结依据问 knowbase（签授权看的是冻结时的许可）
    assert checker.asked[-1] == (OWNER, "m-1", 2, "expert:fa-1@2")
    # 内容变了的照旧不许改
    with pytest.raises(ConcurrencyConflictError) as error:
        await _deliver(
            svc, "knowlab-delivery-3", manifest(tagline="变了"), request_public=True
        )
    assert error.value.code == "knowbot_version_exists"


@pytest.mark.asyncio
async def test_sold_versions_go_on_hold_when_their_data_fails_and_come_back() -> None:
    checker = AllShareable()
    repo = MemoryKnowbots()
    svc = KnowbotService(repo, bindings=checker, public_market=True)
    result = await _deliver(svc, "delivery-0001", manifest(), request_public=True)
    await _deliver(svc, "delivery-0002", manifest("2"))  # 自用的不核
    bot_id = str(result.knowbot_id)
    assert (await svc.check_bindings())["held"] == 0
    checker.states[BOUND] = {"state": "live", "license": "self", "shareable": False}
    done = await svc.check_bindings()
    assert (done["checked"], done["held"]) == (1, 1)
    held = repo.versions[(result.knowbot_id, "1")]
    assert (held.status, held.held_reason) == ("held", "binding_not_shareable")
    assert (await svc.check_bindings())["held"] == 0  # 重复跑不多做
    # 主人照样能请；别人请不了（就算公开）
    await svc.summon(
        owner_subject=OWNER, knowbot_id=bot_id, version="1", runtime="knowbench"
    )
    with pytest.raises(NotFoundError):
        await svc.summon(
            owner_subject=OTHER, knowbot_id=bot_id, version="1", runtime="knowbench"
        )
    checker.down = True
    assert (await svc.check_bindings())["skipped"] == "knowbase_unavailable"
    checker.down = False
    checker.states.clear()
    assert (await svc.check_bindings())["released"] == 1
    assert repo.versions[(result.knowbot_id, "1")].status == "active"
    # 停用了的不再核
    await repo.set_version_status(result.knowbot_id, "1", "stopped")
    checker.states[BOUND] = {"state": "gone", "license": None, "shareable": False}
    assert (await svc.check_bindings())["checked"] == 0


@pytest.mark.asyncio
async def test_a_replay_must_match_public_and_category_too() -> None:
    """同一交付单号重放：公开与否、类目也要一样，不一样 409（第二轮审查第 3 条）。"""
    svc = KnowbotService(MemoryKnowbots(), bindings=AllShareable())
    await _deliver(svc, "knowlab-delivery-1", manifest(), category="tools.lookup")
    again = await _deliver(
        svc, "knowlab-delivery-1", manifest(), category="tools.lookup"
    )
    assert again.replayed
    for kw in ({"request_public": True}, {"category": "tools.office"}):
        with pytest.raises(ConcurrencyConflictError) as error:
            await _deliver(svc, "knowlab-delivery-1", manifest(), **kw)
        assert error.value.code == "delivery_id_reused"


@pytest.mark.asyncio
async def test_withdraw_public_clears_public_and_releases_public_only_hold() -> None:
    """收回公开（第 8 条）：只撤掉该版的 public_allowed、保留自用上架；若此前仅因公开
    许可不够而挂起则恢复 active。"""
    checker = AllShareable()
    repo = MemoryKnowbots()
    svc = KnowbotService(repo, bindings=checker, public_market=True)
    result = await svc.deliver(
        delivery_id="knowlab-delivery-1",
        owner_subject=OWNER,
        knowbot_ref="knowlab-r1",
        manifest=manifest(),
        request_public=True,
    )
    bot_id = str(result.knowbot_id)
    checker.states[BOUND] = {"state": "live", "license": "self", "shareable": False}
    await svc.check_bindings()
    assert repo.versions[(result.knowbot_id, "1")].status == "held"
    await svc.withdraw_public(owner_subject=OWNER, knowbot_id=bot_id, version="1")
    v = repo.versions[(result.knowbot_id, "1")]
    assert not v.public_allowed and v.status == "active" and v.held_reason is None
    # 主人自用召唤照常可用
    await svc.summon(
        owner_subject=OWNER, knowbot_id=bot_id, version="1", runtime="knowbench"
    )
