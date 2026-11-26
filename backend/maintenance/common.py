import hmac
import re
import secrets

from fastapi import Depends, HTTPException, Request
from sqlalchemy import text

from .db import DB, add, cfg, digest, find, password_hash, timestamp


class Problem(HTTPException):
    def __init__(self, status, code, message):
        super().__init__(status, {"code": code, "message": message})


def fail(status, code, message):
    raise Problem(status, code, message)


def get_db(request: Request):
    with DB() as db:
        if db.bind.dialect.name == "sqlite" and request.method in {"POST", "PATCH", "PUT", "DELETE"}:
            db.execute(text("BEGIN IMMEDIATE"))
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise


def login(db, response, user):
    token = secrets.token_urlsafe(32)
    add(db, user.org, "auth", key=digest(token.encode()), owner=user.key,
        expiry=timestamp() + 28800)
    response.set_cookie("maintenance_session", f"{user.org}.{token}", httponly=True, samesite="strict",
                        secure=cfg.secure_cookie, max_age=28800)
    return profile(user)


def identify(request, db):
    cookie = request.cookies.get("maintenance_session", "")
    if "." in cookie:
        org, token = cookie.split(".", 1)
        if re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", org) and 32 < len(token) < 100:
            auth = find(db, org, "auth", key=digest(token.encode()))
            user = find(db, org, "user", key=auth.owner) if auth and auth.data["expiry"] > timestamp() else None
            if user and user.status == "active":
                return user
    return None


def actor(request: Request, db=Depends(get_db, scope="function")):
    user = identify(request, db)
    if user is None:
        fail(401, "AUTH_REQUIRED", "请先登录工作台")
    return user


def profile(user):
    from .permissions import user_permissions

    return dict(name=user.data["name"], username=user.key, role=user.data["role"],
                org=user.org, demo=bool(user.data.get("demo")),
                primary=bool(user.key == "admin" or user.data.get("primary")),
                models=list(user.data.get("models", [])),
                permissions=sorted(user_permissions(user)))


def role(user, *roles):
    if user.data["role"] not in roles:
        fail(403, "FORBIDDEN", "当前角色没有此操作权限")


def need(row, message="记录不存在或无权访问"):
    if row is None:
        fail(404, "NOT_FOUND", message)
    return row


def revision(row, expected):
    if row.rev != expected:
        fail(409, "STALE_REVISION", "记录已更新。请保留当前输入并重新读取最新版本")


def own(db, user, kind, id, lock=False):
    row = need(find(db, user.org, kind, id=id, lock=lock))
    if row.owner != user.key:
        fail(404, "NOT_FOUND", "记录不存在或无权访问")
    return row


def permitted_device(user, device):
    models = user.data.get("models", [])
    return not models or device["model"] in models


def device_for(db, user, device_id):
    device = need(find(db, user.org, "device", id=device_id), "设备已移除或不可访问")
    if not permitted_device(user, device.data):
        fail(404, "NOT_FOUND", "设备已移除或不可访问")
    return device.view()


def verify_password(value, encoded):
    return bool(encoded and hmac.compare_digest(password_hash(value, encoded.split(":")[0]), encoded))

