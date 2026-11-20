"""Keep public listings and acquisitions intact when the author lists for self use."""

import sqlalchemy as sa

from alembic import op

revision = "20261120_0016"
down_revision = "20261120_0015"
branch_labels = None
depends_on = None
table = "formal_method_listing"


def upgrade():
    op.add_column(
        table, sa.Column("scope", sa.String(8), nullable=False, server_default="public")
    )
    op.alter_column(table, "delivery_digest", nullable=True)
    op.alter_column(table, "publisher_id", nullable=True)
    op.drop_constraint(
        "formal_method_listing_owner_subject_method_id_version_key",
        table,
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_formal_listing_scope",
        table,
        ["owner_subject", "method_id", "version", "scope"],
    )
    op.create_check_constraint(
        "ck_formal_listing_scope",
        table,
        "(scope = 'self' AND publisher_id IS NULL AND delivery_digest IS NULL) OR "
        "(scope = 'public' AND publisher_id IS NOT NULL "
        "AND delivery_digest IS NOT NULL)",
    )


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM formal_method_listing WHERE scope='self') THEN
            RAISE EXCEPTION 'Self-use listings require forward repair';
        END IF; END $$""")
    op.drop_constraint("ck_formal_listing_scope", table)
    op.drop_constraint("uq_formal_listing_scope", table)
    op.create_unique_constraint(
        "formal_method_listing_owner_subject_method_id_version_key",
        table,
        ["owner_subject", "method_id", "version"],
    )
    op.alter_column(table, "delivery_digest", nullable=False)
    op.alter_column(table, "publisher_id", nullable=False)
    op.drop_column(table, "scope")
