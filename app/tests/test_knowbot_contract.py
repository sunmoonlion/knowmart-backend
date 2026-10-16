"""父仓 contracts/knowbot/v1 的 schema 必须就是从外层模型生成的那一份，摘要对得上。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.domain.knowbots.schema import render
from tests.knowbot_fakes import manifest

CONTRACT_DIR = Path(__file__).resolve().parents[3] / "contracts/knowbot/v1"
SCHEMA = CONTRACT_DIR / "knowbot-manifest.schema.json"


def _require_contract() -> None:
    if not SCHEMA.exists():  # 只拿到后端子仓时（例如镜像构建上下文）没有父仓
        pytest.skip("parent contracts/ is not checked out")


def test_schema_file_is_generated_from_the_model() -> None:
    _require_contract()
    assert SCHEMA.read_text(encoding="utf-8") == render(), (
        "regenerate: python -m app.domain.knowbots.schema > "
        "../../contracts/knowbot/v1/knowbot-manifest.schema.json"
    )


def test_contract_manifest_digest_matches_the_schema() -> None:
    _require_contract()
    release = json.loads((CONTRACT_DIR / "contract-manifest.json").read_text())
    assert release["contract"] == "knowbot-manifest"
    assert release["major_version"] == 1
    assert release["schema"] == SCHEMA.name
    assert release["sha256"] == hashlib.sha256(SCHEMA.read_bytes()).hexdigest()


def test_example_manifest_satisfies_the_published_schema() -> None:
    _require_contract()
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(manifest(), json.loads(SCHEMA.read_text(encoding="utf-8")))
