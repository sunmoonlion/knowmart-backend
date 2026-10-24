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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
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
KNOWLAB_REF = "knowlab-"  # knowlab 交付时的分身引用都这样开头
OUTCOMES = frozenset({"declined", "rejected"})
MAX_UNLISTED = 200


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
    def __init__(
        self,
        repo: KnowbotRepository,
        *,
        public_market: bool = False,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repo = repo
        # 平台开了公开（PUBLIC_MARKET_ENABLED）：公开、资质有效的专家别人也能请
        # （SDD 0018 第十一节）
        self.public_market = public_market
        self.now = now

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
        self,
        *,
        owner_subject: str,
        knowbot_id: str,
        version: str,
        runtime: str,
    ) -> SummonedVersion:
        """运行方「请」：所有者自己的，或公开、资质有效的；未停用的、运行方是这个调用方的这一版。

        owner_subject 是请的人（运行方那头的登录用户）。
        """
        bot = await self._summonable(owner_subject, knowbot_id)
        if not VERSION.fullmatch(version or ""):
            raise _not_found()
        found = await self.repo.get_version(bot.id, version)
        if found is None or found.status != "active":
            raise _not_found()
        if found.manifest.get("runtime") != runtime:
            raise _not_found()
        return SummonedVersion(
            knowbot_id=bot.id, name=bot.name, tagline=bot.tagline, version=found
        )

    async def _summonable(self, subject: str, knowbot_id: str) -> KnowbotRecord:
        """自己的都能请；别人的要平台开了公开、专家公开、发布主体在这个类目上有效。"""
        if not OWNER.fullmatch(subject or ""):
            raise _not_found()
        try:
            bot_id = uuid.UUID(knowbot_id)
        except (ValueError, TypeError) as exc:
            raise _not_found() from exc
        bot = await self.repo.get_knowbot(bot_id)
        if bot is None:
            raise _not_found()
        if bot.owner_subject == subject:
            return bot
        if (
            self.public_market
            and bot.visibility == "public"
            and bot.publisher_id is not None
            and await self.repo.licensed_publisher(
                bot.publisher_id, bot.category, today=self.now().date()
            )
            is not None
        ):
            return bot
        raise _not_found()

    async def list_for_owner(self, owner_subject: str) -> Sequence[KnowbotRecord]:
        if not OWNER.fullmatch(owner_subject or ""):
            return []
        return await self.repo.list_for_owner(owner_subject)

    async def stop(
        self,
        *,
        owner_subject: str,
        knowbot_id: str,
        version: str,
        by_knowlab: bool = False,
    ) -> None:
        """停用某一版。

        knowlab 交来的专家，一生（含停用）记在 knowlab（SDD 0037 第六节），只认
        knowlab 来停；用户在这里点的回 409 `stop_in_knowlab`，页面带他去 knowlab，
        免得两边对不上。
        """
        async with self.repo.transaction():
            bot = await self._owned(owner_subject, knowbot_id)
            if not by_knowlab and bot.source_ref.startswith(KNOWLAB_REF):
                raise ConcurrencyConflictError(
                    "Stop this version in knowlab", code="stop_in_knowlab"
                )
            if not VERSION.fullmatch(version or ""):
                raise _not_found()
            if await self.repo.get_version(bot.id, version) is None:
                raise _not_found()
            await self.repo.set_version_status(bot.id, version, "stopped")

    async def set_unlisted(
        self,
        *,
        owner_subject: str,
        knowbot_ref: str,
        versions: Sequence[dict[str, Any]],
    ) -> None:
        """knowlab 交来这个引用没上架那几版的结局（SDD 0037 第三节），整份替换。

        版本号、结局不合规矩的回 400；分身已交付过、主人不是这个人的回 409。
        """
        _require(OWNER, owner_subject, "owner_subject")
        _require(SOURCE_REF, knowbot_ref, "knowbot_ref")
        if len(versions) > MAX_UNLISTED:
            raise BadRequestError("Too many versions", code="unlisted_invalid")
        pairs: list[tuple[str, str]] = []
        for item in versions:
            version, outcome = str(item.get("version")), item.get("outcome")
            if not VERSION.fullmatch(version) or outcome not in OUTCOMES:
                raise BadRequestError(
                    "A version or outcome is invalid", code="unlisted_invalid"
                )
            pairs.append((version, str(outcome)))
        if len({v for v, _ in pairs}) != len(pairs):
            raise BadRequestError("A version is listed twice", code="unlisted_invalid")
        async with self.repo.transaction():
            owner = await self.repo.owner_of_source(knowbot_ref)
            if owner is not None and owner != owner_subject:
                raise ConcurrencyConflictError(
                    "This knowbot belongs to another owner",
                    code="knowbot_owner_mismatch",
                )
            await self.repo.replace_unlisted(
                owner_subject=owner_subject, source_ref=knowbot_ref, versions=pairs
            )


def version_view(record: KnowbotVersionRecord) -> dict[str, Any]:
    return {
        "version": record.version,
        "runtime": record.manifest.get("runtime"),
        "digest": record.digest,
        "status": record.status,
        "delivered_at": record.delivered_at.isoformat(),
    }
