"""供需市场（SDD 0018 第十一节）：发布主体与资质、专家与知识两种供给、需求与响应、交易、
评价、留言、驻场规则、举报；运营的审核与下架；资质到期与证件照片清理。

谁能看什么只在这里判，前端拿到什么就画什么：
- 发布主体的实名材料只给本人与运营看；证件照片只给运营看。
- 知识条目：上架的大家都看得到；卖家看得到自己的各种状态。
- 需求：开着的大家都看得到；响应只有需求方与那位响应的卖家看得到，别人只看到有几个。
- 留言：需求下的大家都看得到；响应下的只有需求方与那位卖家。
- 小店（第 7 之二小节）：首页只列有在架供给或公开需求、没被停的店；小店详情页（对外）
  与「我的小店」（自己管）是两种投影，对外的只放别人看得到的；需求在大厅里匿名，卖家
  响应了、成交时才见到店。停店期间公开列表、条目发布、审核转公开、下单、请专家都不过。
看不到的与没有一样（回「找不到」）。本期标价、记成交，不接真实支付。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.application.errors.exceptions import (
    AppException,
    BadGatewayError,
    BadRequestError,
    ConcurrencyConflictError,
    NotFoundError,
    ServiceUnavailableError,
)
from app.application.ports.knowbots import KnowbotRepository
from app.application.ports.market import KnowledgeVault, MarketRepository, Row
from app.domain.knowbots.catalog import (
    BY_CODE,
    CATEGORIES,
    DEFAULT_CATEGORY,
    EXPERT_DIRECTORY_BY_CODE,
    EXPERT_DIRECTORY_CATEGORIES,
    TOOL_BY_CODE,
    TOOL_CATEGORIES,
    category_view,
    in_category,
    matching_tool_categories,
    suggested_expert_category,
    suggested_tool_category,
)
from app.domain.knowbots.manifest import SCHEMA_V3
from app.domain.market import (
    DEFAULT_RULES,
    DOCUMENT_KEEP_DAYS,
    DOCUMENT_KINDS,
    DOCUMENT_MAX_BYTES,
    DOCUMENT_TYPES,
    LICENSE_REMIND_DAYS,
    BuyIn,
    DemandIn,
    ExpertListingIn,
    KnowledgeListingIn,
    LicenseIn,
    MessageIn,
    OfferIn,
    PublisherIn,
    ReplyIn,
    ReportDecision,
    ReportIn,
    ResponseIn,
    ReviewDecision,
    ReviewIn,
    RevokeIn,
    ShopDecision,
    ShopIn,
    default_shop_name,
    demand_open,
    flagged,
    hash_id,
    mask_id,
    shop_name_words,
)

PAGE_SIZE = 24
# 后台核资料一次问 knowbase 几份（knowbase 那头一次最多收 200）
log = logging.getLogger(__name__)
OFFER_BATCH = 200
# 小店详情页上列多少条评价；平均分按最近这么多条算
SHOP_REVIEWS = 20
SHOP_RATING_OVER = 200
UserToken = Callable[[], Awaitable[str]]


def _missing(what: str = "listing") -> NotFoundError:
    return NotFoundError(f"No such {what}", code=f"{what}_not_found")


def _conflict(message: str, code: str) -> ConcurrencyConflictError:
    return ConcurrencyConflictError(message, code=code)


def _money(value: Decimal | None) -> str | None:
    return None if value is None else f"{value:.2f}"


def _iso(value: datetime | date | None) -> str | None:
    return None if value is None else value.isoformat()


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError) as exc:
        raise _missing("item") from exc


def needs_license(category: str) -> bool:
    found = BY_CODE.get(category)
    return bool(found and found.license)


async def still_qualified(
    repo: MarketRepository,
    publisher_id: uuid.UUID | str | None,
    category: str,
    today: date,
) -> bool:
    """按登记的发布主体现核资格：主体实名审过，类目要证照的还有审过、没过期的证照。

    运营批准、买的那一刻都核（Astra 评审 F06）：提交后主体改了资料（要重审、不算
    实名）、证照到期还没等到每天扫表，都不算。
    """
    publisher = await repo.get_publisher(str(publisher_id)) if publisher_id else None
    if (
        publisher is None
        or publisher["review_status"] != "approved"
        or not publisher["real_name_verified"]
    ):
        return False
    return not needs_license(category) or await repo.has_valid_license(
        publisher["id"], category, today
    )


def _buy_digest(listing_id: str, body: BuyIn) -> str:
    """一次购买的参数摘要：同一个幂等键只认同样的参数。"""
    raw = json.dumps([str(listing_id), body.demand_id], separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def check_category(category: str) -> None:
    if category not in BY_CODE or category == DEFAULT_CATEGORY:
        raise BadRequestError("Choose a category", code="category_invalid")


def resolve_formal_categories(
    raw_category: str,
    directory_category: str | None,
    method_kind: str | None,
    title: str,
    purpose: str,
) -> tuple[str, str | None]:
    """Decouple regulated licensing category (BY_CODE) from discovery facet."""
    if raw_category == DEFAULT_CATEGORY:
        raise BadRequestError("Choose a category", code="category_invalid")
    if raw_category in BY_CODE:
        regulated = raw_category
    elif method_kind in (None, "tool") and raw_category in TOOL_BY_CODE:
        regulated = "tools.calculator"
        directory_category = directory_category or raw_category
    elif method_kind in (None, "expert") and raw_category in EXPERT_DIRECTORY_BY_CODE:
        regulated = "business.management"
        directory_category = directory_category or raw_category
    else:
        raise BadRequestError("Choose a category", code="category_invalid")

    choices = (
        (TOOL_BY_CODE | EXPERT_DIRECTORY_BY_CODE)
        if method_kind is None
        else (TOOL_BY_CODE if method_kind == "tool" else EXPERT_DIRECTORY_BY_CODE)
    )
    if directory_category is not None:
        if directory_category not in choices:
            raise BadRequestError("Unknown method category", code="category_invalid")
        resolved_dir: str | None = directory_category
    elif method_kind is None:
        resolved_dir = None
    else:
        resolved_dir = (
            suggested_tool_category(title, purpose)
            if method_kind == "tool"
            else suggested_expert_category(title, purpose)
        )
        if resolved_dir is None and raw_category in choices:
            resolved_dir = raw_category
    return regulated, resolved_dir


def is_platform(publisher: Row | None, platform_credit_code: str) -> bool:
    """这个发布主体是不是平台：看主体（审过的非个人、统一社会信用代码是部署配的平台那一个），
    不看账号（SDD 0018 第十一节第 7 之二小节；没配就没有平台店）。"""
    return bool(
        platform_credit_code
        and publisher is not None
        and publisher["kind"] == "organization"
        and publisher["review_status"] == "approved"
        and publisher["real_name_verified"]
        and str(publisher.get("credit_code") or "").upper()
        == platform_credit_code.strip().upper()
    )


def shop_closed(shop: Row | None) -> bool:
    """小店被停了（没开过店的不算）。"""
    return shop is not None and shop["status"] == "suspended"


async def take_off(repo: MarketRepository, listing_id: str, reason: str) -> bool:
    """卖家交给 knowmart 的资料没了或不再出售：用到它的待审、在架条目自动下架，原因写在
    条目上给卖家看；买家已有的照旧在（所有者 10-29）。下了回 True。"""
    async with repo.transaction():
        listing = await repo.get_listing(listing_id)
        if listing is None or listing["status"] not in ("pending", "listed"):
            return False
        await repo.update_listing(
            listing["id"], status="delisted", delist_reason=reason
        )
    return True


def formal_ref(row):
    return {
        "method_id": str(row["method_id"]),
        "version": row["version"],
        "digest": row["digest"],
    }


class MarketService:
    def __init__(
        self,
        repo: MarketRepository,
        knowbots: KnowbotRepository,
        vault: KnowledgeVault | None,
        *,
        formal_delivery=None,
        id_hash_key: str = "",
        public_market: bool = False,
        platform_credit_code: str = "",
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repo = repo
        self.knowbots = knowbots
        self.vault = vault
        self.formal_delivery = formal_delivery
        self.id_hash_key = id_hash_key
        # 平台开了公开（PUBLIC_MARKET_ENABLED）才有别人看得到的专家
        self.public_market = public_market
        self.platform_credit_code = platform_credit_code
        self.now = now

    # ================= 小店 =================
    async def ensure_shop(self, subject: str) -> Row:
        """一个用户一家小店：第一次用到时开，店名取昵称。"""
        found = await self.repo.shop_of(subject)
        if found is None:
            name = default_shop_name(await self.repo.nickname_of(subject))
            async with self.repo.transaction():
                found = await self.repo.add_shop(subject, name)
        return found

    async def _open_shop(self, subject: str) -> Row:
        """发布、交审、申请公开、发需求、响应前：自己的小店不能是停着的。"""
        shop = await self.ensure_shop(subject)
        if shop_closed(shop):
            raise _conflict("Your shop is suspended", "shop_suspended")
        return shop

    async def _shop_header(self, shop: Row, viewer: str | None) -> Row:
        """店主那一块：店名、发布主体（审过才给看）、资质标识、平台店标。"""
        publisher = await self.repo.publisher_of(shop["owner_subject"])
        shown: Row | None = None
        qualifications: list[Row] = []
        if (
            publisher is not None
            and publisher["review_status"] == "approved"
            and publisher["real_name_verified"]
        ):
            shown = public_publisher(publisher)
            qualifications = [
                category_view(c)
                for c in await self.repo.valid_license_categories(
                    publisher["id"], self.now().date()
                )
            ]
        return {
            "shop_id": str(shop["id"]),
            "name": shop["name"],
            "status": shop["status"],
            "mine": viewer is not None and shop["owner_subject"] == viewer,
            "platform": is_platform(publisher, self.platform_credit_code),
            "publisher": shown,
            "qualifications": qualifications,
        }

    async def shop_refs(self, subjects: list[str]) -> dict[str, Row]:
        """一批人各自的小店（编号、店名），给页面链过去；没开过店的不在里面。"""
        return {
            owner: shop_ref(shop)
            for owner, shop in (await self.repo.shops_of(subjects)).items()
        }

    async def shops(
        self, *, q: str | None, category: str | None, sort: str, page: int
    ) -> Row:
        """首页：小店列表。只列有在架供给或公开需求、没被停的店（所有者 10-26）；
        按店名、类目找；只按时间或店名排，不做推荐、榜单、销量排序（0043 第十一
        节）。店少，全取出来再筛；多了再下推到数据库。"""
        today = self.now().date()
        activity = _activity(
            await self.repo.shop_supply(public_market=self.public_market, today=today)
        )
        shops = await self.repo.shops_of(list(activity))
        words = (q or "").strip().lower()
        chosen = []
        for owner, act in activity.items():
            shop = shops.get(owner)
            if shop is None or shop_closed(shop):
                continue
            if words and words not in shop["name"].lower():
                continue
            if category and not any(
                in_category(c, category) for c in act["categories"]
            ):
                continue
            chosen.append((shop, act))
        if sort == "name":
            chosen.sort(key=lambda x: (x[0]["name"], str(x[0]["id"])))
        elif sort == "category":
            chosen.sort(
                key=lambda x: (
                    sorted(x[1]["categories"])[0] if x[1]["categories"] else "",
                    str(x[0]["id"]),
                )
            )
        else:
            chosen.sort(key=lambda x: (x[1]["at"], str(x[0]["id"])), reverse=True)
        start = (page - 1) * PAGE_SIZE
        items = []
        for shop, act in chosen[start : start + PAGE_SIZE]:
            items.append(
                {
                    **await self._shop_header(shop, None),
                    "experts": act["expert"],
                    "tools": act["tool"],
                    "knowledge": act["knowledge"],
                    "demands": act["demand"],
                    "categories": [category_view(c) for c in sorted(act["categories"])],
                    "updated_at": _iso(act["at"]),
                }
            )
        return {
            "items": items,
            "total": len(chosen),
            "page": page,
            "page_size": PAGE_SIZE,
        }

    async def shop(self, viewer: str, shop_id: str) -> Row:
        """小店详情页（对外）：店主、在架的专家与知识、店主勾了公开的需求、评价。只放别人
        看得到的（店主自己看也一样，看到的就是别人看到的）。停了的店只说停了。"""
        shop = await self.repo.get_shop(shop_id)
        if shop is None:
            raise _missing("shop")
        header = await self._shop_header(shop, viewer)
        if shop_closed(shop):
            return {
                "shop": header,
                "experts": [],
                "formal_methods": [],
                "knowledge": [],
                "demands": [],
                "reviews": [],
                "rating": rating_of([]),
            }
        owner = shop["owner_subject"]
        today = self.now().date()
        experts = (
            await self.repo.public_experts(owner, today) if self.public_market else []
        )
        formal_methods = (
            await self.repo.public_formal_listings_of_owner(owner, today)
            if self.public_market
            else []
        )
        knowledge, _ = await self.repo.listed_knowledge(
            q=None, category=None, limit=1000, offset=0, today=today, seller=owner
        )
        demands = [
            demand_view(d, viewer=viewer, today=today)
            for d in await self.repo.demands_of(owner)
            if d["shop_public"] and demand_open(d["status"], d["deadline"], today)
        ]
        reviews = await self.repo.reviews_of_seller(owner, SHOP_RATING_OVER)
        return {
            "shop": header,
            "experts": [
                {
                    "knowbot_id": str(b["id"]),
                    "name": b["name"],
                    "tagline": b["tagline"],
                    "category": category_view(b["category"]),
                    "version": b["version"],
                    "price": _money(b["price"]),
                }
                for b in experts
            ],
            "formal_methods": [
                {
                    "listing_id": str(f["id"]),
                    "reference": formal_ref(f),
                    "method_kind": f["method_kind"] or "expert",
                    "title": f["title"],
                    "purpose": f["purpose"],
                    "category": category_view(
                        f.get("resolved_directory_category")
                        or f.get("directory_category")
                        or f["category"],
                        tool=f.get("method_kind") == "tool",
                    ),
                    "price": _money(f.get("price")),
                }
                for f in formal_methods
            ],
            "knowledge": [listing_view(x, viewer=viewer) for x in knowledge],
            "demands": demands,
            "reviews": [
                {**review_view(r), "title": r["title"], "kind": r["kind"]}
                for r in reviews[:SHOP_REVIEWS]
            ],
            "rating": rating_of(reviews),
        }

    async def my_shop(self, subject: str) -> Row:
        """我的小店（自己管）：店名、处置状态与说明、在不在首页列表上、店主资料、留痕。
        供给、需求、响应、交易记录各有自己的接口。"""
        shop = await self.ensure_shop(subject)
        today = self.now().date()
        supply = await self.repo.shop_supply(
            public_market=self.public_market, today=today, owner=subject
        )
        reviews = await self.repo.reviews_of_seller(subject, SHOP_RATING_OVER)
        return {
            "shop": {
                **await self._shop_header(shop, subject),
                "status_note": shop["status_note"],
                "status_at": _iso(shop["status_at"]),
                "listed": bool(supply) and not shop_closed(shop),
            },
            "rating": rating_of(reviews),
            "history": [
                shop_event_view(e) for e in await self.repo.shop_events(shop["id"])
            ],
        }

    async def rename_shop(self, subject: str, body: ShopIn) -> Row:
        """改店名：受驻场规则禁止词约束；改了留痕。"""
        found = shop_name_words(body.name)
        if found:
            raise BadRequestError(
                "The shop name holds a forbidden word", code="shop_name_forbidden"
            )
        shop = await self.ensure_shop(subject)
        if body.name != shop["name"]:
            async with self.repo.transaction():
                await self.repo.update_shop(shop["id"], name=body.name)
                await self.repo.add_shop_event(
                    {
                        "shop_id": shop["id"],
                        "kind": "renamed",
                        "old_name": shop["name"],
                        "new_name": body.name,
                        "by_subject": subject,
                    }
                )
        return await self.my_shop(subject)

    # ================= 发布主体与资质 =================
    async def my_publisher(self, subject: str) -> Row:
        found = await self.repo.publisher_of(subject)
        if found is None:
            return {"publisher": None, "documents": [], "licenses": []}
        return {
            "publisher": publisher_view(found, own=True),
            "documents": [
                document_view(d) for d in await self.repo.documents_of(str(found["id"]))
            ],
            "licenses": [
                license_view(lic, today=self.now().date())
                for lic in await self.repo.licenses_of(str(found["id"]))
            ],
        }

    async def save_publisher(self, subject: str, body: PublisherIn) -> Row:
        """入驻或改资料：改了就重新审（审过之前不算实名）。"""
        fields: Row = {
            "kind": body.kind,
            "name": body.name,
            "review_status": "pending",
            "review_note": None,
            "reviewed_by": None,
            "reviewed_at": None,
            "real_name_verified": False,
        }
        if body.kind == "individual":
            assert body.real_name and body.id_number
            if not self.id_hash_key:
                raise AppException(
                    code="publisher_unconfigured",
                    status_code=503,
                    msg="publisher_unconfigured",
                )
            digest = hash_id(body.id_number, self.id_hash_key)
            if await self.repo.publisher_with_id_hash(digest, other_than=subject):
                raise _conflict(
                    "This id number is already used by another account",
                    "id_number_taken",
                )
            fields |= {
                "real_name": body.real_name,
                "id_number_masked": mask_id(body.id_number.upper()),
                "id_number_hash": digest,
                "credit_code": None,
                "legal_person": None,
                "contact_name": None,
            }
        else:
            fields |= {
                "real_name": None,
                "id_number_masked": None,
                "id_number_hash": None,
                "credit_code": (body.credit_code or "").upper(),
                "legal_person": body.legal_person,
                "contact_name": body.contact_name,
            }
        before = await self.repo.publisher_of(subject)
        await self.ensure_shop(subject)
        async with self.repo.transaction():
            saved = await self.repo.save_publisher(subject, fields)
            # 一店一主体，主体可改、要重审、留变更历史（第 7 之二小节）
            if before is None or (before["kind"], before["name"]) != (
                body.kind,
                body.name,
            ):
                await self.repo.add_publisher_change(
                    {"publisher_id": saved["id"], "kind": body.kind, "name": body.name}
                )
        return await self.my_publisher(subject)

    async def add_document(
        self, subject: str, kind: str, media_type: str, data: bytes
    ) -> Row:
        media = media_type.split(";")[0].strip().lower()
        if media not in DOCUMENT_TYPES:
            raise BadRequestError(
                "Only JPEG, PNG or PDF is taken", code="document_type_invalid"
            )
        if not data or len(data) > DOCUMENT_MAX_BYTES:
            raise BadRequestError(
                "A photo can be at most 5 MB", code="document_too_large"
            )
        async with self.repo.transaction():
            publisher = await self._publisher(subject, lock=True)
            if kind not in DOCUMENT_KINDS[publisher["kind"]]:
                raise BadRequestError(
                    "Document kind does not match the publisher",
                    code="document_kind_invalid",
                )
            found = await self.repo.add_document(
                {
                    "publisher_id": publisher["id"],
                    "kind": kind,
                    "media_type": media,
                    "size_bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "content": data,
                }
            )
        return document_view(found)

    async def add_license(self, subject: str, body: LicenseIn) -> Row:
        publisher = await self._publisher(subject)
        check_category(body.category)
        if not needs_license(body.category):
            raise BadRequestError(
                "This category needs no license", code="license_not_needed"
            )
        document = await self.repo.get_document(body.document_id)
        if (
            document is None
            or document["publisher_id"] != publisher["id"]
            or document["kind"] != "license"
        ):
            raise _missing("document")
        async with self.repo.transaction():
            found = await self.repo.add_license(
                {
                    "publisher_id": publisher["id"],
                    "category": body.category,
                    "license_name": body.license_name,
                    "license_no": body.license_no,
                    "valid_until": body.valid_until,
                    "document_id": document["id"],
                }
            )
        return license_view(found, today=self.now().date())

    async def _publisher(self, subject: str, *, lock: bool = False) -> Row:
        found = await self.repo.publisher_of(subject, lock=lock)
        if found is None:
            raise BadRequestError(
                "Register as a publisher first", code="publisher_required"
            )
        return found

    async def _qualified(self, subject: str, category: str) -> Row:
        """能公开、能上架：发布主体审过；类目要证照的，还要有审过、没过期的证照。"""
        publisher = await self._publisher(subject)
        if (
            publisher["review_status"] != "approved"
            or not publisher["real_name_verified"]
        ):
            raise BadRequestError(
                "Your publisher identity is not approved yet",
                code="publisher_not_approved",
            )
        if needs_license(category) and not await self.repo.has_valid_license(
            publisher["id"], category, self.now().date()
        ):
            raise BadRequestError(
                "This category needs an approved license", code="license_required"
            )
        return publisher

    async def _formal_version(self, owner, reference, *, expected=None, skill=None):
        if self.formal_delivery is None:
            raise ServiceUnavailableError("Formal method delivery is unavailable")
        body = await self.formal_delivery.check(owner, reference, skill)
        lock = body.get("delivery_digest")
        if (
            not isinstance(lock, str)
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", lock)
            or expected is not None
            and lock != expected
        ):
            raise BadGatewayError("Formal dependency lock differs")
        return body

    async def owner_formal_listings(self, subject, after, limit=30):
        async with self.repo.transaction():
            rows = await self.repo.owner_formal_listings(subject, after, limit)
            return {
                "regulated_categories": [
                    {**category_view(c.code), "license": bool(c.license)}
                    for c in CATEGORIES
                    if c.code != DEFAULT_CATEGORY
                ],
                "directory_categories": {
                    kind: [category_view(c.code, tool=kind == "tool") for c in choices]
                    for kind, choices in (
                        ("tool", TOOL_CATEGORIES),
                        ("expert", EXPERT_DIRECTORY_CATEGORIES),
                    )
                },
                "items": [
                    {
                        "listing_id": str(row["id"]),
                        "reference": formal_ref(row),
                        "title": row["title"],
                        "purpose": row["purpose"],
                        "suggested_category": (
                            suggested_tool_category(row["title"], row["purpose"])
                            if row.get("method_kind") == "tool"
                            else suggested_expert_category(row["title"], row["purpose"])
                        )
                        if not row.get("tool_category")
                        else None,
                        "method_kind": row.get("method_kind"),
                        "category": row["category"],
                        "editable_category": (
                            row.get("tool_category")
                            or row.get("directory_category")
                            or (
                                row["category"]
                                if row.get("method_kind") == "expert"
                                else DEFAULT_CATEGORY
                            )
                        )
                        if row.get("method_kind") in ("tool", "expert")
                        else None,
                        "price": _money(row.get("price")),
                        "incubation_scope": row.get("incubation_scope") or "none",
                        "review_note": row.get("review_note"),
                        "delist_reason": row.get("delist_reason"),
                        "scope": row["scope"],
                        "status": row["status"],
                    }
                    for row in rows[:limit]
                ],
                "next": str(rows[limit - 1]["id"]) if len(rows) > limit else None,
            }

    async def directory_labels(self, body):
        async with self.repo.transaction():
            rows = await self.repo.directory_labels(
                body.owner_subject,
                body.references,
                self.now().date(),
                self.public_market,
            )
        return {
            "subject": body.owner_subject,
            "categories": {
                kind: [category_view(c.code, tool=kind == "tool") for c in choices]
                for kind, choices in (
                    ("tool", TOOL_CATEGORIES),
                    ("expert", EXPERT_DIRECTORY_CATEGORIES),
                )
            },
            "items": [
                {
                    "reference": formal_ref(row),
                    "kind": row["method_kind"],
                    "category": category_view(
                        row["category"] or DEFAULT_CATEGORY,
                        tool=row["method_kind"] == "tool",
                    )["code"],
                }
                for row in rows
                if row["method_kind"] in ("tool", "expert")
            ],
        }

    async def classify_method(self, subject, listing_id, code):
        async with self.repo.transaction():
            row = await self.repo.formal_listing(listing_id)
            if (
                not row
                or row["owner_subject"] != subject
                or row.get("method_kind") not in ("tool", "expert")
            ):
                raise _missing("formal_listing")
            choices = (
                TOOL_BY_CODE
                if row["method_kind"] == "tool"
                else EXPERT_DIRECTORY_BY_CODE
            )
            if code not in choices:
                raise BadRequestError(
                    "Unknown method category", code="category_invalid"
                )
            await self.repo.classify_method(row, code)
            return {"code": code}

    async def permit_incubation(self, subject, listing_id, permission):
        async with self.repo.transaction():
            row = await self.repo.formal_listing(listing_id)
            if not row or row["owner_subject"] != subject or row["scope"] != "public":
                raise _missing("formal_listing")
            if row.get("method_kind") != "tool":
                raise BadRequestError(
                    "Only formal tool methods can grant incubation permission",
                    code="incubation_tool_only",
                )
            if permission.scope != "none":
                if row["status"] != "listed":
                    raise _missing("formal_listing")
                await self._formal_qualified(row)
                if permission.scope == "platform" and not is_platform(
                    await self.repo.get_publisher(str(row["publisher_id"])),
                    self.platform_credit_code,
                ):
                    raise BadRequestError(
                        "Platform-wide use requires an approved platform publisher"
                    )
            await self.repo.update_formal_listing(
                listing_id, incubation_scope=permission.scope
            )
            return {
                "listing_id": str(listing_id),
                "reference": formal_ref(row),
                "scope": permission.scope,
            }

    async def incubation_tools(self, query):
        async with self.repo.transaction():
            rows = (
                await self.repo.incubation_listings(
                    query, self.now().date(), self.platform_credit_code
                )
                if self.public_market
                else []
            )
            items = []
            codes = matching_tool_categories(query.topic_terms)
            for row in rows[: query.limit]:
                items.append(
                    {
                        "owner_subject": row["owner_subject"],
                        "reference": formal_ref(row),
                        "scope": row["incubation_scope"],
                        **(
                            {"topic_hit": row.get("tool_category") in codes}
                            if query.topic_terms
                            else {}
                        ),
                        "listing_id": str(row["id"]),
                    }
                )
            return {
                "subject": query.subject,
                "items": items,
                "next": str(rows[query.limit - 1]["id"])
                if len(rows) > query.limit
                else None,
            }

    async def submit_formal_self(self, body):
        # KnowLab sends the immutable identity and review over the deliver relation.
        ref = body.reference
        public_contract = dict(body.public_contract or {})
        domain_tags = [
            str(tag)
            for tag in (public_contract.get("domain_tags") or [])
            if isinstance(tag, str)
        ]
        default_dir = body.directory_category
        if default_dir is not None:
            choices = (
                TOOL_BY_CODE if body.method_kind == "tool" else EXPERT_DIRECTORY_BY_CODE
            )
            if default_dir not in choices:
                raise BadRequestError(
                    "Unknown method category", code="category_invalid"
                )
        elif body.method_kind == "tool":
            default_dir = suggested_tool_category(body.title, body.purpose)
        elif body.method_kind == "expert":
            default_dir = suggested_expert_category(body.title, body.purpose)
        async with self.repo.transaction():
            row = await self.repo.submit_formal_listing(
                dict(
                    id=uuid.uuid4(),
                    owner_subject=body.owner_subject,
                    method_id=ref.method_id,
                    version=ref.version,
                    digest=ref.digest,
                    delivery_digest=None,
                    method_kind=body.method_kind,
                    scope="self",
                    publisher_id=None,
                    category=DEFAULT_CATEGORY,
                    directory_category=default_dir,
                    domain_tags=domain_tags,
                    public_contract=public_contract,
                    title=body.title,
                    purpose=body.purpose,
                    status="pending" if body.review_findings else "listed",
                    reviewed_at=None if body.review_findings else self.now(),
                    review_note=", ".join(body.review_findings)
                    or "automatic self-use review",
                )
            )
            if row["digest"] != ref.digest or (
                body.method_kind and row["method_kind"] != body.method_kind
            ):
                raise _conflict(
                    "Different frozen self-use version", "formal_listing_conflict"
                )
            return {
                "listing_id": str(row["id"]),
                "reference": formal_ref(row),
                "scope": "self",
                "status": row["status"],
            }

    async def eligible_formal_refs(
        self, subject, references, *, include_acquired=False
    ):
        async with self.repo.transaction():
            rows = await self.repo.usable_formal_pins(
                subject,
                today=self.now().date(),
                public=self.public_market,
                include_acquired=include_acquired,
                references=references,
            )
        return [formal_ref(row) for row in rows]

    async def planning_window(self, query):
        async with self.repo.transaction():
            rows = await self.repo.usable_formal_pins(
                query.owner_subject,
                today=self.now().date(),
                public=self.public_market,
                include_acquired=True,
                codes=matching_tool_categories(query.topic_terms)
                if query.topic_terms
                else None,
                offset=query.offset,
                limit=query.limit + 1,
            )
        return {
            "owner_subject": query.owner_subject,
            "references": [formal_ref(row) for row in rows[: query.limit]],
            "more": len(rows) > query.limit,
        }

    async def submit_formal(self, subject, body):
        if not self.public_market:
            raise _missing("formal_listing")
        prelim_regulated, _ = resolve_formal_categories(
            body.category, body.directory_category, None, "", ""
        )
        async with self.repo.transaction():
            prior = await self.repo.formal_listing_by_pin(
                subject,
                body.reference.method_id,
                body.reference.version,
                scope="public",
            )
            if prior and prior["status"] == "taken_down":
                raise _conflict(
                    "This formal method was taken down by the platform",
                    "listing_taken_down",
                )
            await self._open_shop(subject)
            await self._qualified(subject, prelim_regulated)
        ref = body.reference.model_dump(mode="json")
        ready = await self._formal_version(subject, ref)
        if ready.get("method_kind") not in ("tool", "expert"):
            raise BadGatewayError("Missing frozen method classification")
        regulated, dir_cat = resolve_formal_categories(
            body.category,
            body.directory_category,
            ready["method_kind"],
            ready["title"],
            ready["purpose"],
        )
        public_contract = dict(ready.get("public_contract") or {})
        domain_tags = [
            str(tag)
            for tag in (public_contract.get("domain_tags") or [])
            if isinstance(tag, str)
        ]
        async with self.repo.transaction():
            await self._open_shop(subject)
            publisher = await self._qualified(subject, regulated)
            fields = dict(
                id=uuid.uuid4(),
                owner_subject=subject,
                method_id=body.reference.method_id,
                version=ref["version"],
                digest=ref["digest"],
                delivery_digest=ready["delivery_digest"],
                title=ready["title"],
                purpose=ready["purpose"],
                method_kind=ready["method_kind"],
                category=regulated,
                directory_category=dir_cat,
                domain_tags=domain_tags,
                public_contract=public_contract,
                price=body.price,
                publisher_id=publisher["id"],
                scope="public",
                status="pending",
            )
            row = await self.repo.submit_formal_listing(fields)
            if row["status"] == "taken_down":
                raise _conflict(
                    "This formal method was taken down by the platform",
                    "listing_taken_down",
                )
            if any(
                row[k] != fields[k]
                for k in (
                    "digest",
                    "delivery_digest",
                    "category",
                    "publisher_id",
                    "method_kind",
                )
            ):
                raise _conflict(
                    "Listing version already has another review scope",
                    "formal_listing_conflict",
                )
            updates: dict[str, Any] = {}
            if body.price is not None and row.get("price") != body.price:
                updates["price"] = body.price
            if body.directory_category and row.get("directory_category") != dir_cat:
                updates["directory_category"] = dir_cat
            if row["status"] == "withdrawn":
                updates["status"] = "pending"
                updates["delist_reason"] = None
            if updates:
                await self.repo.update_formal_listing(row["id"], **updates)
                row = {**row, **updates}
            return {
                "listing_id": str(row["id"]),
                "status": row["status"],
                "reference": ref,
            }

    async def _formal_qualified(self, row):
        if (
            not self.public_market
            or shop_closed(await self.repo.shop_of(row["owner_subject"]))
            or not await still_qualified(
                self.repo, row["publisher_id"], row["category"], self.now().date()
            )
        ):
            raise _missing("formal_listing")

    async def _reviewable_formal(self, operator, listing_id, approve, *, lock):
        row = await self.repo.formal_listing(listing_id, lock=lock)
        allowed = ("pending",) if approve else ("pending", "listed", "withdrawn")
        if not row or row["status"] not in allowed:
            raise _missing("formal_listing")
        if row["owner_subject"] == operator:
            raise BadRequestError(
                "An author cannot review their own listing", code="formal_self_review"
            )
        if approve and row["scope"] == "public":
            await self._formal_qualified(row)
        return row

    async def review_formal(self, operator, listing_id, decision):
        if decision.approve:
            async with self.repo.transaction():
                original = await self._reviewable_formal(
                    operator, listing_id, True, lock=False
                )
            if original["scope"] == "public":
                await self._formal_version(
                    original["owner_subject"],
                    formal_ref(original),
                    expected=original["delivery_digest"],
                )
        async with self.repo.transaction():
            row = await self._reviewable_formal(
                operator, listing_id, decision.approve, lock=True
            )
            if decision.approve and row != original:
                raise _conflict(
                    "Listing changed during review", "formal_listing_conflict"
                )
            state = "listed" if decision.approve else "rejected"
            await self.repo.update_formal_listing(
                listing_id,
                status=state,
                reviewed_by=operator,
                reviewed_at=self.now(),
                review_note=decision.note,
            )
            return {"listing_id": str(listing_id), "status": state}

    async def withdraw_formal(self, subject, listing_id):
        async with self.repo.transaction():
            row = await self.repo.formal_listing(listing_id)
            if not row or row["owner_subject"] != subject:
                raise _missing("formal_listing")
            if row["status"] == "taken_down":
                raise _conflict(
                    "Listing was taken down by the platform", "listing_taken_down"
                )
            if row["status"] == "rejected":
                raise _conflict(
                    "Listing is restricted by the operator", "formal_listing_rejected"
                )
            if row["status"] != "withdrawn":
                await self.repo.update_formal_listing(
                    listing_id, status="withdrawn", incubation_scope="none"
                )
            return {"listing_id": str(listing_id), "status": "withdrawn"}

    async def _acquirable_formal(self, subject, listing_id, runtime, *, lock):
        row = await self.repo.formal_listing(listing_id, lock=lock)
        allowed_states = ("listed", "withdrawn") if runtime else ("listed",)
        if not row or row["scope"] != "public" or row["status"] not in allowed_states:
            raise _missing("formal_listing")
        if runtime and not await self.repo.has_formal_acquisition(listing_id, subject):
            raise _missing("formal_acquisition")
        await self._formal_qualified(row)
        return row

    async def _chosen_formal_demand(
        self, buyer: str, explicit_demand_id: str | None, row: Row
    ) -> uuid.UUID | None:
        method_ref = f"{row['method_id']}@{row['version']}"
        listing_str = str(row["id"])
        expected_kinds = (
            ("formal_tool",)
            if row.get("method_kind") == "tool"
            else ("formal_expert", "expert")
        )
        if explicit_demand_id is not None:
            demand = await self.repo.get_demand(explicit_demand_id)
            if (
                demand is None
                or demand["buyer_subject"] != buyer
                or demand["status"] != "chosen"
            ):
                raise _missing("demand")
            response = (
                await self.repo.get_response(str(demand["chosen_response_id"]))
                if demand["chosen_response_id"]
                else None
            )
            if (
                response is None
                or response["listing_kind"] not in expected_kinds
                or response["listing_ref"] not in (listing_str, method_ref)
            ):
                raise BadRequestError(
                    "The chosen response did not offer this listing",
                    code="not_the_chosen_offer",
                )
            return demand["id"]
        for demand in await self.repo.demands_of(buyer):
            if demand["status"] != "chosen" or not demand["chosen_response_id"]:
                continue
            response = await self.repo.get_response(str(demand["chosen_response_id"]))
            if (
                response is not None
                and response["listing_kind"] in expected_kinds
                and response["listing_ref"] in (listing_str, method_ref)
            ):
                return demand["id"]
        return None

    async def acquire_formal(
        self, subject, listing_id, *, runtime=False, skill=None, demand_id=None
    ):
        if skill is not None and not runtime:
            raise _missing("formal_acquisition")
        async with self.repo.transaction():
            original = await self._acquirable_formal(
                subject, listing_id, runtime, lock=False
            )
        ref = formal_ref(original)
        ready = await self._formal_version(
            original["owner_subject"],
            ref,
            expected=original["delivery_digest"],
            skill=skill,
        )
        async with self.repo.transaction():
            row = await self._acquirable_formal(
                subject, listing_id, runtime, lock=not runtime
            )
            if (
                formal_ref(row) != ref
                or row["owner_subject"] != original["owner_subject"]
                or row["delivery_digest"] != original["delivery_digest"]
            ):
                raise _conflict(
                    "Listing changed during delivery", "formal_listing_conflict"
                )
            if not runtime:
                await self.repo.acquire_formal_listing(listing_id, subject)
                if subject != row["owner_subject"]:
                    matched_demand = await self._chosen_formal_demand(
                        subject, demand_id, row
                    )
                    trade_kind = (
                        "formal_tool"
                        if row.get("method_kind") == "tool"
                        else "formal_expert"
                    )
                    await self.repo.add_trade(
                        {
                            "kind": trade_kind,
                            "buyer_subject": subject,
                            "seller_subject": row["owner_subject"],
                            "listing_ref": f"{row['method_id']}@{row['version']}",
                            "title": f"{row['title']} v{row['version']}",
                            "price": row.get("price"),
                            "granted": [
                                {"name": row["title"], "item_id": str(row["id"])}
                            ],
                            "demand_id": matched_demand,
                            "seller_publisher": await self._publisher_then(
                                row["publisher_id"]
                            ),
                        }
                    )
                    if matched_demand is not None:
                        await self.repo.update_demand(matched_demand, status="closed")
            result = {
                "listing_id": str(listing_id),
                "reference": ref,
                "delivery_digest": row["delivery_digest"],
                "status": "acquired",
            }
            if skill is not None and runtime:
                package = ready.get("package")
                if not isinstance(package, dict) or package.get("method") != skill:
                    raise BadGatewayError("Different Skill delivery")
                result["package"] = package
            result["skill_dependencies"] = ready["skill_dependencies"]
            return result

    async def formal_detail(self, viewer: str, listing_id: uuid.UUID) -> Row:
        today = self.now().date()
        async with self.repo.transaction():
            row = await self.repo.formal_listing(listing_id, lock=False)
            if row is None:
                raise _missing("formal_listing")
            owned = row["owner_subject"] == viewer
            acquired = await self.repo.has_formal_acquisition(listing_id, viewer)
            shop = await self.repo.shop_of(row["owner_subject"])
            if not owned:
                if (
                    not self.public_market
                    or row["scope"] != "public"
                    or shop_closed(shop)
                    or not await still_qualified(
                        self.repo, row["publisher_id"], row["category"], today
                    )
                    or not (
                        row["status"] == "listed"
                        or (row["status"] == "withdrawn" and acquired)
                    )
                ):
                    raise _missing("formal_listing")
            versions = await self.repo.formal_versions_of_method(
                row["owner_subject"],
                row["method_id"],
                viewer,
                public=self.public_market,
                today=today,
            )
            labels = await self.repo.directory_labels(
                viewer,
                [
                    type(
                        "Pin",
                        (),
                        {
                            "method_id": row["method_id"],
                            "version": row["version"],
                            "digest": row["digest"],
                        },
                    )()
                ],
                today,
                self.public_market,
            )
            dir_code = (
                labels[0]["category"]
                if labels
                else (
                    row.get("directory_category")
                    or (
                        row["category"]
                        if row.get("method_kind") == "expert"
                        else DEFAULT_CATEGORY
                    )
                )
            )
            publisher_info = None
            if row["publisher_id"]:
                pub = await self.repo.get_publisher(str(row["publisher_id"]))
                if (
                    pub
                    and pub["review_status"] == "approved"
                    and pub["real_name_verified"]
                ):
                    lics = [
                        {
                            "name": lic["license_name"],
                            "number": lic["license_no"],
                            "valid_until": _iso(lic["valid_until"]),
                        }
                        for lic in await self.repo.licenses_of(str(pub["id"]))
                        if lic["review_status"] == "approved"
                        and (lic["valid_until"] is None or lic["valid_until"] >= today)
                    ]
                    publisher_info = {
                        **public_publisher(pub),
                        "licenses": lics,
                    }
            reviews = await self.repo.reviews_for(str(row["method_id"]))
            raw_contract = row.get("public_contract") or {}
            method_kind = row.get("method_kind") or "expert"
            stamp = (
                row.get("reviewed_at") or row.get("updated_at") or row.get("created_at")
            )
            return {
                "listing_id": str(row["id"]),
                "reference": formal_ref(row),
                "method_kind": method_kind,
                "title": row["title"],
                "purpose": row["purpose"],
                "scope": row["scope"],
                "status": row["status"],
                "owned": owned,
                "acquired": bool(acquired),
                "summonable": bool(
                    method_kind == "expert"
                    and (owned or row["status"] == "listed" or acquired)
                ),
                "category": category_view(dir_code, tool=method_kind == "tool"),
                "regulated_category": category_view(row["category"]),
                "price": _money(row.get("price")),
                "incubation_scope": row.get("incubation_scope") or "none",
                "updated_at": _iso(stamp) or "",
                "public_contract": formal_contract_view(
                    raw_contract,
                    method_kind=method_kind,
                    title=row["title"],
                    purpose=row["purpose"],
                ),
                "versions": [
                    {
                        "listing_id": str(v["id"]),
                        "version": v["version"],
                        "scope": v["scope"],
                        "status": v["status"],
                        "updated_at": _iso(
                            v.get("reviewed_at")
                            or v.get("updated_at")
                            or v.get("created_at")
                        )
                        or "",
                    }
                    for v in versions
                ],
                "shop": shop_ref(shop) if shop else None,
                "publisher": publisher_info,
                "reviews": [review_view(r) for r in reviews],
                "rating": rating_of(reviews),
            }

    async def formal_summon_return(
        self,
        viewer: str,
        listing_id: uuid.UUID,
        source: str,
        ref: str | None,
        sources: dict[str, Any],
    ) -> Row:
        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

        from app.domain.cross_app import clean_origin, known_apps, return_url

        origin = clean_origin(source, ref, known_apps=known_apps(sources))
        target = return_url(sources.get(origin.app) if origin else None, origin)
        if origin is None or target is None:
            raise BadRequestError(
                "There is no registered place to return to", code="no_return_target"
            )
        detail = await self.formal_detail(viewer, listing_id)
        if not detail["owned"]:
            await self.acquire_formal(viewer, listing_id)
        pin = detail["reference"]
        parts = urlsplit(target)
        query = [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k != "formal_method"
        ]
        query.append(("formal_method", f"{pin['method_id']}@{pin['version']}"))
        return {"return_url": urlunsplit(parts._replace(query=urlencode(query)))}

    # ================= 专家条目 =================
    async def set_expert_listing(
        self, subject: str, knowbot_id: str, body: ExpertListingIn
    ) -> Row:
        """自用：马上生效；申请公开：要资质，运营审过才公开。价格是每次请的。"""
        bot = await self.repo.get_knowbot(knowbot_id)
        if bot is None or bot["owner_subject"] != subject:
            raise _missing("knowbot")
        if body.visibility == "private":
            changes: Row = {
                "price": body.price,
                "visibility": "private",
                # 被平台下架的仍记着下架，不被「只自用」冲掉（Fable 评审 P2-10）
                "listing_status": "taken_down"
                if bot["listing_status"] == "taken_down"
                else "self",
            }
        else:
            category = body.category or bot["category"]
            check_category(category)
            await self._open_shop(subject)
            publisher = await self._qualified(subject, category)
            if bot["listing_status"] == "taken_down":
                raise _conflict(
                    "This expert was taken down by the platform", "listing_taken_down"
                )
            changes = {
                "price": body.price,
                "category": category,
                "publisher_id": publisher["id"],
                "listing_status": "public"
                if bot["visibility"] == "public" and category == bot["category"]
                else "pending",
            }
            if category != bot["category"]:
                changes.update(
                    visibility="private",
                    listing_review_note=None,
                    listing_reviewed_by=None,
                    listing_reviewed_at=None,
                )
        async with self.repo.transaction():
            await self.repo.update_knowbot(bot["id"], **changes)
        found = await self.repo.get_knowbot(knowbot_id)
        assert found is not None
        return expert_listing_view(found)

    async def ask_public_on_delivery(self, subject: str, knowbot_id: str) -> str:
        """knowlab 交付时带了「申请公开」（SDD 0037 第十八之二节）。

        那边运营审核定了公开：发布主体与类目资质都过的，这一位进上架审核；不过的留
        自用、不报错（资质在这里审，卖家补齐后可在「我发布的」再申请）。已在审、
        已公开仍重查当前资质；被平台下架的不动。回上架状态；没进上架审核的照实回为什么：还没选
        类目回 category_needed，资质没过回 not_qualified（只在回包里，不存；Fable
        评审 P2-1），小店停着回 shop_suspended。
        """
        bot = await self.repo.get_knowbot(knowbot_id)
        if bot is None or bot["owner_subject"] != subject:
            raise _missing("knowbot")
        if bot["listing_status"] == "taken_down":
            return "taken_down"
        if shop_closed(await self.ensure_shop(subject)):
            return "shop_suspended"
        try:
            check_category(bot["category"])
        except BadRequestError:
            reason = "category_needed"
        else:
            reason = None
        publisher = None
        if reason is None:
            try:
                publisher = await self._qualified(subject, bot["category"])
            except BadRequestError:
                reason = "not_qualified"
        if reason is not None:
            if bot["listing_status"] in ("pending", "public"):
                async with self.repo.transaction():
                    await self.repo.update_knowbot(
                        bot["id"], visibility="private", listing_status="self"
                    )
            return reason
        if bot["listing_status"] in ("pending", "public"):
            return str(bot["listing_status"])
        assert publisher is not None
        async with self.repo.transaction():
            await self.repo.update_knowbot(
                bot["id"], publisher_id=publisher["id"], listing_status="pending"
            )
        return "pending"

    async def withdraw_public_on_delivery(self, subject: str, knowbot_id: str) -> str:
        """knowlab 收回某一版的公开（转公开交付撤回撞上已收下）：如果这一位再没有别的
        可公开版本，且条目还在待审或已公开（非平台下架），退回自用，不留在公开待审里。"""
        bot = await self.repo.get_knowbot(knowbot_id)
        if bot is None or bot["owner_subject"] != subject:
            raise _missing("knowbot")
        known = await self.knowbots.get_knowbot(bot["id"])

        has_public_version = known is not None and any(
            v.public_allowed and v.status != "stopped" for v in known.versions
        )
        if not has_public_version and bot["listing_status"] not in (
            "self",
            "taken_down",
        ):
            async with self.repo.transaction():
                await self.repo.update_knowbot(
                    bot["id"],
                    visibility="private",
                    listing_status="self",
                    listing_review_note=None,
                )
            return "self"
        return str(bot["listing_status"])

    async def my_experts(self, subject: str) -> list[Row]:
        """我的专家：上架状态、价格、审核说明、类目（「我发布的」里设置公开与价格）；
        待处理的那几版与原因（所绑数据失效，别人请不了，0043 第 4.3 节）。"""
        held = {
            str(bot.id): [
                {
                    "version": v.version,
                    "reason": v.held_reason,
                    "at": _iso(v.held_at),
                }
                for v in bot.versions
                if v.manifest.get("schema") == SCHEMA_V3 and v.status == "held"
            ]
            for bot in await self.knowbots.list_for_owner(subject)
            if any(v.manifest.get("schema") == SCHEMA_V3 for v in bot.versions)
        }
        return [
            {
                **expert_listing_view(bot),
                "category": category_view(bot["category"]),
                "held": held.get(str(bot["id"]), []),
            }
            for bot in await self.repo.knowbots_of(subject)
            if str(bot["id"]) in held
        ]

    async def record_summon(
        self,
        *,
        buyer: str,
        knowbot_id: str,
        version: str,
        demand_id: str | None = None,
        response_id: str | None = None,
    ) -> Row | None:
        """按买家与版本记一次成交。只有委托人显式选择的响应才能随成交结案。"""
        bot = await self.repo.get_knowbot(knowbot_id)
        if bot is None or bot["owner_subject"] == buyer:
            return None
        ref = f"{bot['id']}@{version}"
        if (
            demand_id is None
            and response_id is None
            and await self.repo.expert_trade(buyer, ref) is not None
        ):
            return None
        async with self.repo.transaction():
            if (demand_id is None) != (response_id is None):
                raise BadRequestError(
                    "A demand and its response are required together",
                    code="not_the_chosen_offer",
                )
            chosen = await self._chosen_demand(
                buyer,
                demand_id,
                "expert",
                ref,
                response_id=response_id,
                seller=bot["owner_subject"],
            )
            trade = await self.repo.add_trade(
                {
                    "kind": "expert",
                    "buyer_subject": buyer,
                    "seller_subject": bot["owner_subject"],
                    "listing_ref": f"{bot['id']}@{version}",
                    "title": f"{bot['name']} 第 {version} 版",
                    "price": bot["price"],
                    "granted": [],
                    "demand_id": chosen,
                    "seller_publisher": await self._publisher_then(bot["publisher_id"]),
                }
            )
            if chosen is not None:
                if trade["demand_id"] != chosen:
                    raise _conflict(
                        "This version already has a separate trade",
                        "expert_already_acquired",
                    )
                await self.repo.update_demand(chosen, status="closed")
            return trade_view(trade, viewer=buyer)

    async def expert_refs_bought(self, buyer: str) -> set[str]:
        """这个人请过（记了成交）的专家版本：{knowbot_id@version}。"""
        return {
            str(t["listing_ref"])
            for t in await self.repo.trades(buyer=buyer, limit=1000)
            if t["kind"] == "expert"
        }

    async def _publisher_then(self, publisher_id: uuid.UUID | None) -> Row | None:
        """成交那一刻卖的发布主体（名称、个人或非个人），记在成交上（第 7 之二
        小节）。"""
        publisher = (
            await self.repo.get_publisher(str(publisher_id)) if publisher_id else None
        )
        if publisher is None:
            return None
        return {"name": publisher["name"], "kind": publisher["kind"]}

    async def expert_terms(self, buyer: str, entries: list[Row]) -> list[Row]:
        """给「找专家」的每位补上价格与免不免费请（第 4.9 节第 3 条）：作者是他自己、
        价格
        为零或没标价、他请过这一版，都不用另付钱。本期不收钱，价格照条目上写的给他看。"""
        bought = await self.expert_refs_bought(buyer)
        out = []
        for entry in entries:
            bot = await self.repo.get_knowbot(entry["knowbot_id"])
            price = None if bot is None else bot["price"]
            free = (
                entry["owned"]
                or price is None
                or price == 0
                or f"{entry['knowbot_id']}@{entry['version']}" in bought
            )
            out.append(
                {
                    **entry,
                    "price": _money(None if price is None else Decimal(str(price))),
                    "free": bool(free),
                }
            )
        return out

    async def expert_market(self, knowbot_id: str, viewer: str | None = None) -> Row:
        """专家详情页上的市场部分：价格、评价。非本人且停店时回 404。"""
        bot = await self.repo.get_knowbot(knowbot_id)
        if bot is None:
            raise _missing("knowbot")
        shop = await self.repo.shop_of(bot["owner_subject"])
        if viewer is not None and bot["owner_subject"] != viewer and shop_closed(shop):
            raise _missing("knowbot")
        reviews = await self.repo.reviews_for(str(bot["id"]))
        return {
            "price": _money(bot["price"]),
            "listing_status": bot["listing_status"],
            "reviews": [review_view(r) for r in reviews],
            "rating": rating_of(reviews),
            # 专家在哪家小店（第 7 之二小节：跨店找到的落到对应小店）
            "shop": shop_ref(shop) if shop else None,
        }

    # ================= 知识条目 =================
    async def create_listing(
        self, subject: str, body: KnowledgeListingIn, user_token: UserToken
    ) -> Row:
        check_category(body.category)
        items = await self._items(subject, body, user_token)
        await self.ensure_shop(subject)
        async with self.repo.transaction():
            found = await self.repo.add_listing(
                {
                    "seller_subject": subject,
                    "title": body.title,
                    "category": body.category,
                    "description": body.description,
                    "items": items,
                    "sample": body.sample,
                    "rights_statement": body.rights_statement,
                    "resale_allowed": body.resale_allowed,
                    "price": body.price,
                    "status": "draft",
                }
            )
        return listing_view(found, viewer=subject)

    async def update_listing(
        self,
        subject: str,
        listing_id: str,
        body: KnowledgeListingIn,
        user_token: UserToken,
    ) -> Row:
        listing = await self._own_listing(subject, listing_id)
        if listing["status"] in ("pending", "listed", "taken_down"):
            raise _conflict("Delist it before changing it", "listing_not_editable")
        check_category(body.category)
        items = await self._items(subject, body, user_token)
        async with self.repo.transaction():
            await self.repo.update_listing(
                listing["id"],
                title=body.title,
                category=body.category,
                description=body.description,
                items=items,
                sample=body.sample,
                rights_statement=body.rights_statement,
                resale_allowed=body.resale_allowed,
                price=body.price,
                status="draft",
                review_note=None,
                delist_reason=None,
            )
        return await self.listing(subject, listing_id)

    async def submit_listing(self, subject: str, listing_id: str) -> Row:
        """交运营审：发布主体审过、类目要的证照有，才能交。"""
        listing = await self._own_listing(subject, listing_id)
        if listing["status"] not in ("draft", "rejected", "delisted"):
            raise _conflict("This listing cannot be submitted now", "listing_state")
        await self._open_shop(subject)
        publisher = await self._qualified(subject, listing["category"])
        async with self.repo.transaction():
            await self.repo.update_listing(
                listing["id"],
                status="pending",
                publisher_id=publisher["id"],
                delist_reason=None,
            )
        return await self.listing(subject, listing_id)

    async def delist_listing(self, subject: str, listing_id: str) -> Row:
        """卖家下架：买家那份照旧在。"""
        listing = await self._own_listing(subject, listing_id)
        if listing["status"] not in ("pending", "listed"):
            raise _conflict("This listing is not up", "listing_state")
        async with self.repo.transaction():
            await self.repo.update_listing(
                listing["id"], status="delisted", delist_reason=None
            )
        return await self.listing(subject, listing_id)

    async def my_listings(
        self, subject: str, user_token: UserToken | None = None
    ) -> list[Row]:
        """卖家自己的条目。带着本人令牌来时顺带核一遍待审、在架的：用到的资料不在
        knowbase 的 knowmart 栏了、或不再出售，就自动下架、写明原因（所有者 10-29）。
        knowbase 一时连不上就不核，条目照常列。"""
        listings = await self.repo.listings_of(subject)
        up = [x for x in listings if x["status"] in ("pending", "listed")]
        if up and user_token is not None and self.vault is not None:
            try:
                offered = {
                    m["material_id"]: m
                    for m in await self.vault.materials(await user_token())
                }
            except AppException:
                offered = None
            if offered is not None:
                for listing in up:
                    reason = _missing_reason(listing["items"], offered)
                    if reason is not None:
                        await take_off(self.repo, str(listing["id"]), reason)
                listings = await self.repo.listings_of(subject)
        return [listing_view(item, viewer=subject) for item in listings]

    async def knowledge_market(
        self, *, q: str | None, category: str | None, page: int
    ) -> Row:
        found, total = await self.repo.listed_knowledge(
            q=q,
            category=category,
            limit=PAGE_SIZE,
            offset=(page - 1) * PAGE_SIZE,
            today=self.now().date(),
        )
        return {
            "items": [listing_view(item, viewer=None) for item in found],
            "total": total,
            "page": page,
            "page_size": PAGE_SIZE,
        }

    async def listing(self, subject: str, listing_id: str) -> Row:
        listing = await self.repo.get_listing(listing_id)
        if listing is None:
            raise _missing()
        shop = await self.repo.shop_of(listing["seller_subject"])
        if listing["seller_subject"] != subject and (
            listing["status"] != "listed" or shop_closed(shop)
        ):
            raise _missing()
        reviews = await self.repo.reviews_for(str(listing["id"]))
        publisher = (
            await self.repo.get_publisher(str(listing["publisher_id"]))
            if listing["publisher_id"]
            else None
        )
        return {
            **listing_view(listing, viewer=subject),
            "publisher": public_publisher(publisher) if publisher else None,
            "reviews": [review_view(r) for r in reviews],
            "rating": rating_of(reviews),
            "shop": shop_ref(shop) if shop else None,
        }

    async def buy(self, subject: str, listing_id: str, body: BuyIn) -> Row:
        """买知识：记一笔成交，请 knowbase 给买家逐份建使用权（只到成交那一版，不复制；
        SDD 0043 第 4.3 节），资料进买家总栏的「买来的」。

        可恢复（Astra 评审 F07）：先落一笔「办理中」（状态 granting：要建的几份、
        成交编号定下来），逐份建、逐份记下，都建好才算成交。同一个 request_id 重来接着做
        那一笔；换了键但同样参数、上一笔还没办完的，也接着做那一笔；同一个键参数不同就拒。
        knowbase 按买家、资料与交易编号幂等，重来不会多出使用权。
        """
        digest = _buy_digest(listing_id, body)
        trade = await self.repo.trade_by_request(subject, body.request_id)
        if trade is None:
            trade = await self.repo.granting_trade(subject, digest)
            if trade is not None:
                # 换了键接手：新键也记在这笔上，以后用新键重试回的还是它（Fable P2-7）
                async with self.repo.transaction():
                    await self.repo.add_request_alias(
                        subject, body.request_id, trade["id"]
                    )
        if trade is None:
            trade = await self._start_buying(subject, listing_id, body, digest)
        if trade["request_digest"] != digest:
            raise _conflict(
                "This request id was used for another purchase", "request_reused"
            )
        if trade["status"] == "granting":
            trade = await self._grant(trade)
        return trade_view(trade, viewer=subject)

    async def _start_buying(
        self, subject: str, listing_id: str, body: BuyIn, digest: str
    ) -> Row:
        listing = await self.repo.get_listing(listing_id)
        if listing is None or listing["status"] != "listed":
            raise _missing()
        if listing["seller_subject"] == subject:
            raise BadRequestError("This is your own listing", code="own_listing")
        if not await still_qualified(
            self.repo, listing["publisher_id"], listing["category"], self.now().date()
        ) or shop_closed(await self.repo.shop_of(listing["seller_subject"])):
            raise _conflict("This listing cannot be bought now", "listing_unavailable")
        demand_id = await self._chosen_demand(
            subject, body.demand_id, "knowledge", listing_id
        )
        self._need_vault()
        async with self.repo.transaction():
            listing = await self.repo.get_listing(listing_id, lock=True)
            if listing is None or listing["status"] != "listed":
                raise _missing()
            return await self.repo.start_trade(
                {
                    "kind": "knowledge",
                    "buyer_subject": subject,
                    "seller_subject": listing["seller_subject"],
                    "listing_ref": str(listing["id"]),
                    "title": listing["title"],
                    "price": listing["price"],
                    # 资料不再按应用分区（user-data/v2），新成交不记放进哪个应用
                    "target_app": None,
                    "granted": [],
                    "demand_id": demand_id,
                    "status": "granting",
                    "resale_allowed": bool(listing["resale_allowed"]),
                    "seller_publisher": await self._publisher_then(
                        listing["publisher_id"]
                    ),
                    "request_key": body.request_id,
                    "request_digest": digest,
                    "planned": [
                        {
                            "seller_item_id": str(item["item_id"]),
                            "name": item.get("name"),
                            "sha256": item.get("sha256"),
                        }
                        for item in listing["items"]
                    ],
                }
            )

    async def _grant(self, trade: Row) -> Row:
        """接着建没建完的使用权，逐份记下；都建好记成交、关需求。"""
        vault = self._need_vault()
        for n, item in enumerate(trade["planned"]):
            async with self.repo.transaction():
                trade = await self._locked_trade(trade["id"])
                if trade["status"] != "granting":
                    return trade
                if n < len(trade["granted"]):
                    continue
                try:
                    got = await vault.purchase(
                        seller=trade["seller_subject"],
                        material_id=str(item["seller_item_id"]),
                        buyer=trade["buyer_subject"],
                        transaction_ref=f"knowmart-trade:{trade['id']}:{n}",
                        resale_allowed=trade["resale_allowed"] is True,
                    )
                except (NotFoundError, ConcurrencyConflictError) as exc:
                    if exc.code not in ("item_gone", "item_not_for_sale"):
                        raise
                    await take_off(self.repo, trade["listing_ref"], exc.code)
                    await self.repo.update_trade(
                        trade["id"], status="failed", failure=exc.code
                    )
                    return {**trade, "status": "failed", "failure": exc.code}
                sha = got.get("sha256")
                if (
                    got.get("material_id") != str(item["seller_item_id"])
                    or got.get("buyer") != trade["buyer_subject"]
                    or got.get("license")
                    != ("sale" if trade["resale_allowed"] else "self")
                    or type(got.get("version_no")) is not int
                    or got["version_no"] < 1
                    or not isinstance(sha, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", sha)
                ):
                    raise BadGatewayError(
                        "Purchase receipt differs from the requested holding",
                        code="purchase_receipt_invalid",
                    )
                granted = [
                    *trade["granted"],
                    {
                        "seller_item_id": str(item["seller_item_id"]),
                        "item_id": got["material_id"],
                        "version_no": got["version_no"],
                        "name": item.get("name"),
                        "sha256": sha,
                    },
                ]
                await self.repo.update_trade(trade["id"], granted=granted)
        async with self.repo.transaction():
            trade = await self._locked_trade(trade["id"])
            if trade["status"] == "granting":
                await self.repo.update_trade(trade["id"], status="done")
                if trade["demand_id"] is not None:
                    await self.repo.update_demand(trade["demand_id"], status="closed")
                trade = {**trade, "status": "done"}
        return trade

    async def _locked_trade(self, trade_id: uuid.UUID) -> Row:
        found = await self.repo.get_trade(trade_id, for_update=True)
        assert found is not None
        return found

    def _need_vault(self) -> KnowledgeVault:
        if self.vault is None:
            raise AppException(
                code="knowbase_unconfigured",
                status_code=503,
                msg="knowbase_unconfigured",
            )
        return self.vault

    async def materials(self, user_token: UserToken) -> list[Row]:
        """小店选资料：卖家在 knowbase 交给 knowmart 的资料（knowmart 栏）。"""
        vault = self._need_vault()
        return await vault.materials(await user_token())

    async def _items(
        self, subject: str, body: KnowledgeListingIn, user_token: UserToken
    ) -> list[Row]:
        """核每一份都是卖家交给 knowmart 的、自己的、许可是「可以出售」的（SDD 0043
        第 4.3 节）；买来、不许转卖的同样内容（下载了再传的）不能拿来卖。"""
        vault = self._need_vault()
        offered = {
            m["material_id"]: m for m in await vault.materials(await user_token())
        }
        no_resale = await self.repo.bought_without_resale(subject)
        items = []
        for ref in body.items:
            got = offered.get(ref.item_id)
            if got is None:
                raise NotFoundError(
                    "This material is not handed to knowmart", code="item_not_found"
                )
            if not got["sellable"]:
                raise BadRequestError(
                    f"{got['name']} is not licensed for sale", code="item_not_for_sale"
                )
            # 按买过的摘要：买来、不许转卖的，下载了再传的同样内容也不能卖
            if got["sha256"] in no_resale:
                raise BadRequestError(
                    f"{got['name']} was bought without the right to resell",
                    code="resale_not_allowed",
                )
            items.append(
                {
                    "item_id": ref.item_id,
                    "name": got["name"],
                    "media_type": got["media_type"],
                    "size_bytes": got["size_bytes"],
                    "sha256": got["sha256"],
                }
            )
        return items

    async def _own_listing(self, subject: str, listing_id: str) -> Row:
        listing = await self.repo.get_listing(listing_id)
        if listing is None or listing["seller_subject"] != subject:
            raise _missing()
        return listing

    # ================= 需求与响应 =================
    async def create_demand(self, subject: str, body: DemandIn) -> Row:
        if body.deadline < self.now().date():
            raise BadRequestError("The deadline has passed", code="deadline_passed")
        async with self.repo.transaction():
            found = await self.repo.add_demand(
                {
                    "buyer_subject": subject,
                    "kind": body.kind,
                    "title": body.title,
                    "problem": body.problem,
                    "data_wanted": body.data_wanted,
                    "deadline": body.deadline,
                    "budget": body.budget,
                    "status": "open",
                    "shop_public": body.public_to_shop,
                }
            )
        return demand_view(found, viewer=subject, today=self.now().date())

    async def demand_hall(
        self, *, kind: str | None, q: str | None, page: int, viewer: str
    ) -> Row:
        today = self.now().date()
        found, total = await self.repo.open_demands(
            kind=kind, q=q, today=today, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE
        )
        return {
            "items": [demand_view(d, viewer=viewer, today=today) for d in found],
            "total": total,
            "page": page,
            "page_size": PAGE_SIZE,
        }

    async def my_demands(self, subject: str) -> list[Row]:
        today = self.now().date()
        return [
            demand_view(d, viewer=subject, today=today)
            for d in await self.repo.demands_of(subject)
        ]

    async def demand(self, subject: str, demand_id: str) -> Row:
        found = await self.repo.get_demand(demand_id)
        if found is None or (
            found["status"] == "taken_down" and found["buyer_subject"] != subject
        ):
            raise _missing("demand")
        responses = await self.repo.responses_to(found["id"])
        mine = found["buyer_subject"] == subject
        shown = [r for r in responses if mine or r["seller_subject"] == subject]
        # 需求在大厅里匿名；卖家响应了才见到买家的店，买家看得到响应的卖家的店
        # （第 7 之二小节）
        refs = await self.shop_refs(
            [found["buyer_subject"], *(r["seller_subject"] for r in shown)]
        )
        return {
            **demand_view(found, viewer=subject, today=self.now().date()),
            "shop": refs.get(found["buyer_subject"]) if mine or shown else None,
            "response_count": len(
                [r for r in responses if r["status"] not in ("withdrawn", "taken_down")]
            ),
            "responses": [
                {
                    **response_view(r, viewer=subject, buyer=found["buyer_subject"]),
                    "shop": refs.get(r["seller_subject"]),
                }
                for r in shown
                if r["status"] != "taken_down" or r["seller_subject"] == subject
            ],
        }

    async def close_demand(self, subject: str, demand_id: str) -> Row:
        async with self.repo.transaction():
            found = await self.repo.get_demand(demand_id, lock=True)
            if found is None or found["buyer_subject"] != subject:
                raise _missing("demand")
            # 选定了也能关：比如线下谈完了、不在这里成交（Fable 评审 P2-9）
            if found["status"] not in ("open", "chosen"):
                raise _conflict("This demand is not open", "demand_state")
            await self.repo.update_demand(found["id"], status="closed")
        return await self.demand(subject, demand_id)

    async def respond(self, subject: str, demand_id: str, body: ResponseIn) -> Row:
        found = await self.repo.get_demand(demand_id)
        today = self.now().date()
        if found is None or found["status"] == "taken_down":
            raise _missing("demand")
        if found["buyer_subject"] == subject:
            raise BadRequestError("This is your own demand", code="own_demand")
        if not demand_open(found["status"], found["deadline"], today):
            raise _conflict("This demand no longer takes responses", "demand_closed")
        await self._open_shop(subject)
        if body.listing_kind is not None:
            assert body.listing_ref is not None
            await self._check_own_listing_ref(
                subject, body.listing_kind, body.listing_ref
            )
        try:
            async with self.repo.transaction():
                current = await self.repo.get_demand(demand_id, lock=True)
                if not current or not demand_open(
                    current["status"], current["deadline"], self.now().date()
                ):
                    raise _conflict(
                        "This demand no longer takes responses", "demand_closed"
                    )
                created = await self.repo.add_response(
                    {
                        "demand_id": found["id"],
                        "seller_subject": subject,
                        "listing_kind": body.listing_kind,
                        "listing_ref": body.listing_ref,
                        "proposal": body.proposal,
                        "price": body.price,
                        "days": body.days,
                        "status": "open",
                    }
                )
        except Exception as exc:  # 同一个卖家对同一条需求只响应一次（唯一约束）
            if "uq_demand_response_seller" in str(exc):
                raise _conflict(
                    "You already responded to this demand", "already_responded"
                ) from None
            raise
        return response_view(created, viewer=subject, buyer=found["buyer_subject"])

    async def my_responses(self, subject: str) -> list[Row]:
        """我响应过的需求：响应本身与那条需求的标题、状态。"""
        out = []
        today = self.now().date()
        for r in await self.repo.responses_of(subject):
            demand = await self.repo.get_demand(str(r["demand_id"]))
            if demand is None:
                continue
            out.append(
                {
                    **response_view(r, viewer=subject, buyer=demand["buyer_subject"]),
                    "demand": demand_view(demand, viewer=subject, today=today),
                }
            )
        return out

    async def withdraw_response(self, subject: str, response_id: str) -> Row:
        async with self.repo.transaction():
            found = await self.repo.get_response(response_id, lock=True)
            if found is None or found["seller_subject"] != subject:
                raise _missing("response")
            if found["status"] != "open":
                raise _conflict("This response cannot be withdrawn", "response_state")
            await self.repo.update_response(found["id"], status="withdrawn")
        found = await self.repo.get_response(response_id)
        assert found is not None
        demand = await self.repo.get_demand(str(found["demand_id"]))
        return response_view(
            found, viewer=subject, buyer=demand["buyer_subject"] if demand else ""
        )

    async def attach_offer(self, subject: str, response_id: str, body: OfferIn) -> Row:
        """被选中的卖家补附条目：先谈好、做好上架再给买家（Fable 评审 P2-9）。

        买家照这一条买或请，成交随即关掉需求。
        """
        found = await self.repo.get_response(response_id)
        if found is None or found["seller_subject"] != subject:
            raise _missing("response")
        if found["status"] != "chosen":
            raise _conflict(
                "Only the chosen response can attach a listing", "response_state"
            )
        await self._open_shop(subject)
        await self._check_own_listing_ref(subject, body.listing_kind, body.listing_ref)
        async with self.repo.transaction():
            await self.repo.update_response(
                found["id"],
                listing_kind=body.listing_kind,
                listing_ref=body.listing_ref,
            )
        found = await self.repo.get_response(response_id)
        assert found is not None
        demand = await self.repo.get_demand(str(found["demand_id"]))
        return response_view(
            found, viewer=subject, buyer=demand["buyer_subject"] if demand else ""
        )

    async def choose_response(
        self, subject: str, demand_id: str, response_id: str
    ) -> Row:
        """买家选一家：别的响应结束，写明买家选了别家。"""
        async with self.repo.transaction():
            found = await self.repo.get_demand(demand_id, lock=True)
            if found is None or found["buyer_subject"] != subject:
                raise _missing("demand")
            if found["status"] != "open":
                raise _conflict("This demand is not open", "demand_state")
            response = await self.repo.get_response(response_id, lock=True)
            if (
                response is None
                or response["demand_id"] != found["id"]
                or response["status"] != "open"
            ):
                raise _missing("response")
            await self.repo.update_response(response["id"], status="chosen")
            await self.repo.close_other_responses(found["id"], response["id"])
            await self.repo.update_demand(
                found["id"], status="chosen", chosen_response_id=response["id"]
            )
        return await self.demand(subject, demand_id)

    async def _check_own_listing_ref(self, subject: str, kind: str, ref: str) -> None:
        if kind == "knowledge":
            listing = await self.repo.get_listing(ref)
            if (
                listing is None
                or listing["seller_subject"] != subject
                or listing["status"] != "listed"
            ):
                raise _missing()
            return
        if kind in ("formal_expert", "formal_tool"):
            expected_kind = "tool" if kind == "formal_tool" else "expert"
            if "@" in ref:
                mid_str, _, ver_str = ref.partition("@")
                try:
                    mid, ver = uuid.UUID(mid_str), int(ver_str)
                except ValueError as exc:
                    raise _missing("formal_listing") from exc
                row = await self.repo.formal_listing_by_pin(
                    subject, mid, ver, scope="public"
                )
            else:
                row = await self.repo.formal_listing(_uuid(ref), lock=False)
            if (
                row is None
                or row["owner_subject"] != subject
                or row["scope"] != "public"
                or row["status"] != "listed"
                or row.get("method_kind") != expected_kind
            ):
                raise _missing("formal_listing")
            return
        knowbot_id, _, version = ref.partition("@")
        bot = await self.repo.get_knowbot(knowbot_id)
        if bot is None or bot["owner_subject"] != subject or not version:
            raise _missing("knowbot")
        found = await self.knowbots.get_version(bot["id"], version)
        if found is None or found.status != "active":
            raise _missing("knowbot")

    async def _chosen_demand(
        self,
        subject: str,
        demand_id: str | None,
        kind: str,
        ref: str,
        *,
        response_id: str | None = None,
        seller: str | None = None,
    ) -> uuid.UUID | None:
        """从需求响应来的成交：需求是买家的、选中的那家附的就是这一条。"""
        if demand_id is None:
            return None
        demand = await self.repo.get_demand(demand_id, lock=True)
        if demand is None or demand["buyer_subject"] != subject:
            raise _missing("demand")
        if demand["status"] != "chosen":
            if demand["status"] != "closed" or kind != "expert" or response_id is None:
                raise _missing("demand")
            receipt = await self.repo.expert_trade(subject, ref)
            if receipt is None or receipt["demand_id"] != demand["id"]:
                raise _missing("demand")
        response = (
            await self.repo.get_response(str(demand["chosen_response_id"]), lock=True)
            if demand["chosen_response_id"]
            else None
        )
        if (
            response is None
            or response["status"] != "chosen"
            or (response_id is not None and str(response["id"]) != response_id)
            or (seller is not None and response["seller_subject"] != seller)
            or (response["listing_kind"], response["listing_ref"]) != (kind, ref)
        ):
            raise BadRequestError(
                "The chosen response did not offer this listing",
                code="not_the_chosen_offer",
            )
        return demand["id"]

    # ================= 交易、评价 =================
    async def my_trades(self, subject: str, role: str) -> list[Row]:
        found = await self.repo.trades(
            buyer=subject if role == "buyer" else None,
            seller=subject if role == "seller" else None,
        )
        refs = await self.shop_refs([t["seller_subject"] for t in found])
        out = []
        for trade in found:
            review = await self.repo.get_review(trade["id"])
            state = await self._listing_state(trade)
            out.append(
                {
                    **trade_view(trade, viewer=subject),
                    "review": review_view(review) if review else None,
                    # 小店「交易记录」：卖家的店、条目现在还在不在架（专家：还请
                    # 不请得到）（第 7 之二小节；直达链接由页面按 kind、granted、
                    # listing_ref 拼）
                    "shop": refs.get(trade["seller_subject"]),
                    "listing_state": "off" if state == "retired" else state,
                    **(
                        {"expert_link_available": state not in ("retired", "revoked")}
                        if trade["kind"] in ("expert", "formal_expert", "formal_tool")
                        else {}
                    ),
                }
            )
        return out

    async def _listing_state(self, trade: Row) -> str:
        """成交的那一条现在：on（知识在架、专家这一版别人请得到）/ off / revoked。"""
        if trade.get("failure") == "violation_taken_down":
            return "revoked"
        today = self.now().date()
        if trade["kind"] == "knowledge":
            listing = await self.repo.get_listing(str(trade["listing_ref"]))
            up = (
                listing is not None
                and listing["status"] == "listed"
                and await still_qualified(
                    self.repo, listing["publisher_id"], listing["category"], today
                )
                and not shop_closed(await self.repo.shop_of(listing["seller_subject"]))
            )
            return "on" if up else "off"
        if trade["kind"] in ("formal_expert", "formal_tool"):
            mid_str, _, ver_str = str(trade["listing_ref"]).partition("@")
            try:
                mid, ver = uuid.UUID(mid_str), int(ver_str)
            except ValueError:
                return "retired"
            row = await self.repo.formal_listing_by_pin(
                trade["seller_subject"], mid, ver, scope="public"
            )
            if row is None:
                return "retired"
            up = (
                self.public_market
                and row["status"] == "listed"
                and await still_qualified(
                    self.repo, row["publisher_id"], row["category"], today
                )
                and not shop_closed(await self.repo.shop_of(row["owner_subject"]))
            )
            return "on" if up else "off"
        knowbot_id, _, version = str(trade["listing_ref"]).partition("@")
        bot = await self.repo.get_knowbot(knowbot_id)
        if bot is None or not version:
            return "retired"
        found = await self.knowbots.get_version(bot["id"], version)
        if found is None or found.manifest.get("schema") != SCHEMA_V3:
            return "retired"
        up = (
            self.public_market
            and found is not None
            and found.status == "active"
            and found.public_allowed
            and bot["visibility"] == "public"
            and bot["listing_status"] == "public"
            and await still_qualified(
                self.repo, bot["publisher_id"], bot["category"], today
            )
            and not shop_closed(await self.repo.shop_of(bot["owner_subject"]))
        )
        return "on" if up else "off"

    async def review(self, subject: str, trade_id: str, body: ReviewIn) -> Row:
        trade = await self.repo.get_trade(trade_id)
        if trade is None or trade["buyer_subject"] != subject:
            raise _missing("trade")
        if await self.repo.get_review(trade["id"]) is not None:
            raise _conflict("This trade is already reviewed", "already_reviewed")
        async with self.repo.transaction():
            found = await self.repo.add_review(
                {"trade_id": trade["id"], "rating": body.rating, "text": body.text}
            )
        return review_view(found)

    async def reply(self, subject: str, trade_id: str, body: ReplyIn) -> Row:
        trade = await self.repo.get_trade(trade_id)
        if trade is None or trade["seller_subject"] != subject:
            raise _missing("trade")
        review = await self.repo.get_review(trade["id"])
        if review is None:
            raise _missing("review")
        if review["reply"] is not None:
            raise _conflict("You already replied", "already_replied")
        async with self.repo.transaction():
            await self.repo.update_review(
                trade["id"], reply=body.reply, replied_at=self.now()
            )
        found = await self.repo.get_review(trade["id"])
        assert found is not None
        return review_view(found)

    # ================= 留言 =================
    async def messages(
        self, subject: str, thread_kind: str, thread_id: str
    ) -> list[Row]:
        parties = await self._thread(subject, thread_kind, thread_id)
        return [
            message_view(m, viewer=subject, parties=parties)
            for m in await self.repo.messages(thread_kind, _uuid(thread_id))
        ]

    async def post_message(
        self, subject: str, thread_kind: str, thread_id: str, body: MessageIn
    ) -> Row:
        parties = await self._thread(subject, thread_kind, thread_id)
        async with self.repo.transaction():
            found = await self.repo.add_message(
                {
                    "thread_kind": thread_kind,
                    "thread_id": _uuid(thread_id),
                    "author_subject": subject,
                    "body": body.body,
                }
            )
        return message_view(found, viewer=subject, parties=parties)

    async def _thread(self, subject: str, thread_kind: str, thread_id: str) -> Row:
        """（需求方，响应的卖家）：需求下的大家都能看能说；响应下的只有这两位。"""
        if thread_kind == "demand":
            demand = await self.repo.get_demand(thread_id)
            if demand is None or demand["status"] == "taken_down":
                raise _missing("demand")
            return {"buyer": demand["buyer_subject"], "seller": None}
        if thread_kind == "response":
            response = await self.repo.get_response(thread_id)
            demand = (
                await self.repo.get_demand(str(response["demand_id"]))
                if response
                else None
            )
            if response is None or demand is None:
                raise _missing("response")
            if subject not in (demand["buyer_subject"], response["seller_subject"]):
                raise _missing("response")
            return {
                "buyer": demand["buyer_subject"],
                "seller": response["seller_subject"],
            }
        raise _missing("thread")

    # ================= 驻场规则、举报 =================
    async def rules(self) -> Row:
        found = await self.repo.current_rules()
        if found is None:
            return {"version": 0, "body": DEFAULT_RULES, "created_at": None}
        return {
            "version": found["version"],
            "body": found["body"],
            "created_at": _iso(found["created_at"]),
        }

    async def report(self, subject: str, body: ReportIn) -> Row:
        async with self.repo.transaction():
            found = await self.repo.add_report(
                {
                    "reporter_subject": subject,
                    "target_kind": body.target_kind,
                    "target_id": body.target_id,
                    "reason": body.reason,
                    "status": "open",
                }
            )
        return {"report_id": str(found["id"]), "status": found["status"]}


class MarketAdmin:
    """运营：审发布主体、资质、条目；处理举报；改驻场规则；看成交；资质到期与照片清理。"""

    def __init__(
        self,
        repo: MarketRepository,
        *,
        vault: KnowledgeVault | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        platform_credit_code: str = "",
    ) -> None:
        self.repo = repo
        self.vault = vault
        self.now = now
        self.platform_credit_code = platform_credit_code

    # ------ 小店的处置（SDD 0018 第十一节第 7 之二小节；数据拓扑第十一节） ------
    async def shops(self, *, q: str | None, status: str | None, page: int) -> list[Row]:
        """全部小店（含没东西、停了的），按最近改动排；带留痕。"""
        out = []
        for shop in await self.repo.all_shops(
            q=q, status=status, limit=PAGE_SIZE * 4, offset=(page - 1) * PAGE_SIZE * 4
        ):
            publisher = await self.repo.publisher_of(shop["owner_subject"])
            out.append(
                {
                    **shop_ref(shop),
                    "owner_subject": shop["owner_subject"],
                    "status": shop["status"],
                    "status_note": shop["status_note"],
                    "status_at": _iso(shop["status_at"]),
                    "publisher": publisher_view(publisher, own=False)
                    if publisher
                    else None,
                    "history": [
                        shop_event_view(e, for_admin=True)
                        for e in await self.repo.shop_events(shop["id"], limit=20)
                    ],
                }
            )
        return out

    async def suspend_shop(
        self, shop_id: str, body: ShopDecision, reviewer: str
    ) -> None:
        """停店：持久的状态，停着时公开列表、条目发布、审核转公开、下单、请专家都不过；
        条目自己的状态不动，历史订单不撤（买家已有的照旧）。"""
        await self._set_shop(shop_id, "suspended", body.note, reviewer)

    async def restore_shop(
        self, shop_id: str, body: ShopDecision, reviewer: str
    ) -> None:
        """恢复：只恢复店；被单独下架的条目照旧下架（停店时本来就没动条目）。"""
        await self._set_shop(shop_id, "open", body.note, reviewer)

    async def _set_shop(
        self, shop_id: str, status: str, note: str, reviewer: str
    ) -> None:
        shop = await self.repo.get_shop(shop_id)
        if shop is None:
            raise _missing("shop")
        if shop["status"] == status:
            raise _conflict("The shop is already so", "shop_state")
        async with self.repo.transaction():
            await self.repo.update_shop(
                shop["id"],
                status=status,
                status_note=note,
                status_by=reviewer,
                status_at=self.now(),
            )
            await self.repo.add_shop_event(
                {
                    "shop_id": shop["id"],
                    "kind": "suspended" if status == "suspended" else "restored",
                    "note": note,
                    "by_subject": reviewer,
                }
            )

    async def _shop_open(self, subject: str) -> None:
        if shop_closed(await self.repo.shop_of(subject)):
            raise _conflict("The shop is suspended", "shop_suspended")

    # ---------- 资料还在不在（后台定时核，「待你决定」第 7 条） ----------
    async def check_offers(self) -> Row:
        """待审、在架的知识条目，用到的资料在 knowbase 没了或不再出售的，自动下架、写明
        原因（同卖家打开「我发布的」时那一核，只是不等卖家来）。后台没有卖家本人的令牌，
        以服务身份成批问 knowbase；knowbase 没配、连不上就这一轮不核，下一轮再来。"""
        if self.vault is None:
            return {"checked": 0, "delisted": 0, "skipped": "knowbase_unconfigured"}
        await self._finish_revocations(await self.repo.pending_purchase_revocations())
        listings = await self.repo.listings_up()
        pairs = list(
            dict.fromkeys(
                (str(x["seller_subject"]), str(i.get("item_id")))
                for x in listings
                for i in x["items"]
            )
        )
        states: dict[tuple[str, str], str] = {}
        valid_pairs = [
            (s, m) for s, m in pairs if s and m and len(s) <= 255 and len(m) <= 64
        ]
        skipped = None
        for start in range(0, len(valid_pairs), OFFER_BATCH):
            batch = valid_pairs[start : start + OFFER_BATCH]
            try:
                states |= await self.vault.offer_states(batch)
            except AppException as exc:
                skipped = exc.code
                if exc.code == "knowbase_unavailable":
                    break
                # 这一批这一轮不算数（不下架），下一轮再核；记下来，不悄悄跳过
                log.warning(
                    "offer_check_batch_refused code=%s items=%d", exc.code, len(batch)
                )
                continue
        checked = delisted = 0
        for listing in listings:
            complete = bool(listing["items"]) and all(
                (str(listing["seller_subject"]), str(item.get("item_id"))) in states
                for item in listing["items"]
            )
            reason = _offer_reason(listing, states)
            if complete or reason is not None:
                checked += 1
            else:
                skipped = skipped or "offer_state_missing"
            if reason is not None and await take_off(
                self.repo, str(listing["id"]), reason
            ):
                delisted += 1
        return {"checked": checked, "delisted": delisted, "skipped": skipped}

    async def pending_formal(self, page):
        rows = await self.repo.pending_formal_listings(
            (page - 1) * PAGE_SIZE, PAGE_SIZE
        )
        return [
            {
                "listing_id": str(r["id"]),
                "reference": formal_ref(r),
                "category": r["category"],
                "publisher_id": str(r["publisher_id"]) if r["publisher_id"] else None,
                "scope": r["scope"],
                "review_note": r["review_note"],
                "title": r["title"],
                "purpose": r["purpose"],
            }
            for r in rows
        ]

    async def queue(self) -> Row:
        today = self.now().date()
        publishers = []
        for p in await self.repo.pending_publishers():
            matches_platform = bool(
                self.platform_credit_code
                and str(p.get("credit_code") or "").strip().upper()
                == self.platform_credit_code.strip().upper()
            )
            publishers.append(
                {
                    **publisher_view(p, own=True),
                    "matches_platform_credit_code": matches_platform,
                    "documents": [
                        document_view(d)
                        for d in await self.repo.documents_of(str(p["id"]))
                    ],
                    # 主体改过的，重审时看得到改之前叫什么（第 7 之二小节）
                    "history": [
                        {
                            "kind": c["kind"],
                            "name": c["name"],
                            "at": _iso(c["created_at"]),
                        }
                        for c in await self.repo.publisher_changes(str(p["id"]))
                    ],
                }
            )
        experts = []
        for bot in await self.repo.pending_experts():
            experts.append(
                {
                    **expert_listing_view(bot),
                    "category": category_view(bot["category"]),
                    "flagged": flagged(bot["name"], bot["tagline"]),
                }
            )
        # 审过的：能撤销（Fable 评审 P2-8）
        approved = []
        for p in await self.repo.approved_publishers():
            approved.append(
                {
                    **publisher_view(p, own=True),
                    "licenses": [
                        license_view(lic, today=today)
                        for lic in await self.repo.licenses_of(str(p["id"]))
                        if lic["review_status"] == "approved"
                    ],
                }
            )
        return {
            "publishers": publishers,
            "approved_publishers": approved,
            "licenses": [
                license_view(lic, today=today)
                for lic in await self.repo.pending_licenses()
            ],
            "knowledge": [
                {
                    **listing_view(item, viewer=None, admin=True),
                    "flagged": flagged(
                        item["title"], item["description"], item["sample"]
                    ),
                }
                for item in await self.repo.pending_listings()
            ],
            "experts": experts,
            "formal_listings": [
                {
                    "listing_id": str(r["id"]),
                    "reference": formal_ref(r),
                    "method_kind": r.get("method_kind") or "expert",
                    "scope": r["scope"],
                    "title": r["title"],
                    "purpose": r["purpose"],
                    "category": category_view(r["category"]),
                    "directory_category": category_view(
                        r.get("directory_category")
                        or (
                            r["category"]
                            if r.get("method_kind") == "expert"
                            else DEFAULT_CATEGORY
                        ),
                        tool=r.get("method_kind") == "tool",
                    ),
                    "price": _money(r.get("price")),
                    "publisher_id": str(r["publisher_id"])
                    if r.get("publisher_id")
                    else None,
                    "review_note": r.get("review_note"),
                    "public_contract": formal_contract_view(
                        r.get("public_contract") or {},
                        method_kind=r.get("method_kind") or "expert",
                        title=r["title"],
                        purpose=r["purpose"],
                    ),
                    "flagged": flagged(
                        r["title"],
                        r["purpose"],
                        *[
                            str(x)
                            for x in (
                                (r.get("public_contract") or {}).get("applies_when")
                                or ()
                            )
                        ],
                        *[
                            str(x)
                            for x in (
                                (r.get("public_contract") or {}).get("excludes") or ()
                            )
                        ],
                        *[
                            str(x)
                            for x in (
                                (
                                    (r.get("public_contract") or {}).get("promises")
                                    or {}
                                ).values()
                            )
                        ],
                    ),
                }
                for r in await self.repo.pending_formal_listings(0, PAGE_SIZE * 4)
            ],
        }

    async def document(self, document_id: str) -> Row:
        found = await self.repo.get_document(document_id)
        if found is None or found["content"] is None:
            raise _missing("document")
        return found

    async def decide_publisher(
        self, publisher_id: str, decision: ReviewDecision, reviewer: str
    ) -> None:
        found = await self.repo.get_publisher(publisher_id)
        if found is None or found["review_status"] != "pending":
            raise _missing("publisher")
        now = self.now()
        async with self.repo.transaction():
            await self.repo.review_publisher(
                publisher_id,
                review_status="approved" if decision.approve else "rejected",
                real_name_verified=decision.approve,
                review_note=decision.note or None,
                reviewed_by=reviewer,
                reviewed_at=now,
            )
            documents = await self.repo.documents_of(publisher_id)
            await self.repo.keep_documents_until(
                [d["id"] for d in documents if d["kind"] != "license"],
                now + timedelta(days=DOCUMENT_KEEP_DAYS),
            )

    async def decide_license(
        self, license_id: str, decision: ReviewDecision, reviewer: str
    ) -> None:
        found = await self.repo.get_license(license_id)
        if found is None or found["review_status"] != "pending":
            raise _missing("license")
        now = self.now()
        async with self.repo.transaction():
            await self.repo.review_license(
                license_id,
                review_status="approved" if decision.approve else "rejected",
                review_note=decision.note or None,
                reviewed_by=reviewer,
                reviewed_at=now,
            )
            if found["document_id"]:
                await self.repo.keep_documents_until(
                    [found["document_id"]], now + timedelta(days=DOCUMENT_KEEP_DAYS)
                )

    async def revoke_publisher(
        self, publisher_id: str, body: RevokeIn, reviewer: str
    ) -> None:
        """撤销审过的发布主体（证件伪造、资格没了）：即刻不算实名，公开的专家改回自用、
        在架或在审的知识下架；现核的几处随即都不过（Fable 评审 P2-8）。卖家可改资料
        重审。
        """
        found = await self.repo.get_publisher(publisher_id)
        if found is None or found["review_status"] != "approved":
            raise _missing("publisher")
        async with self.repo.transaction():
            await self.repo.review_publisher(
                publisher_id,
                review_status="revoked",
                real_name_verified=False,
                review_note=body.note,
                reviewed_by=reviewer,
                reviewed_at=self.now(),
            )
            await self._withdraw(found["id"], None, f"发布主体资格已撤销：{body.note}")

    async def revoke_license(
        self, license_id: str, body: RevokeIn, reviewer: str
    ) -> None:
        """撤销审过的证照：这个类目没有别的有效证照了，就把这个类目下公开的专家改回自用、
        在架或在审的知识下架（Fable 评审 P2-8）。"""
        found = await self.repo.get_license(license_id)
        if found is None or found["review_status"] != "approved":
            raise _missing("license")
        async with self.repo.transaction():
            await self.repo.review_license(
                license_id,
                review_status="revoked",
                review_note=body.note,
                reviewed_by=reviewer,
                reviewed_at=self.now(),
            )
            if not await self.repo.has_valid_license(
                found["publisher_id"], found["category"], self.now().date()
            ):
                await self._withdraw(
                    found["publisher_id"], found["category"], f"资质已撤销：{body.note}"
                )

    async def _withdraw(
        self, publisher_id: uuid.UUID, category: str | None, note: str
    ) -> None:
        for bot in await self.repo.public_experts_of(publisher_id, category):
            await self.repo.update_knowbot(
                bot["id"],
                visibility="private",
                listing_status="self",
                listing_review_note=note[:2000],
            )
        for item in await self.repo.listed_by_publisher(publisher_id, category):
            await self.repo.update_listing(
                item["id"], status="delisted", review_note=note[:2000]
            )
        for formal in await self.repo.public_formal_by_publisher(
            publisher_id, category
        ):
            await self.repo.update_formal_listing(
                formal["id"],
                status="withdrawn",
                incubation_scope="none",
                delist_reason="qualification_revoked",
                review_note=note[:2000],
            )

    async def decide_listing(
        self, listing_id: str, decision: ReviewDecision, reviewer: str
    ) -> None:
        found = await self.repo.get_listing(listing_id)
        if found is None or found["status"] != "pending":
            raise _missing()
        if decision.approve:
            await self._shop_open(found["seller_subject"])
        if decision.approve and not await still_qualified(
            self.repo, found["publisher_id"], found["category"], self.now().date()
        ):
            raise _conflict(
                "The seller is not qualified for this category now",
                "publisher_not_qualified",
            )
        async with self.repo.transaction():
            await self.repo.update_listing(
                found["id"],
                status="listed" if decision.approve else "rejected",
                review_note=decision.note or None,
                reviewed_by=reviewer,
                reviewed_at=self.now(),
            )

    async def decide_expert(
        self, knowbot_id: str, decision: ReviewDecision, reviewer: str
    ) -> None:
        found = await self.repo.get_knowbot(knowbot_id)
        if found is None or found["listing_status"] != "pending":
            raise _missing("knowbot")
        if decision.approve:
            await self._shop_open(found["owner_subject"])
        if decision.approve and not await still_qualified(
            self.repo, found["publisher_id"], found["category"], self.now().date()
        ):
            raise _conflict(
                "The publisher is not qualified for this category now",
                "publisher_not_qualified",
            )
        changes: Row = {
            "listing_status": "public" if decision.approve else "rejected",
            "listing_review_note": decision.note or None,
            "listing_reviewed_by": reviewer,
            "listing_reviewed_at": self.now(),
        }
        if decision.approve:
            changes["visibility"] = "public"
        async with self.repo.transaction():
            await self.repo.update_knowbot(found["id"], **changes)

    async def take_down(
        self,
        kind: str,
        target_id: str,
        note: str,
        reviewer: str,
        *,
        violation: bool = False,
    ) -> None:
        """平台下架：知识买家那份照旧在；专家下架后不能再请（在跑的跑完）。"""
        async with self.repo.transaction():
            await self._take_down(kind, target_id, note, reviewer, violation=violation)

        if kind == "knowledge" and violation and self.vault is not None:
            await self._finish_revocations(
                await self.repo.trades_for_listing(target_id)
            )

    async def _finish_revocations(self, trades):
        for trade in trades:
            if trade.get("failure") != "violation_revoke_pending":
                continue
            try:
                for n, item in enumerate(trade["planned"] or trade["granted"]):
                    await self.vault.revoke_purchase(
                        seller=trade["seller_subject"],
                        buyer=trade["buyer_subject"],
                        material_id=str(item["seller_item_id"]),
                        transaction_ref=f"knowmart-trade:{trade['id']}:{n}",
                    )
            except AppException as exc:
                log.warning(
                    "purchase_revocation_pending trade=%s reason=%s",
                    trade["id"],
                    exc.code,
                )
                # Rotate failed requests behind the rest of the pending batch.
                async with self.repo.transaction():
                    current = await self.repo.get_trade(trade["id"], for_update=True)
                    if current and current["failure"] == "violation_revoke_pending":
                        await self.repo.update_trade(
                            trade["id"],
                            failure="violation_revoke_pending",
                            revoke_attempted_at=self.now(),
                        )
                continue
            async with self.repo.transaction():
                await self.repo.update_trade(
                    trade["id"], failure="violation_taken_down"
                )

    async def _take_down(
        self,
        kind: str,
        target_id: str,
        note: str,
        reviewer: str,
        *,
        violation: bool = False,
    ) -> None:
        now = self.now()
        if kind == "knowledge":
            found = await self.repo.get_listing(target_id, lock=True)
            if found is None:
                raise _missing()
            review_note = f"违规下架：{note}" if violation else note
            await self.repo.update_listing(
                found["id"],
                status="taken_down",
                review_note=review_note,
                reviewed_by=reviewer,
                reviewed_at=now,
            )
            if violation:
                # 违规下架收回买家使用权留痕
                trades = await self.repo.trades_for_listing(str(found["id"]))
                for t in trades:
                    trade = await self.repo.get_trade(t["id"], for_update=True)
                    if trade["failure"] != "violation_taken_down":
                        await self.repo.update_trade(
                            t["id"],
                            failure="violation_revoke_pending",
                            status="failed"
                            if trade["status"] == "granting"
                            else trade["status"],
                        )
        elif kind == "expert":
            bot = await self.repo.get_knowbot(target_id.partition("@")[0])
            if bot is None:
                raise _missing("knowbot")
            await self.repo.update_knowbot(
                bot["id"],
                visibility="private",
                listing_status="taken_down",
                listing_review_note=note,
                listing_reviewed_by=reviewer,
                listing_reviewed_at=now,
            )
        elif kind == "formal_method":
            formal = await self.repo.formal_listing(_uuid(target_id))
            if formal is None:
                raise _missing("formal_listing")
            review_note = f"违规下架：{note}" if violation else note
            await self.repo.update_formal_listing(
                formal["id"],
                status="taken_down",
                incubation_scope="none",
                delist_reason="violation" if violation else "operator_takedown",
                review_note=review_note,
                reviewed_by=reviewer,
                reviewed_at=now,
            )
            if violation:
                prefix = f"{formal['method_id']}@{formal['version']}"
                for t in await self.repo.trades_for_formal(prefix):
                    await self.repo.update_trade(
                        t["id"], failure="violation_taken_down"
                    )
        elif kind == "demand":
            demand = await self.repo.get_demand(target_id)
            if demand is None:
                raise _missing("demand")
            await self.repo.update_demand(demand["id"], status="taken_down")
        elif kind == "response":
            response = await self.repo.get_response(target_id)
            if response is None:
                raise _missing("response")
            await self.repo.update_response(response["id"], status="taken_down")
        elif kind == "message":
            message = await self.repo.get_message(target_id)
            if message is None:
                raise _missing("message")
            await self.repo.hide_message(message["id"])
        else:
            raise _missing("item")

    async def reports(self, status: str | None) -> list[Row]:
        return [report_view(r) for r in await self.repo.reports(status)]

    async def decide_report(
        self, report_id: str, decision: ReportDecision, reviewer: str
    ) -> None:
        async with self.repo.transaction():
            found = await self.repo.get_report(report_id, for_update=True)
            if found is None or found["status"] != "open":
                raise _missing("report")
            if decision.uphold:
                await self._take_down(
                    found["target_kind"],
                    found["target_id"],
                    decision.note or found["reason"],
                    reviewer,
                )
            await self.repo.update_report(
                found["id"],
                status="upheld" if decision.uphold else "dismissed",
                note=decision.note or None,
                handled_by=reviewer,
                handled_at=self.now(),
            )

    async def rules(self) -> Row:
        found = await self.repo.current_rules()
        if found is None:
            return {"version": 0, "body": DEFAULT_RULES, "created_at": None}
        return {
            "version": found["version"],
            "body": found["body"],
            "created_at": _iso(found["created_at"]),
        }

    async def publish_rules(self, body: str, reviewer: str) -> Row:
        async with self.repo.transaction():
            found = await self.repo.add_rules(body, reviewer)
        return {"version": found["version"], "created_at": _iso(found["created_at"])}

    async def trades(self, page: int) -> list[Row]:
        return [
            trade_view(t, viewer=None)
            for t in await self.repo.trades(
                granting=True, limit=PAGE_SIZE * 4, offset=(page - 1) * PAGE_SIZE * 4
            )
        ]

    async def sweep(self) -> Row:
        """资质到期与照片清理（定时任务每天跑）。

        到期前 30 天提醒（记下提醒过）；过期的：相关公开专家改回自用、知识条目下架；
        审过一年的证件照片清掉内容。
        """
        now = self.now()
        today = now.date()
        reminded = 0
        expired = 0
        async with self.repo.transaction():
            for lic in await self.repo.licenses_expiring(
                today + timedelta(days=LICENSE_REMIND_DAYS)
            ):
                await self.repo.review_license(str(lic["id"]), reminded_at=now)
                reminded += 1
            for lic in await self.repo.licenses_expired(today):
                if await self.repo.has_valid_license(
                    lic["publisher_id"], lic["category"], today
                ):
                    continue  # 换了新证照的不动
                for bot in await self.repo.public_experts_of(
                    lic["publisher_id"], lic["category"]
                ):
                    await self.repo.update_knowbot(
                        bot["id"],
                        visibility="private",
                        listing_status="self",
                        listing_review_note="资质已过期，改回自用",
                    )
                    expired += 1
                for item in await self.repo.listed_by_publisher(
                    lic["publisher_id"], lic["category"]
                ):
                    await self.repo.update_listing(
                        item["id"], status="delisted", review_note="资质已过期，已下架"
                    )
                    expired += 1
                for formal in await self.repo.public_formal_by_publisher(
                    lic["publisher_id"], lic["category"]
                ):
                    await self.repo.update_formal_listing(
                        formal["id"],
                        status="withdrawn",
                        incubation_scope="none",
                        delist_reason="license_expired",
                        review_note="资质已过期，已撤回公开",
                    )
                    expired += 1
            purged = await self.repo.purge_documents(now)
        return {"reminded": reminded, "expired": expired, "purged": purged}


# ================= 给页面的样子 =================


def publisher_view(p: Row, *, own: bool) -> Row:
    view: Row = {
        "publisher_id": str(p["id"]),
        "kind": p["kind"],
        "name": p["name"],
        "review_status": p["review_status"],
        "review_note": p["review_note"],
        "real_name_verified": bool(p["real_name_verified"]),
        "reviewed_at": _iso(p["reviewed_at"]),
    }
    if own:
        view |= {
            "real_name": p["real_name"],
            "id_number_masked": p["id_number_masked"],
            "credit_code": p["credit_code"],
            "legal_person": p["legal_person"],
            "contact_name": p["contact_name"],
        }
    return view


def public_publisher(p: Row) -> Row:
    return {
        "name": p["name"],
        "kind": p["kind"],
        "real_name_verified": bool(p["real_name_verified"]),
    }


def document_view(d: Row) -> Row:
    return {
        "document_id": str(d["id"]),
        "kind": d["kind"],
        "media_type": d["media_type"],
        "size_bytes": d["size_bytes"],
        "created_at": _iso(d["created_at"]),
        "purged": d.get("purged_at") is not None,
    }


def license_view(lic: Row, *, today: date) -> Row:
    until = lic["valid_until"]
    return {
        "license_id": str(lic["id"]),
        "publisher_id": str(lic["publisher_id"]),
        "category": category_view(lic["category"]),
        "license_name": lic["license_name"],
        "license_no": lic["license_no"],
        "valid_until": _iso(until),
        "review_status": lic["review_status"],
        "review_note": lic.get("review_note"),
        "document_id": str(lic["document_id"]) if lic.get("document_id") else None,
        "expiring": bool(
            until
            and lic["review_status"] == "approved"
            and until <= today + timedelta(days=LICENSE_REMIND_DAYS)
        ),
        "expired": bool(until and until < today),
    }


def expert_listing_view(bot: Row) -> Row:
    return {
        "knowbot_id": str(bot["id"]),
        "name": bot["name"],
        "visibility": bot["visibility"],
        "listing_status": bot["listing_status"],
        "price": _money(bot["price"]),
        "review_note": bot["listing_review_note"],
    }


def listing_view(item: Row, *, viewer: str | None, admin: bool = False) -> Row:
    own = viewer is not None and item["seller_subject"] == viewer
    view: Row = {
        "listing_id": str(item["id"]),
        "title": item["title"],
        "category": category_view(item["category"]),
        "description": item["description"],
        "items": [
            {
                "name": i.get("name"),
                "media_type": i.get("media_type"),
                "size_bytes": i.get("size_bytes"),
                **({"item_id": i.get("item_id")} if own or admin else {}),
            }
            for i in item["items"]
        ],
        "sample": item["sample"],
        "rights_statement": item["rights_statement"],
        "resale_allowed": item["resale_allowed"],
        "price": _money(item["price"]),
        "mine": own,
        "updated_at": _iso(item["updated_at"]),
    }
    if own or admin:
        view |= {
            "status": item["status"],
            "review_note": item["review_note"],
            "delist_reason": item.get("delist_reason"),
        }
    return view


def shop_ref(shop: Row) -> Row:
    return {"shop_id": str(shop["id"]), "name": shop["name"]}


def shop_event_view(e: Row, *, for_admin: bool = False) -> Row:
    """留痕给本人与运营看；停店、恢复是谁做的不给本人（只给说明）。

    运营看得到 by_subject。
    """
    out = {
        "kind": e["kind"],
        "old_name": e["old_name"],
        "new_name": e["new_name"],
        "note": e["note"],
        "at": _iso(e["created_at"]),
    }
    if for_admin and "by_subject" in e and e["by_subject"]:
        out["by_subject"] = e["by_subject"]
    return out


def _activity(supply: list[Row]) -> dict[str, Row]:
    """按人数一数：在架专家、在架工具、在架知识、公开需求各几样，供给的类目，最近一次改动。"""
    out: dict[str, Row] = {}
    for row in supply:
        act = out.setdefault(
            row["owner"],
            {
                "expert": 0,
                "tool": 0,
                "knowledge": 0,
                "demand": 0,
                "categories": set(),
                "at": None,
            },
        )
        act[row["kind"]] += 1
        if row["category"]:
            act["categories"].add(row["category"])
        if act["at"] is None or row["at"] > act["at"]:
            act["at"] = row["at"]
    return out


def formal_contract_view(
    raw: dict[str, Any], *, method_kind: str, title: str, purpose: str
) -> dict[str, Any]:
    """Normalize stored PublicMethodContract without leaking any private steps."""
    return {
        "method_kind": raw.get("method_kind") or method_kind,
        "title": raw.get("title") or title,
        "purpose": raw.get("purpose") or purpose,
        "applies_when": list(raw.get("applies_when") or []),
        "excludes": list(raw.get("excludes") or []),
        "promises": dict(raw.get("promises") or {}),
        "public_inputs": list(raw.get("public_inputs") or []),
        "what_to_return": list(raw.get("what_to_return") or []),
        "access_summary": list(raw.get("access_summary") or []),
        "v0_check_kinds": list(raw.get("v0_check_kinds") or []),
        "has_blind_review": bool(raw.get("has_blind_review")),
        "per_action_approval_count": int(raw.get("per_action_approval_count") or 0),
        "domain_tags": list(raw.get("domain_tags") or []),
    }


def _offer_reason(listing: Row, states: dict[tuple[str, str], str]) -> str | None:
    """knowbase 说条目里有一份没了 / 不再出售就回原因；没回的那几份不算数。"""
    for item in listing["items"]:
        state = states.get((str(listing["seller_subject"]), str(item.get("item_id"))))
        if state == "gone":
            return "item_gone"
        if state == "not_for_sale":
            return "item_not_for_sale"
    return None


def _missing_reason(items: list[Row], offered: dict[str, Row]) -> str | None:
    """条目里的资料还在不在卖家的 knowmart 栏、还能不能卖；都在都能卖回 None。"""
    for item in items:
        got = offered.get(str(item.get("item_id")))
        if got is None:
            return "item_gone"
        if not got.get("sellable"):
            return "item_not_for_sale"
    return None


def demand_view(d: Row, *, viewer: str, today: date) -> Row:
    return {
        "demand_id": str(d["id"]),
        "kind": d["kind"],
        "title": d["title"],
        "problem": d["problem"],
        "data_wanted": d["data_wanted"],
        "deadline": _iso(d["deadline"]),
        "budget": _money(d["budget"]),
        "status": d["status"],
        "taking_responses": demand_open(d["status"], d["deadline"], today),
        "mine": d["buyer_subject"] == viewer,
        "chosen_response_id": str(d["chosen_response_id"])
        if d["chosen_response_id"]
        else None,
        "created_at": _iso(d["created_at"]),
    }


def response_view(r: Row, *, viewer: str, buyer: str) -> Row:
    return {
        "response_id": str(r["id"]),
        "demand_id": str(r["demand_id"]),
        "listing_kind": r["listing_kind"],
        "listing_ref": r["listing_ref"],
        "proposal": r["proposal"],
        "price": _money(r["price"]),
        "days": r["days"],
        "status": r["status"],
        "mine": r["seller_subject"] == viewer,
        "to_me": buyer == viewer,
        "created_at": _iso(r["created_at"]),
    }


def trade_view(t: Row, *, viewer: str | None) -> Row:
    view: Row = {
        "trade_id": str(t["id"]),
        "kind": t["kind"],
        "listing_ref": t["listing_ref"],
        "title": t["title"],
        "price": _money(t["price"]),
        "target_app": t["target_app"],
        "granted": [
            {"name": c.get("name"), "item_id": c.get("item_id")}
            for c in t["granted"] or []
        ],
        "demand_id": str(t["demand_id"]) if t["demand_id"] else None,
        "status": t["status"],
        "failure": t.get("failure"),
        "seller_publisher": t.get("seller_publisher"),
        "created_at": _iso(t["created_at"]),
    }
    if viewer is not None:
        view["role"] = "buyer" if t["buyer_subject"] == viewer else "seller"
    return view


def review_view(r: Row) -> Row:
    return {
        "rating": r["rating"],
        "text": r["text"],
        "reply": r["reply"],
        "created_at": _iso(r["created_at"]),
        "replied_at": _iso(r["replied_at"]),
    }


def rating_of(reviews: list[Row]) -> Row:
    if not reviews:
        return {"count": 0, "average": None}
    average = sum(r["rating"] for r in reviews) / len(reviews)
    return {"count": len(reviews), "average": round(average, 1)}


def message_view(m: Row, *, viewer: str, parties: Row) -> Row:
    author = m["author_subject"]
    role = (
        "buyer"
        if author == parties.get("buyer")
        else "seller"
        if author == parties.get("seller")
        else "user"
    )
    return {
        "message_id": str(m["id"]),
        "body": m["body"],
        "role": role,
        "mine": author == viewer,
        "created_at": _iso(m["created_at"]),
    }


def report_view(r: Row) -> Row:
    return {
        "report_id": str(r["id"]),
        "target_kind": r["target_kind"],
        "target_id": r["target_id"],
        "reason": r["reason"],
        "status": r["status"],
        "note": r["note"],
        "created_at": _iso(r["created_at"]),
        "handled_at": _iso(r["handled_at"]),
    }
