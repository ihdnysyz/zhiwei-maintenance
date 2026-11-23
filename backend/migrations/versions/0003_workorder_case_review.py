from alembic import op
from sqlalchemy import select

from maintenance.db import Record, now, uid

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    table = Record.__table__
    cases = connection.execute(select(table.c.id, table.c.org).where(
        table.c.kind == "source",
        table.c.status == "calibration",
        table.c.data["category"].as_string() == "case",
        table.c.data["workorderLocator"].as_string() != "",
        table.c.data["indexReady"].as_boolean().is_(True),
    )).all()
    for case_id, org in cases:
        instant = now()
        change = connection.execute(table.update().where(
            table.c.id == case_id, table.c.status == "calibration",
        ).values(status="review", updated=instant, rev=table.c.rev + 1))
        if change.rowcount:
            event_id = uid()
            connection.execute(table.insert().values(
                id=event_id, key=event_id, org=org, kind="audit", owner="worker", status="", rev=1,
                created=instant, updated=instant,
                data={"target": case_id, "action": "已校验案例转入独立审核", "actor": "worker", "requestId": ""},
            ))


def downgrade():
    pass
