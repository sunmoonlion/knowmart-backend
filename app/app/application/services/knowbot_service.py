"""分身的存放与「请」（SDD 0027 §3.2、§四）。

交付、按所有者取清单、按所有者列出、停用某一版。不变量：
- 分身只归一个所有者（Casdoor 用户 id），所有读写都按所有者过滤；
  不是你的和不存在一样，都是找不到；
- 版本只插不改：同一分身同一版本号只认一份内容；
- 交付幂等：同一个交付单号重发，内容一样就返回原结果，不一样就冲突；
- 停用只挡新的「请」，已经请去在跑的不受影响（工作台按摘要钉死并缓存了清单）；
- 收清单时硬查所绑数据的许可（0043 第 4.3 节）：自用要每份还在，公开要每份都能经专家
  给别人用；knowbase 说不准（没配、连不上）就不收；
- 在售那一版所绑数据失效转「待处理」（数据拓扑第十节第 5 条）：后台每 10 分钟核一次，
  别人请不了、主人照样能请，数据好了自动回在售。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.application.errors.exceptions import (
    AppException,
    BadRequestError,
    ConcurrencyConflictError,
    NotFoundError,
    ServiceUnavailableError,
)
from app.application.ports.knowbots import (
    BindingChecker,
    KnowbotRecord,
    KnowbotRepository,
    KnowbotVersionRecord,
)
from app.domain.knowbots.delivery import (
    CATEGORY_CODES,
    binding_items,
    binding_problem,
)
from app.domain.knowbots.manifest import KnowbotManifest, parse_manifest

DELIVERY_ID = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
SOURCE_REF = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
OWNER = re.compile(r"^[A-Za-z0-9._:@-]{1,512}$")
VERSION = re.compile(r"^[1-9][0-9]{0,5}$")
KNOWLAB_REF = "knowlab-"  # knowlab 交付时的分身引用都这样开头
OUTCOMES = frozenset({"declined", "rejected"})
CHECK_BATCH = 200  # 一次问 knowbase 最多几份（同 knowbase 的上限）


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
        bindings: BindingChecker | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repo = repo
        # 平台开了公开（PUBLIC_MARKET_ENABLED）：公开、资质有效的专家别人也能请
        # （SDD 0018 第十一节）
        self.public_market = public_market
        # knowbase 核所绑数据（0043 第 4.3 节）；没配时带绑定的清单一律不收
        self.bindings = bindings
        self.now = now

    async def _check_bindings(
        self, owner_subject: str, parsed: KnowbotManifest, *, public: bool
    ) -> None:
        """收清单时硬查（0043 第 4.3 节「两种许可检查」）：不行回 409 带原因。"""
        if not parsed.bindings:
            return
        if self.bindings is None:
            raise ServiceUnavailableError(
                "knowbase is not configured to check the bindings",
                code="knowbase_unconfigured",
            )
        items = binding_items(
            owner_subject, [b.model_dump(mode="json") for b in parsed.bindings]
        )
        states = await self.bindings.binding_states(items)
        if len(states) != len(items):
            raise ServiceUnavailableError(
                "knowbase answered the binding check partly",
                code="knowbase_unavailable",
            )
        problem = binding_problem(states, public=public)
        if problem is not None:
            raise ConcurrencyConflictError(
                "The bound data cannot go with this expert", code=problem
            )

    async def deliver(
        self,
        *,
        delivery_id: str,
        owner_subject: str,
        knowbot_ref: str,
        manifest: object,
        request_public: bool = False,
        category: str | None = None,
    ) -> DeliveryResult:
        """knowlab 交一版。

        request_public：那边运营把这一版审成「公开」，只有这样的版本才给别人看、给别人
        请；审成自用的只给主人（Fable 评审 P2-2）。先交成自用、后转公开的同一版再交一次
        带 true：内容一样就重查许可后放开（0043 第 4.3 节「自用转公开时重查」）。
        category：平台人员选的类目（0032 待定第一项），给了就记在专家上。
        """
        _require(DELIVERY_ID, delivery_id, "delivery_id")
        _require(OWNER, owner_subject, "owner_subject")
        _require(SOURCE_REF, knowbot_ref, "knowbot_ref")
        if category is not None and category not in CATEGORY_CODES:
            raise BadRequestError("category is invalid", code="category_invalid")
        try:
            parsed = parse_manifest(manifest)
        except ValueError as exc:
            raise BadRequestError(
                "The knowbot manifest is invalid", code="manifest_invalid"
            ) from exc
        digest = parsed.digest()
        prior = await self.repo.find_delivery(delivery_id)
        if prior is None:
            await self._check_bindings(owner_subject, parsed, public=request_public)
        async with self.repo.transaction():
            prior = await self.repo.find_delivery(delivery_id)
            if prior is not None:
                bot_then = await self.repo.get_knowbot(prior.version.knowbot_id)
                # 重放要整件一样：公开与否、类目也比（knowlab 撤回后再交、单号撞上时
                # 别把自用错成公开）
                same = (
                    prior.owner_subject == owner_subject
                    and prior.source_ref == knowbot_ref
                    and prior.version.version == parsed.version
                    and prior.version.digest == digest
                    and prior.version.public_allowed == request_public
                    and (
                        category is None
                        or (bot_then is not None and bot_then.category == category)
                    )
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
            if category is not None:
                await self.repo.set_category(bot.id, category)
            existing = await self.repo.get_version(bot.id, parsed.version)
            if existing is not None:
                if (
                    request_public
                    and existing.digest == digest
                    and existing.public_allowed
                ):
                    # 自用转公开已经成了（knowlab 超时重试）：当重放，回 200
                    return DeliveryResult(
                        knowbot_id=bot.id,
                        version=parsed.version,
                        digest=digest,
                        replayed=True,
                    )
                if (
                    request_public
                    and existing.digest == digest
                    and not existing.public_allowed
                    and existing.status != "stopped"
                ):
                    # 自用转公开：同一版、同一份内容，许可上面已重查过
                    await self.repo.allow_public(bot.id, parsed.version)
                    return DeliveryResult(
                        knowbot_id=bot.id,
                        version=parsed.version,
                        digest=digest,
                        replayed=False,
                    )
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
                public_allowed=request_public,
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
        mine = bot.owner_subject == owner_subject
        # 待处理的（所绑数据失效）别人请不了，主人照样能请
        if found is None or found.status not in (
            ("active", "held") if mine else ("active",)
        ):
            raise _not_found()
        if bot.owner_subject != owner_subject and not found.public_allowed:
            raise _not_found()  # 审成自用的那一版只给主人（Fable 评审 P2-2）
        if found.manifest.get("runtime") != runtime:
            raise _not_found()
        return SummonedVersion(
            knowbot_id=bot.id, name=bot.name, tagline=bot.tagline, version=found
        )

    async def _summonable(self, subject: str, knowbot_id: str) -> KnowbotRecord:
        """自己的都能请；别人的要平台开了公开、专家公开、发布主体在这个类目上有效、
        主人的小店没被停（SDD 0018 第十一节第 7 之二小节）。"""
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
            and await self.repo.owner_shop_open(bot.owner_subject)
        ):
            return bot
        raise _not_found()

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
        免得两边对不上。knowlab 来停的也只能停 knowlab 交来的，别的当没有（Kimi 评审
        第 1 条）。
        """
        async with self.repo.transaction():
            bot = await self._owned(owner_subject, knowbot_id)
            from_knowlab = bot.source_ref.startswith(KNOWLAB_REF)
            if by_knowlab and not from_knowlab:
                raise _not_found()
            if not by_knowlab and from_knowlab:
                raise ConcurrencyConflictError(
                    "Stop this version in knowlab", code="stop_in_knowlab"
                )
            if not VERSION.fullmatch(version or ""):
                raise _not_found()
            if await self.repo.get_version(bot.id, version) is None:
                raise _not_found()
            await self.repo.set_version_status(bot.id, version, "stopped")

    async def withdraw_public(
        self,
        *,
        owner_subject: str,
        knowbot_id: str,
        version: str,
    ) -> None:
        """knowlab 收回某一版的公开（转公开撤回撞上已收下）：只撤掉这一版的公开许可，
        保留作者的自用上架（已停用的不复活；因公开许可不足待处理的回在售）。只认这个
        所有者的、knowlab 交来的；撤过的再撤照样 204。"""
        async with self.repo.transaction():
            bot = await self._owned(owner_subject, knowbot_id)
            if not bot.source_ref.startswith(KNOWLAB_REF):
                raise _not_found()
            if not VERSION.fullmatch(version or ""):
                raise _not_found()
            if await self.repo.get_version(bot.id, version) is None:
                raise _not_found()
            await self.repo.withdraw_public(bot.id, version)

    async def set_unlisted(
        self, *, owner_subject: str, knowbot_ref: str, version: str, outcome: str
    ) -> None:
        """knowlab 交来这个引用没上架的那一版的结局（SDD 0037 第三节），按版本逐条写。

        结局是终态（用户不要 / 审核没过），重发同一条无害，各版本互不覆盖，乱序到达
        也收敛（Astra 评审 F08）。版本号、结局不合规矩的回 400；分身已交付过、主人
        不是这个人的回 409。
        """
        _require(OWNER, owner_subject, "owner_subject")
        _require(SOURCE_REF, knowbot_ref, "knowbot_ref")
        if not knowbot_ref.startswith(KNOWLAB_REF):
            raise BadRequestError(
                "Only knowlab sends unlisted outcomes", code="unlisted_invalid"
            )
        if not VERSION.fullmatch(version or "") or outcome not in OUTCOMES:
            raise BadRequestError(
                "A version or outcome is invalid", code="unlisted_invalid"
            )
        async with self.repo.transaction():
            owner = await self.repo.owner_of_source(knowbot_ref)
            if owner is not None and owner != owner_subject:
                raise ConcurrencyConflictError(
                    "This knowbot belongs to another owner",
                    code="knowbot_owner_mismatch",
                )
            await self.repo.put_unlisted(
                owner_subject=owner_subject,
                source_ref=knowbot_ref,
                version=version,
                outcome=outcome,
            )

    async def check_bindings(self) -> dict[str, object]:
        """后台每 10 分钟：在售（审成公开、没停用）那几版所绑的数据还能不能经
        专家给别人用（数据拓扑第十节第 5 条）。不行的转待处理、记原因；待处理的
        好了回在售。knowbase 没配、连不上就这一轮不核，没回的那几份不据此改。"""
        if self.bindings is None:
            return {"checked": 0, "held": 0, "released": 0, "skipped": "unconfigured"}
        watched = await self.repo.watched_versions()
        wanted: dict[tuple[str, str, int, str | None], None] = {}
        per_version: list[
            tuple[KnowbotVersionRecord, list[tuple[str, str, int, str | None]]]
        ] = []
        for owner, version in watched:
            items = binding_items(owner, version.manifest.get("bindings") or [])
            per_version.append((version, items))
            wanted.update(dict.fromkeys(items))
        asked = list(wanted)
        states: dict[tuple[str, str, int, str | None], dict[str, object]] = {}
        for start in range(0, len(asked), CHECK_BATCH):
            batch = asked[start : start + CHECK_BATCH]
            try:
                answer = await self.bindings.binding_states(batch)
            except AppException as exc:
                return {"checked": 0, "held": 0, "released": 0, "skipped": exc.code}
            if len(answer) != len(batch):
                return {"checked": 0, "held": 0, "released": 0, "skipped": "partial"}
            states.update(zip(batch, answer, strict=True))
        held = released = 0
        at = self.now()
        for version, items in per_version:
            problem = binding_problem([states[i] for i in items], public=True)
            async with self.repo.transaction():
                changed = await self.repo.hold_version(
                    version.knowbot_id, version.version, problem, at
                )
            if changed and problem is not None:
                held += 1
            elif changed:
                released += 1
        return {
            "checked": len(per_version),
            "held": held,
            "released": released,
            "skipped": None,
        }

    async def source_versions(
        self, owner_subject: str, knowbot_ref: str
    ) -> list[dict[str, object]]:
        """knowlab 取回它交来的各版现在怎样（在售、待处理与原因、停用、给不给别人请），
        在 knowlab 的专家栏照实写（0043 第 4.3 节）。只认这个所有者、knowlab 交来的；
        还没交成的回空。"""
        _require(OWNER, owner_subject, "owner_subject")
        _require(SOURCE_REF, knowbot_ref, "knowbot_ref")
        if not knowbot_ref.startswith(KNOWLAB_REF):
            raise _not_found()
        bot = await self.repo.find_by_source(knowbot_ref)
        if bot is None or bot.owner_subject != owner_subject:
            return []
        return [
            {
                "version": v.version,
                "status": v.status,
                "held_reason": v.held_reason,
                "public_allowed": v.public_allowed,
            }
            for v in bot.versions
        ]
