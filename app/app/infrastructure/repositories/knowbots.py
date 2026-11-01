"""分身存放的 PostgreSQL 实现（SDD 0027）。

一个实例对应一次请求的数据库会话；transaction 可嵌套，最外层提交、出错回滚。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Any

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports.knowbots import (
    DeliveryRecord,
    KnowbotRecord,
    KnowbotVersionRecord,
    PublisherRecord,
    VersionStatus,
)
from app.domain.knowbots.catalog import CATEGORIES, DEFAULT_CATEGORY
from app.infrastructure.models.knowbots import (
    Knowbot,
    KnowbotUnlistedVersion,
    KnowbotVersion,
    Publisher,
    PublisherLicense,
)
from app.infrastructure.models.market import Shop


def _version(row: KnowbotVersion) -> KnowbotVersionRecord:
    return KnowbotVersionRecord(
        knowbot_id=row.knowbot_id,
        version=row.version,
        digest=row.digest,
        status=row.status,  # type: ignore[arg-type]
        delivery_id=row.delivery_id,
        delivered_at=row.delivered_at,
        manifest=row.manifest,
        public_allowed=row.public_allowed,
        held_reason=row.held_reason,
        held_at=row.held_at,
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
        category=row.category,
        visibility=row.visibility,  # type: ignore[arg-type]
        publisher_id=row.publisher_id,
    )


# 不要证照就能公开的类目（未分类不能公开）
NO_LICENSE_CATEGORIES = tuple(
    c.code for c in CATEGORIES if not c.license and c.code != DEFAULT_CATEGORY
)


def _license_valid(today: date):
    """审核通过、未过期的资质（valid_until 为空算长期有效）。"""
    return and_(
        PublisherLicense.review_status == "approved",
        or_(
            PublisherLicense.valid_until.is_(None),
            PublisherLicense.valid_until >= today,
        ),
    )


def public_to_others(today: date):
    """别人看得到、请得到的专家（平台开了公开时）：可见范围是公开；发布主体实名审过，
    类目要证照的还要有审过、没过期的证照（SDD 0018 第十一节第 5 小节；有证照不免主体
    审核，Astra 评审 F06）；至少有一版是 knowlab 审成公开的（Fable 评审 P2-2）；主人
    的小店没被停（停店期间不在公开列表、请不了，SDD 0018 第十一节第 7 之二小节）。
    """
    approved = exists().where(
        Publisher.id == Knowbot.publisher_id,
        Publisher.review_status == "approved",
        Publisher.real_name_verified.is_(True),
    )
    licensed = exists().where(
        PublisherLicense.publisher_id == Knowbot.publisher_id,
        PublisherLicense.category == Knowbot.category,
        _license_valid(today),
    )
    shown = exists().where(
        KnowbotVersion.knowbot_id == Knowbot.id,
        KnowbotVersion.public_allowed.is_(True),
    )
    return and_(
        Knowbot.visibility == "public",
        approved,
        shown,
        or_(Knowbot.category.in_(NO_LICENSE_CATEGORIES), licensed),
        ~shop_suspended(Knowbot.owner_subject),
    )


def shop_suspended(owner_column: Any):
    """这个主人的小店被停了（关联子查询，跟在别的表的主人那一栏上用）。"""
    return exists().where(
        Shop.owner_subject == owner_column, Shop.status == "suspended"
    )


async def shop_open(session: AsyncSession, owner_subject: str) -> bool:
    status = (
        await session.execute(
            select(Shop.status).where(Shop.owner_subject == owner_subject)
        )
    ).scalar_one_or_none()
    return status != "suspended"


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
        public_allowed: bool = False,
    ) -> KnowbotVersionRecord:
        row = KnowbotVersion(
            knowbot_id=knowbot_id,
            version=version,
            digest=digest,
            manifest=manifest,
            delivery_id=delivery_id,
            status="active",
            public_allowed=public_allowed,
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
        return await self._with_versions(
            select(Knowbot)
            .where(Knowbot.owner_subject == owner_subject)
            .order_by(Knowbot.created_at, Knowbot.id)
        )

    async def list_visible(
        self, owner_subject: str, *, public_market: bool, today: date
    ) -> Sequence[KnowbotRecord]:
        visible = Knowbot.owner_subject == owner_subject
        if public_market:
            visible = or_(visible, public_to_others(today))
        return await self._with_versions(
            select(Knowbot).where(visible).order_by(Knowbot.created_at, Knowbot.id)
        )

    async def owner_shop_open(self, owner_subject: str) -> bool:
        """主人的小店没被停（没开过店的也算开着）。"""
        return await shop_open(self.session, owner_subject)

    async def find_by_source(self, source_ref: str) -> KnowbotRecord | None:
        found = await self._with_versions(
            select(Knowbot).where(Knowbot.source_ref == source_ref)
        )
        return found[0] if found else None

    async def owner_of_source(self, source_ref: str) -> str | None:
        return (
            await self.session.execute(
                select(Knowbot.owner_subject).where(Knowbot.source_ref == source_ref)
            )
        ).scalar_one_or_none()

    async def put_unlisted(
        self, *, owner_subject: str, source_ref: str, version: str, outcome: str
    ) -> None:
        values = {
            "source_ref": source_ref,
            "version": version,
            "owner_subject": owner_subject,
            "outcome": outcome,
        }
        await self.session.execute(
            insert(KnowbotUnlistedVersion)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[
                    KnowbotUnlistedVersion.source_ref,
                    KnowbotUnlistedVersion.version,
                ],
                set_={
                    "owner_subject": owner_subject,
                    "outcome": outcome,
                    "updated_at": func.now(),
                },
            )
        )

    async def unlisted_of(self, source_ref: str, owner_subject: str) -> dict[str, str]:
        rows = await self.session.execute(
            select(
                KnowbotUnlistedVersion.version, KnowbotUnlistedVersion.outcome
            ).where(
                KnowbotUnlistedVersion.source_ref == source_ref,
                KnowbotUnlistedVersion.owner_subject == owner_subject,
            )
        )
        return {version: outcome for version, outcome in rows.all()}

    async def licensed_publisher(
        self, publisher_id: uuid.UUID, category: str, *, today: date
    ) -> PublisherRecord | None:
        publisher = await self.session.get(Publisher, publisher_id)
        if publisher is None:
            return None
        licenses = (
            (
                await self.session.execute(
                    select(PublisherLicense)
                    .where(
                        PublisherLicense.publisher_id == publisher_id,
                        PublisherLicense.category == category,
                        _license_valid(today),
                    )
                    .order_by(PublisherLicense.created_at)
                )
            )
            .scalars()
            .all()
        )
        if publisher.review_status != "approved" or not publisher.real_name_verified:
            return None  # 有证照也不免主体审核（Astra 评审 F06）
        if not licenses and category not in NO_LICENSE_CATEGORIES:
            return None
        return PublisherRecord(
            id=publisher.id,
            kind=publisher.kind,  # type: ignore[arg-type]
            name=publisher.name,
            real_name_verified=publisher.real_name_verified,
            licenses=tuple(
                {
                    "name": lic.license_name,
                    "number": lic.license_no,
                    "valid_until": lic.valid_until.isoformat()
                    if lic.valid_until
                    else None,
                }
                for lic in licenses
            ),
        )

    async def _with_versions(self, query) -> Sequence[KnowbotRecord]:
        bots = (await self.session.execute(query)).scalars().all()
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
            # 停用待处理的那一版：原因跟着清掉（约束：只有待处理的带原因）
            .values(status=status, held_reason=None, held_at=None)
        )

    async def allow_public(self, knowbot_id: uuid.UUID, version: str) -> None:
        await self.session.execute(
            update(KnowbotVersion)
            .where(
                KnowbotVersion.knowbot_id == knowbot_id,
                KnowbotVersion.version == version,
            )
            .values(public_allowed=True)
        )

    async def withdraw_public(self, knowbot_id: uuid.UUID, version: str) -> None:
        row = await self.session.get(
            KnowbotVersion, (knowbot_id, version), with_for_update=True
        )
        if row is None:
            return
        row.public_allowed = False
        if row.status == "held" and row.held_reason in (
            "binding_not_shareable",
            "binding_frozen_self_only",
        ):
            row.status = "active"
            row.held_reason = None
            row.held_at = None
        await self.session.flush()

    async def set_category(self, knowbot_id: uuid.UUID, category: str) -> None:
        await self.session.execute(
            update(Knowbot).where(Knowbot.id == knowbot_id).values(category=category)
        )

    async def watched_versions(self) -> Sequence[tuple[str, KnowbotVersionRecord]]:
        rows = (
            await self.session.execute(
                select(Knowbot.owner_subject, KnowbotVersion)
                .join(Knowbot, Knowbot.id == KnowbotVersion.knowbot_id)
                .where(
                    KnowbotVersion.public_allowed.is_(True),
                    KnowbotVersion.status.in_(("active", "held")),
                )
                .order_by(KnowbotVersion.knowbot_id, KnowbotVersion.version)
            )
        ).all()
        return [
            (owner, _version(v))
            for owner, v in rows
            if isinstance(v.manifest.get("bindings"), list) and v.manifest["bindings"]
        ]

    async def hold_version(
        self,
        knowbot_id: uuid.UUID,
        version: str,
        reason: str | None,
        at: datetime,
    ) -> bool:
        where = [
            KnowbotVersion.knowbot_id == knowbot_id,
            KnowbotVersion.version == version,
        ]
        if reason is None:
            values: dict[str, Any] = {
                "status": "active",
                "held_reason": None,
                "held_at": None,
            }
            where.append(KnowbotVersion.status == "held")
        else:
            values = {"status": "held", "held_reason": reason, "held_at": at}
            where += [
                KnowbotVersion.status.in_(("active", "held")),
                or_(
                    KnowbotVersion.held_reason.is_(None),
                    KnowbotVersion.held_reason != reason,
                ),
            ]
        result = await self.session.execute(
            update(KnowbotVersion).where(*where).values(**values)
        )
        return bool(result.rowcount)
