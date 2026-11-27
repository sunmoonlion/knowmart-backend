"""Unify formal method listings across KnowMart catalog, shops, trades and admin."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261126_0021"
down_revision = "20261124_0020"
branch_labels = None
depends_on = None
table = "formal_method_listing"


def upgrade() -> None:
    op.add_column(
        table,
        sa.Column(
            "public_contract",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(table, sa.Column("directory_category", sa.String(64), nullable=True))
    op.add_column(
        table,
        sa.Column(
            "domain_tags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(table, sa.Column("price", sa.Numeric(12, 2), nullable=True))
    op.add_column(table, sa.Column("delist_reason", sa.String(32), nullable=True))
    op.add_column(
        table,
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.add_column(
        table,
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.execute(
        """
        DO $$
        DECLARE r record;
        BEGIN
            FOR r IN
                SELECT conname
                FROM pg_constraint
                WHERE conrelid = 'formal_method_listing'::regclass
                  AND contype = 'c'
                  AND pg_get_constraintdef(oid) LIKE '%status%'
            LOOP
                EXECUTE format(
                    'ALTER TABLE formal_method_listing DROP CONSTRAINT %I',
                    r.conname
                );
            END LOOP;
        END $$;
        """
    )
    op.create_check_constraint(
        "ck_formal_listing_status",
        table,
        "status IN ('pending','listed','withdrawn','rejected','taken_down')",
    )
    op.execute(
        """
        UPDATE formal_method_listing
        SET incubation_scope = 'none'
        WHERE incubation_scope <> 'none' AND method_kind IS DISTINCT FROM 'tool'
        """
    )
    op.create_check_constraint(
        "ck_formal_incubation_tool_only",
        table,
        "incubation_scope = 'none' OR method_kind = 'tool'",
    )
    op.execute(
        """
        UPDATE formal_method_listing l
        SET directory_category = c.code
        FROM formal_method_classification c
        WHERE l.owner_subject = c.owner_subject
          AND l.method_id = c.method_id
          AND l.version = c.version
          AND l.digest = c.digest
          AND l.directory_category IS NULL
        """
    )
    op.create_index(
        "ix_trade_buyer_formal_ref",
        "trade",
        ["buyer_subject", "listing_ref"],
        unique=True,
        postgresql_where=sa.text("kind IN ('formal_expert', 'formal_tool')"),
    )


def downgrade() -> None:
    op.drop_index("ix_trade_buyer_formal_ref", table_name="trade")
    op.drop_constraint("ck_formal_incubation_tool_only", table, type_="check")
    op.execute(
        "UPDATE formal_method_listing "
        "SET status = 'withdrawn' WHERE status = 'taken_down'"
    )
    op.drop_constraint("ck_formal_listing_status", table, type_="check")
    op.create_check_constraint(
        "formal_method_listing_status_check",
        table,
        "status IN ('pending','listed','withdrawn','rejected')",
    )
    op.drop_column(table, "updated_at")
    op.drop_column(table, "created_at")
    op.drop_column(table, "delist_reason")
    op.drop_column(table, "price")
    op.drop_column(table, "domain_tags")
    op.drop_column(table, "directory_category")
    op.drop_column(table, "public_contract")
