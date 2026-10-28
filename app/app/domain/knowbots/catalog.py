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


def asks_by_question(manifest: dict[str, Any]) -> bool:
    """清单 v2：每件委托由第 0 步按你的问题提出要哪些数据、你确认（decisions ⑭）。"""
    return manifest.get("schema") == "knowbot-manifest/v2"


def accepts_user_data(manifest: dict[str, Any]) -> bool:
    """请它时要不要请的人提供自己的数据：清单 v2 都要（按问题提出、你确认）。"""
    return asks_by_question(manifest)


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
    sections: dict[str, Any] = {
        "can_do": can_do,
        "scope": {
            "does": _text(scope.get("does")),
            "does_not": _text(scope.get("does_not")),
            "elsewhere": _text(scope.get("elsewhere")),
        },
        "data": {
            # 清单 v2：每件委托按你的问题提出要哪些数据、你确认
            "asks_by_question": asks_by_question(manifest),
            # 清单 v2：作者挑选的数据留在 knowbase，这里只说有几份（不给浏览、下载入口）
            "bound_count": len(
                [b for b in manifest.get("bindings") or [] if isinstance(b, dict)]
            ),
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
