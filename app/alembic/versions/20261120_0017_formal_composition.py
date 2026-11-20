"""Explicit permission to reference a listed version during incubation."""

import sqlalchemy as sa

from alembic import op

revision = "20261120_0017"
down_revision = "20261120_0016"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "formal_method_listing",
        sa.Column(
            "incubation_scope", sa.String(12), nullable=False, server_default="none"
        ),
    )
    op.create_check_constraint(
        "ck_formal_incubation_scope",
        "formal_method_listing",
        "incubation_scope = 'none' OR (scope = 'public' AND "
        "incubation_scope IN ('acquired','platform'))",
    )


def downgrade():
    op.drop_constraint("ck_formal_incubation_scope", "formal_method_listing")
    op.drop_column("formal_method_listing", "incubation_scope")
