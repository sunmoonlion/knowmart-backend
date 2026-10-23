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
    # 没选类目的（所有者 2026-10-18 采纳审读第 6 条）：不能公开，排在最后
    Category("uncategorized", "uncategorized", "未分类", "未分类"),
)
BY_CODE = {c.code: c for c in CATEGORIES}
GROUPS = tuple(dict.fromkeys(c.group for c in CATEGORIES))
# knowlab 交付时由运营选类目（0032 待定第一项）；没选的记未分类，不默认成投资咨询
DEFAULT_CATEGORY = "uncategorized"


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
    """请它时要不要请的人提供自己的数据：写了数据需求声明的要（SDD 0042 第五节）。"""
    return bool(data_needs(manifest)) or manifest.get("accepts_user_data") is True


def data_needs(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """详情页「需要你提供哪些数据」：清单的数据需求声明（交付时已按外层模型核过）。"""
    raw = manifest.get("data_needs")
    needs = []
    for n in raw if isinstance(raw, list) else []:
        if not isinstance(n, dict):
            continue
        tables = [t for t in n.get("tables") or [] if isinstance(t, dict)]
        needs.append(
            {
                "key": _text(n.get("key"), 40),
                "name": _text(n.get("name"), 80),
                "description": _text(n.get("description"), 500),
                "formats": [f for f in n.get("formats") or [] if isinstance(f, str)],
                "required": n.get("required", True) is not False,
                "min_count": int(n.get("min_count") or 1),
                "max_count": int(n.get("max_count") or 1),
                "tables": [
                    {
                        "sheet": _text(t.get("sheet"), 100) or None,
                        "columns": [
                            _text(c, 100)
                            for c in t.get("columns") or []
                            if _text(c, 100)
                        ],
                    }
                    for t in tables
                ],
            }
        )
    return needs


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
            # 写了数据需求声明的（新格式）：请它时按这几样从自己的知识库选
            "needs": data_needs(manifest),
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
