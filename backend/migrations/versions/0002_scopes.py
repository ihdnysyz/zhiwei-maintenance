from datetime import datetime

from alembic import op
from sqlalchemy import select
from maintenance.db import Record, Scope

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    table = Scope.__table__
    table.create(connection, checkfirst=True)
    for org, state, data in connection.execute(select(Record.org, Record.status, Record.data).where(Record.kind == "scope")):
        connection.execute(table.insert().values(
            org=org, number=data["number"], model=data["model"], batch=data["batch"], source=data["sourceId"],
            status=state, start=datetime.fromisoformat(data["start"]),
            end=datetime.fromisoformat(data["end"]) if data["end"] else None,
        ))
    if connection.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
        op.execute("""ALTER TABLE effective_scopes ADD CONSTRAINT effective_scope_no_overlap EXCLUDE USING gist
            (org WITH =, number WITH =, model WITH =, batch WITH =, tstzrange(start, "end", '[)') WITH &&)""")


def downgrade():
    Scope.__table__.drop(op.get_bind())

