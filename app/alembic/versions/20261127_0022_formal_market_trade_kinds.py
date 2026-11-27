"""Accept formal method offers and trades on existing KnowMart databases."""

from alembic import op

revision = "20261127_0022"
down_revision = "20261126_0021"
branch_labels = None
depends_on = None


def _replace_kind_checks(kinds: str) -> None:
    op.drop_constraint("ck_trade_kind", "trade", type_="check")
    op.create_check_constraint("ck_trade_kind", "trade", f"kind IN ({kinds})")
    op.drop_constraint("ck_demand_response_listing", "demand_response", type_="check")
    op.create_check_constraint(
        "ck_demand_response_listing",
        "demand_response",
        "(listing_kind IS NULL AND listing_ref IS NULL)"
        f" OR (listing_kind IN ({kinds}) AND listing_ref IS NOT NULL)",
    )


def upgrade() -> None:
    _replace_kind_checks("'expert', 'knowledge', 'formal_expert', 'formal_tool'")


def downgrade() -> None:
    # PostgreSQL validates existing rows before committing either constraint change.
    _replace_kind_checks("'expert', 'knowledge'")
