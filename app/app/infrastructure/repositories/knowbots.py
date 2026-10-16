"""分身存放的 PostgreSQL 实现（SDD 0027）。

一个实例对应一次请求的数据库会话；transaction 可嵌套，最外层提交、出错回滚。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports.knowbots import (
    DeliveryRecord,
    KnowbotRecord,
    KnowbotVersionRecord,
    VersionStatus,
)
from app.infrastructure.models.knowbots import Knowbot, KnowbotVersion


def _version(row: KnowbotVersion) -> KnowbotVersionRecord:
    return KnowbotVersionRecord(
        knowbot_id=row.knowbot_id,
        version=row.version,
        digest=row.digest,
        status=row.status,  # type: ignore[arg-type]
        delivery_id=row.delivery_id,
        delivered_at=row.delivered_at,
        manifest=row.manifest,
    )


def _knowbot(row: Knowbot, versions: Sequence[KnowbotVersion] = ()) -> KnowbotRecord:
    return KnowbotRecord(
        id=row.id,
        owner_subject=row.owner_subject,
        source_ref=row.source_ref,
        name=row.name,
        tagline=row.tagline,
        current_version=row.current_version,
        versions=tuple(
            _version(v) for v in sorted(versions, key=lambda v: int(v.version))
        ),
    )


class SqlKnowbotRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self._depth = 0

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        outer = self._depth == 0
        self._depth += 1
        try:
            yield
            if outer:
                await self.session.commit()
        except BaseException:
            if outer:
                await self.session.rollback()
            raise
        finally:
            self._depth -= 1

    async def find_delivery(self, delivery_id: str) -> DeliveryRecord | None:
        row = (
            await self.session.execute(
                select(KnowbotVersion, Knowbot)
                .join(Knowbot, Knowbot.id == KnowbotVersion.knowbot_id)
                .where(KnowbotVersion.delivery_id == delivery_id)
            )
        ).first()
        if row is None:
            return None
        version, bot = row
        return DeliveryRecord(
            delivery_id=delivery_id,
            owner_subject=bot.owner_subject,
            source_ref=bot.source_ref,
            version=_version(version),
        )

    async def lock_or_create_knowbot(
        self, *, owner_subject: str, source_ref: str, name: str, tagline: str
    ) -> KnowbotRecord:
        await self.session.execute(
            insert(Knowbot)
            .values(
                id=uuid.uuid4(),
                owner_subject=owner_subject,
                source_ref=source_ref,
                name=name,
                tagline=tagline,
            )
            .on_conflict_do_nothing(index_elements=[Knowbot.source_ref])
        )
        bot = (
            await self.session.execute(
                select(Knowbot)
                .where(Knowbot.source_ref == source_ref)
                .with_for_update()
            )
        ).scalar_one()
        return _knowbot(bot)

    async def get_version(
        self, knowbot_id: uuid.UUID, version: str
    ) -> KnowbotVersionRecord | None:
        row = await self.session.get(KnowbotVersion, (knowbot_id, version))
        return None if row is None else _version(row)

    async def add_version(
        self,
        *,
        knowbot_id: uuid.UUID,
        version: str,
        digest: str,
        manifest: dict[str, Any],
        delivery_id: str,
    ) -> KnowbotVersionRecord:
        row = KnowbotVersion(
            knowbot_id=knowbot_id,
            version=version,
            digest=digest,
            manifest=manifest,
            delivery_id=delivery_id,
            status="active",
        )
        self.session.add(row)
        await self.session.flush()
        await self.session.refresh(row)
        return _version(row)

    async def set_current(
        self, knowbot_id: uuid.UUID, *, version: str, name: str, tagline: str
    ) -> None:
        await self.session.execute(
            update(Knowbot)
            .where(Knowbot.id == knowbot_id)
            .values(current_version=version, name=name, tagline=tagline)
        )

    async def get_knowbot(self, knowbot_id: uuid.UUID) -> KnowbotRecord | None:
        bot = await self.session.get(Knowbot, knowbot_id)
        if bot is None:
            return None
        versions = (
            await self.session.execute(
                select(KnowbotVersion).where(KnowbotVersion.knowbot_id == knowbot_id)
            )
        ).scalars()
        return _knowbot(bot, list(versions))

    async def list_for_owner(self, owner_subject: str) -> Sequence[KnowbotRecord]:
        bots = (
            (
                await self.session.execute(
                    select(Knowbot)
                    .where(Knowbot.owner_subject == owner_subject)
                    .order_by(Knowbot.created_at, Knowbot.id)
                )
            )
            .scalars()
            .all()
        )
        if not bots:
            return []
        versions = (
            (
                await self.session.execute(
                    select(KnowbotVersion).where(
                        KnowbotVersion.knowbot_id.in_([b.id for b in bots])
                    )
                )
            )
            .scalars()
            .all()
        )
        by_bot: dict[uuid.UUID, list[KnowbotVersion]] = {}
        for v in versions:
            by_bot.setdefault(v.knowbot_id, []).append(v)
        return [_knowbot(b, by_bot.get(b.id, [])) for b in bots]

    async def set_version_status(
        self, knowbot_id: uuid.UUID, version: str, status: VersionStatus
    ) -> None:
        await self.session.execute(
            update(KnowbotVersion)
            .where(
                KnowbotVersion.knowbot_id == knowbot_id,
                KnowbotVersion.version == version,
            )
            .values(status=status)
        )
