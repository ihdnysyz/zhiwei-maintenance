from alembic import op
from maintenance.db import Chunk, Job, Record

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    for table in (Record, Chunk, Job):
        table.__table__.create(connection)
    if connection.dialect.name == "postgresql":
        op.execute("CREATE TABLE chunks_other PARTITION OF chunks DEFAULT")
        op.execute("ALTER TABLE chunks ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE chunks FORCE ROW LEVEL SECURITY")
        op.execute("""CREATE POLICY chunk_org ON chunks USING (org = current_setting('maintenance.org', true))
                      WITH CHECK (org = current_setting('maintenance.org', true))""")
        op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'maintenance_app') THEN
                      REVOKE ALL ON chunks_other FROM maintenance_app;
                      END IF; END $$""")


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TABLE chunks_other")
    for table in (Job, Chunk, Record):
        table.__table__.drop(op.get_bind())

