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
