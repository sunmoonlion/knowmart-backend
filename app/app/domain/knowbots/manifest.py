"""分身清单 `knowbot-manifest/v1` 的外层（SDD 0027 §3.1）。

清单分两层。外层由 knowmart 拥有、在这里严格校验：格式标识、名字与说明、版本、
运行方、数据需求声明、这一版能读的用户数据、每条判断规则的出处。里面的 `pack`
是可执行部分（步骤、验收、底稿的形状），由执行它的工作台拥有、按自己的模型完整
校验；这里只核最基本的骨架，免得复制一份执行方的模型。

专家只是逻辑、不绑数据（10-23，SDD 0042 第五节）：新格式的清单写「数据需求声明」
（`data_needs`，请专家的人按它从自己的知识库选），交到这里只带声明、不带绑定；
`data_bindings` 留给旧格式的专家（作者绑的那几版），到「财务分析与预测」第 2 版
上架为止。
两样都是只加不改：没写声明的清单、绑定上没写对应哪一样的，规范 JSON 与以前一字不差。

摘要是外层规范 JSON（键排序、紧凑分隔、UTF-8）的 sha256：同一个版本只认一份内容。
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA = "knowbot-manifest/v1"
MAX_MANIFEST_BYTES = 1_000_000


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


NEED_KEY = r"^[a-z][a-z0-9_]{0,39}$"
DataFormat = Literal["pdf", "excel", "csv", "text"]
TABULAR: frozenset[str] = frozenset({"excel", "csv"})
MAX_DATA_ITEMS = 64  # 一次请专家最多选这么多份（与 knowbase 一条运行授权的上限一致）


class DataTable(Strict):
    """表格类数据要有的一张表与几列（按表头的字认，不按位置）。

    CSV 没有表名，sheet 不写。
    """

    sheet: str | None = Field(default=None, min_length=1, max_length=100)
    columns: tuple[Annotated[str, Field(min_length=1, max_length=100)], ...] = Field(
        min_length=1, max_length=50
    )


class DataNeed(Strict):
    """数据需求声明里的一样（SDD 0042 第五节第 3 项）：要请专家的人提供什么。

    请专家时按这一样从本地或云端知识库选 min_count 到 max_count 份；必填的缺了不能开始。
    格式沿用 knowlab 收的几种：PDF、Excel（xlsx、xls）、CSV、文本（txt、md）。
    """

    key: str = Field(pattern=NEED_KEY)  # 稳定标识：测试绑定、委托钉住的数据按它对上
    name: str = Field(min_length=1, max_length=80)  # 如「年报」
    description: str = Field(default="", max_length=500)
    formats: tuple[DataFormat, ...] = Field(min_length=1, max_length=4)
    required: bool = True
    min_count: int = Field(default=1, ge=1, le=MAX_DATA_ITEMS)
    max_count: int = Field(default=1, ge=1, le=MAX_DATA_ITEMS)
    tables: tuple[DataTable, ...] = Field(default=(), max_length=20)

    @model_validator(mode="after")
    def _consistent(self) -> DataNeed:
        if len(set(self.formats)) != len(self.formats):
            raise ValueError("a format may be listed only once")
        if self.max_count < self.min_count:
            raise ValueError("max_count must not be below min_count")
        if self.tables and not set(self.formats) <= TABULAR:
            raise ValueError("tables can be required only of Excel or CSV data")
        return self


class DataBinding(Strict):
    """这一版运行时能读的一份用户数据：knowlab 的数据集、版本与内容摘要。

    C-L2：分身运行时只读用户自己的数据。新格式里只出现在 knowlab 的测试绑定中（交到这里
    的不带），`need` 写它对应声明里的哪一样。
    """

    dataset_id: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,128}$")
    version: str = Field(min_length=1, max_length=64)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    need: str | None = Field(
        default=None, pattern=NEED_KEY, exclude_if=lambda value: value is None
    )


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
    # 运行方：这个分身在哪个应用里运行（现在只有 knowbench）。
    # 取清单只给运行方是调用方自己的
    runtime: str = Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")
    pack: dict[str, Any]
    # 数据需求声明（新格式）；没写的不进规范 JSON，旧清单的摘要不变
    data_needs: tuple[DataNeed, ...] = Field(
        default=(), max_length=20, exclude_if=lambda value: not value
    )
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
        keys = [n.key for n in self.data_needs]
        if len(keys) != len(set(keys)):
            raise ValueError("each data need needs its own key")
        if sum(n.max_count for n in self.data_needs) > MAX_DATA_ITEMS:
            raise ValueError(f"the data needs ask for more than {MAX_DATA_ITEMS} items")
        if any(b.need is not None and b.need not in keys for b in self.data_bindings):
            raise ValueError("a data binding names a need that is not declared")
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
    """解析交付来的清单；超大、非对象、外层不合格都抛 ValueError。

    写了数据需求声明的（新格式）不许再带绑定：专家不绑数据，测试绑定留在 knowlab。
    """
    if not isinstance(document, dict):
        raise ValueError("manifest must be an object")
    if len(canonical_bytes(document)) > MAX_MANIFEST_BYTES:
        raise ValueError("manifest is too large")
    parsed = KnowbotManifestV1.model_validate(document)
    if parsed.data_needs and parsed.data_bindings:
        raise ValueError("a manifest with data needs is delivered without bindings")
    return parsed
