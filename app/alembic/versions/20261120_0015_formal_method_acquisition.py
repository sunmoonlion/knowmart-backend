"""Owner-scoped, version-pinned formal method listings and acquisitions."""

import sqlalchemy as sa

from alembic import op

revision = "20261120_0015"
down_revision = "20261101_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "formal_method_listing",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_subject", sa.String(512), nullable=False),
        sa.Column("method_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("digest", sa.String(71), nullable=False),
        sa.Column("delivery_digest", sa.String(71), nullable=False),
        sa.Column(
            "publisher_id",
            sa.Uuid(),
            sa.ForeignKey("publisher.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reviewed_by", sa.String(512)),
        sa.Column("review_note", sa.Text()),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("owner_subject", "method_id", "version"),
        sa.CheckConstraint("version > 0"),
        sa.CheckConstraint("status IN ('pending','listed','withdrawn','rejected')"),
    )
    op.create_table(
        "formal_method_acquisition",
        sa.Column(
            "listing_id",
            sa.Uuid(),
            sa.ForeignKey("formal_method_listing.id"),
            primary_key=True,
        ),
        sa.Column("buyer_subject", sa.String(512), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("formal_method_acquisition")
    op.drop_table("formal_method_listing")
