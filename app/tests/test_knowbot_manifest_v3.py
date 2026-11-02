"""V3 stores a frozen author catalog; V2 bytes and digest stay compatible."""

import hashlib
import json
from pathlib import Path

import jsonschema
import pytest

from app.domain.knowbots.manifest import KnowbotManifestV3, parse_manifest
from app.domain.knowbots.schema import render
from tests.knowbot_fakes import manifest as fixed_manifest


def manifest():
    doc = fixed_manifest()
    doc["schema"] = "knowbot-manifest/v3"
    doc["pack"] = {
        "pack_id": "synthetic-methods",
        "version": "1",
        "runtime_contract": "knowbench/dynamic-methods/v1",
        "planning": {"method_text": "Synthetic; full validation belongs to knowbench"},
        "method_units": [{"contract": {"step_id": "inspect"}}],
    }
    return doc


def test_v3_canonical_roundtrip_and_frozen_binding_survive():
    parsed = parse_manifest(manifest())
    assert isinstance(parsed, KnowbotManifestV3)
    assert (
        parsed.canonical()["bindings"]
        == parse_manifest(fixed_manifest()).canonical()["bindings"]
    )
    assert parse_manifest(parsed.canonical()).digest() == parsed.digest()
    changed = parsed.canonical()
    changed["pack"]["planning"]["method_text"] = "Another author method"
    assert parse_manifest(changed).digest() != parsed.digest()


@pytest.mark.parametrize(
    "change",
    [
        {"workflow": [{}]},
        {"runtime_contract": "unknown"},
        {"method_units": []},
        {"planning": None},
        {"version": "2"},
    ],
)
def test_invalid_or_mixed_catalog_is_rejected(change):
    doc = manifest()
    doc["pack"].update(change)
    with pytest.raises(ValueError):
        parse_manifest(doc)


def test_schema_label_alone_does_not_convert_fixed_and_dynamic():
    doc = fixed_manifest()
    doc["schema"] = "knowbot-manifest/v3"
    with pytest.raises(ValueError):
        parse_manifest(doc)
    doc = manifest()
    doc["schema"] = "knowbot-manifest/v2"
    with pytest.raises(ValueError):
        parse_manifest(doc)


def test_provider_schema_and_digest_are_generated():
    path = (
        Path(__file__).resolve().parents[3]
        / "contracts/knowbot/v3/knowbot-manifest.schema.json"
    )
    if not path.exists():
        pytest.skip("parent contract checkout required")
    assert path.read_text() == render(3)
    release = json.loads((path.parent / "contract-manifest.json").read_text())
    assert release["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert release["major_version"] == 3
    jsonschema.validate(manifest(), json.loads(path.read_text()))


def test_v2_canonical_digest_matches_pre_dynamic_baseline():
    # Captured from 731cf3de, before the shared envelope was extracted.
    assert (
        parse_manifest(fixed_manifest()).digest()
        == "sha256:90723e8e45c0f336dfb89559d00efcba9a6cbb3bfd91110c42deeea5fe6cec0b"
    )


@pytest.mark.parametrize(
    "filename", ["dynamic-methods-manifest.json", "referenced-methods-manifest.json"]
)
def test_actual_compiler_fixture_is_accepted_without_rewriting_the_catalog(filename):
    doc = compiled_manifest(filename)
    parsed = parse_manifest(doc)
    assert parsed.pack == doc["pack"]
    assert parse_manifest(parsed.canonical()).digest() == parsed.digest()


def test_reference_catalog_requires_explicit_runtime_and_snapshot():
    doc = manifest()
    doc["pack"]["method_sources"] = {"inspect": {}}
    with pytest.raises(ValueError):
        parse_manifest(doc)
    doc["pack"]["runtime_contract"] = "knowbench/dynamic-methods/v2"
    doc["pack"]["method_sources"] = {}
    with pytest.raises(ValueError):
        parse_manifest(doc)


def compiled_manifest(filename):
    fixture = (
        Path(__file__).resolve().parents[4]
        / "knowbench-app/knowbench-backend/app/tests/fixtures"
        / filename
    )
    if not fixture.exists():
        pytest.skip("knowbench checkout required")
    return json.loads(fixture.read_text())
