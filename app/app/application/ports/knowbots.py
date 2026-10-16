"""分身存放要的持久化（SDD 0027）。

实现在 app/infrastructure/repositories/knowbots.py。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

VersionStatus = Literal["active", "stopped"]


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
