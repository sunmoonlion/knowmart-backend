"""父仓 contracts/knowbot/v2 的 schema 必须就是从外层模型生成的那一份，摘要对得上。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.domain.knowbots.catalog import CATEGORIES, DEFAULT_CATEGORY
from app.domain.knowbots.schema import render, render_delivery
from tests.knowbot_fakes import manifest

CONTRACT_DIR = Path(__file__).resolve().parents[3] / "contracts/knowbot/v2"
SCHEMA = CONTRACT_DIR / "knowbot-manifest.schema.json"
DELIVERY_DIR = Path(__file__).resolve().parents[3] / "contracts/knowbot-delivery/v1"
DELIVERY = DELIVERY_DIR / "knowbot-delivery.schema.json"


def _require_contract() -> None:
    if not SCHEMA.exists():  # 只拿到后端子仓时（例如镜像构建上下文）没有父仓
        pytest.skip("parent contracts/ is not checked out")


def test_schema_file_is_generated_from_the_model() -> None:
    _require_contract()
    assert SCHEMA.read_text(encoding="utf-8") == render(2), (
        "regenerate: python -m app.domain.knowbots.schema 2 > "
        "../../contracts/knowbot/v2/knowbot-manifest.schema.json"
    )


def test_contract_manifest_digest_matches_the_schema() -> None:
    _require_contract()
    release = json.loads((CONTRACT_DIR / "contract-manifest.json").read_text())
    assert (release["contract"], release["major_version"]) == ("knowbot-manifest", 2)
    assert release["schema"] == SCHEMA.name
    assert release["sha256"] == hashlib.sha256(SCHEMA.read_bytes()).hexdigest()


def test_v1_contract_is_gone() -> None:
    _require_contract()
    assert not (CONTRACT_DIR.parent / "v1").exists()  # decisions ⑰：不留旧版


def test_example_manifest_satisfies_the_published_schema() -> None:
    _require_contract()
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    jsonschema.validate(manifest(), schema)


# —— 交付请求 knowbot-delivery/v1（带类目，0032 待定第一项）——


def test_delivery_schema_is_generated_and_its_digest_matches() -> None:
    _require_contract()
    assert DELIVERY.read_text(encoding="utf-8") == render_delivery(), (
        "regenerate: python -m app.domain.knowbots.schema delivery > "
        "../../contracts/knowbot-delivery/v1/knowbot-delivery.schema.json"
    )
    release = json.loads((DELIVERY_DIR / "contract-manifest.json").read_text())
    assert (release["contract"], release["major_version"]) == ("knowbot-delivery", 1)
    assert release["schema"] == DELIVERY.name
    assert release["sha256"] == hashlib.sha256(DELIVERY.read_bytes()).hexdigest()


def test_delivery_categories_are_the_catalog_without_uncategorized() -> None:
    _require_contract()
    schema = json.loads(DELIVERY.read_text(encoding="utf-8"))
    [codes] = [
        a["enum"] for a in schema["properties"]["category"]["anyOf"] if "enum" in a
    ]
    assert codes == [c.code for c in CATEGORIES if c.code != DEFAULT_CATEGORY]
