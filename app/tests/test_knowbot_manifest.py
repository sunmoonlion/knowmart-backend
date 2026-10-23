from __future__ import annotations

import pytest

from app.domain.knowbots.manifest import manifest_digest, parse_manifest
from tests.knowbot_fakes import manifest


def test_valid_manifest_parses_and_digest_ignores_key_order() -> None:
    doc = manifest()
    parsed = parse_manifest(doc)
    reordered = dict(reversed(list(doc.items())))
    assert parse_manifest(reordered).digest() == parsed.digest()
    assert parsed.digest() == manifest_digest(parsed.canonical())
    assert parsed.digest().startswith("sha256:") and len(parsed.digest()) == 71
    assert parsed.canonical()["schema"] == "knowbot-manifest/v1"


def test_content_change_changes_digest() -> None:
    a = parse_manifest(manifest())
    b = parse_manifest(manifest(tagline="另一句话"))
    assert a.digest() != b.digest()


@pytest.mark.parametrize(
    "doc",
    [
        manifest(schema="knowbot-manifest/v2"),
        manifest(version="0"),
        manifest(pack={"pack_id": "x", "version": "2", "workflow": [{}]}),
        manifest(pack={"pack_id": "x", "version": "1", "workflow": []}),
        manifest(pack={"version": "1", "workflow": [{}]}),
        manifest(rule_sources=[]),
        manifest(
            data_bindings=[
                {"dataset_id": "d", "version": "1", "sha256": "a" * 64},
                {"dataset_id": "d", "version": "2", "sha256": "b" * 64},
            ]
        ),
        manifest(data_bindings=[{"dataset_id": "d", "version": "1", "sha256": "x"}]),
        manifest(rule_sources=[{"rule": "r", "source": "platform", "ref": "x"}]),
        manifest(extra="not allowed"),
        manifest(name=""),
    ],
)
def test_invalid_envelopes_are_rejected(doc: dict) -> None:
    with pytest.raises(ValueError):
        parse_manifest(doc)


def test_non_object_and_oversized_manifests_are_rejected() -> None:
    with pytest.raises(ValueError, match="object"):
        parse_manifest(["not", "an", "object"])
    huge = manifest()
    huge["pack"]["workflow"] = [{"text": "x" * 1000}] * 1100
    with pytest.raises(ValueError, match="too large"):
        parse_manifest(huge)


# ---------- 数据需求声明（SDD 0042 第五节第 3 项） ----------

NEED = {
    "key": "annual_reports",
    "name": "年报",
    "description": "最近几年的年度报告全文",
    "formats": ["pdf"],
    "min_count": 3,
    "max_count": 5,
}
TABLE_NEED = {
    "key": "statements",
    "name": "三张报表",
    "formats": ["excel", "csv"],
    "required": False,
    "tables": [{"sheet": "利润表", "columns": ["营业收入", "营业成本"]}],
}
BINDING = {"dataset_id": "d", "version": "1", "sha256": "a" * 64}


def test_old_manifests_keep_their_digests() -> None:
    """加了声明这一栏以后，没写声明、绑定没写对应哪一样的清单，摘要与以前一样
    （已交付、已钉住的版本靠它核对）。数值由加这一栏之前的模型算出。"""
    assert parse_manifest(manifest()).digest() == (
        "sha256:cbb8bc877992fd03a11abb0c3f145afd5f85acb6dba0aef7dea6d983171793c0"
    )
    assert parse_manifest(manifest(data_bindings=[BINDING])).digest() == (
        "sha256:f7b8ad95744185dbd760f552498ce0070759799bedac50747c71b2316958abdd"
    )


def test_data_needs_are_kept_in_the_canonical_form() -> None:
    parsed = parse_manifest(manifest(data_needs=[NEED, TABLE_NEED], data_bindings=[]))
    needs = parsed.canonical()["data_needs"]
    assert [n["key"] for n in needs] == ["annual_reports", "statements"]
    assert needs[0]["required"] is True and needs[1]["min_count"] == 1
    assert needs[1]["tables"] == [
        {"sheet": "利润表", "columns": ["营业收入", "营业成本"]}
    ]
    assert "data_bindings" in parsed.canonical()  # 空的绑定照旧在（与以前一样）


@pytest.mark.parametrize(
    "needs",
    [
        [NEED, {**NEED, "name": "又一样"}],  # key 重了
        [{**NEED, "min_count": 6}],  # 最少比最多还多
        [{**NEED, "formats": []}],
        [{**NEED, "formats": ["pdf", "pdf"]}],
        [{**NEED, "formats": ["word"]}],
        [{**NEED, "key": "年报"}],
        [{**NEED, "tables": [{"columns": ["营业收入"]}]}],  # PDF 不能要求表
        [{**TABLE_NEED, "tables": [{"sheet": "利润表", "columns": []}]}],
        [
            {**NEED, "key": f"n{i}", "max_count": 64} for i in range(2)
        ],  # 合起来超过 64 份
    ],
)
def test_bad_data_needs_are_rejected(needs: list) -> None:
    with pytest.raises(ValueError):
        parse_manifest(manifest(data_needs=needs))


def test_a_new_manifest_is_delivered_without_bindings() -> None:
    with pytest.raises(ValueError, match="without bindings"):
        parse_manifest(
            manifest(
                data_needs=[NEED], data_bindings=[{**BINDING, "need": NEED["key"]}]
            )
        )


def test_a_binding_may_only_name_a_declared_need() -> None:
    from app.domain.knowbots.manifest import KnowbotManifestV1

    doc = manifest(data_needs=[NEED], data_bindings=[{**BINDING, "need": "other"}])
    with pytest.raises(ValueError, match="not declared"):
        KnowbotManifestV1.model_validate(doc)
    doc["data_bindings"][0]["need"] = NEED["key"]
    tested = KnowbotManifestV1.model_validate(doc)  # knowlab 测试用的清单：声明加绑定
    assert tested.canonical()["data_bindings"][0]["need"] == "annual_reports"
