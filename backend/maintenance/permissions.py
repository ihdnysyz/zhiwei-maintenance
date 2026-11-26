"""Unified RBAC permission matrix shared across backend routes and frontend profiles."""
from .common import fail


class Perm:
    ASK_USE = "ask:use"
    DRAFT_USE = "draft:use"
    KNOWLEDGE_READ = "knowledge:read"
    KNOWLEDGE_MANAGE = "knowledge:manage"
    KNOWLEDGE_REVIEW = "knowledge:review"
    EXPERT_TASK = "expert:task"
    CASE_FEED = "case:feed"
    DEVICE_MANAGE = "device:manage"
    ACCOUNT_MANAGE = "account:manage"
    SYSTEM_MANAGE = "system:manage"


ALL_PERMISSIONS = frozenset({
    Perm.ASK_USE,
    Perm.DRAFT_USE,
    Perm.KNOWLEDGE_READ,
    Perm.KNOWLEDGE_MANAGE,
    Perm.KNOWLEDGE_REVIEW,
    Perm.EXPERT_TASK,
    Perm.CASE_FEED,
    Perm.DEVICE_MANAGE,
    Perm.ACCOUNT_MANAGE,
    Perm.SYSTEM_MANAGE,
})

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "platform": ALL_PERMISSIONS,
    "library": frozenset({
        Perm.ASK_USE,
        Perm.DRAFT_USE,
        Perm.KNOWLEDGE_READ,
        Perm.KNOWLEDGE_MANAGE,
        Perm.CASE_FEED,
    }),
    "expert": frozenset({
        Perm.ASK_USE,
        Perm.DRAFT_USE,
        Perm.KNOWLEDGE_READ,
        Perm.KNOWLEDGE_REVIEW,
        Perm.EXPERT_TASK,
        Perm.CASE_FEED,
    }),
    "tech": frozenset({
        Perm.ASK_USE,
        Perm.DRAFT_USE,
        Perm.KNOWLEDGE_READ,
    }),
}


def user_permissions(user) -> frozenset[str]:
    return ROLE_PERMISSIONS.get(user.data.get("role", ""), frozenset())


def has_perm(user, *perms: str) -> bool:
    granted = user_permissions(user)
    return any(perm in granted for perm in perms)


def require_perm(user, *perms: str, message: str = "当前账号没有此操作权限"):
    if not has_perm(user, *perms):
        fail(403, "FORBIDDEN", message)


def can_manage_knowledge(user) -> bool:
    return has_perm(user, Perm.KNOWLEDGE_MANAGE, Perm.KNOWLEDGE_REVIEW)
