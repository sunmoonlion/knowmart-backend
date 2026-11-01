"""分身测试共用：合格清单与内存仓储（实现 app/application/ports/knowbots.py）。"""

from __future__ import annotations

import copy
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any

from app.application.errors.exceptions import ServiceUnavailableError
from app.application.ports.knowbots import (
    DeliveryRecord,
    KnowbotRecord,
    KnowbotVersionRecord,
    PublisherRecord,
    VersionStatus,
)
from app.application.services.market import needs_license
from app.domain.knowbots.catalog import DEFAULT_CATEGORY


def manifest(version: str = "1", **overrides: Any) -> dict[str, Any]:
    """合格的清单 v2：逻辑 + 作者挑选的数据（只带冻结的引用）；没有数据需求声明。"""
    doc: dict[str, Any] = {
        "schema": "knowbot-manifest/v2",
        "name": "财务分析与预测",
        "tagline": "按我的方法分析一家公司的财报",
        "version": version,
        "runtime": "knowbench",
        "pack": {
            "pack_id": "fin-forecast",
            "version": version,
            "workflow": [{"step_id": "s1", "title": "读财报"}],
        },
        "bindings": [
            {
                "material_id": "m-1",
                "version_no": 2,
                "sha256": "b" * 64,
                "basis_ref": "expert:fa-1@2",
            }
        ],
        "rule_sources": [
            {"rule": "毛利率口径", "source": "user_text", "ref": "想法原文第 2 段"}
        ],
    }
    doc.update(overrides)
    return doc


class MemoryKnowbots:
    def __init__(self) -> None:
        self.bots: dict[uuid.UUID, dict[str, Any]] = {}
        self.versions: dict[tuple[uuid.UUID, str], KnowbotVersionRecord] = {}
        # 发布主体与资质（SDD 0032）：测试直接往里放
        self.publishers: dict[uuid.UUID, dict[str, Any]] = {}
        self.licenses: list[dict[str, Any]] = []
        # 没上架那几版的结局（SDD 0037）：（引用，版本号）→（主人，结局）
        self.unlisted: dict[tuple[str, str], tuple[str, str]] = {}
        # 被停了的小店的主人（SDD 0018 第十一节第 7 之二小节）
        self.suspended_shops: set[str] = set()
        self.commits = 0
        self.rollbacks = 0

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        snapshot = (copy.deepcopy(self.bots), dict(self.versions))
        try:
            yield
            self.commits += 1
        except BaseException:
            self.bots, self.versions = snapshot
            self.rollbacks += 1
            raise

    def _record(self, bot: dict[str, Any]) -> KnowbotRecord:
        versions = sorted(
            (v for (k, _), v in self.versions.items() if k == bot["id"]),
            key=lambda v: int(v.version),
        )
        return KnowbotRecord(versions=tuple(versions), **bot)

    async def find_delivery(self, delivery_id: str) -> DeliveryRecord | None:
        for v in self.versions.values():
            if v.delivery_id == delivery_id:
                bot = self.bots[v.knowbot_id]
                return DeliveryRecord(
                    delivery_id=delivery_id,
                    owner_subject=bot["owner_subject"],
                    source_ref=bot["source_ref"],
                    version=v,
                )
        return None

    async def lock_or_create_knowbot(
        self, *, owner_subject: str, source_ref: str, name: str, tagline: str
    ) -> KnowbotRecord:
        for bot in self.bots.values():
            if bot["source_ref"] == source_ref:
                return self._record(bot)
        bot = {
            "id": uuid.uuid4(),
            "owner_subject": owner_subject,
            "source_ref": source_ref,
            "name": name,
            "tagline": tagline,
            "current_version": None,
        }
        self.bots[bot["id"]] = bot
        return self._record(bot)

    async def get_version(
        self, knowbot_id: uuid.UUID, version: str
    ) -> KnowbotVersionRecord | None:
        return self.versions.get((knowbot_id, version))

    async def add_version(
        self,
        *,
        knowbot_id: uuid.UUID,
        version: str,
        digest: str,
        manifest: dict[str, Any],
        delivery_id: str,
        public_allowed: bool = False,
    ) -> KnowbotVersionRecord:
        record = KnowbotVersionRecord(
            knowbot_id=knowbot_id,
            version=version,
            digest=digest,
            status="active",
            delivery_id=delivery_id,
            delivered_at=datetime.now(UTC),
            manifest=manifest,
            public_allowed=public_allowed,
        )
        self.versions[(knowbot_id, version)] = record
        return record

    async def set_current(
        self, knowbot_id: uuid.UUID, *, version: str, name: str, tagline: str
    ) -> None:
        self.bots[knowbot_id].update(
            current_version=version, name=name, tagline=tagline
        )

    async def get_knowbot(self, knowbot_id: uuid.UUID) -> KnowbotRecord | None:
        bot = self.bots.get(knowbot_id)
        return None if bot is None else self._record(bot)

    async def list_for_owner(self, owner_subject: str) -> Sequence[KnowbotRecord]:
        return [
            self._record(b)
            for b in self.bots.values()
            if b["owner_subject"] == owner_subject
        ]

    def _valid_licenses(
        self, publisher_id: uuid.UUID | None, category: str, today: date
    ) -> list[dict[str, Any]]:
        return [
            lic
            for lic in self.licenses
            if lic["publisher_id"] == publisher_id
            and lic["category"] == category
            and lic["review_status"] == "approved"
            and (lic.get("valid_until") is None or lic["valid_until"] >= today)
        ]

    def _qualified(
        self, publisher_id: uuid.UUID | None, category: str, today: date
    ) -> bool:
        """同 SQL：主体实名审过；类目要证照的还要有效证照（Astra 评审 F06）。"""
        publisher = self.publishers.get(publisher_id) if publisher_id else None
        if publisher is None or category == DEFAULT_CATEGORY:
            return False
        if (
            publisher.get("review_status", "approved") != "approved"
            or not publisher["real_name_verified"]
        ):
            return False
        return not needs_license(category) or bool(
            self._valid_licenses(publisher_id, category, today)
        )

    async def list_visible(
        self, owner_subject: str, *, public_market: bool, today: date
    ) -> Sequence[KnowbotRecord]:
        out = []
        for bot in self.bots.values():
            mine = bot["owner_subject"] == owner_subject
            public = (
                public_market
                and bot.get("visibility") == "public"
                and any(
                    v.public_allowed
                    for (k, _), v in self.versions.items()
                    if k == bot["id"]
                )
                and self._qualified(
                    bot.get("publisher_id"), bot.get("category", "uncategorized"), today
                )
                and bot["owner_subject"] not in self.suspended_shops
            )
            if mine or public:
                out.append(self._record(bot))
        return out

    async def owner_shop_open(self, owner_subject: str) -> bool:
        return owner_subject not in self.suspended_shops

    async def find_by_source(self, source_ref: str) -> KnowbotRecord | None:
        for bot in self.bots.values():
            if bot["source_ref"] == source_ref:
                return self._record(bot)
        return None

    async def owner_of_source(self, source_ref: str) -> str | None:
        for bot in self.bots.values():
            if bot["source_ref"] == source_ref:
                return str(bot["owner_subject"])
        return None

    async def put_unlisted(
        self, *, owner_subject: str, source_ref: str, version: str, outcome: str
    ) -> None:
        self.unlisted[(source_ref, version)] = (owner_subject, outcome)

    async def unlisted_of(self, source_ref: str, owner_subject: str) -> dict[str, str]:
        return {
            version: outcome
            for (ref, version), (owner, outcome) in self.unlisted.items()
            if ref == source_ref and owner == owner_subject
        }

    async def licensed_publisher(
        self, publisher_id: uuid.UUID, category: str, *, today: date
    ) -> PublisherRecord | None:
        publisher = self.publishers.get(publisher_id)
        licenses = self._valid_licenses(publisher_id, category, today)
        if publisher is None or not self._qualified(publisher_id, category, today):
            return None
        return PublisherRecord(
            licenses=tuple(
                {
                    "name": lic["license_name"],
                    "number": lic["license_no"],
                    "valid_until": lic["valid_until"].isoformat()
                    if lic.get("valid_until")
                    else None,
                }
                for lic in licenses
            ),
            **{k: v for k, v in publisher.items() if k != "review_status"},
        )

    async def set_version_status(
        self, knowbot_id: uuid.UUID, version: str, status: VersionStatus
    ) -> None:
        old = self.versions[(knowbot_id, version)]
        self.versions[(knowbot_id, version)] = replace(
            old, status=status, held_reason=None, held_at=None
        )

    async def allow_public(self, knowbot_id: uuid.UUID, version: str) -> None:
        old = self.versions[(knowbot_id, version)]
        self.versions[(knowbot_id, version)] = replace(old, public_allowed=True)

    async def withdraw_public(self, knowbot_id: uuid.UUID, version: str) -> None:
        old = self.versions[(knowbot_id, version)]
        held = old.status == "held" and old.held_reason in (
            "binding_not_shareable",
            "binding_frozen_self_only",
        )
        self.versions[(knowbot_id, version)] = replace(
            old,
            public_allowed=False,
            status="active" if held else old.status,
            held_reason=None if held else old.held_reason,
            held_at=None if held else old.held_at,
        )

    async def set_category(self, knowbot_id: uuid.UUID, category: str) -> None:
        self.bots[knowbot_id]["category"] = category

    async def watched_versions(self) -> Sequence[tuple[str, KnowbotVersionRecord]]:
        return [
            (self.bots[k]["owner_subject"], v)
            for (k, _), v in sorted(self.versions.items(), key=lambda x: str(x[0]))
            if v.public_allowed
            and v.status in ("active", "held")
            and v.manifest.get("bindings")
        ]

    async def hold_version(
        self, knowbot_id: uuid.UUID, version: str, reason: str | None, at: datetime
    ) -> bool:
        old = self.versions[(knowbot_id, version)]
        if reason is None:
            if old.status != "held":
                return False
            new = replace(old, status="active", held_reason=None, held_at=None)
        else:
            if old.status == "stopped" or old.held_reason == reason:
                return False
            new = replace(old, status="held", held_reason=reason, held_at=at)
        self.versions[(knowbot_id, version)] = new
        return True


class AllShareable:
    """knowbase 核所绑数据的替身：默认每份都在、都能经专家给别人用；测试改 `states`
    （（作者, 资料, 版本）→ 那一份的回答）或 `down` 让它连不上。"""

    def __init__(self) -> None:
        self.states: dict[tuple[str, str, int], dict[str, Any]] = {}
        self.asked: list[tuple[str, str, int]] = []
        self.down = False

    async def binding_states(
        self, items: Sequence[tuple[str, str, int, str | None]]
    ) -> list[dict[str, Any]]:
        if self.down:
            raise ServiceUnavailableError(
                "knowbase is unreachable", code="knowbase_unavailable"
            )
        self.asked.extend(items)
        return [
            self.states.get(
                i[:3], {"state": "live", "license": "via_expert", "shareable": True}
            )
            for i in items
        ]
