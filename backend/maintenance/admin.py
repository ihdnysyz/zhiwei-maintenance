import json
import re
from typing import Literal
from urllib.request import getproxies

import httpx
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select

from .common import actor, fail, get_db, need, permitted_device, revision
from .conversations import redact, turn_view
from .db import Job, Record, Scope, add, audit, audit_events, cfg, current_scopes, enqueue, find, now, password_hash, records, timestamp
from .library import WORKORDER_CASE_VERSION, queue_source, workorder_case_number
from .knowledge import CODE_LABEL
from .model_config import (
    chat_completion,
    chat_ready,
    embed_texts_remote,
    embedding_ready,
    get_ai_settings,
    public_ai_settings,
    save_ai_settings,
)
from .permissions import Perm, has_perm, require_perm
from .worker import external_headers

router = APIRouter()


class DeviceBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    number: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=120)
    batch: str = Field(default="", max_length=80)
    location: str = Field(default="", max_length=120)


class Account(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    username: str = Field(min_length=2, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    name: str = Field(min_length=1, max_length=80)
    role: Literal["tech", "library", "expert", "platform"]
    password: str = Field(default="", max_length=150)
    models: list[str] = Field(default_factory=list, max_length=100)
    active: bool = True


class AccountBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accounts: list[Account] = Field(min_length=1, max_length=50)


class AccountBatchCreated(BaseModel):
    count: int


class TaskAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int
    action: Literal["claim", "assign", "reply", "close", "revise"]
    note: str = Field(default="", max_length=4000)
    assignee: str = Field(default="", max_length=64)


class CaseBody(BaseModel):
    text: str = Field(min_length=20, max_length=16000)


class AISettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    chatUrl: str = Field(default="", max_length=300)
    chatApiKey: str | None = Field(default=None, max_length=300)
    clearChatApiKey: bool = False
    chatModel: str = Field(default="", max_length=120)
    embeddingUrl: str = Field(default="", max_length=300)
    embeddingApiKey: str | None = Field(default=None, max_length=300)
    clearEmbeddingApiKey: bool = False
    embeddingModel: str = Field(default="", max_length=120)
    chunkSize: int = Field(default=600, ge=200, le=2000)
    chunkOverlap: int = Field(default=100, ge=0, le=400)
    topK: int = Field(default=6, ge=2, le=15)
    maxReactSteps: int = Field(default=5, ge=1, le=8)


def device_row(db, org, info):
    row = find(db, org, "device", key=info["number"], lock=True)
    if row:
        row.change(**info)
    else:
        row = add(db, org, "device", key=info["number"], **info)
    return row.view()


@router.get("/devices")
def devices(user=Depends(actor), db=Depends(get_db, scope="function")):
    return [row.view() for row in db.scalars(records(user.org, "device").order_by(Record.key))
            if permitted_device(user, row.data)]


@router.post("/devices")
def upsert_device(body: DeviceBody, request: Request, user=Depends(actor), db=Depends(get_db, scope="function")):
    require_perm(user, Perm.DEVICE_MANAGE)
    value = device_row(db, user.org, body.model_dump())
    audit(db, user.org, user, "台账更新", value["id"], request.state.request_id)
    return value


@router.post("/devices/sync")
def sync_devices(request: Request, user=Depends(actor), db=Depends(get_db, scope="function")):
    require_perm(user, Perm.DEVICE_MANAGE)
    if not cfg.registry_url:
        fail(409, "NOT_CONFIGURED", "请配置企业设备台账接口")
    try:
        response = httpx.get(cfg.registry_url, headers=external_headers(), timeout=12)
        response.raise_for_status()
        rows = [DeviceBody.model_validate(row).model_dump() for row in response.json()["devices"]]
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        fail(502, "REGISTRY_FAILED", "台账同步失败，原有设备记录已保留")
    for info in rows:
        device_row(db, user.org, info)
    audit(db, user.org, user, "台账同步", str(len(rows)), request.state.request_id)
    return {"count": len(rows)}


def is_primary_admin(row):
    return bool(row and (row.key == "admin" or row.data.get("primary")))


def account_view(row):
    return {**row.view("password"), "primary": is_primary_admin(row)}


@router.get("/accounts")
def accounts(user=Depends(actor), db=Depends(get_db, scope="function")):
    require_perm(user, Perm.ACCOUNT_MANAGE)
    return [account_view(row) for row in db.scalars(records(user.org, "user").order_by(Record.key))]


def account_values(body: Account):
    if body.password and len(body.password) < 12:
        fail(422, "WEAK_PASSWORD", f"账号 {body.username} 的本地密码至少需要 12 个字符")
    values = dict(name=body.name, role=body.role, models=body.models, status="active" if body.active else "disabled")
    if body.password:
        values["password"] = password_hash(body.password)
    return values


@router.post("/accounts")
def upsert_account(body: Account, request: Request, user=Depends(actor), db=Depends(get_db, scope="function")):
    require_perm(user, Perm.ACCOUNT_MANAGE)
    row = find(db, user.org, "user", key=body.username, lock=True)
    if row and is_primary_admin(row):
        if not is_primary_admin(user):
            fail(403, "PRIMARY_ADMIN_PROTECTED", "仅主管理员可修改主管理员账号")
        if not body.active or body.role != "platform":
            fail(409, "ADMIN_REQUIRED", "主管理员账号须保持启用且保留平台管理员权限")
    if user.key == body.username and (not body.active or body.role != "platform"):
        fail(409, "ADMIN_REQUIRED", "当前登录账户须保留平台管理员权限")
    data = account_values(body)
    if row:
        revoke = (bool(body.password) or row.status != data["status"]
                  or row.data.get("role") != data["role"] or row.data.get("models", []) != data["models"])
        row.change(**data)
        if revoke and row.key != user.key:
            db.execute(delete(Record).where(Record.org == user.org, Record.kind == "auth", Record.owner == row.key))
    else:
        row = add(db, user.org, "user", key=body.username, **data)
    audit(db, user.org, user, "账号权限更新", row.id, request.state.request_id)
    return account_view(row)


@router.delete("/accounts/{username}", status_code=204)
def delete_account(username: str, request: Request, user=Depends(actor), db=Depends(get_db, scope="function")):
    require_perm(user, Perm.ACCOUNT_MANAGE)
    row = need(find(db, user.org, "user", key=username, lock=True), "账号不存在")
    if is_primary_admin(row):
        fail(403, "PRIMARY_ADMIN_PROTECTED", "主管理员账号不可删除")
    if row.key == user.key:
        fail(409, "ADMIN_REQUIRED", "不可删除当前登录账号")
    if row.data.get("role") == "platform" and not is_primary_admin(user):
        fail(403, "PRIMARY_ADMIN_REQUIRED", "仅主管理员可删除其他平台管理员")
    db.execute(delete(Record).where(Record.org == user.org, Record.kind == "auth", Record.owner == row.key))
    audit(db, user.org, user, "删除账号", row.key, request.state.request_id)
    db.delete(row)
    return Response(status_code=204)


@router.post("/accounts/batch", status_code=201)
def create_accounts(body: AccountBatch, request: Request, user=Depends(actor),
                    db=Depends(get_db, scope="function")) -> AccountBatchCreated:
    require_perm(user, Perm.ACCOUNT_MANAGE)
    usernames = set()
    for item in body.accounts:
        if item.username in usernames:
            fail(409, "DUPLICATE_ACCOUNT", f"本批次内账号 {item.username} 重复，请修改后重试")
        usernames.add(item.username)
    existing = db.scalar(select(Record.key).where(
        Record.org == user.org, Record.kind == "user", Record.key.in_(usernames)).order_by(Record.key).limit(1))
    if existing:
        fail(409, "ACCOUNT_EXISTS", f"账号 {existing} 已存在，请修改后重试")
    for item in sorted(body.accounts, key=lambda account: account.username):
        row = add(db, user.org, "user", key=item.username, **account_values(item))
        audit(db, user.org, user, "批量创建账号", row.id, request.state.request_id)
    return AccountBatchCreated(count=len(body.accounts))


def can_see_task(user, row):
    return ((has_perm(user, Perm.EXPERT_TASK) or user.key == row.owner)
            and permitted_device(user, row.data))


def task_view(row):
    due = row.data.get("dueAt")
    return {**row.view(), "overdue": bool(due and row.status in {"open", "assigned"} and due < now())}


@router.get("/tasks")
def tasks(user=Depends(actor), db=Depends(get_db, scope="function")):
    return [task_view(row) for row in db.scalars(records(user.org, "expert").order_by(Record.updated.desc()))
            if can_see_task(user, row)]


@router.get("/tasks/{task_id}")
def task(task_id: str, user=Depends(actor), db=Depends(get_db, scope="function")):
    row = need(find(db, user.org, "expert", id=task_id))
    if not can_see_task(user, row):
        fail(404, "NOT_FOUND", "待办不存在")
    session = need(find(db, user.org, "session", id=row.data["sessionId"]))
    turn = need(find(db, user.org, "turn", id=row.data["turnId"]))
    return {**task_view(row), "turn": turn_view(db, user, turn, session),
            "operationHistory": audit_events(db, user.org, [row.id])}


@router.post("/tasks/{task_id}/actions")
def task_action(task_id: str, body: TaskAction, request: Request, user=Depends(actor), db=Depends(get_db, scope="function")):
    require_perm(user, Perm.EXPERT_TASK)
    row = need(find(db, user.org, "expert", id=task_id, lock=True))
    if not can_see_task(user, row):
        fail(404, "NOT_FOUND", "待办不存在")
    revision(row, body.revision)
    assigned = row.data["assignee"]
    note = body.note.strip()
    if body.action == "claim" and row.status == "open":
        row.change("assigned", assignee=user.key)
    elif body.action == "assign" and row.status in {"open", "assigned"} and (not assigned or assigned == user.key):
        other = need(find(db, user.org, "user", key=body.assignee), "专家账号不存在")
        if other.status != "active" or not has_perm(other, Perm.EXPERT_TASK) or not permitted_device(other, row.data):
            fail(422, "ASSIGNEE_SCOPE", "目标账号必须具有专家处理与设备访问权限")
        row.change("assigned", assignee=other.key)
    elif body.action == "reply" and row.status in {"assigned", "answered"} and assigned == user.key and note:
        row.change("answered", reply=note)
    elif body.action == "close" and row.status == "answered" and assigned == user.key:
        row.change("closed")
    elif body.action == "revise" and row.status in {"answered", "closed"} and assigned == user.key and note:
        if row.data.get("revisionCandidate"):
            fail(409, "CANDIDATE_EXISTS", "此待办已有修订候选")
        info = dict(number=f"EX-{row.id[:12]}", version="1.0", title="专家知识修订候选",
                    model=row.data["model"], batch="", category="case")
        source, _ = queue_source(db, user.org, user.key, info, "修订候选.txt", redact(note).encode())
        row.change(revisionCandidate=source.id)
        audit(db, user.org, user, "提交知识修订", source.id, request.state.request_id)
    else:
        fail(409, "TASK_STATE", "当前状态不支持此操作，或需要补充答复")
    audit(db, user.org, user, "专家待办·" + body.action, row.id, request.state.request_id)
    return {**task_view(row), "operationHistory": audit_events(db, user.org, [row.id])}


@router.post("/drafts/{draft_id}/case")
def feed_case(draft_id: str, body: CaseBody, request: Request, user=Depends(actor), db=Depends(get_db, scope="function")):
    require_perm(user, Perm.CASE_FEED)
    row = need(find(db, user.org, "draft", id=draft_id, lock=True))
    device = row.data["device"]
    if row.status != "linked" or not row.data.get("completedAt") or not permitted_device(user, device):
        fail(409, "CASE_NOT_ELIGIBLE", "需要已登记工单及相应设备权限")
    number = workorder_case_number(row.id)
    original = redact(body.text).encode()
    fault = row.data.get("fields", {}).get("faultCode", "").strip()
    text = original
    if fault and not re.search(rf"(?im)^[ \t]*(?:{CODE_LABEL})[ \t]*[：:]?[ \t]*\S", body.text):
        text = redact(f"故障码：{fault}\n{body.text}").encode()
    existing = find(db, user.org, "source", key=f"{number}:{WORKORDER_CASE_VERSION}")
    if existing:
        if (existing.data.get("category") == "case" and permitted_device(user, existing.data)
                and existing.data.get("workorderLocator") == row.data["externalNumber"] and existing.blob in {original, text}):
            return existing.view("digest", "approvedModelEndpoint")
        fail(409, "CASE_ALREADY_SUBMITTED", "该工单已有案例，请在案例回流中打开原记录继续维护")
    info = dict(number=number, version=WORKORDER_CASE_VERSION, title=f"{device['model']} 已结单经验案例",
                model=device["model"], batch=device["batch"], category="case")
    case, _ = queue_source(db, user.org, user.key, info, "脱敏案例.txt", text)
    case.change(workorderLocator=row.data["externalNumber"])
    audit(db, user.org, user, "工单案例回流", case.id, request.state.request_id)
    audit(db, user.org, user, "工单案例回流", row.id, request.state.request_id)
    return case.view("digest")


@router.get("/completed")
def completed(user=Depends(actor), db=Depends(get_db, scope="function")):
    require_perm(user, Perm.CASE_FEED)
    eligible = records(user.org, "draft").where(Record.status == "linked",
                                                Record.data["completedAt"].as_string() != "")
    models = user.data.get("models")
    if models:
        eligible = eligible.where(Record.data["device"]["model"].as_string().in_(models))
    drafts = list(db.scalars(eligible.order_by(Record.updated.desc()).limit(100)))
    keys = {row.id: f"{workorder_case_number(row.id)}:{WORKORDER_CASE_VERSION}" for row in drafts}
    sources = {row.key: row for row in db.scalars(records(user.org, "source").where(Record.key.in_(keys.values())))
               if permitted_device(user, row.data)}
    active = {row.source for row in db.scalars(current_scopes(user.org).where(
        Scope.status == "bind", Scope.source.in_({row.id for row in sources.values()})))}
    histories = {}
    for event in audit_events(db, user.org, keys.keys()):
        histories.setdefault(event["target"], []).append(event)
    result = []
    for draft in drafts:
        item = draft.view("snapshot", "changes", "origins")
        item["operationHistory"] = histories.get(draft.id, [])
        case = sources.get(keys[draft.id])
        if case:
            item["caseSource"] = dict(id=case.id, status=case.status, active=case.id in active)
        result.append(item)
    return result


@router.get("/settings/ai")
def read_ai_settings(user=Depends(actor), db=Depends(get_db, scope="function")):
    require_perm(user, Perm.SYSTEM_MANAGE)
    return public_ai_settings(get_ai_settings(db, user.org))


@router.put("/settings/ai")
def update_ai_settings(body: AISettingsBody, request: Request, user=Depends(actor), db=Depends(get_db, scope="function")):
    require_perm(user, Perm.SYSTEM_MANAGE)
    saved = save_ai_settings(db, user.org, body.model_dump())
    audit(db, user.org, user, "更新模型与RAG配置", "ai", request.state.request_id)
    return public_ai_settings(saved)


@router.get("/operations")
def operations(user=Depends(actor), db=Depends(get_db, scope="function")):
    require_perm(user, Perm.SYSTEM_MANAGE)
    ai = get_ai_settings(db, user.org)
    jobs = list(db.scalars(select(Job).where(Job.org == user.org).order_by(Job.available.desc()).limit(100)))
    history = db.scalars(records(user.org, "audit").order_by(Record.created.desc()).limit(100))
    submissions = list(db.scalars(records(user.org, "submission").order_by(Record.updated.desc()).limit(100)))
    submission_history = {}
    for event in audit_events(db, user.org, [row.id for row in submissions]):
        submission_history.setdefault(event["target"], []).append(event)
    alerts = []
    for kind in ("turn", "parse", "submit", "check"):
        recent = [job for job in jobs if job.kind == kind and job.state in {"done", "failed"}][:5]
        if len(recent) == 5 and all(job.error for job in recent):
            alerts.append({"kind": kind, "message": "连续 5 个后台任务失败或降级"})
    return dict(alerts=alerts, jobs=[dict(id=job.id, kind=job.kind, state=job.state, attempts=job.attempts,
                           error=job.error) for job in jobs], audit=[row.view() for row in history],
                submissions=[{**row.view("payload"), "operationHistory": submission_history.get(row.id, [])}
                             for row in submissions],
                integrations=dict(model=chat_ready(ai),
                                  embedding=embedding_ready(ai),
                                  identity=bool(cfg.id_userinfo_url),
                                  registry=bool(cfg.registry_url), workorder=bool(cfg.workorder_url and cfg.workorder_enabled)))


@router.post("/integrations/model/test")
def test_model_connection(user=Depends(actor), db=Depends(get_db, scope="function")):
    require_perm(user, Perm.SYSTEM_MANAGE)
    ai = get_ai_settings(db, user.org)
    if not (ai["chatUrl"] and ai["chatModel"]):
        fail(409, "MODEL_NOT_CONFIGURED", "请先填写对话模型接口地址与模型名称")
    try:
        response = chat_completion(
            ai,
            [{"role": "user", "content": 'Return this JSON object exactly: {"ok":true}'}],
            temperature=0,
            max_tokens=96,
            response_format={"type": "json_object"},
            timeout=15.0,
            ignore_enabled=True,
        )
        content = response["choices"][0]["message"]["content"]
        if json.loads(content).get("ok") is not True:
            raise ValueError("Unexpected test response")
        emb_dim = 0
        if ai.get("embeddingModel"):
            vecs = embed_texts_remote(ai, ["ping"], timeout=15.0, ignore_enabled=True)
            emb_dim = len(vecs[0]) if vecs else 0
    except httpx.HTTPStatusError as error:
        status = error.response.status_code
        message = {401: "模型 API Key 无效或未授权", 402: "模型账户余额不足",
                   403: "模型服务拒绝访问", 429: "模型服务暂时限流"}.get(status, f"模型服务返回 HTTP {status}")
        fail(502, "MODEL_REQUEST_FAILED", message)
    except httpx.RequestError as error:
        message = str(error)
        system_code = re.search(r"\[(?:WinError|Errno) \d+\]", message)
        tls_code = re.search(r"\[SSL: ([A-Z_]+)\]", message)
        diagnostic = " · ".join(filter(None, [
            type(error).__name__, system_code.group() if system_code else "",
            tls_code.group(1) if tls_code else "",
            "已检测到 HTTPS 代理" if getproxies().get("https") else "未检测到 HTTPS 代理",
        ]))
        fail(502, "MODEL_UNREACHABLE", f"无法连接模型服务（{diagnostic}），请检查网络和接口地址")
    except (ValueError, KeyError, TypeError, IndexError, AttributeError):
        fail(502, "MODEL_RESPONSE_INVALID", "模型未返回符合约定的响应，请检查模型名和接口协议")
    return {"status": "ok", "model": ai["chatModel"], "embeddingModel": ai.get("embeddingModel", ""), "embeddingDim": emb_dim}


class SubmissionAction(BaseModel):
    revision: int
    action: Literal["check", "retry"]


@router.post("/submissions/{submission_id}/actions", status_code=202)
def check_submission(submission_id: str, body: SubmissionAction, request: Request,
                     user=Depends(actor), db=Depends(get_db, scope="function")):
    require_perm(user, Perm.SYSTEM_MANAGE)
    row = need(find(db, user.org, "submission", id=submission_id, lock=True))
    revision(row, body.revision)
    if row.status not in {"manual_review", "not_created"}:
        fail(409, "RECONCILIATION_STATE", "仅待人工核对或终局未创建的工单可以处理")
    if body.action == "retry":
        if row.status != "not_created" or not row.data.get("sameKeyRetry") or not cfg.workorder_enabled:
            fail(409, "RETRY_DENIED", "缺少外部系统提供的同键重试证据")
        row.change("retry_queued", checkSince=timestamp())
        enqueue(db, user.org, "submit", row.id)
    else:
        row.change("processing", checkSince=timestamp())
        enqueue(db, user.org, "check", row.id)
    audit(db, user.org, user, "外部工单·重试" if body.action == "retry" else "外部工单·核对", row.id, request.state.request_id)
    return row.view("payload")

