from .db import add, find


DEMO_ORG = "demo"
DEMO_USERS = {
    "tech": ("维修人员", "tech"),
    "library": ("知识管理员", "library"),
    "expert": ("班组专家", "expert"),
    "platform": ("平台管理员", "platform"),
}


def seed_demo_accounts(db):
    if find(db, DEMO_ORG, "user", key="tech"):
        return
    for username, (name, role) in DEMO_USERS.items():
        add(db, DEMO_ORG, "user", key=username, status="active", name=name, role=role, demo=True)

