"""Align the expert trade index on databases which already passed 0014."""
from alembic import op
import sqlalchemy as sa

revision = "20261128_0024"
down_revision = "20261128_0023"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE trade ADD COLUMN IF NOT EXISTS duplicate_of uuid")
    op.drop_index("ix_trade_buyer_expert_ref", table_name="trade")
    op.create_index("ix_trade_buyer_expert_ref", "trade", ["buyer_subject", "listing_ref"],
                    unique=True, postgresql_where=sa.text("kind='expert' AND duplicate_of IS NULL"))


def downgrade():
    # The corrected historical 0014 also uses this field and the same index.
    pass
