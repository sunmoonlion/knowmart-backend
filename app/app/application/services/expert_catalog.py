"""专家目录（SDD 0032）：首页列表、类目、详情。

谁看得到一个专家只在这里判，前端拿到什么就画什么：
- 自己的专家都看得到；
- 别人的，要平台开了公开（`PUBLIC_MARKET_ENABLED`）、可见范围是公开、
  发布主体在这个类目上有审核通过且未过期的资质，三样都满足才看得到；
  现在开关关着、专家一律自用。
看不到的专家和不存在一样，详情回「找不到」，不回「没权限」，免得别人借此知道它存在。
筛选、排序、分页在这里做（专家少，全取出来再筛；多了再下推到数据库）。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from app.application.errors.exceptions import NotFoundError
from app.application.ports.knowbots import (
    KnowbotRecord,
    KnowbotRepository,
    KnowbotVersionRecord,
)
from app.domain.knowbots.catalog import (
    BY_CODE,
    CATEGORIES,
    DEFAULT_CATEGORY,
    category_view,
    expert_sections,
    in_category,
)

OWNER = re.compile(r"^[A-Za-z0-9._:@-]{1,512}$")
VERSION = re.compile(r"^[1-9][0-9]{0,5}$")
PAGE_SIZE = 24
KNOWLAB_REF = "knowlab-"  # knowlab 交付时的分身引用都这样开头
MAX_GAP_SCAN = 1000
UPDATED_WITHIN = {"7d": timedelta(days=7), "30d": timedelta(days=30)}

UpdatedFilter = Literal["any", "7d", "30d"]
SortOrder = Literal["updated", "name"]


@dataclass(frozen=True)
class CatalogQuery:
    category: str | None = None  # 二级或一级类目代码
    q: str | None = None
    updated: UpdatedFilter = "any"
    sort: SortOrder = "updated"
    page: int = 1


def _not_found() -> NotFoundError:
    return NotFoundError("No such expert", code="expert_not_found")


def _seen_by(bot: KnowbotRecord, viewer: str) -> KnowbotRecord:
    """别人看到的只有 knowlab 审成公开的那几版；主人看到全部（Fable 评审 P2-2）。"""
    if bot.owner_subject == viewer:
        return bot
    return replace(bot, versions=tuple(v for v in bot.versions if v.public_allowed))


def _usable(version: KnowbotVersionRecord, owned: bool) -> bool:
    """请得了：在售的；待处理的（所绑数据失效，0043 第 4.3 节）只有主人请得了。"""
    return version.status == "active" or (owned and version.status == "held")


def _latest(bot: KnowbotRecord, owned: bool = False) -> KnowbotVersionRecord | None:
    """最新的可用版；都停用了就是最新的一版（还能看，请不了）。"""
    active = [v for v in bot.versions if _usable(v, owned)]
    pool = active or list(bot.versions)
    return max(pool, key=lambda v: int(v.version)) if pool else None


class ExpertCatalog:
    def __init__(
        self,
        repo: KnowbotRepository,
        *,
        public_market: bool = False,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repo = repo
        self.public_market = public_market
        self.now = now

    async def _visible(self, owner_subject: str) -> Sequence[KnowbotRecord]:
        if not OWNER.fullmatch(owner_subject or ""):
            return []
        return [
            _seen_by(bot, owner_subject)
            for bot in await self.repo.list_visible(
                owner_subject, public_market=self.public_market, today=self.now().date()
            )
        ]

    def _matches(self, bot: KnowbotRecord, query: CatalogQuery) -> bool:
        latest = _latest(bot)
        if latest is None:
            return False
        words = (query.q or "").strip().lower()
        if words and words not in f"{bot.name}\n{bot.tagline}".lower():
            return False
        window = UPDATED_WITHIN.get(query.updated)
        if window is not None and latest.delivered_at < self.now() - window:
            return False
        return True

    def _item(self, bot: KnowbotRecord, owner_subject: str) -> dict[str, Any]:
        owned = bot.owner_subject == owner_subject
        latest = _latest(bot, owned)
        assert latest is not None
        return {
            "knowbot_id": str(bot.id),
            "name": bot.name,
            "tagline": bot.tagline,
            "category": category_view(bot.category),
            "version": latest.version,
            "summonable": _usable(latest, owned),
            "updated_at": latest.delivered_at.isoformat(),
            "owned": bot.owner_subject == owner_subject,
        }

    async def list(self, owner_subject: str, query: CatalogQuery) -> dict[str, Any]:
        """首页：筛过、排过、分好页的专家，加上按同样条件（类目除外）数出的类目。"""
        candidates = [
            b for b in await self._visible(owner_subject) if self._matches(b, query)
        ]
        counted = _category_tree(candidates)
        chosen = [b for b in candidates if in_category(b.category, query.category)]
        if query.sort == "name":
            chosen.sort(key=lambda b: b.name)
        else:
            chosen.sort(
                key=lambda b: _latest(b).delivered_at,  # type: ignore[union-attr]
                reverse=True,
            )
        page = max(1, query.page)
        start = (page - 1) * PAGE_SIZE
        return {
            "items": [
                self._item(b, owner_subject) for b in chosen[start : start + PAGE_SIZE]
            ],
            "total": len(chosen),
            "page": page,
            "page_size": PAGE_SIZE,
            "categories": counted,
        }

    async def categories(self, owner_subject: str) -> list[dict[str, Any]]:
        visible = [b for b in await self._visible(owner_subject) if _latest(b)]
        return _category_tree(visible)

    async def detail(
        self, owner_subject: str, knowbot_id: str, version: str | None = None
    ) -> dict[str, Any]:
        if not OWNER.fullmatch(owner_subject or ""):
            raise _not_found()
        try:
            bot_id = uuid.UUID(knowbot_id)
        except (ValueError, TypeError) as exc:
            raise _not_found() from exc
        bot = await self.repo.get_knowbot(bot_id)
        if bot is None:
            raise _not_found()
        owned = bot.owner_subject == owner_subject
        publisher = None
        if bot.visibility == "public" and bot.publisher_id is not None:
            publisher = await self.repo.licensed_publisher(
                bot.publisher_id, bot.category, today=self.now().date()
            )
        if not owned and not (self.public_market and publisher is not None):
            raise _not_found()
        bot = _seen_by(bot, owner_subject)
        if version is not None:
            if not VERSION.fullmatch(version):
                raise _not_found()
            selected = next((v for v in bot.versions if v.version == version), None)
        else:
            selected = _latest(bot, owned)
        if selected is None:
            raise _not_found()
        outcomes = (
            await self.repo.unlisted_of(bot.source_ref, bot.owner_subject)
            if owned
            else {}
        )
        return {
            "knowbot_id": str(bot.id),
            "name": bot.name,
            "tagline": bot.tagline,
            "category": category_view(bot.category),
            "owned": owned,
            "version": selected.version,
            "summonable": _usable(selected, owned),
            "updated_at": selected.delivered_at.isoformat(),
            "versions": [
                {
                    "version": v.version,
                    "status": v.status,
                    "delivered_at": v.delivered_at.isoformat(),
                    # 待处理的原因只给主人看（作者在小店里看得到原因）
                    "held_reason": v.held_reason if owned else None,
                }
                for v in sorted(
                    bot.versions, key=lambda v: int(v.version), reverse=True
                )
            ],
            # 空号：knowlab 测试通过、没上架的那几版（SDD 0037 第三节）；结局只给主人看
            "unlisted": [
                {"version": number, "outcome": outcomes.get(number)}
                for number in _gaps(bot.versions)
            ],
            # 停用在哪儿做：knowlab 交来的到 knowlab 停（两边一生记在一处）
            "stop_in": "knowlab" if bot.source_ref.startswith(KNOWLAB_REF) else "here",
            "sections": expert_sections(selected.manifest, owner=owned),
            "publisher": None
            if publisher is None
            else {
                "name": publisher.name,
                "kind": publisher.kind,
                "real_name_verified": publisher.real_name_verified,
                "licenses": list(publisher.licenses),
            },
        }

    async def runtime_catalog(
        self, owner_subject: str, runtime: str
    ) -> list[dict[str, Any]]:
        """运行方替用户「找专家」时看的（请专家方案设计第 4.9 节第 2 条）：
        这个用户请得动、
        运行方是这个调用方的专家，各取最新一版能请的；只给公开信息——名称、一句话、
        接什么 / 不接什么 / 不接的去哪问（清单的 `pack.scope`）、版本、类目。不排序、
        不加权：
        按名称排，匹配由用户自己的模型按专家自述的范围做。"""
        out: list[dict[str, Any]] = []
        for bot in await self._visible(owner_subject):
            owned = bot.owner_subject == owner_subject
            usable = [
                v
                for v in bot.versions
                if _usable(v, owned)
                and (owned or v.public_allowed)
                and v.manifest.get("runtime") == runtime
            ]
            if not usable:
                continue
            latest = max(usable, key=lambda v: int(v.version))
            pack = latest.manifest.get("pack")
            scope = pack.get("scope") if isinstance(pack, dict) else None
            scope = scope if isinstance(scope, dict) else {}
            out.append(
                {
                    "knowbot_id": str(bot.id),
                    "name": bot.name,
                    "tagline": bot.tagline,
                    "version": latest.version,
                    "category": bot.category,
                    "scope": {
                        k: str(scope.get(k) or "")[:2000]
                        for k in ("does", "does_not", "elsewhere")
                    },
                    "owned": bot.owner_subject == owner_subject,
                }
            )
        return sorted(out, key=lambda e: (e["name"], e["knowbot_id"]))


def _gaps(versions: Sequence[KnowbotVersionRecord]) -> list[str]:
    """上架了的版本号里缺的那几个，新的在前；版本号离谱地大时不列（防一页几十万行）。"""
    have = {int(v.version) for v in versions}
    top = max(have, default=0)
    if top > MAX_GAP_SCAN:
        return []
    return [str(n) for n in range(top, 0, -1) if n not in have]


def _category_tree(bots: Sequence[KnowbotRecord]) -> list[dict[str, Any]]:
    """只列有专家的类目，按类目表的顺序；一级下面挂二级，都带数量。"""
    counts: dict[str, int] = {}
    for bot in bots:
        code = bot.category if bot.category in BY_CODE else DEFAULT_CATEGORY
        counts[code] = counts.get(code, 0) + 1
    tree: list[dict[str, Any]] = []
    for category in CATEGORIES:
        count = counts.get(category.code)
        if not count:
            continue
        if not tree or tree[-1]["group"] != category.group:
            tree.append(
                {
                    "group": category.group,
                    "group_name": category.group_name,
                    "count": 0,
                    "children": [],
                }
            )
        tree[-1]["count"] += count
        tree[-1]["children"].append(
            {"code": category.code, "name": category.name, "count": count}
        )
    return tree
