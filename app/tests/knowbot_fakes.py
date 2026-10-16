"""分身测试共用：合格清单与内存仓储（实现 app/application/ports/knowbots.py）。"""

from __future__ import annotations

import copy
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from app.application.ports.knowbots import (
    DeliveryRecord,
    KnowbotRecord,
    KnowbotVersionRecord,
    VersionStatus,
)


def manifest(version: str = "1", **overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schema": "knowbot-manifest/v1",
        "name": "财务分析与预测",
        "tagline": "按我的方法分析一家公司的财报",
        "version": version,
        "runtime": "investment",
        "pack": {
            "pack_id": "fin-forecast",
            "version": version,
            "workflow": [{"step_id": "s1", "title": "读财报"}],
        },
        "data_bindings": [
            {"dataset_id": "ds-600519", "version": "3", "sha256": "a" * 64}
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
    ) -> KnowbotVersionRecord:
        record = KnowbotVersionRecord(
            knowbot_id=knowbot_id,
            version=version,
            digest=digest,
            status="active",
            delivery_id=delivery_id,
            delivered_at=datetime.now(UTC),
            manifest=manifest,
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

    async def set_version_status(
        self, knowbot_id: uuid.UUID, version: str, status: VersionStatus
    ) -> None:
        old = self.versions[(knowbot_id, version)]
        self.versions[(knowbot_id, version)] = KnowbotVersionRecord(
            knowbot_id=old.knowbot_id,
            version=old.version,
            digest=old.digest,
            status=status,
            delivery_id=old.delivery_id,
            delivered_at=old.delivered_at,
            manifest=old.manifest,
        )
