import json
import re
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .common import actor, fail, get_db, need, permitted_device, revision, role
from .conversations import redact, turn_view
from .db import Job, Record, Scope, add, audit, audit_events, cfg, current_scopes, enqueue, find, now, password_hash, records, timestamp
from .library import queue_source
from .knowledge import model_call
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


class TaskAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int
    action: Literal["claim", "assign", "reply", "close", "revise"]
    note: str = Field(default="", max_length=4000)
    assignee: str = Field(default="", max_length=64)


class CaseBody(BaseModel):
    text: str = Field(min_length=20, max_length=16000)


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
    role(user, "platform")
    value = device_row(db, user.org, body.model_dump())
    audit(db, user.org, user, "台账更新", value["id"], request.state.request_id)
    return value


@router.post("/devices/sync")
def sync_devices(request: Request, user=Depends(actor), db=Depends(get_db, scope="function")):
    role(user, "platform")
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


@router.get("/accounts")
def accounts(user=Depends(actor), db=Depends(get_db, scope="function")):
    role(user, "platform")
    return [row.view("password") for row in db.scalars(records(user.org, "user").order_by(Record.key))]


@router.post("/accounts")
def upsert_account(body: Account, request: Request, user=Depends(actor), db=Depends(get_db, scope="function")):
    role(user, "platform")
    row = find(db, user.org, "user", key=body.username, lock=True)
    if user.key == body.username and (not body.active or body.role != "platform"):
        fail(409, "ADMIN_REQUIRED", "当前登录账户须保留平台管理员权限")
    if body.password and len(body.password) < 12:
        fail(422, "WEAK_PASSWORD", "本地密码至少 12 个字符")
    data = dict(name=body.name, role=body.role, models=body.models)
    if body.password:
        data["password"] = password_hash(body.password)
    status = "active" if body.active else "disabled"
    if row:
        row.change(status, **data)
    else:
        row = add(db, user.org, "user", key=body.username, status=status, **data)
    audit(db, user.org, user, "账号权限更新", row.id, request.state.request_id)
    return row.view("password")


def can_see_task(user, row):
    return ((user.data["role"] == "expert" or user.key == row.owner)
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
    role(user, "expert")
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
        if other.status != "active" or other.data["role"] != "expert" or not permitted_device(other, row.data):
            fail(422, "ASSIGNEE_SCOPE", "目标专家必须具有设备访问权限")
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
    role(user, "library", "expert")
    row = need(find(db, user.org, "draft", id=draft_id, lock=True))
    device = row.data["device"]
    if row.status != "linked" or not row.data.get("completedAt") or not permitted_device(user, device):
        fail(409, "CASE_NOT_ELIGIBLE", "需要已登记工单及相应设备权限")
    number = f"CASE-{row.id[:12]}"
    text = redact(body.text).encode()
    existing = find(db, user.org, "source", key=f"{number}:1.0")
    if existing:
        if (existing.data.get("category") == "case" and permitted_device(user, existing.data)
                and existing.data.get("workorderLocator") == row.data["externalNumber"] and existing.blob == text):
            return existing.view("digest", "approvedModelEndpoint")
        fail(409, "CASE_ALREADY_SUBMITTED", "该工单已有案例，请在案例回流中打开原记录继续维护")
    info = dict(number=number, version="1.0", title=f"{device['model']} 已结单经验案例",
                model=device["model"], batch=device["batch"], category="case")
    case, _ = queue_source(db, user.org, user.key, info, "脱敏案例.txt", text)
    case.change(workorderLocator=row.data["externalNumber"])
    audit(db, user.org, user, "工单案例回流", case.id, request.state.request_id)
    audit(db, user.org, user, "工单案例回流", row.id, request.state.request_id)
    return case.view("digest")


@router.get("/completed")
def completed(user=Depends(actor), db=Depends(get_db, scope="function")):
    role(user, "library", "expert")
    eligible = records(user.org, "draft").where(Record.status == "linked",
                                                Record.data["completedAt"].as_string() != "")
    models = user.data.get("models")
    if models:
        eligible = eligible.where(Record.data["device"]["model"].as_string().in_(models))
    drafts = list(db.scalars(eligible.order_by(Record.updated.desc()).limit(100)))
    keys = {row.id: f"CASE-{row.id[:12]}:1.0" for row in drafts}
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


@router.get("/operations")
def operations(user=Depends(actor), db=Depends(get_db, scope="function")):
    role(user, "platform")
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
                integrations=dict(model=bool(cfg.model_url and cfg.model_key and cfg.allow_model_data and cfg.chat_model),
                                  embedding=bool(cfg.embedding_model and cfg.allow_model_data and not cfg.model_source_review),
                                  identity=bool(cfg.id_userinfo_url),
                                  registry=bool(cfg.registry_url), workorder=bool(cfg.workorder_url and cfg.workorder_enabled)))


@router.post("/integrations/model/test")
def test_model_connection(user=Depends(actor)):
    role(user, "platform")
    if not (cfg.model_url and cfg.model_key and cfg.chat_model):
        fail(409, "MODEL_NOT_CONFIGURED", "请在本地 .env 填写 MODEL_URL、CHAT_MODEL 和 MODEL_KEY 后重启")
    try:
        response = model_call("chat/completions", {
            "model": cfg.chat_model,
            "temperature": 0,
            "max_tokens": 96,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": 'Return this JSON object exactly: {"ok":true}'}],
        }, connection_test=True)
        if json.loads(response["choices"][0]["message"]["content"]).get("ok") is not True:
            raise ValueError("Unexpected test response")
    except httpx.HTTPStatusError as error:
        status = error.response.status_code
        message = {401: "模型 API Key 无效或未授权", 402: "模型账户余额不足",
                   403: "模型服务拒绝访问", 429: "模型服务暂时限流"}.get(status, f"模型服务返回 HTTP {status}")
        fail(502, "MODEL_REQUEST_FAILED", message)
    except httpx.RequestError:
        fail(502, "MODEL_UNREACHABLE", "无法连接模型服务，请检查网络和接口地址")
    except (ValueError, KeyError, TypeError, IndexError, AttributeError):
        fail(502, "MODEL_RESPONSE_INVALID", "模型未返回符合约定的 JSON，请检查模型名和接口协议")
    return {"status": "ok", "model": cfg.chat_model}


class SubmissionAction(BaseModel):
    revision: int
    action: Literal["check", "retry"]


@router.post("/submissions/{submission_id}/actions", status_code=202)
def check_submission(submission_id: str, body: SubmissionAction, request: Request,
                     user=Depends(actor), db=Depends(get_db, scope="function")):
    role(user, "platform")
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

