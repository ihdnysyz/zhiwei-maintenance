import os
import re
from contextlib import asynccontextmanager
from typing import Literal

import httpx
from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, text
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException

from . import admin, conversations, drafts, library
from .common import actor, fail, get_db, identify, login, profile, verify_password
from .db import Record, audit, cfg, digest, find, init_db, password_hash, uid
from .seed import DEMO_ORG


@asynccontextmanager
async def lifespan(_):
    if os.getenv("API_INIT", "true").lower() == "true":
        init_db()
    yield


app = FastAPI(title="知维 · 设备维修知识库", version="1.4.0", lifespan=lifespan,
              openapi_url="/api/v1/openapi.json", docs_url="/api/v1/docs", redoc_url=None)
app.openapi_version = "3.1.1"


@app.middleware("http")
async def request_metadata(request, call_next):
    request.state.request_id = uid()
    if request.method in {"POST", "PATCH", "PUT", "DELETE"}:
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse(error(request, "ORIGIN_DENIED", "请求来源不受信任"), 403)
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    response.headers["Cache-Control"] = "private, no-store"
    return response


def error(request, code, message):
    return {"requestId": request.state.request_id, "code": code, "message": message}


@app.exception_handler(HTTPException)
async def request_error(request, exc):
    detail = exc.detail if isinstance(exc.detail, dict) else {"code": "HTTP_ERROR", "message": exc.detail}
    return JSONResponse(error(request, detail["code"], detail["message"]), exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    fields = [".".join(map(str, entry["loc"][1:])) for entry in exc.errors()[:5]]
    return JSONResponse(error(request, "VALIDATION", "请检查输入参数 " + "、".join(fields)), 422)


@app.exception_handler(IntegrityError)
async def concurrency_error(request, exc):
    return JSONResponse(error(request, "CONFLICT", "记录已有更新或重复，请刷新后重试"), 409)


@app.get("/api/v1/health")
def health(db=Depends(get_db, scope="function")):
    db.execute(text("SELECT 1"))
    return {"status": "ok"}


class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=150)
    org: str = Field(default="", max_length=64, pattern=r"^([A-Za-z0-9_-]+)?$")


class PasswordChange(BaseModel):
    currentPassword: str = Field(min_length=1, max_length=150)
    newPassword: str = Field(min_length=6, max_length=150)


class DemoLogin(BaseModel):
    role: Literal["tech", "expert", "platform", "library"] = "tech"


class IdentityToken(BaseModel):
    token: str = Field(min_length=12, max_length=8192)


@app.get("/api/v1/auth/state")
def state(request: Request, db=Depends(get_db, scope="function")):
    user = identify(request, db)
    return {"profile": profile(user) if user else None, "demoAvailable": cfg.demo_mode,
            "defaultOrg": cfg.admin_org,
            "enterpriseIdentity": bool(cfg.id_userinfo_url),
            "workorderEnabled": bool(cfg.workorder_enabled and cfg.workorder_url)}


@app.post("/api/v1/auth/login")
def local_login(body: Credentials, response: Response, db=Depends(get_db, scope="function")):
    org = body.org.strip() or cfg.admin_org
    user = find(db, org, "user", key=body.username.strip())
    if not user or user.status != "active" or not verify_password(body.password, user.data.get("password")):
        fail(401, "INVALID_LOGIN", "组织、账号或密码不正确")
    return login(db, response, user)


@app.post("/api/v1/auth/password")
def change_password(body: PasswordChange, request: Request, response: Response,
                    user=Depends(actor), db=Depends(get_db, scope="function")):
    row = find(db, user.org, "user", key=user.key, lock=True)
    if not row or not verify_password(body.currentPassword, row.data.get("password")):
        fail(401, "INVALID_PASSWORD", "当前密码不正确")
    row.change(password=password_hash(body.newPassword))
    db.execute(delete(Record).where(Record.org == user.org, Record.kind == "auth", Record.owner == user.key))
    audit(db, user.org, row, "修改登录密码", row.id, request.state.request_id)
    return login(db, response, row)


@app.post("/api/v1/auth/enterprise")
def enterprise_login(body: IdentityToken, response: Response, db=Depends(get_db, scope="function")):
    if not cfg.id_userinfo_url:
        fail(409, "IDENTITY_DISABLED", "企业身份服务尚未配置")
    try:
        result = httpx.get(cfg.id_userinfo_url, headers={"Authorization": f"Bearer {body.token}"}, timeout=10)
        result.raise_for_status()
        data = result.json()
        org, subject = data["org"], data["sub"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", org) or not re.fullmatch(r"[A-Za-z0-9_-]{2,64}", subject):
            raise ValueError()
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        fail(401, "IDENTITY_REJECTED", "无法验证企业身份")
    user = find(db, org, "user", key=subject)
    if not user or user.status != "active":
        fail(403, "ACCOUNT_NOT_ASSIGNED", "该企业账号尚未分配工作台权限")
    return login(db, response, user)


@app.post("/api/v1/auth/demo")
def demo_login(body: DemoLogin, response: Response, db=Depends(get_db, scope="function")):
    if not cfg.demo_mode:
        fail(404, "DEMO_DISABLED", "演示模式已关闭")
    user = find(db, DEMO_ORG, "user", key=body.role)
    if not user or user.status != "active":
        fail(404, "DEMO_UNAVAILABLE", "账号不可用")
    return login(db, response, user)


@app.post("/api/v1/auth/logout")
def logout(request: Request, response: Response, db=Depends(get_db, scope="function")):
    cookie = request.cookies.get("maintenance_session", "")
    if "." in cookie:
        org, token = cookie.split(".", 1)
        auth = find(db, org[:64], "auth", key=digest(token.encode()))
        if auth:
            db.delete(auth)
    response.delete_cookie("maintenance_session", httponly=True, samesite="strict", secure=cfg.secure_cookie)
    return {"ok": True}


for router in (admin.router, conversations.router, drafts.router, library.router):
    app.include_router(router, prefix="/api/v1")

