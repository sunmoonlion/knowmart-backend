"""Look up exact acquired versions starting from the buyer's planning catalog."""

from alembic import op

revision = "20261122_0018"
down_revision = "20261120_0017"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_formal_acquisition_buyer_listing",
        "formal_method_acquisition",
        ["buyer_subject", "listing_id"],
    )


def downgrade():
    op.drop_index(
        "ix_formal_acquisition_buyer_listing", table_name="formal_method_acquisition"
    )
