"""Store delivery-time public scope and category for idempotent version replay."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "20261128_0023"
down_revision = "20261127_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "knowbot_version", sa.Column("delivery_options", JSONB(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("knowbot_version", "delivery_options")
