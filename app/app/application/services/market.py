"""供需市场（SDD 0018 第十一节）：发布主体与资质、专家与知识两种供给、需求与响应、交易、
评价、留言、驻场规则、举报；运营的审核与下架；资质到期与证件照片清理。

谁能看什么只在这里判，前端拿到什么就画什么：
- 发布主体的实名材料只给本人与运营看；证件照片只给运营看。
- 知识条目：上架的大家都看得到；卖家看得到自己的各种状态。
- 需求：开着的大家都看得到；响应只有需求方与那位响应的卖家看得到，别人只看到有几个。
- 留言：需求下的大家都看得到；响应下的只有需求方与那位卖家。
看不到的与没有一样（回「找不到」）。本期标价、记成交，不接真实支付。
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.application.errors.exceptions import (
    AppException,
    BadRequestError,
    ConcurrencyConflictError,
    NotFoundError,
)
from app.application.ports.knowbots import KnowbotRepository
from app.application.ports.market import KnowledgeVault, MarketRepository, Row
from app.domain.knowbots.catalog import BY_CODE, DEFAULT_CATEGORY, category_view
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
    PublisherIn,
    ReplyIn,
    ReportDecision,
    ReportIn,
    ResponseIn,
    ReviewDecision,
    ReviewIn,
    demand_open,
    flagged,
    hash_id,
    mask_id,
)

PAGE_SIZE = 24
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


def check_category(category: str) -> None:
    if category not in BY_CODE or category == DEFAULT_CATEGORY:
        raise BadRequestError("Choose a category", code="category_invalid")


class MarketService:
    def __init__(
        self,
        repo: MarketRepository,
        knowbots: KnowbotRepository,
        vault: KnowledgeVault | None,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repo = repo
        self.knowbots = knowbots
        self.vault = vault
        self.now = now

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
            digest = hash_id(body.id_number)
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
        async with self.repo.transaction():
            await self.repo.save_publisher(subject, fields)
        return await self.my_publisher(subject)

    async def add_document(
        self, subject: str, kind: str, media_type: str, data: bytes
    ) -> Row:
        publisher = await self._publisher(subject)
        if kind not in DOCUMENT_KINDS:
            raise BadRequestError("Unknown document kind", code="document_kind_invalid")
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

    async def _publisher(self, subject: str) -> Row:
        found = await self.repo.publisher_of(subject)
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
                "listing_status": "self",
            }
        else:
            check_category(bot["category"])
            publisher = await self._qualified(subject, bot["category"])
            if bot["listing_status"] == "taken_down":
                raise _conflict(
                    "This expert was taken down by the platform", "listing_taken_down"
                )
            changes = {
                "price": body.price,
                "publisher_id": publisher["id"],
                "listing_status": "public"
                if bot["visibility"] == "public"
                else "pending",
            }
        async with self.repo.transaction():
            await self.repo.update_knowbot(bot["id"], **changes)
        found = await self.repo.get_knowbot(knowbot_id)
        assert found is not None
        return expert_listing_view(found)

    async def record_summon(
        self, *, buyer: str, knowbot_id: str, version: str
    ) -> Row | None:
        """有人「请」了别人的专家：记一笔成交（价格按条目上写的，本期不收钱）。自己请自己的不记。"""
        bot = await self.repo.get_knowbot(knowbot_id)
        if bot is None or bot["owner_subject"] == buyer:
            return None
        async with self.repo.transaction():
            return trade_view(
                await self.repo.add_trade(
                    {
                        "kind": "expert",
                        "buyer_subject": buyer,
                        "seller_subject": bot["owner_subject"],
                        "listing_ref": f"{bot['id']}@{version}",
                        "title": f"{bot['name']} 第 {version} 版",
                        "price": bot["price"],
                        "copied": [],
                    }
                ),
                viewer=buyer,
            )

    async def expert_market(self, knowbot_id: str) -> Row:
        """专家详情页上的市场部分：价格、评价。"""
        bot = await self.repo.get_knowbot(knowbot_id)
        if bot is None:
            raise _missing("knowbot")
        reviews = await self.repo.reviews_for(str(bot["id"]))
        return {
            "price": _money(bot["price"]),
            "listing_status": bot["listing_status"],
            "reviews": [review_view(r) for r in reviews],
            "rating": rating_of(reviews),
        }

    # ================= 知识条目 =================
    async def create_listing(
        self, subject: str, body: KnowledgeListingIn, user_token: UserToken
    ) -> Row:
        check_category(body.category)
        items = await self._items(subject, body, user_token)
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
            )
        return await self.listing(subject, listing_id)

    async def submit_listing(self, subject: str, listing_id: str) -> Row:
        """交运营审：发布主体审过、类目要的证照有，才能交。"""
        listing = await self._own_listing(subject, listing_id)
        if listing["status"] not in ("draft", "rejected", "delisted"):
            raise _conflict("This listing cannot be submitted now", "listing_state")
        publisher = await self._qualified(subject, listing["category"])
        async with self.repo.transaction():
            await self.repo.update_listing(
                listing["id"], status="pending", publisher_id=publisher["id"]
            )
        return await self.listing(subject, listing_id)

    async def delist_listing(self, subject: str, listing_id: str) -> Row:
        """卖家下架：买家那份照旧在。"""
        listing = await self._own_listing(subject, listing_id)
        if listing["status"] not in ("pending", "listed"):
            raise _conflict("This listing is not up", "listing_state")
        async with self.repo.transaction():
            await self.repo.update_listing(listing["id"], status="delisted")
        return await self.listing(subject, listing_id)

    async def my_listings(self, subject: str) -> list[Row]:
        return [
            listing_view(item, viewer=subject)
            for item in await self.repo.listings_of(subject)
        ]

    async def knowledge_market(
        self, *, q: str | None, category: str | None, page: int
    ) -> Row:
        found, total = await self.repo.listed_knowledge(
            q=q, category=category, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE
        )
        return {
            "items": [listing_view(item, viewer=None) for item in found],
            "total": total,
            "page": page,
            "page_size": PAGE_SIZE,
        }

    async def listing(self, subject: str, listing_id: str) -> Row:
        listing = await self.repo.get_listing(listing_id)
        if listing is None or (
            listing["status"] != "listed" and listing["seller_subject"] != subject
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
        }

    async def buy(self, subject: str, listing_id: str, body: BuyIn) -> Row:
        """买知识：复制一份进买家选的知识库（knowbench 云端或 knowlab），记一笔成交。"""
        listing = await self.repo.get_listing(listing_id)
        if listing is None or listing["status"] != "listed":
            raise _missing()
        if listing["seller_subject"] == subject:
            raise BadRequestError("This is your own listing", code="own_listing")
        demand_id = await self._chosen_demand(
            subject, body.demand_id, "knowledge", listing_id
        )
        if self.vault is None:
            raise AppException(
                code="knowbase_unconfigured",
                status_code=503,
                msg="knowbase_unconfigured",
            )
        trade_id = uuid.uuid4()
        copied = []
        for n, item in enumerate(listing["items"]):
            got = await self.vault.purchase(
                seller=listing["seller_subject"],
                seller_item_id=str(item["item_id"]),
                buyer=subject,
                buyer_app=body.target_app,
                transaction_ref=f"knowmart-trade:{trade_id}:{n}",
            )
            copied.append(
                {
                    "seller_item_id": str(item["item_id"]),
                    "item_id": str(got.get("item_id")),
                    "name": str(got.get("name") or item.get("name")),
                    "sha256": str(got.get("sha256") or item.get("sha256")),
                }
            )
        async with self.repo.transaction():
            trade = await self.repo.add_trade(
                {
                    "id": trade_id,
                    "kind": "knowledge",
                    "buyer_subject": subject,
                    "seller_subject": listing["seller_subject"],
                    "listing_ref": str(listing["id"]),
                    "title": listing["title"],
                    "price": listing["price"],
                    "target_app": body.target_app,
                    "copied": copied,
                    "demand_id": demand_id,
                }
            )
            if demand_id is not None:
                await self.repo.update_demand(demand_id, status="closed")
        return trade_view(trade, viewer=subject)

    async def _items(
        self, subject: str, body: KnowledgeListingIn, user_token: UserToken
    ) -> list[Row]:
        """核每一份都在卖家自己的 knowmart 知识库里；买来、不许转卖的不能拿来卖。"""
        if self.vault is None:
            raise AppException(
                code="knowbase_unconfigured",
                status_code=503,
                msg="knowbase_unconfigured",
            )
        token = await user_token()
        no_resale = await self.repo.bought_without_resale(subject)
        items = []
        for ref in body.items:
            got = await self.vault.item(token, ref.item_id)
            if str(got.get("sha256")) in no_resale:
                raise BadRequestError(
                    f"{got.get('name')} was bought without the right to resell",
                    code="resale_not_allowed",
                )
            items.append(
                {
                    "item_id": ref.item_id,
                    "name": str(got.get("name")),
                    "media_type": str(got.get("media_type")),
                    "size_bytes": int(got.get("size_bytes") or 0),
                    "sha256": str(got.get("sha256")),
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
        return {
            **demand_view(found, viewer=subject, today=self.now().date()),
            "response_count": len(
                [r for r in responses if r["status"] not in ("withdrawn", "taken_down")]
            ),
            "responses": [
                response_view(r, viewer=subject, buyer=found["buyer_subject"])
                for r in shown
                if r["status"] != "taken_down" or r["seller_subject"] == subject
            ],
        }

    async def close_demand(self, subject: str, demand_id: str) -> Row:
        found = await self.repo.get_demand(demand_id)
        if found is None or found["buyer_subject"] != subject:
            raise _missing("demand")
        if found["status"] != "open":
            raise _conflict("This demand is not open", "demand_state")
        async with self.repo.transaction():
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
        if body.listing_kind is not None:
            assert body.listing_ref is not None
            await self._check_own_listing_ref(
                subject, body.listing_kind, body.listing_ref
            )
        try:
            async with self.repo.transaction():
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
        found = await self.repo.get_response(response_id)
        if found is None or found["seller_subject"] != subject:
            raise _missing("response")
        if found["status"] != "open":
            raise _conflict("This response cannot be withdrawn", "response_state")
        async with self.repo.transaction():
            await self.repo.update_response(found["id"], status="withdrawn")
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
        found = await self.repo.get_demand(demand_id)
        if found is None or found["buyer_subject"] != subject:
            raise _missing("demand")
        if found["status"] != "open":
            raise _conflict("This demand is not open", "demand_state")
        response = await self.repo.get_response(response_id)
        if (
            response is None
            or response["demand_id"] != found["id"]
            or response["status"] != "open"
        ):
            raise _missing("response")
        async with self.repo.transaction():
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
        knowbot_id, _, version = ref.partition("@")
        bot = await self.repo.get_knowbot(knowbot_id)
        if bot is None or bot["owner_subject"] != subject or not version:
            raise _missing("knowbot")
        found = await self.knowbots.get_version(bot["id"], version)
        if found is None or found.status != "active":
            raise _missing("knowbot")

    async def _chosen_demand(
        self, subject: str, demand_id: str | None, kind: str, ref: str
    ) -> uuid.UUID | None:
        """从需求响应来的成交：需求是买家的、选中的那家附的就是这一条。"""
        if demand_id is None:
            return None
        demand = await self.repo.get_demand(demand_id)
        if (
            demand is None
            or demand["buyer_subject"] != subject
            or demand["status"] != "chosen"
        ):
            raise _missing("demand")
        response = (
            await self.repo.get_response(str(demand["chosen_response_id"]))
            if demand["chosen_response_id"]
            else None
        )
        if response is None or (response["listing_kind"], response["listing_ref"]) != (
            kind,
            ref,
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
        out = []
        for trade in found:
            review = await self.repo.get_review(trade["id"])
            out.append(
                {
                    **trade_view(trade, viewer=subject),
                    "review": review_view(review) if review else None,
                }
            )
        return out

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
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repo = repo
        self.now = now

    async def queue(self) -> Row:
        today = self.now().date()
        publishers = []
        for p in await self.repo.pending_publishers():
            publishers.append(
                {
                    **publisher_view(p, own=True),
                    "documents": [
                        document_view(d)
                        for d in await self.repo.documents_of(str(p["id"]))
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
        return {
            "publishers": publishers,
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

    async def decide_listing(
        self, listing_id: str, decision: ReviewDecision, reviewer: str
    ) -> None:
        found = await self.repo.get_listing(listing_id)
        if found is None or found["status"] != "pending":
            raise _missing()
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
        self, kind: str, target_id: str, note: str, reviewer: str
    ) -> None:
        """平台下架：知识买家那份照旧在；专家下架后不能再请（在跑的跑完）。"""
        now = self.now()
        async with self.repo.transaction():
            if kind == "knowledge":
                found = await self.repo.get_listing(target_id)
                if found is None:
                    raise _missing()
                await self.repo.update_listing(
                    found["id"],
                    status="taken_down",
                    review_note=note,
                    reviewed_by=reviewer,
                    reviewed_at=now,
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
        found = await self.repo.get_report(report_id)
        if found is None or found["status"] != "open":
            raise _missing("report")
        if decision.uphold:
            await self.take_down(
                found["target_kind"],
                found["target_id"],
                decision.note or found["reason"],
                reviewer,
            )
        async with self.repo.transaction():
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
                limit=PAGE_SIZE * 4, offset=(page - 1) * PAGE_SIZE * 4
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
        view |= {"status": item["status"], "review_note": item["review_note"]}
    return view


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
        "copied": [
            {"name": c.get("name"), "item_id": c.get("item_id")}
            for c in t["copied"] or []
        ],
        "demand_id": str(t["demand_id"]) if t["demand_id"] else None,
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
