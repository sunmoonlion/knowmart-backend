"""分身清单 `knowbot-manifest/v2` 的外层（SDD 0027 §3.1、SDD 0043 第三、四节）。

清单分两层。外层由 knowmart 拥有、在这里严格校验：格式标识、名字与说明、版本、
运行方、作者挑选的数据（冻结的引用）、每条判断规则的出处。里面的 `pack` 是可执行
部分（步骤、验收、底稿的形状），由执行它的工作台拥有、按自己的模型完整校验；这里只核
最基本的骨架，免得复制一份执行方的模型。

清单 v2（10-28 起，decisions ⑭）= 逻辑 + 作者挑选的数据：没有数据需求声明，每件
委托要什么数据由委托第 0 步用户自己的模型按专家逻辑和用户问题提出、用户确认。
v1（数据需求声明、knowlab 数据集绑定）不再收（10-28，decisions ⑰）。
v3（WP-10）显式发布动态作者目录，旧 v2 固定专家保持原样；目录的完整规则仍归运行方。

摘要是外层规范 JSON（键排序、紧凑分隔、UTF-8）的 sha256：同一个版本只认一份内容。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_MANIFEST_BYTES = 1_000_000


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


def check_pack(pack: dict[str, Any]) -> dict[str, Any]:
    """pack 的骨架（完整的由运行方按自己的模型核）。"""
    if not isinstance(pack.get("pack_id"), str) or not pack["pack_id"]:
        raise ValueError("pack.pack_id is required")
    workflow = pack.get("workflow")
    if not isinstance(workflow, list) or not workflow:
        raise ValueError("pack.workflow must be a non-empty list")
    return pack


class RuleSource(Strict):
    """一条判断规则出自哪里（C-L1：投资逻辑只来自用户）。"""

    rule: str = Field(min_length=1, max_length=200)
    source: Literal["user_text", "user_confirmed"]  # 用户原文；用户确认过的工程化改写
    ref: str = Field(min_length=1, max_length=500)  # 出处：原文位置或确认记录


def canonical_bytes(document: dict[str, Any]) -> bytes:
    return json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def manifest_digest(document: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(document)).hexdigest()


SCHEMA_V2 = "knowbot-manifest/v2"
SCHEMA_V3 = "knowbot-manifest/v3"
_FROZEN = r"^expert:[A-Za-z0-9._-]{1,64}@[0-9][0-9.]{0,31}$"


class FrozenBinding(Strict):
    """作者挑选、交付时冻结的一份（SDD 0043 第 4.4 节）。

    数据留在 knowbase，这里只记引用：哪份资料、哪一版与摘要（要读法的再钉读法），
    凭的是 `expert:<专家>@<版本>`；别人请这位专家时按受益人授权读。
    """

    material_id: str = Field(min_length=1, max_length=64)
    version_no: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rendition_kind: Literal["text", "pages", "table", "index", "parse"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    rendition_no: int | None = Field(
        default=None, ge=1, exclude_if=lambda value: value is None
    )
    rendition_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$", exclude_if=lambda value: value is None
    )
    basis_ref: str = Field(pattern=_FROZEN)


class ManifestEnvelope[SchemaT: str](Strict):
    """清单 v2（SDD 0043 第三、四节）：逻辑 + 作者挑选的数据（只带冻结的引用）。
    不再有数据需求声明（decisions ⑭），也不再有 knowlab 数据集的绑定。"""

    schema_: SchemaT = Field(alias="schema")
    name: str = Field(min_length=1, max_length=80)
    tagline: str = Field(default="", max_length=200)
    version: str = Field(pattern=r"^[1-9][0-9]{0,5}$")
    runtime: str = Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")
    pack: dict[str, Any]
    bindings: tuple[FrozenBinding, ...] = Field(default=(), max_length=200)
    rule_sources: tuple[RuleSource, ...] = Field(min_length=1, max_length=500)

    @field_validator("pack")
    @classmethod
    def _pack_skeleton(cls, pack: dict[str, Any]) -> dict[str, Any]:
        return check_pack(pack)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if str(self.pack.get("version")) != self.version:
            raise ValueError("pack.version must equal the manifest version")
        pinned = [(b.material_id, b.version_no) for b in self.bindings]
        if len(pinned) != len(set(pinned)):
            raise ValueError("a material version may be bound only once")
        if len({b.basis_ref for b in self.bindings}) > 1:
            raise ValueError("all bindings are frozen for one expert version")
        return self

    def canonical(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)

    def digest(self) -> str:
        return manifest_digest(self.canonical())


class KnowbotManifestV2(ManifestEnvelope[Literal["knowbot-manifest/v2"]]):
    """清单 v2（SDD 0043 第三、四节）：逻辑 + 作者挑选的数据（只带冻结的引用）。
    不再有数据需求声明（decisions ⑭），也不再有 knowlab 数据集的绑定。"""


class KnowbotManifestV3(ManifestEnvelope[Literal["knowbot-manifest/v3"]]):
    """动态作者方法目录；任务计划由运行方另存，不能混进发布清单。"""

    @field_validator("pack")
    @classmethod
    def _pack_skeleton(cls, pack: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(pack.get("pack_id"), str) or not pack["pack_id"]:
            raise ValueError("pack.pack_id is required")
        if pack.get("runtime_contract") not in (
            "knowbench/dynamic-methods/v1",
            "knowbench/dynamic-methods/v2",
            "knowbench/dynamic-methods/v3",
        ):
            raise ValueError("unsupported dynamic runtime contract")
        if pack["runtime_contract"].endswith("/v3"):
            from app.domain.knowbots.directory import PublicCapability

            capabilities = pack.get("public_capabilities")
            if not isinstance(capabilities, list) or not 1 <= len(capabilities) <= 30:
                raise ValueError("capability catalog needs public capabilities")
            parsed = [PublicCapability.model_validate(cap) for cap in capabilities]
            if len({cap.id for cap in parsed}) != len(parsed):
                raise ValueError("public capability ids must be unique")
            if not isinstance(pack.get("method_sources"), dict):
                raise ValueError("capability catalog requires method_sources")
            if pack.get("method_compiler") != "knowlab/workdraft-compiler/v2":
                raise ValueError("unsupported capability compiler")
        elif pack["runtime_contract"].endswith("/v2"):
            if (
                not isinstance(pack.get("method_sources"), dict)
                or not pack["method_sources"]
            ):
                raise ValueError("referenced catalog requires method_sources")
        elif "method_sources" in pack:
            raise ValueError("method references require dynamic-methods/v2")
        if "workflow" in pack:
            raise ValueError("a method catalog must not contain a workflow")
        if not isinstance(pack.get("method_units"), list) or not pack["method_units"]:
            raise ValueError("pack.method_units must be a non-empty list")
        if not isinstance(pack.get("planning"), dict):
            raise ValueError("pack.planning is required")
        return pack


KnowbotManifest = KnowbotManifestV2 | KnowbotManifestV3


def parse_manifest(document: object) -> KnowbotManifest:
    """解析交付来的 v2 固定 / v3 动态清单；未知版本或不合格外层抛 ValueError。"""
    if not isinstance(document, dict):
        raise ValueError("manifest must be an object")
    if len(canonical_bytes(document)) > MAX_MANIFEST_BYTES:
        raise ValueError("manifest is too large")
    if document.get("schema") == SCHEMA_V2:
        return KnowbotManifestV2.model_validate(document)
    if document.get("schema") == SCHEMA_V3:
        return KnowbotManifestV3.model_validate(document)
    raise ValueError(f"manifest schema must be {SCHEMA_V2} or {SCHEMA_V3}")
