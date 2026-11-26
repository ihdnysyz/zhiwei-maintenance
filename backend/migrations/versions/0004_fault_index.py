from alembic import op
from sqlalchemy import select, text

from maintenance.db import Chunk, FaultIndex, Record
from maintenance.knowledge import fault_index_entries

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    index = FaultIndex.__table__
    chunks = Chunk.__table__
    index.create(connection)
    postgres = connection.dialect.name == "postgresql"
    if postgres:
        op.execute("ALTER TABLE fault_index ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE fault_index FORCE ROW LEVEL SECURITY")
        op.execute("""CREATE POLICY fault_index_org ON fault_index
                      USING (org = current_setting('maintenance.org', true))
                      WITH CHECK (org = current_setting('maintenance.org', true))""")
    orgs = connection.scalars(select(Record.org).where(Record.kind == "source").distinct()).all()
    for org in orgs:
        if postgres:
            connection.execute(text("SELECT set_config('maintenance.org', :org, true)"), {"org": org})
        query = select(chunks.c.id, chunks.c.source, chunks.c.position, chunks.c.body).where(chunks.c.org == org)
        for batch in connection.execute(query).partitions(250):
            entries = [entry for chunk, source, position, body in batch
                       for entry in fault_index_entries(org, source, chunk, position, body)]
            if entries:
                connection.execute(index.insert(), entries)


def downgrade():
    FaultIndex.__table__.drop(op.get_bind())
