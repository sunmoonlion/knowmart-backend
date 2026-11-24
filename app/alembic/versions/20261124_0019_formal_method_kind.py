"""Classify exact market versions from their approved frozen definitions."""

import sqlalchemy as sa

from alembic import op

revision = "20261124_0019"
down_revision = "20261122_0018"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("formal_method_listing", sa.Column("method_kind", sa.String(8)))
    op.create_check_constraint(
        "ck_formal_method_kind",
        "formal_method_listing",
        "method_kind IN ('tool','expert')",
    )


def downgrade():
    op.drop_constraint("ck_formal_method_kind", "formal_method_listing")
    op.drop_column("formal_method_listing", "method_kind")
