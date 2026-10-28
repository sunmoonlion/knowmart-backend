"""清单 v2（SDD 0043 第三、四节）：逻辑 + 冻结的作者挑选的数据；与 v1 并存。"""

from __future__ import annotations

import pytest

from app.domain.knowbots.manifest import KnowbotManifestV2, parse_manifest
from tests.knowbot_fakes import manifest, manifest_v2

NEED = {
    "key": "annual",
    "name": "年报",
    "doc_type": "annual_report",
    "period": {"kind": "latest", "count": 3},
    "formats": ["pdf"],
}


def test_v2_parses_by_schema_and_v1_still_parses() -> None:
    parsed = parse_manifest(manifest_v2())
    assert isinstance(parsed, KnowbotManifestV2)
    assert parsed.canonical()["schema"] == "knowbot-manifest/v2"
    assert "data_needs" not in parsed.canonical()
    # 没给的读法不进规范 JSON：摘要只看写了的
    assert "rendition_kind" not in parsed.canonical()["bindings"][0]
    assert parse_manifest(manifest()).canonical()["schema"] == "knowbot-manifest/v1"
    reordered = dict(reversed(list(manifest_v2().items())))
    assert parse_manifest(reordered).digest() == parsed.digest()


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
    ],
)
def test_invalid_v2_manifests_are_rejected(overrides: dict) -> None:
    with pytest.raises(ValueError):
        parse_manifest(manifest_v2(**overrides))
