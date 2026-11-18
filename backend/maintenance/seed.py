from datetime import datetime, timezone

from .db import Chunk, Scope, add, find, set_org


DEMO_ORG = "demo"
DEMO_USERS = {
    "tech": ("维修人员", "tech"),
    "library": ("知识管理员", "library"),
    "expert": ("班组专家", "expert"),
    "platform": ("平台管理员", "platform"),
}


def seed_demo(db):
    if find(db, DEMO_ORG, "user", key="tech"):
        return
    for username, (name, role) in DEMO_USERS.items():
        add(db, DEMO_ORG, "user", key=username, status="active", name=name, role=role, demo=True)
    add(db, DEMO_ORG, "device", key="MTR-001", number="MTR-001", model="IM-200",
                 name="1 号输送线电机", batch="A01", location="一号车间 · 输送线", demo=True)
    add(db, DEMO_ORG, "device", key="MTR-002", number="MTR-002", model="IM-200",
        name="2 号输送线电机", batch="A02", location="一号车间 · 输送线", demo=True)
    sample = [
        ("MAN-001", "IM-200 系列电机维护手册", "manual", "2.1", "第 8 页 · 报警记录",
         "演示资料。示例故障码 E07 表示温度告警。记录告警时间、面板提示和当时负载，核对历史趋势与设备铭牌。现场原因须由维修人员核实。"),
        ("WO-001", "输送线电机温度告警记录", "workorder", "1.0", "工单 WO-001",
         "演示工单。现象：设备在连续运行后出现 E07 温度告警。已确认原因：示例环境中通风区域积尘。结果：班组记录处理前后温度，复查告警记录。案例条件仅供参考。"),
        ("CASE-001", "电机振动与异响排查案例", "case", "1.2", "案例 CASE-001",
         "演示案例。现象：电机振动增大，出现间歇性异响。现场记录包含负载、振动发生时间及历史维护单。经班组复核后补充仪表测量记录，案例条件仅供参考。"),
    ]
    set_org(db, DEMO_ORG)
    for number, title, category, version, position, body in sample:
        add(db, DEMO_ORG, "family", key=number)
        source = add(db, DEMO_ORG, "source", key=number + ":" + version, status="published",
                     owner="library", title=title, number=number, version=version, model="IM-200",
                     batch="", category=category, reviewer="expert", indexReady=True, demo=True)
        db.add(Chunk(org=DEMO_ORG, source=source.id, position=position, body=body))
        db.add(Scope(org=DEMO_ORG, status="bind", number=number, source=source.id,
                     model="IM-200", batch="", start=datetime.now(timezone.utc)))

