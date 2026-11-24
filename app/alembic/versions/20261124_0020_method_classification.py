"""Owner-selected capability facets shared by listings of an exact method version."""

from alembic import op

revision = "20261124_0020"
down_revision = "20261124_0019"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE formal_method_classification (
        method_id uuid NOT NULL, version integer NOT NULL, digest varchar(71) NOT NULL,
        owner_subject varchar(512) NOT NULL, code varchar(64) NOT NULL,
        PRIMARY KEY(method_id,version,digest,owner_subject))""")


def downgrade():
    op.drop_table("formal_method_classification")
