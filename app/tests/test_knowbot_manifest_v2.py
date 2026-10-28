"""清单 v2（SDD 0043 第三、四节）：逻辑 + 冻结的作者挑选的数据。

v1 不再收（decisions ⑰）。"""

from __future__ import annotations

import pytest

from app.domain.knowbots.manifest import (
    KnowbotManifestV2,
    manifest_digest,
    parse_manifest,
)
from tests.knowbot_fakes import manifest

NEED = {
    "key": "annual",
    "name": "年报",
    "doc_type": "annual_report",
    "period": {"kind": "latest", "count": 3},
    "formats": ["pdf"],
}


def test_v2_parses_and_digest_ignores_key_order() -> None:
    parsed = parse_manifest(manifest())
    assert isinstance(parsed, KnowbotManifestV2)
    assert parsed.canonical()["schema"] == "knowbot-manifest/v2"
    assert "data_needs" not in parsed.canonical()
    # 没给的读法不进规范 JSON：摘要只看写了的
    assert "rendition_kind" not in parsed.canonical()["bindings"][0]
    reordered = dict(reversed(list(manifest().items())))
    assert parse_manifest(reordered).digest() == parsed.digest()
    assert parsed.digest() == manifest_digest(parsed.canonical())
    assert parsed.digest().startswith("sha256:") and len(parsed.digest()) == 71


def test_content_change_changes_digest() -> None:
    a = parse_manifest(manifest())
    b = parse_manifest(manifest(tagline="另一句话"))
    assert a.digest() != b.digest()


def test_non_object_oversized_and_v1_manifests_are_rejected() -> None:
    with pytest.raises(ValueError, match="object"):
        parse_manifest(["not", "an", "object"])
    huge = manifest()
    huge["pack"]["workflow"] = [{"text": "x" * 1000}] * 1100
    with pytest.raises(ValueError, match="too large"):
        parse_manifest(huge)
    v1 = {k: v for k, v in manifest().items() if k != "bindings"}
    for schema in ("knowbot-manifest/v1", None):
        with pytest.raises(ValueError, match="knowbot-manifest/v2"):
            parse_manifest({**v1, "schema": schema, "data_bindings": []})


@pytest.mark.parametrize(
    "overrides",
    [
        {"data_bindings": []},
        # 10-28 起清单 v2 不再有数据需求声明（decisions ⑭）
        {"data_needs": [NEED]},
        {
            "bindings": [
                {
                    "material_id": "m-1",
                    "version_no": 1,
                    "sha256": "b" * 64,
                    "basis_ref": "expert:fa-1",
                }
            ]
        },
        {
            "bindings": [
                {
                    "material_id": "m-1",
                    "version_no": 1,
                    "sha256": "b" * 64,
                    "basis_ref": "expert:fa-1@2",
                    "need": "nope",
                }
            ]
        },
        {
            "bindings": [
                {
                    "material_id": "m-1",
                    "version_no": 1,
                    "sha256": "b" * 64,
                    "basis_ref": "expert:fa-1@2",
                },
                {
                    "material_id": "m-1",
                    "version_no": 1,
                    "sha256": "c" * 64,
                    "basis_ref": "expert:fa-1@2",
                },
            ]
        },
        {
            "bindings": [
                {
                    "material_id": "m-1",
                    "version_no": 1,
                    "sha256": "b" * 64,
                    "basis_ref": "expert:fa-1@2",
                },
                {
                    "material_id": "m-2",
                    "version_no": 1,
                    "sha256": "c" * 64,
                    "basis_ref": "expert:fa-1@3",
                },
            ]
        },
        {"pack": {"pack_id": "x", "version": "2", "workflow": [{}]}},
        {"pack": {"pack_id": "x", "version": "1", "workflow": []}},
        {"pack": {"version": "1", "workflow": [{}]}},
        {"version": "0"},
        {"rule_sources": []},
        {"rule_sources": [{"rule": "r", "source": "platform", "ref": "x"}]},
        {"extra": "not allowed"},
        {"name": ""},
    ],
)
def test_invalid_v2_manifests_are_rejected(overrides: dict) -> None:
    with pytest.raises(ValueError):
        parse_manifest(manifest(**overrides))
