"""PR-5 regression suite: KnowMart FormalListing full-stack unification.

Covers:
1. Decoupled regulated vs directory category + auto-default seeding (X06).
2. Tool-only incubation permission enforcement (X06 / ck_formal_incubation_tool_only).
3. PublicMethodContract 5-tab detail, shop supply & ShopScreen visibility (X04 / X05).
4. Idempotent Trade + Demand offer closure + TradeReview on formal acquisition (X07).
5. MarketAdmin queue flagged scan (C-L5), expiry/revoke, and takedown (X08/C-L4).
6. W08 backfill_formal_method_kinds.sh syntax & active-only blocking semantics.
"""

from __future__ import annotations

import subprocess
import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.application.errors.exceptions import (
    BadRequestError,
    ConcurrencyConflictError,
    NotFoundError,
)
from app.application.services.expert_catalog import CatalogQuery, ExpertCatalog
from app.application.services.market import MarketAdmin, MarketService
from app.domain.market import (
    DemandIn,
    FormalListingIn,
    IncubationPermission,
    LicenseIn,
    ResponseIn,
    ReviewDecision,
    ReviewIn,
    RevokeIn,
)
from tests.test_knowbot_repository_db import db  # noqa: F401
from tests.test_market_db import BUYER, OPS, OTHER, PNG, SELLER, approved_publisher
from tests.test_shop_db import CheckingVault, ShopWorld


@pytest.fixture
def world(db):  # noqa: F811
    return ShopWorld(db, CheckingVault())


def _sample_public_contract(method_kind: str, title: str, purpose: str) -> dict:
    return {
        "method_kind": method_kind,
        "title": title,
        "purpose": purpose,
        "applies_when": ["输入标准财报三表"],
        "excludes": ["不做投资买卖评级"],
        "promises": {"reconciled": "输出资产负债与现金流勾稽核验回执"},
        "public_inputs": [
            {
                "name": "statement_rows",
                "description": "报表数据行",
                "required": True,
                "schema_type": "array",
                "domain_id": "fin.statement.row",
                "domain_label": "财务报表行",
            }
        ],
        "what_to_return": [
            {
                "name": "reconciliation_report",
                "description": "勾稽核验回执",
                "required": True,
                "schema_type": "object",
                "domain_id": "audit.check_receipt",
                "domain_label": "核验回执",
            }
        ],
        "access_summary": [],
        "v0_check_kinds": ["numeric_identity", "prohibited_terms"],
        "has_blind_review": method_kind == "expert",
        "per_action_approval_count": 0,
        "domain_tags": ["fin.statement.row", "audit.check_receipt"],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("offer_kind", "offer_price"),
    [("formal_expert", "29.00"), ("formal_tool", "12.50")],
)
async def test_decoupled_categories_public_detail_shop_trade_demand_and_reviews(
    world: ShopWorld,
    offer_kind: str,
    offer_price: str,
) -> None:
    await approved_publisher(world)

    tool_ref = {
        "method_id": str(uuid.uuid4()),
        "version": 1,
        "digest": "sha256:" + "a" * 64,
    }
    expert_ref = {
        "method_id": str(uuid.uuid4()),
        "version": 1,
        "digest": "sha256:" + "b" * 64,
    }

    async def check_delivery(owner, ref, skill=None):
        assert owner == SELLER
        is_tool = ref["method_id"] == tool_ref["method_id"]
        title = "三表报表勾稽算子" if is_tool else "年度报表核验专家"
        purpose = "执行资产负债平衡核验" if is_tool else "对报表核验并出具勾稽说明"
        kind = "tool" if is_tool else "expert"
        return {
            "title": title,
            "purpose": purpose,
            "method_kind": kind,
            "public_contract": _sample_public_contract(kind, title, purpose),
            "skill_dependencies": [],
            "delivery_digest": "sha256:" + ("c" if is_tool else "d") * 64,
        }

    async def run_flow(svc: MarketService, admin: MarketAdmin):
        svc.formal_delivery = SimpleNamespace(check=check_delivery)

        # 1. Submitting a tool facet code ('financial.reconciliation') succeeds!
        tool_sub = await svc.submit_formal(
            SELLER,
            FormalListingIn(
                reference=tool_ref,
                category="financial.reconciliation",
                price=Decimal("12.50"),
            ),
        )
        tool_id = uuid.UUID(tool_sub["listing_id"])

        # 2. Submitting an analysis.* expert facet ('analysis.statements') succeeds!
        expert_sub = await svc.submit_formal(
            SELLER,
            FormalListingIn(
                reference=expert_ref,
                category="analysis.statements",
                price=Decimal("29.00"),
            ),
        )
        expert_id = uuid.UUID(expert_sub["listing_id"])

        # Admin queue includes formal_listings!
        q = await admin.queue()
        queued_ids = {item["listing_id"] for item in q["formal_listings"]}
        assert {str(tool_id), str(expert_id)} <= queued_ids

        await svc.review_formal(OPS, tool_id, ReviewDecision(approve=True))
        await svc.review_formal(OPS, expert_id, ReviewDecision(approve=True))

        # 3. permit_incubation rejects formal experts and accepts formal tools!
        with pytest.raises(BadRequestError) as exc:
            await svc.permit_incubation(
                SELLER, expert_id, IncubationPermission(scope="acquired")
            )
        assert exc.value.code == "incubation_tool_only"

        await svc.permit_incubation(
            SELLER, tool_id, IncubationPermission(scope="acquired")
        )
        owned = (await svc.owner_formal_listings(SELLER, None))["items"]
        assert [
            r["listing_id"] for r in owned if r["incubation_scope"] == "acquired"
        ] == [str(tool_id)]

        # 4. Owner can re-classify directory_category without losing regulated status!
        await svc.classify_method(SELLER, expert_id, "analysis.operations")
        detail = await svc.formal_detail(BUYER, expert_id)
        assert detail["category"]["code"] == "analysis.operations"
        assert detail["regulated_category"]["code"] == "business.management"
        assert detail["price"] == "29.00"
        assert detail["public_contract"]["v0_check_kinds"] == [
            "numeric_identity",
            "prohibited_terms",
        ]
        assert detail["summonable"] is True and detail["acquired"] is False

        # 5. Shops home & ShopScreen include formal experts and tools!
        shops_page = await svc.shops(q=None, category=None, sort="time", page=1)
        assert len(shops_page["items"]) == 1
        assert shops_page["items"][0]["experts"] == 1
        assert shops_page["items"][0]["tools"] == 1
        shop_detail = await svc.shop(BUYER, shops_page["items"][0]["shop_id"])
        assert len(shop_detail["formal_methods"]) == 2

        # 6. Method directory returns enriched Five-Element summary, price and shop!
        cat_svc = ExpertCatalog(svc.knowbots, public_market=True, now=svc.now)
        dir_page = await cat_svc.method_directory(
            BUYER, CatalogQuery(category="analysis.operations"), "expert", svc.repo
        )
        assert dir_page["total"] == 1
        card = dir_page["items"][0]
        assert card["price"] == "29.00"
        assert card["shop"]["shop_id"] == shops_page["items"][0]["shop_id"]
        assert len(card["public_contract"]["what_to_return"]) == 1

        # 7. Demand offer -> chosen -> acquire records Trade & closes Demand!
        dm = await svc.create_demand(
            BUYER,
            DemandIn(
                kind="expert",
                title="需要财报核验专家",
                problem="核验三表勾稽",
                deadline=date(2026, 12, 31),
                budget=Decimal("50.00"),
            ),
        )
        offer_id = tool_id if offer_kind == "formal_tool" else expert_id
        resp = await svc.respond(
            SELLER,
            dm["demand_id"],
            ResponseIn(
                listing_kind=offer_kind,
                listing_ref=str(offer_id),
                proposal="可提供报表核验方法",
                price=Decimal(offer_price),
                days=1,
            ),
        )
        await svc.choose_response(BUYER, dm["demand_id"], resp["response_id"])

        acq = await svc.acquire_formal(BUYER, offer_id, demand_id=dm["demand_id"])
        assert acq["status"] == "acquired"
        assert (await svc.demand(BUYER, dm["demand_id"]))["status"] == "closed"
        assert await svc.acquire_formal(BUYER, offer_id) == acq

        trades = await svc.my_trades(BUYER, "buyer")
        assert len(trades) == 1
        assert trades[0]["kind"] == offer_kind
        assert trades[0]["price"] == offer_price
        assert trades[0]["listing_state"] == "on"

        # The review belongs to the acquired listing.
        await svc.review(
            BUYER, trades[0]["trade_id"], ReviewIn(rating=5, text="核验非常准确")
        )
        refreshed = await svc.formal_detail(OTHER, offer_id)
        assert refreshed["rating"] == {"count": 1, "average": 5.0}
        assert refreshed["reviews"][0]["text"] == "核验非常准确"

    await world.run(run_flow)


@pytest.mark.asyncio
async def test_admin_flagged_scan_license_expiry_revocation_and_violation_takedown(
    world: ShopWorld,
) -> None:
    await approved_publisher(world)
    fin_ref = {
        "method_id": str(uuid.uuid4()),
        "version": 1,
        "digest": "sha256:" + "e" * 64,
    }
    exp_ref = {
        "method_id": str(uuid.uuid4()),
        "version": 1,
        "digest": "sha256:" + "f" * 64,
    }

    async def check_delivery(owner, ref, skill=None):
        flagged_text = (
            "保证收益翻倍与荐股评级" if ref == fin_ref else "合规证券咨询分析"
        )
        return {
            "title": "证券方法",
            "purpose": flagged_text,
            "method_kind": "expert",
            "public_contract": _sample_public_contract(
                "expert", "证券方法", flagged_text
            ),
            "skill_dependencies": [],
            "delivery_digest": "sha256:" + "9" * 64,
        }

    state: dict[str, str] = {}

    async def setup_and_check(svc: MarketService, admin: MarketAdmin):
        svc.formal_delivery = SimpleNamespace(check=check_delivery)
        doc = await svc.add_document(SELLER, "license", "application/pdf", PNG)
        lic = await svc.add_license(
            SELLER,
            LicenseIn(
                category="finance.securities_advisory",
                license_name="经营证券期货业务许可证",
                license_no="LIC-001",
                valid_until=world.now.date() + timedelta(days=10),
                document_id=doc["document_id"],
            ),
        )
        await admin.decide_license(lic["license_id"], ReviewDecision(approve=True), OPS)
        state["lic_id"] = lic["license_id"]

        sub1 = await svc.submit_formal(
            SELLER,
            FormalListingIn(reference=fin_ref, category="finance.securities_advisory"),
        )
        sub2 = await svc.submit_formal(
            SELLER,
            FormalListingIn(reference=exp_ref, category="finance.securities_advisory"),
        )
        state["id1"] = sub1["listing_id"]
        state["id2"] = sub2["listing_id"]

        # C-L5: queue() flags prohibited words without auto-rejecting
        q = await admin.queue()
        by_id = {x["listing_id"]: x for x in q["formal_listings"]}
        assert set(by_id[state["id1"]]["flagged"]) >= {
            "荐股",
            "评级",
            "保证收益",
            "翻倍",
        }
        assert by_id[state["id2"]]["flagged"] == []

        await svc.review_formal(
            OPS, uuid.UUID(state["id1"]), ReviewDecision(approve=True)
        )
        await svc.review_formal(
            OPS, uuid.UUID(state["id2"]), ReviewDecision(approve=True)
        )
        await svc.acquire_formal(BUYER, uuid.UUID(state["id1"]))

        # C-L4: Violation takedown marks taken_down, blocks re-submit, revokes Trade!
        await admin.take_down(
            "formal_method", state["id1"], "违规承诺收益", OPS, violation=True
        )
        with pytest.raises(NotFoundError):
            await svc.formal_detail(OTHER, uuid.UUID(state["id1"]))
        with pytest.raises(ConcurrencyConflictError) as exc:
            await svc.submit_formal(
                SELLER,
                FormalListingIn(
                    reference=fin_ref, category="finance.securities_advisory"
                ),
            )
        assert exc.value.code == "listing_taken_down"
        [buyer_trade] = await svc.my_trades(BUYER, "buyer")
        assert buyer_trade["listing_state"] == "revoked"

    await world.run(setup_and_check)

    # Advance clock past license expiry -> sweep() automatically withdraws id2!
    world.now = world.now + timedelta(days=15)

    async def verify_sweep_and_revoke(svc: MarketService, admin: MarketAdmin):
        res = await admin.sweep()
        assert res["expired"] >= 1
        mine = await svc.owner_formal_listings(SELLER, None)
        by_id = {x["listing_id"]: x for x in mine["items"]}
        assert by_id[state["id2"]]["status"] == "withdrawn"
        assert by_id[state["id2"]]["delist_reason"] == "license_expired"

        # Renew license, re-submit id2, approve, then revoke license -> withdrawal!
        svc.formal_delivery = SimpleNamespace(check=check_delivery)
        doc = await svc.add_document(SELLER, "license", "application/pdf", PNG)
        lic2 = await svc.add_license(
            SELLER,
            LicenseIn(
                category="finance.securities_advisory",
                license_name="经营证券期货业务许可证",
                license_no="LIC-002",
                valid_until=world.now.date() + timedelta(days=365),
                document_id=doc["document_id"],
            ),
        )
        await admin.decide_license(
            lic2["license_id"], ReviewDecision(approve=True), OPS
        )
        re_sub = await svc.submit_formal(
            SELLER,
            FormalListingIn(reference=exp_ref, category="finance.securities_advisory"),
        )
        assert re_sub["status"] == "pending"
        await svc.review_formal(
            OPS, uuid.UUID(state["id2"]), ReviewDecision(approve=True)
        )
        await admin.revoke_license(lic2["license_id"], RevokeIn(note="证照吊销"), OPS)
        mine_after = await svc.owner_formal_listings(SELLER, None)
        by_id_after = {x["listing_id"]: x for x in mine_after["items"]}
        assert by_id_after[state["id2"]]["status"] == "withdrawn"
        assert by_id_after[state["id2"]]["delist_reason"] == "qualification_revoked"

    await world.run(verify_sweep_and_revoke)


def test_backfill_formal_method_kinds_script_syntax_and_active_guard() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "scripts/backfill_formal_method_kinds.sh"
    )
    subprocess.run(["bash", "-n", str(script)], check=True)
    text = script.read_text(encoding="utf-8")
    assert "WHERE l.status IN ('listed', 'pending')" in text
    assert "RAISE WARNING" in text


def test_tool_category_precheck_and_negated_flagged_disclaimers() -> None:
    from app.application.services.market import resolve_formal_categories
    from app.domain.market import flagged

    # Tool directory category succeeds during precheck (method_kind=None) and tool check
    assert resolve_formal_categories(
        "financial.reconciliation", None, None, "", ""
    ) == ("tools.calculator", "financial.reconciliation")
    assert resolve_formal_categories(
        "tools.calculator", "financial.reconciliation", None, "", ""
    ) == ("tools.calculator", "financial.reconciliation")
    assert resolve_formal_categories(
        "financial.reconciliation", None, "tool", "三表勾稽工具", "核验平衡"
    ) == ("tools.calculator", "financial.reconciliation")
    with pytest.raises(BadRequestError):
        resolve_formal_categories(
            "financial.reconciliation", None, "expert", "专家", "分析"
        )

    # Negated disclaimers do not trigger false-positive flagged words
    assert flagged("不做投资买卖评级", "不提供荐股、目标价或收益承诺") == []
    assert flagged("本专家仅输出客观勾稽核对结果；不含买入或卖出建议，不作评级。") == []
    # Affirmative violations in multi-clause or double-negated sentences still flag!
    assert set(flagged("不做投资买卖评级，但保证收益翻倍")) == {"保证收益", "翻倍"}
    assert set(flagged("并非不做买入评级")) >= {"评级", "买入"}
