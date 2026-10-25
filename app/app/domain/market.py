"""供需市场的规矩（SDD 0018 第十一节）。

条目、需求、响应、交易、评价、举报的形状与状态。纯逻辑。

- 两种并列的商品：专家（请，用请的人自己的数据）与知识（买，复制一份进买家的
  知识库）。
- 交易方式：直接买 / 请；需求响应（买家发需求、卖家响应、买家选一家）。
  比稿、计件本期不做。
- 本期标价、记成交，不接真实支付（第 9 小节第 2 项）。
- 驻场规则由平台定（第 4 小节）；禁止内容由运营审，这里只把明显的词标出来给运营看。
"""

from __future__ import annotations

import hashlib
import hmac
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Kind = Literal["expert", "knowledge"]
TargetApp = Literal["knowbench", "knowlab"]
ListingStatus = Literal[
    "draft", "pending", "listed", "rejected", "delisted", "taken_down"
]
ExpertListing = Literal["self", "pending", "public", "rejected", "taken_down"]
MAX_PRICE = Decimal("1000000")
MAX_KNOWLEDGE_ITEMS = 20
DOCUMENT_MAX_BYTES = 5 * 1024 * 1024
DOCUMENT_TYPES = frozenset({"image/jpeg", "image/png", "application/pdf"})
DOCUMENT_KINDS = frozenset({"id_front", "id_back", "business_license", "license"})
# 证件照片审过后留多久（第 9 小节第 4 项）；资质到期前多少天提醒（第 5 小节）
DOCUMENT_KEEP_DAYS = 365
LICENSE_REMIND_DAYS = 30
# C-L5：荐股、目标价、评级、收益承诺类；标出来给运营看，不自动拒
FLAGGED_WORDS: tuple[str, ...] = (
    "荐股",
    "目标价",
    "评级",
    "买入",
    "卖出",
    "增持",
    "减持",
    "稳赚",
    "保本",
    "收益承诺",
    "保证收益",
    "翻倍",
)
_ID_NUMBER = re.compile(r"^[0-9]{17}[0-9Xx]$")
_CREDIT_CODE = re.compile(r"^[0-9A-HJ-NPQRTUWXY]{2}[0-9]{6}[0-9A-HJ-NPQRTUWXY]{10}$")
Text = Annotated[str, Field(min_length=1, max_length=4000)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _price(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    if value < 0 or value > MAX_PRICE or value.as_tuple().exponent < -2:  # type: ignore[operator]
        raise ValueError("price must be 0 to 1,000,000 with at most two decimals")
    return value


class PublisherIn(Strict):
    """入驻：个人用实名（姓名、身份证号）；非个人用营业执照（名称、统一社会信用代码、法人、联系人）。

    照片另传（documents）。改了就重新审。
    """

    kind: Literal["individual", "organization"]
    name: str = Field(min_length=1, max_length=200)  # 对外显示的发布主体名称
    real_name: str | None = Field(default=None, min_length=1, max_length=100)
    id_number: str | None = Field(default=None, min_length=18, max_length=18)
    credit_code: str | None = Field(default=None, min_length=18, max_length=18)
    legal_person: str | None = Field(default=None, min_length=1, max_length=100)
    contact_name: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def _by_kind(self) -> PublisherIn:
        if self.kind == "individual":
            if not self.real_name or not self.id_number:
                raise ValueError("an individual gives a real name and an id number")
            if not _ID_NUMBER.fullmatch(self.id_number):
                raise ValueError("the id number is not valid")
        else:
            if not self.credit_code or not self.legal_person or not self.contact_name:
                raise ValueError(
                    "an organization gives its credit code, legal person and contact"
                )
            if not _CREDIT_CODE.fullmatch(self.credit_code.upper()):
                raise ValueError("the credit code is not valid")
        return self


def mask_id(number: str) -> str:
    """只留前 3 位、后 4 位。"""
    return number[:3] + "*" * (len(number) - 7) + number[-4:]


def hash_id(number: str, key: str) -> str:
    """身份证号的查重摘要：带服务端钥匙的 HMAC。身份证号熵低，裸 sha256 库泄后可穷举还原
    （Kimi 评审第 5 条）；钥匙不进库，换钥匙要整表重算。"""
    return hmac.new(key.encode(), number.upper().encode(), hashlib.sha256).hexdigest()


class LicenseIn(Strict):
    category: str = Field(min_length=1, max_length=64)
    license_name: str = Field(min_length=1, max_length=200)
    license_no: str = Field(min_length=1, max_length=100)
    valid_until: date | None = None
    document_id: str = Field(min_length=36, max_length=36)


class ExpertListingIn(Strict):
    """专家条目：申请公开（要资质）或留着自用；每次请的价格，空是免费。

    申请公开时选类目（没选过的必须选；knowlab 交来的专家起初没有类目，Fable P2-1）。
    """

    visibility: Literal["private", "public"]
    price: Decimal | None = None
    category: str | None = Field(default=None, min_length=1, max_length=64)

    _check_price = field_validator("price")(_price)


class KnowledgeItemRef(Strict):
    item_id: str = Field(min_length=1, max_length=64)


class KnowledgeListingIn(Strict):
    """知识条目（驻场规则必填：名称、类目、内容说明、来源与权利声明；样张可选；能不能转卖）。"""

    title: str = Field(min_length=1, max_length=120)
    category: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=4000)
    items: tuple[KnowledgeItemRef, ...] = Field(
        min_length=1, max_length=MAX_KNOWLEDGE_ITEMS
    )
    sample: str = Field(default="", max_length=5000)
    rights_statement: str = Field(min_length=1, max_length=2000)
    resale_allowed: bool = False
    price: Decimal | None = None

    _check_price = field_validator("price")(_price)

    @model_validator(mode="after")
    def _distinct(self) -> KnowledgeListingIn:
        ids = [i.item_id for i in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("an item is listed twice")
        return self


class DemandIn(Strict):
    """需求（必填：要解决什么、要专家还是知识、要什么样的资料、期限；预算可空）。"""

    kind: Kind
    title: str = Field(min_length=1, max_length=120)
    problem: str = Field(min_length=1, max_length=4000)
    data_wanted: str = Field(default="", max_length=4000)
    deadline: date
    budget: Decimal | None = None

    _check_budget = field_validator("budget")(_price)


class ResponseIn(Strict):
    """卖家响应需求：附自己已有的条目（可不附）、方案、报价、多久。"""

    listing_kind: Kind | None = None
    listing_ref: str | None = Field(default=None, min_length=1, max_length=128)
    proposal: str = Field(min_length=1, max_length=4000)
    price: Decimal | None = None
    days: int | None = Field(default=None, ge=1, le=365)

    _check_price = field_validator("price")(_price)

    @model_validator(mode="after")
    def _listing_pair(self) -> ResponseIn:
        if (self.listing_kind is None) != (self.listing_ref is None):
            raise ValueError("an attached listing has both a kind and a ref")
        return self


class MessageIn(Strict):
    body: str = Field(min_length=1, max_length=2000)


class ReviewIn(Strict):
    rating: int = Field(ge=1, le=5)
    text: str = Field(default="", max_length=1000)


class ReplyIn(Strict):
    reply: str = Field(min_length=1, max_length=1000)


class ReportIn(Strict):
    target_kind: Literal["expert", "knowledge", "demand", "response", "message"]
    target_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=2000)


class BuyIn(Strict):
    """买知识：复制进买家哪个知识库（knowbench 云端或 knowlab）。

    从需求响应来的带需求编号。request_id 是这一次购买的幂等键（网页每次打开买的那一处
    生成一个，重试沿用）：同一个键重来接着做原来那一笔，参数不同就拒（Astra 评审 F07）。
    """

    request_id: str = Field(pattern=r"^[A-Za-z0-9-]{8,64}$")
    target_app: TargetApp
    demand_id: str | None = Field(default=None, min_length=36, max_length=36)


class ReviewDecision(Strict):
    """运营的审核结论：通过 / 不通过与理由。"""

    approve: bool
    note: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def _reason(self) -> ReviewDecision:
        if not self.approve and not self.note:
            raise ValueError("say why it does not pass")
        return self


class ReportDecision(Strict):
    uphold: bool
    note: str = Field(default="", max_length=2000)


class RulesIn(Strict):
    body: str = Field(min_length=1, max_length=50_000)


def flagged(*texts: str) -> list[str]:
    """条目里出现的禁止类词（C-L5），给运营审核时看；不自动拒。"""
    joined = "\n".join(texts)
    return [w for w in FLAGGED_WORDS if w in joined]


def demand_open(status: str, deadline: date, today: date) -> bool:
    """还在收响应：没选、没关、没过期限。"""
    return status == "open" and deadline >= today


# 驻场规则第 1 版的文字（平台定，管理端可改、留版本）；放在同目录的 Markdown 里
DEFAULT_RULES = (Path(__file__).parent / "house_rules_v1.md").read_text(
    encoding="utf-8"
)
