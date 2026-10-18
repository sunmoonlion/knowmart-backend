"""专家目录的类目与详情页内容（SDD 0032）。

类目照腾讯 WorkBuddy 开放平台的服务类目取名与分级，只收用得到的
（2026-10-18 所有者定）；代码写死在这里，名称可改。
详情页的各块都从清单里取：纯函数，不碰数据库。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Category:
    code: str  # 二级类目代码
    group: str  # 一级类目代码
    group_name: str
    name: str
    # 公开所需的资质（照腾讯）；空 = 个人主体也可公开
    license: str = ""


CATEGORIES: tuple[Category, ...] = (
    Category(
        "finance.securities_advisory",
        "finance",
        "金融",
        "证券 / 期货投资咨询",
        "《经营证券期货业务许可证》",
    ),
    Category(
        "finance.public_fund",
        "finance",
        "金融",
        "公募基金",
        "证券期货业务许可证（含基金）等",
    ),
    Category(
        "finance.private_fund",
        "finance",
        "金融",
        "私募基金",
        "私募基金备案证明；只能展示、介绍、咨询",
    ),
    Category("tools.office", "tools", "工具", "办公"),
    Category("tools.lookup", "tools", "工具", "信息查询"),
    Category("tools.calculator", "tools", "工具", "计算器"),
    Category("business.management", "business", "商业服务", "企业管理"),
)
BY_CODE = {c.code: c for c in CATEGORIES}
GROUPS = tuple(dict.fromkeys(c.group for c in CATEGORIES))
# 现有的专家都是财务类；knowlab 出专家时选类目之前，一律记这一类
DEFAULT_CATEGORY = "finance.securities_advisory"


def category_view(code: str) -> dict[str, str]:
    category = BY_CODE.get(code) or BY_CODE[DEFAULT_CATEGORY]
    return {
        "code": category.code,
        "name": category.name,
        "group": category.group,
        "group_name": category.group_name,
    }


def in_category(code: str, wanted: str | None) -> bool:
    """wanted 可以是二级代码，也可以是一级代码（如 finance）。"""
    if not wanted:
        return True
    category = BY_CODE.get(code) or BY_CODE[DEFAULT_CATEGORY]
    return wanted in (category.code, category.group)


# 详情页「能做什么」：六问里给人看的几问（清单里的键是作者写的中文问题）
ANSWER_LABELS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("解决什么", "它解决什么问题？"), "解决什么"),
    (("不解决什么", "它不解决什么？"), "不解决什么"),
    (("输入", "输入是什么", "输入是什么？"), "输入是什么"),
    (("输出", "输出长什么样", "输出长什么样？"), "输出长什么样"),
    (("怎样算做对", "怎样算做对了？"), "怎样算做对"),
)


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: object, limit: int = 2000) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def accepts_user_data(manifest: dict[str, Any]) -> bool:
    """专家接不接受请的人自己的数据（第二期起清单才会声明；现在都不接受）。"""
    return manifest.get("accepts_user_data") is True


def expert_sections(manifest: dict[str, Any], *, owner: bool) -> dict[str, Any]:
    """详情页的各块。所有者另外看得到方法的步骤（名字与一句话），别人看不到。"""
    pack = _dict(manifest.get("pack"))
    answers = _dict(pack.get("answers"))
    can_do = []
    for keys, label in ANSWER_LABELS:
        text = next((_text(answers.get(k)) for k in keys if _text(answers.get(k))), "")
        if text:
            can_do.append({"label": label, "text": text})
    scope = _dict(pack.get("scope"))
    workflow = [s for s in pack.get("workflow") or [] if isinstance(s, dict)]
    bindings = [b for b in manifest.get("data_bindings") or [] if isinstance(b, dict)]
    sections: dict[str, Any] = {
        "can_do": can_do,
        "scope": {
            "does": _text(scope.get("does")),
            "does_not": _text(scope.get("does_not")),
            "elsewhere": _text(scope.get("elsewhere")),
        },
        "data": {
            "bundled": [
                {
                    "dataset_id": _text(b.get("dataset_id"), 128),
                    "version": _text(b.get("version"), 64),
                }
                for b in bindings
            ],
            "accepts_user_data": accepts_user_data(manifest),
        },
        "delivers": [
            {
                "step": _text(s.get("title"), 200),
                "file": _text(s.get("output_file"), 300) or None,
            }
            for s in workflow
            if _text(s.get("output_file"), 300)
        ]
        or (
            [{"step": _text(workflow[-1].get("title"), 200), "file": None}]
            if workflow
            else []
        ),
        "steps": None,
    }
    if owner:
        sections["steps"] = [
            {
                "title": _text(s.get("title"), 200),
                "summary": _text(s.get("summary"), 500),
            }
            for s in workflow
        ]
    return sections
