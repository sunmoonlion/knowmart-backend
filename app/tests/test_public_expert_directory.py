"""Finders receive complete public declarations, never the private method pack."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.domain.knowbots.catalog import expert_sections
from app.domain.knowbots.directory import public_directory, render
from app.domain.knowbots.manifest import parse_manifest
from tests.knowbot_fakes import MemoryKnowbots
from tests.test_expert_catalog import ME, catalog, deliver


def test_provider_schema_is_exact():
    path = (
        Path(__file__).resolve().parents[3]
        / "contracts/expert-directory/v2/catalog.schema.json"
    )
    assert path.read_text() == render()


async def test_public_snapshot_does_not_truncate_or_leak_private_methods():
    repo = MemoryKnowbots()
    bot = await deliver(repo, ME, "capability-synthetic")
    key = next(key for key in repo.versions if key[0] == bot)
    old = repo.versions[key]
    public = [
        {
            "id": "trend",
            "statement": "分析已授权数据趋势",
            "conditions": [],
            "exclusions": [],
            "delivers": ["趋势报告"],
        }
    ]
    long_scope = "作者的范围" * 501
    repo.versions[key] = replace(
        old,
        manifest={
            **old.manifest,
            "pack": {
                **old.manifest["pack"],
                "scope": {"does": long_scope},
                "runtime_contract": "knowbench/dynamic-methods/v3",
                "public_capabilities": public,
                "planning": {"capability_bindings": {"private": "hidden"}},
                "method_sources": {"private": "secret instructions"},
            },
        },
    )
    legacy = await catalog(repo).runtime_catalog(ME, "knowbench")
    assert len(legacy[0]["scope"]["does"]) == 2000
    rows = await catalog(repo).runtime_catalog(
        ME, "knowbench", capability_declarations=True
    )
    actual = public_directory(
        [{**entry, "price": None, "free": True} for entry in rows]
    )
    assert actual.complete
    expert = actual.experts[0]
    assert expert.scope.does == long_scope
    assert expert.digest == old.digest
    assert expert.declaration == "capabilities/v1"
    assert expert.public_capabilities[0].statement == "分析已授权数据趋势"
    dumped = actual.model_dump_json()
    assert "secret" not in dumped and "capability_bindings" not in dumped
    assert "owned" not in dumped and "method_sources" not in dumped
    detail = expert_sections(repo.versions[key].manifest, owner=False)
    assert {"label": "可完成的任务", "text": "分析已授权数据趋势"} in detail["can_do"]
    assert {"label": "交付成果", "text": "趋势报告"} in detail["can_do"]
    assert detail["steps"] is None
    assert detail["delivers"] == [{"step": "趋势报告", "file": None}]
    assert expert_sections(
        {**repo.versions[key].manifest, "schema": "knowbot-manifest/v3"}, owner=False
    )["data"]["asks_by_question"]
    assert "secret" not in json.dumps(detail)


def test_actual_author_compilation_passes_market_envelope():
    root = Path(__file__).resolve().parents[4]
    doc = json.loads(
        (
            root
            / "knowbench-app/knowbench-backend/app/tests/fixtures"
            / "capability-methods-manifest.json"
        ).read_text()
    )
    # The synthetic compiler fixture intentionally has no author acceptance.
    # This tests only envelope shape, not approval or real asset publication.
    doc["rule_sources"] = [
        {"rule": "合成测试", "source": "user_confirmed", "ref": "synthetic:test"}
    ]
    assert (
        parse_manifest(doc).pack["runtime_contract"] == "knowbench/dynamic-methods/v3"
    )
    doc["pack"]["public_capabilities"] = []
    with pytest.raises(ValueError):
        parse_manifest(doc)
