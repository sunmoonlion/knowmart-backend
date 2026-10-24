"""分身存放要的持久化（SDD 0027）。

实现在 app/infrastructure/repositories/knowbots.py。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal, Protocol

VersionStatus = Literal["active", "stopped"]
Visibility = Literal["private", "public"]


@dataclass(frozen=True)
class KnowbotVersionRecord:
    knowbot_id: uuid.UUID
    version: str
    digest: str
    status: VersionStatus
    delivery_id: str
    delivered_at: datetime
    manifest: dict[str, Any] = field(repr=False)


@dataclass(frozen=True)
class KnowbotRecord:
    id: uuid.UUID
    owner_subject: str
    source_ref: str
    name: str
    tagline: str
    current_version: str | None
    versions: tuple[KnowbotVersionRecord, ...] = ()
    # 专家目录（SDD 0032）
    category: str = "uncategorized"
    visibility: Visibility = "private"
    publisher_id: uuid.UUID | None = None


@dataclass(frozen=True)
class PublisherRecord:
    id: uuid.UUID
    kind: Literal["individual", "organization"]
    name: str
    real_name_verified: bool
    # 这个类目上审核通过、未过期的资质（名称与编号）
    licenses: tuple[dict[str, str | None], ...] = ()


@dataclass(frozen=True)
class DeliveryRecord:
    """一次交付落到了哪个分身的哪一版：重发时按它判断是不是同一件事。"""

    delivery_id: str
    owner_subject: str
    source_ref: str
    version: KnowbotVersionRecord


class KnowbotRepository(Protocol):
    def transaction(self) -> AbstractAsyncContextManager[None]: ...

    async def find_delivery(self, delivery_id: str) -> DeliveryRecord | None: ...

    async def lock_or_create_knowbot(
        self, *, owner_subject: str, source_ref: str, name: str, tagline: str
    ) -> KnowbotRecord:
        """按 knowlab 的分身引用找到并锁住分身；没有就建。别人的也原样返回，
        由调用方判断。"""
        ...

    async def get_version(
        self, knowbot_id: uuid.UUID, version: str
    ) -> KnowbotVersionRecord | None: ...

    async def add_version(
        self,
        *,
        knowbot_id: uuid.UUID,
        version: str,
        digest: str,
        manifest: dict[str, Any],
        delivery_id: str,
    ) -> KnowbotVersionRecord: ...

    async def set_current(
        self, knowbot_id: uuid.UUID, *, version: str, name: str, tagline: str
    ) -> None: ...

    async def get_knowbot(self, knowbot_id: uuid.UUID) -> KnowbotRecord | None: ...

    async def list_for_owner(self, owner_subject: str) -> Sequence[KnowbotRecord]: ...

    async def set_version_status(
        self, knowbot_id: uuid.UUID, version: str, status: VersionStatus
    ) -> None: ...

    async def list_visible(
        self, owner_subject: str, *, public_market: bool, today: date
    ) -> Sequence[KnowbotRecord]:
        """这个用户看得到的专家（SDD 0032），带版本：自己的，加上平台开了公开、
        可见范围是公开、发布主体在这个类目上有审核通过且 today 未过期的资质的。"""
        ...

    async def owner_of_source(self, source_ref: str) -> str | None:
        """这个 knowlab 引用对应的分身归谁；还没交付过的回 None。"""
        ...

    async def put_unlisted(
        self, *, owner_subject: str, source_ref: str, version: str, outcome: str
    ) -> None:
        """记下这个引用没上架的那一版的结局（declined | rejected）；有了就照新的写。"""
        ...

    async def unlisted_of(self, source_ref: str, owner_subject: str) -> dict[str, str]:
        """版本号 → 结局；只认这个所有者交过来的。"""
        ...

    async def licensed_publisher(
        self, publisher_id: uuid.UUID, category: str, *, today: date
    ) -> PublisherRecord | None:
        """发布主体在这个类目上有审核通过、未过期的资质时返回它（带这些资质），
        否则 None。"""
        ...
