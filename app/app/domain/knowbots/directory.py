"""Public matching projection; private methods and bindings never leave here."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal, Self

from pydantic import Field, model_validator

from app.domain.knowbots.manifest import Strict

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,47}$")]
Text = Annotated[str, Field(min_length=1, max_length=2000)]


class PublicCapability(Strict):
    id: Identifier
    statement: Text
    conditions: tuple[Text, ...] = Field(max_length=30)
    exclusions: tuple[Text, ...] = Field(max_length=30)
    delivers: tuple[Text, ...] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def _nonblank(self) -> Self:
        if any(
            not text.strip()
            for text in (
                self.statement,
                *self.conditions,
                *self.exclusions,
                *self.delivers,
            )
        ):
            raise ValueError("public capability text must not be blank")
        return self


class Scope(Strict):
    does: str = ""
    does_not: str = ""
    elsewhere: str = ""


class PublicExpert(Strict):
    knowbot_id: str = Field(min_length=1, max_length=64)
    version: str = Field(pattern=r"^[1-9][0-9]{0,5}$")
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    name: str = Field(min_length=1, max_length=80)
    tagline: str = Field(max_length=200)
    scope: Scope
    declaration: Literal["scope/v1", "capabilities/v1"]
    runtime_contract: Literal[
        "knowbench/fixed/v1",
        "knowbench/dynamic-methods/v1",
        "knowbench/dynamic-methods/v2",
        "knowbench/dynamic-methods/v3",
    ]
    public_capabilities: tuple[PublicCapability, ...] = Field(max_length=30)
    free: bool = Field(strict=True)
    price: str | None = Field(pattern=r"^[0-9]+(\.[0-9]+)?$", max_length=32)

    @model_validator(mode="after")
    def _declaration(self) -> Self:
        ids = {cap.id for cap in self.public_capabilities}
        if len(ids) != len(self.public_capabilities):
            raise ValueError("public capabilities must be unique")
        if (self.declaration == "capabilities/v1") != bool(ids):
            raise ValueError("public declaration format differs")
        if self.runtime_contract.endswith("/v3") and not ids:
            raise ValueError("capability runtime needs public capabilities")
        return self


class ExpertDirectory(Strict):
    contract: Literal["knowmart/expert-directory/v2"]
    complete: bool = Field(strict=True)
    experts: tuple[PublicExpert, ...] = Field(max_length=1000)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        keys = [(entry.knowbot_id, entry.version) for entry in self.experts]
        if len(set(keys)) != len(keys):
            raise ValueError("directory expert versions must be unique")
        return self


def public_directory(entries: list[dict[str, Any]]) -> ExpertDirectory:
    # A bound that cannot represent the entire directory is an explicit error.
    # Never silently truncate then certify 'complete'.
    return ExpertDirectory.model_validate(
        {
            "contract": "knowmart/expert-directory/v2",
            "complete": True,
            "experts": [
                {key: entry[key] for key in PublicExpert.model_fields}
                for entry in entries
            ],
        }
    )


def render() -> str:
    return (
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "https://contracts.sunmoonai.com/knowmart/expert-directory/v2/catalog.schema.json",
                **ExpertDirectory.model_json_schema(),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
