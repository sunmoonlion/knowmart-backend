"""分身清单 `knowbot-manifest/v1` 的外层（SDD 0027 §3.1）。

清单分两层。外层由 knowmart 拥有、在这里严格校验：格式标识、名字与说明、版本、
这一版能读的用户数据、每条判断规则的出处。里面的 `pack` 是可执行部分（步骤、
验收、底稿的形状），由执行它的工作台拥有、按自己的模型完整校验；这里只核最基本
的骨架，免得复制一份执行方的模型。

摘要是外层规范 JSON（键排序、紧凑分隔、UTF-8）的 sha256：同一个版本只认一份内容。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA = "knowbot-manifest/v1"
MAX_MANIFEST_BYTES = 1_000_000


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class DataBinding(Strict):
    """这一版运行时能读的一份用户数据：knowlab 的数据集、版本与内容摘要。

    C-L2：分身运行时只读用户自己的数据。
    """

    dataset_id: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,128}$")
    version: str = Field(min_length=1, max_length=64)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RuleSource(Strict):
    """一条判断规则出自哪里（C-L1：投资逻辑只来自用户）。"""

    rule: str = Field(min_length=1, max_length=200)
    source: Literal["user_text", "user_confirmed"]  # 用户原文；用户确认过的工程化改写
    ref: str = Field(min_length=1, max_length=500)  # 出处：原文位置或确认记录


class KnowbotManifestV1(Strict):
    schema_: Literal["knowbot-manifest/v1"] = Field(alias="schema")
    name: str = Field(min_length=1, max_length=80)
    tagline: str = Field(default="", max_length=200)
    version: str = Field(pattern=r"^[1-9][0-9]{0,5}$")
    pack: dict[str, Any]
    data_bindings: tuple[DataBinding, ...] = Field(default=(), max_length=64)
    rule_sources: tuple[RuleSource, ...] = Field(min_length=1, max_length=500)

    @field_validator("pack")
    @classmethod
    def _pack_skeleton(cls, pack: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(pack.get("pack_id"), str) or not pack["pack_id"]:
            raise ValueError("pack.pack_id is required")
        workflow = pack.get("workflow")
        if not isinstance(workflow, list) or not workflow:
            raise ValueError("pack.workflow must be a non-empty list")
        return pack

    @model_validator(mode="after")
    def _versions_agree(self) -> KnowbotManifestV1:
        if str(self.pack.get("version")) != self.version:
            raise ValueError("pack.version must equal the manifest version")
        datasets = [b.dataset_id for b in self.data_bindings]
        if len(datasets) != len(set(datasets)):
            raise ValueError("a dataset may be bound only once per version")
        return self

    def canonical(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)

    def digest(self) -> str:
        return manifest_digest(self.canonical())


def canonical_bytes(document: dict[str, Any]) -> bytes:
    return json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def manifest_digest(document: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(document)).hexdigest()


def parse_manifest(document: object) -> KnowbotManifestV1:
    """解析交付来的清单；超大、非对象、外层不合格都抛 ValueError。"""
    if not isinstance(document, dict):
        raise ValueError("manifest must be an object")
    if len(canonical_bytes(document)) > MAX_MANIFEST_BYTES:
        raise ValueError("manifest is too large")
    return KnowbotManifestV1.model_validate(document)
