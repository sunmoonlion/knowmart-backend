"""knowlab 交一版专家来的请求 `knowbot-delivery/v1`，与收清单时的许可检查（SDD 0027、
0032 待定第一项、0043 第 4.3 节）。

- 类目：knowlab 交付时由平台人员按方法内容选（0032 第六节「每个专家只属于一个二级类目，
  knowlab 出专家时由运营按方法内容选；用户不能自己改」），带过来记在专家上；不带的不动
  （新专家照旧是未分类，不能公开）。可选的就是 `catalog.py` 的类目表，未分类不算一种
  选择。
- 两种许可检查（0043 第 4.3 节）：交到本人自用，所绑的每份在、作者还能用就行（「只许
  本人用」的也可以）；公开供别人用，所绑每份都要「可以经专家给别人用」以上。knowmart
  收清单时硬查，在售专家的后台定时核也用同一条规矩（数据拓扑第十节第 5 条）。

模型是 `contracts/knowbot-delivery/v1/` 的真源，生成见 schema.py。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.knowbots.catalog import CATEGORIES, DEFAULT_CATEGORY

CATEGORY_CODES: tuple[str, ...] = tuple(
    c.code for c in CATEGORIES if c.code != DEFAULT_CATEGORY
)
if TYPE_CHECKING:
    CategoryCode = str
else:
    # Pydantic builds the enum from the single authoritative category catalog.
    CategoryCode = Literal[CATEGORY_CODES]

# 所绑数据不行的原因，先说最要紧的（前端照代码写成人话）
HOLD_REASONS: tuple[str, ...] = (
    "binding_gone",  # 没了：作者不再有那一版的使用权、或那份已清除
    "binding_disabled",  # 平台禁用了
    "binding_deleted",  # 作者（或卖家）删了
    "binding_not_shareable",  # 许可改低了，不能再经专家给别人用
)


class KnowbotDeliveryV1(BaseModel):
    """knowlab 交一版过了两道门的专家（服务关系 `knowmart:deliver`）。"""

    model_config = ConfigDict(extra="forbid")

    delivery_id: str = Field(min_length=8, max_length=128)
    owner_subject: str = Field(min_length=1, max_length=512)
    knowbot_ref: str = Field(min_length=1, max_length=128)
    manifest: dict[str, Any]
    # knowlab 运营审核定了公开（SDD 0037 第十八之二节）：资质过的进上架审核，不过的
    # 留自用；同一版先自用、后转公开的，再交一次带 true（knowmart 重查许可后放开）
    request_public: bool = False
    # 平台人员选的二级类目（0032 待定第一项）；不带的不改专家现在的类目
    category: CategoryCode | None = None


def binding_items(
    owner: str, bindings: Sequence[Mapping[str, Any]]
) -> list[tuple[str, str, int, str | None]]:
    """清单 v2 的 `bindings` → 要问 knowbase 的（作者, 资料, 版本, 凭的冻结依据）。

    带上 `expert:<专家>@<版本>`：knowbase 签请专家的授权看冻结时的许可，所以硬查也要
    看到冻结的那一档（只看现在的许可，冻结时只许本人用的照样会过）。"""
    return [
        (
            owner,
            str(b.get("material_id")),
            int(b.get("version_no") or 0),
            str(b["basis_ref"]) if b.get("basis_ref") else None,
        )
        for b in bindings
    ]


def binding_problem(states: Sequence[Mapping[str, Any]], *, public: bool) -> str | None:
    """knowbase 回的每份状态 → 不行的原因（HOLD_REASONS 之一），都行回 None。

    公开要每份都能经专家给别人用；自用只要每份还在、作者还能用。
    """
    found = set()
    for s in states:
        state = s.get("state")
        if state == "gone":
            found.add("binding_gone")
        elif state == "disabled":
            found.add("binding_disabled")
        elif state == "deleted":
            found.add("binding_deleted")
        elif state != "live":
            found.add("binding_gone")
        elif public and s.get("shareable") is not True:
            found.add("binding_not_shareable")
    return next((r for r in HOLD_REASONS if r in found), None)
