"""Confirm old knowledge takedowns with the vault."""

from alembic import op

revision = "20261128_0025"
down_revision = "20261128_0024"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE trade ADD COLUMN revoke_attempted_at timestamptz")
    op.execute("""UPDATE trade SET failure='violation_revoke_pending',
        status=CASE WHEN status='granting' THEN 'failed' ELSE status END
        WHERE kind='knowledge' AND failure='violation_taken_down'""")


def downgrade():
    # A missing receipt must stay pending across a downgrade.
    op.drop_column("trade", "revoke_attempted_at")
