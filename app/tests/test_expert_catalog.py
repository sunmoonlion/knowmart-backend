"""专家目录（SDD 0032）：谁看得到、筛选、排序、分页、类目、详情。内存仓储。"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from app.application.errors.exceptions import NotFoundError
from app.application.services.expert_catalog import (
    PAGE_SIZE,
    CatalogQuery,
    ExpertCatalog,
)
from app.application.services.knowbot_service import KnowbotService
from app.domain.knowbots.catalog import expert_sections
from tests.knowbot_fakes import MemoryKnowbots, manifest

ME = "user-me"
OTHER = "user-other"
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


async def deliver(repo, owner: str, ref: str, version: str = "1", **kw) -> uuid.UUID:
    result = await KnowbotService(repo).deliver(
        delivery_id=f"delivery-{ref}-{version}",
        owner_subject=owner,
        knowbot_ref=ref,
        manifest=manifest(version, **kw),
    )
    return result.knowbot_id


def make_public(repo, bot_id, *, category="finance.securities_advisory", **license):
    publisher = uuid.uuid4()
    repo.publishers[publisher] = {
        "id": publisher,
        "kind": "organization",
        "name": "某某证券",
        "real_name_verified": True,
    }
    repo.bots[bot_id].update(
        visibility="public", publisher_id=publisher, category=category
    )
    repo.licenses.append(
        {
            "publisher_id": publisher,
            "category": category,
            "license_name": "经营证券期货业务许可证",
            "license_no": "000000001",
            "review_status": "approved",
            "valid_until": None,
            **license,
        }
    )
    return publisher


def catalog(repo, public_market=False) -> ExpertCatalog:
    return ExpertCatalog(repo, public_market=public_market, now=lambda: NOW)


def names(found) -> list[str]:
    return [item["name"] for item in found["items"]]


@pytest.mark.asyncio
async def test_each_user_sees_own_experts_and_never_others_private_ones() -> None:
    repo = MemoryKnowbots()
    await deliver(repo, ME, "mine", name="我的专家")
    await deliver(repo, OTHER, "theirs", name="别人的专家")
    found = await catalog(repo).list(ME, CatalogQuery())
    assert names(found) == ["我的专家"]
    assert found["items"][0]["owned"] is True
    assert (await catalog(repo).list("not a subject!", CatalogQuery()))["items"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("public_market", "license", "visible"),
    [
        (False, {}, False),  # 平台开关关着：别人的公开专家也看不到
        (True, {}, True),
        (True, {"review_status": "pending"}, False),
        (True, {"review_status": "rejected"}, False),
        (True, {"valid_until": date(2026, 10, 17)}, False),  # 过期
        (True, {"valid_until": date(2026, 10, 18)}, True),  # 当天还算有效
        (True, {"category": "finance.public_fund"}, False),  # 资质不是这个类目的
    ],
)
async def test_public_experts_need_the_switch_and_a_valid_license_for_the_category(
    public_market, license, visible
) -> None:
    repo = MemoryKnowbots()
    bot = await deliver(repo, OTHER, "public", name="公开专家")
    category = license.pop("category", "finance.securities_advisory")
    publisher = make_public(repo, bot)
    repo.licenses[-1].update(category=category, **license)
    svc = catalog(repo, public_market=public_market)
    assert (names(await svc.list(ME, CatalogQuery())) == ["公开专家"]) is visible
    if visible:
        detail = await svc.detail(ME, str(bot))
        assert detail["owned"] is False
        assert detail["publisher"]["name"] == "某某证券"
        assert detail["publisher"]["licenses"][0]["number"] == "000000001"
        # 别人的专家看不到方法的步骤
        assert detail["sections"]["steps"] is None
    else:
        with pytest.raises(NotFoundError):
            await svc.detail(ME, str(bot))
    assert publisher in repo.publishers


@pytest.mark.asyncio
async def test_filters_sort_and_pages() -> None:
    repo = MemoryKnowbots()
    old = await deliver(repo, ME, "old", name="甲 旧专家", tagline="看财报")
    await deliver(repo, ME, "new", name="乙 新专家", tagline="做预测")
    taking = await deliver(
        repo, ME, "taking", name="丙 要你的数据", tagline="读你给的表"
    )
    # 交付时间：旧的 40 天前，新的今天；「要你的数据」声明接受外来数据
    first = repo.versions[(old, "1")]
    repo.versions[(old, "1")] = replace(first, delivered_at=NOW - timedelta(days=40))
    taking_v = repo.versions[(taking, "1")]
    repo.versions[(taking, "1")] = replace(
        taking_v, manifest={**taking_v.manifest, "accepts_user_data": True}
    )
    svc = catalog(repo)
    assert names(await svc.list(ME, CatalogQuery(q="财报"))) == ["甲 旧专家"]
    assert names(await svc.list(ME, CatalogQuery(q="预测"))) == ["乙 新专家"]
    assert "甲 旧专家" not in names(await svc.list(ME, CatalogQuery(updated="30d")))
    assert names(await svc.list(ME, CatalogQuery(data="user"))) == ["丙 要你的数据"]
    assert "丙 要你的数据" not in names(await svc.list(ME, CatalogQuery(data="own")))
    assert names(await svc.list(ME, CatalogQuery(sort="name"))) == [
        "丙 要你的数据",
        "乙 新专家",
        "甲 旧专家",
    ]
    assert names(await svc.list(ME, CatalogQuery()))[-1] == "甲 旧专家"


@pytest.mark.asyncio
async def test_more_than_a_page_splits_into_pages() -> None:
    repo = MemoryKnowbots()
    for i in range(PAGE_SIZE + 1):
        await deliver(repo, ME, f"bot-{i}", name=f"专家 {i:02d}")
    svc = catalog(repo)
    first = await svc.list(ME, CatalogQuery(sort="name"))
    second = await svc.list(ME, CatalogQuery(sort="name", page=2))
    assert first["total"] == PAGE_SIZE + 1 and len(first["items"]) == PAGE_SIZE
    assert names(second) == [f"专家 {PAGE_SIZE:02d}"]


@pytest.mark.asyncio
async def test_categories_count_only_what_the_user_sees_and_skip_empty_ones() -> None:
    repo = MemoryKnowbots()
    a = await deliver(repo, ME, "a", name="甲")
    b = await deliver(repo, ME, "b", name="乙")
    await deliver(repo, OTHER, "c", name="别人的")
    repo.bots[a]["category"] = "finance.securities_advisory"
    repo.bots[b]["category"] = "tools.calculator"
    svc = catalog(repo)
    tree = await svc.categories(ME)
    assert tree == [
        {
            "group": "finance",
            "group_name": "金融",
            "count": 1,
            "children": [
                {
                    "code": "finance.securities_advisory",
                    "name": "证券 / 期货投资咨询",
                    "count": 1,
                }
            ],
        },
        {
            "group": "tools",
            "group_name": "工具",
            "count": 1,
            "children": [{"code": "tools.calculator", "name": "计算器", "count": 1}],
        },
    ]
    # 选了类目：列表只剩这一类，类目数量不受类目筛选影响
    found = await svc.list(ME, CatalogQuery(category="tools"))
    assert names(found) == ["乙"]
    assert [g["count"] for g in found["categories"]] == [1, 1]
    assert names(
        await svc.list(ME, CatalogQuery(category="finance.securities_advisory"))
    ) == ["甲"]
    assert a in repo.bots


@pytest.mark.asyncio
async def test_experts_without_a_chosen_category_are_uncategorized() -> None:
    """没选类目不默认成「证券 / 期货投资咨询」（所有者 2026-10-18 采纳审读第 6 条）。"""
    repo = MemoryKnowbots()
    await deliver(repo, ME, "a", name="甲")
    svc = catalog(repo)
    assert await svc.categories(ME) == [
        {
            "group": "uncategorized",
            "group_name": "未分类",
            "count": 1,
            "children": [{"code": "uncategorized", "name": "未分类", "count": 1}],
        }
    ]
    [item] = (await svc.list(ME, CatalogQuery()))["items"]
    assert item["category"]["name"] == "未分类"


@pytest.mark.asyncio
async def test_detail_for_the_owner_shows_steps_and_versions() -> None:
    repo = MemoryKnowbots()
    bot = await deliver(repo, ME, "mine")
    await deliver(repo, ME, "mine", version="2", name="财务分析与预测")
    await KnowbotService(repo).stop(owner_subject=ME, knowbot_id=str(bot), version="2")
    svc = catalog(repo)
    detail = await svc.detail(ME, str(bot))
    # 第 2 版停用了：默认给最新的可用版
    assert detail["version"] == "1" and detail["summonable"] is True
    assert [v["version"] for v in detail["versions"]] == ["2", "1"]
    assert detail["sections"]["steps"] == [{"title": "读财报", "summary": ""}]
    assert detail["publisher"] is None
    stopped = await svc.detail(ME, str(bot), "2")
    assert stopped["version"] == "2" and stopped["summonable"] is False
    for bad in ("9", "x", "0"):
        with pytest.raises(NotFoundError):
            await svc.detail(ME, str(bot), bad)
    for bad_id in ("not-a-uuid", str(uuid.uuid4())):
        with pytest.raises(NotFoundError):
            await svc.detail(ME, bad_id)
    with pytest.raises(NotFoundError):
        await svc.detail(OTHER, str(bot))


@pytest.mark.asyncio
async def test_the_detail_writes_what_became_of_versions_never_listed() -> None:
    """SDD 0037 第三节：上架的版本号可能有空号，详情页写明空号那一版的结局。

    结局只给主人看；knowlab 交来的专家停用在 knowlab 做。
    """
    repo = MemoryKnowbots()
    bot = await deliver(repo, ME, "knowlab-r1", version="3")
    await KnowbotService(repo).set_unlisted(
        owner_subject=ME,
        knowbot_ref="knowlab-r1",
        version="1",
        outcome="declined",
    )
    mine = await catalog(repo).detail(ME, str(bot))
    assert mine["unlisted"] == [
        {"version": "2", "outcome": None},  # 还没定、或 knowlab 还没交来
        {"version": "1", "outcome": "declined"},
    ]
    assert mine["stop_in"] == "knowlab"
    make_public(repo, bot)
    theirs = await catalog(repo, public_market=True).detail(OTHER, str(bot))
    assert theirs["unlisted"] == [
        {"version": "2", "outcome": None},
        {"version": "1", "outcome": None},
    ]
    plain = await deliver(repo, ME, "mine")
    assert (await catalog(repo).detail(ME, str(plain)))["stop_in"] == "here"


def test_sections_read_what_the_manifest_has() -> None:
    doc = manifest()
    doc["pack"]["answers"] = {
        "解决什么": "看清走势",
        "不解决什么": "买卖建议",
        "输入是什么": "一个问题",
        "谁做判断": "用户",
    }
    doc["pack"]["scope"] = {"does": "看走势", "does_not": "荐股"}
    doc["pack"]["workflow"] = [
        {"step_id": "s1", "title": "取数", "summary": "按口径取数"},
        {"step_id": "s2", "title": "成稿", "output_file": "底稿/成稿.md"},
    ]
    owner = expert_sections(doc, owner=True)
    assert owner["can_do"] == [
        {"label": "解决什么", "text": "看清走势"},
        {"label": "不解决什么", "text": "买卖建议"},
        {"label": "输入是什么", "text": "一个问题"},
    ]
    assert owner["scope"] == {"does": "看走势", "does_not": "荐股", "elsewhere": ""}
    assert owner["data"] == {
        "bundled": [{"dataset_id": "ds-600519", "version": "3"}],
        "needs": [],
        "accepts_user_data": False,
    }
    assert owner["delivers"] == [{"step": "成稿", "file": "底稿/成稿.md"}]
    assert [s["title"] for s in owner["steps"]] == ["取数", "成稿"]
    assert expert_sections(doc, owner=False)["steps"] is None
    # 没写成稿文件的：交回的是最后一步的结果
    bare = manifest()
    assert expert_sections(bare, owner=False)["delivers"] == [
        {"step": "读财报", "file": None}
    ]


def test_the_detail_page_says_what_data_you_bring() -> None:
    """SDD 0042 第五节：写了数据需求声明的专家，详情页列出要你提供的几样。"""
    doc = manifest(
        data_bindings=[],
        data_needs=[
            {
                "key": "statements",
                "name": "三张报表",
                "description": "最近三年",
                "formats": ["excel", "csv"],
                "required": True,
                "min_count": 1,
                "max_count": 3,
                "tables": [{"sheet": "利润表", "columns": ["营业收入"]}],
            }
        ],
    )
    data = expert_sections(doc, owner=False)["data"]
    assert data["accepts_user_data"] is True and data["bundled"] == []
    assert data["needs"] == [
        {
            "key": "statements",
            "name": "三张报表",
            "description": "最近三年",
            "formats": ["excel", "csv"],
            "required": True,
            "min_count": 1,
            "max_count": 3,
            "tables": [{"sheet": "利润表", "columns": ["营业收入"]}],
        }
    ]
