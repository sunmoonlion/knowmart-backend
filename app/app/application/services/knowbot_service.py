"""分身的存放与「请」（SDD 0027 §3.2、§四）。

交付、按所有者取清单、按所有者列出、停用某一版。不变量：
- 分身只归一个所有者（Casdoor 用户 id），所有读写都按所有者过滤；
  不是你的和不存在一样，都是找不到；
- 版本只插不改：同一分身同一版本号只认一份内容；
- 交付幂等：同一个交付单号重发，内容一样就返回原结果，不一样就冲突；
- 停用只挡新的「请」，已经请去在跑的不受影响（工作台按摘要钉死并缓存了清单）。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.application.errors.exceptions import (
    BadRequestError,
    ConcurrencyConflictError,
    NotFoundError,
)
from app.application.ports.knowbots import (
    KnowbotRecord,
    KnowbotRepository,
    KnowbotVersionRecord,
)
from app.domain.knowbots.manifest import parse_manifest

DELIVERY_ID = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
SOURCE_REF = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
OWNER = re.compile(r"^[A-Za-z0-9._:@-]{1,512}$")
VERSION = re.compile(r"^[1-9][0-9]{0,5}$")


@dataclass(frozen=True)
class DeliveryResult:
    knowbot_id: uuid.UUID
    version: str
    digest: str
    replayed: bool


@dataclass(frozen=True)
class SummonedVersion:
    knowbot_id: uuid.UUID
    name: str
    tagline: str
    version: KnowbotVersionRecord


def _require(pattern: re.Pattern[str], value: object, field: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise BadRequestError(f"{field} is invalid", code=f"{field}_invalid")
    return value


def _not_found() -> NotFoundError:
    return NotFoundError("No such knowbot", code="knowbot_not_found")


class KnowbotService:
    def __init__(self, repo: KnowbotRepository) -> None:
        self.repo = repo

    async def deliver(
        self,
        *,
        delivery_id: str,
        owner_subject: str,
        knowbot_ref: str,
        manifest: object,
    ) -> DeliveryResult:
        _require(DELIVERY_ID, delivery_id, "delivery_id")
        _require(OWNER, owner_subject, "owner_subject")
        _require(SOURCE_REF, knowbot_ref, "knowbot_ref")
        try:
            parsed = parse_manifest(manifest)
        except ValueError as exc:
            raise BadRequestError(
                "The knowbot manifest is invalid", code="manifest_invalid"
            ) from exc
        digest = parsed.digest()
        async with self.repo.transaction():
            prior = await self.repo.find_delivery(delivery_id)
            if prior is not None:
                same = (
                    prior.owner_subject == owner_subject
                    and prior.source_ref == knowbot_ref
                    and prior.version.version == parsed.version
                    and prior.version.digest == digest
                )
                if not same:
                    raise ConcurrencyConflictError(
                        "This delivery id was used for different content",
                        code="delivery_id_reused",
                    )
                return DeliveryResult(
                    knowbot_id=prior.version.knowbot_id,
                    version=prior.version.version,
                    digest=digest,
                    replayed=True,
                )
            bot = await self.repo.lock_or_create_knowbot(
                owner_subject=owner_subject,
                source_ref=knowbot_ref,
                name=parsed.name,
                tagline=parsed.tagline,
            )
            if bot.owner_subject != owner_subject:
                raise ConcurrencyConflictError(
                    "This knowbot belongs to another owner",
                    code="knowbot_owner_mismatch",
                )
            if await self.repo.get_version(bot.id, parsed.version) is not None:
                raise ConcurrencyConflictError(
                    "This knowbot version was already delivered; versions never change",
                    code="knowbot_version_exists",
                )
            await self.repo.add_version(
                knowbot_id=bot.id,
                version=parsed.version,
                digest=digest,
                manifest=parsed.canonical(),
                delivery_id=delivery_id,
            )
            if bot.current_version is None or int(parsed.version) > int(
                bot.current_version
            ):
                await self.repo.set_current(
                    bot.id,
                    version=parsed.version,
                    name=parsed.name,
                    tagline=parsed.tagline,
                )
        return DeliveryResult(
            knowbot_id=bot.id, version=parsed.version, digest=digest, replayed=False
        )

    async def _owned(self, owner_subject: str, knowbot_id: str) -> KnowbotRecord:
        if not OWNER.fullmatch(owner_subject or ""):
            raise _not_found()
        try:
            bot_id = uuid.UUID(knowbot_id)
        except (ValueError, TypeError) as exc:
            raise _not_found() from exc
        bot = await self.repo.get_knowbot(bot_id)
        if bot is None or bot.owner_subject != owner_subject:
            raise _not_found()
        return bot

    async def summon(
        self, *, owner_subject: str, knowbot_id: str, version: str
    ) -> SummonedVersion:
        """工作台「请」：只给所有者自己的、未停用的这一版。"""
        bot = await self._owned(owner_subject, knowbot_id)
        if not VERSION.fullmatch(version or ""):
            raise _not_found()
        found = await self.repo.get_version(bot.id, version)
        if found is None or found.status != "active":
            raise _not_found()
        return SummonedVersion(
            knowbot_id=bot.id, name=bot.name, tagline=bot.tagline, version=found
        )

    async def list_for_owner(self, owner_subject: str) -> Sequence[KnowbotRecord]:
        if not OWNER.fullmatch(owner_subject or ""):
            return []
        return await self.repo.list_for_owner(owner_subject)

    async def stop(self, *, owner_subject: str, knowbot_id: str, version: str) -> None:
        async with self.repo.transaction():
            bot = await self._owned(owner_subject, knowbot_id)
            if not VERSION.fullmatch(version or ""):
                raise _not_found()
            if await self.repo.get_version(bot.id, version) is None:
                raise _not_found()
            await self.repo.set_version_status(bot.id, version, "stopped")


def version_view(record: KnowbotVersionRecord) -> dict[str, Any]:
    return {
        "version": record.version,
        "digest": record.digest,
        "status": record.status,
        "delivered_at": record.delivered_at.isoformat(),
    }
