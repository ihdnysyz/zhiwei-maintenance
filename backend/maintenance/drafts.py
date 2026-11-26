import json
import re
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from fastapi import APIRouter, Depends, File, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import IntegrityError

from .common import actor, device_for, fail, get_db, need, own, permitted_device, revision
from .conversations import active_session, question_context, session_turns, turn_view
from .db import Record, add, audit, audit_events, cfg, digest, enqueue, find, now, records, uid
from .knowledge import applicability, code_key, fault_codes, symptom_signals
from .library import WORKORDER_CASE_VERSION, file_response, workorder_case_number

router = APIRouter()
FIELDS = {"symptom": "故障现象", "faultCode": "故障码", "cause": "已确认原因",
          "actions": "实际处置", "result": "现场结果"}
REQUIRED = ("symptom", "cause", "actions", "result")
ATTACHMENTS = {".pdf": ("application/pdf", b"%PDF-"), ".png": ("image/png", b"\x89PNG\r\n\x1a\n"),
               ".jpg": ("image/jpeg", b"\xff\xd8\xff"), ".jpeg": ("image/jpeg", b"\xff\xd8\xff")}


class DraftCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sessionId: str


class DraftPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int
    fields: dict[Literal["symptom", "faultCode", "cause", "actions", "result"], str]
    number: str | None = Field(default=None, max_length=64)

    @field_validator("fields")
    @classmethod
    def validate_fields(cls, value):
        if any(len(text) > 8000 for text in value.values()):
            raise ValueError("每个字段不超过 8000 字")
        return {key: text.strip() for key, text in value.items()}


class Revision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int


class Register(Revision):
    number: str = Field(min_length=3, max_length=64)


class Confirm(Revision):
    number: str | None = Field(default=None, max_length=64)


def ticket_number(value):
    number = value.strip()
    if not re.fullmatch(cfg.allowed_ticket_pattern, number):
        fail(422, "INVALID_TICKET", "外部工单编号格式不正确")
    return number


def link_ticket(db, row, number):
    key = number.upper()
    if find(db, row.org, "ticket", key=key):
        fail(409, "DUPLICATE_TICKET", "该外部单号已登记")
    try:
        with db.begin_nested():
            add(db, row.org, "ticket", key=key, owner=row.id)
            db.flush()
    except IntegrityError:
        if not find(db, row.org, "ticket", key=key):
            raise
        fail(409, "DUPLICATE_TICKET", "该外部单号已登记")


def attachments(db, org, draft_id):
    return list(db.scalars(records(org, "attachment").where(Record.owner == draft_id).order_by(Record.created)))


def case_progress(db, user, draft):
    if not draft.data.get("completedAt") or not permitted_device(user, draft.data["device"]):
        return None
    number = workorder_case_number(draft.id)
    case = find(db, user.org, "source", key=f"{number}:{WORKORDER_CASE_VERSION}")
    if (not case or case.data.get("category") != "case" or not permitted_device(user, case.data)
            or case.data.get("workorderLocator") != draft.data.get("externalNumber")):
        return {"status": "not_submitted"}
    device = find(db, user.org, "device", id=draft.data["device"]["id"])
    applicable, _ = applicability(db, user.org, device.data) if device and permitted_device(user, device.data) else ({}, False)
    return {"status": case.status, "sourceId": case.id if case.id in applicable else None}


def view(db, user, row):
    session = find(db, user.org, "session", id=row.data["sessionId"])
    ref = []
    if session and row.data.get("turnId"):
        turn = find(db, user.org, "turn", id=row.data["turnId"])
        if turn:
            ref = turn_view(db, user, turn, session).get("citations", [])
    submission = find(db, user.org, "submission", key=row.id) if row.status in {"submitting", "linked"} else None
    targets = [row.id, submission.id] if submission else [row.id]
    return {**row.view("snapshot"), "references": ref, "sessionAvailable": bool(session and session.status != "deleted"),
            "operationHistory": audit_events(db, user.org, targets), "caseProgress": case_progress(db, user, row),
            "attachments": [item.view() for item in attachments(db, user.org, row.id)]}


def mutable(row):
    if row.status in {"linked", "submitting"}:
        fail(409, "DRAFT_LOCKED", "工单已登记或提交，当前版本已锁定")


@router.get("/drafts")
def list_drafts(user=Depends(actor), db=Depends(get_db, scope="function")):
    return [row.view("snapshot", "changes") for row in db.scalars(records(user.org, "draft").where(
        Record.owner == user.key).order_by(Record.updated.desc()).limit(100))]


@router.post("/drafts", status_code=201)
def create(body: DraftCreate, request: Request, user=Depends(actor), db=Depends(get_db, scope="function")):
    session = active_session(db, user, body.sessionId, lock=True)
    device = device_for(db, user, session.data["deviceId"])
    turns = session_turns(db, user.org, session)
    if not turns:
        fail(422, "MISSING_CONTEXT", "请先描述现场情况")
    existing = find(db, user.org, "draft", key=session.id)
    if existing:
        return view(db, user, existing)
    last = turns[-1]
    symptom = "\n".join(row.data["text"] for row in turns)[-8000:]
    last_codes = fault_codes(last.data["text"], device)
    context_start = last.data.get("contextStart")
    if context_start is None:
        previous, _, _ = question_context(turns[:-1], last.data["text"],
                                          last_codes or symptom_signals(last.data["text"]))
        context_start = previous[0].data["sequence"] if previous else last.data["sequence"]
    context_codes = {code_key(code): code for turn in turns if turn.data["sequence"] >= context_start
                     for code in fault_codes(turn.data["text"], device)}
    fault = next(iter(context_codes.values())) if len(context_codes) == 1 else ""
    fault_origin = ("最近一轮现场描述" if last_codes else "当前诊断上下文") if fault else ""
    fields = {"symptom": symptom, "faultCode": fault, "cause": "", "actions": "", "result": ""}
    row = add(db, user.org, "draft", key=session.id, owner=user.key, status="draft", sessionId=session.id,
              turnId=last.id, device={key: device[key] for key in ("id", "name", "number", "model", "batch", "location")},
              fields=fields, origins={"symptom": "会话现场描述", "faultCode": fault_origin,
                                     "device": "设备台账"}, changes=[], snapshot=None)
    audit(db, user.org, user, "生成工单草稿", row.id, request.state.request_id)
    return view(db, user, row)


@router.get("/drafts/{draft_id}")
def get_draft(draft_id: str, user=Depends(actor), db=Depends(get_db, scope="function")):
    return view(db, user, own(db, user, "draft", draft_id))


@router.patch("/drafts/{draft_id}")
def patch_draft(draft_id: str, body: DraftPatch, request: Request, user=Depends(actor), db=Depends(get_db, scope="function")):
    row = own(db, user, "draft", draft_id, lock=True)
    revision(row, body.revision)
    mutable(row)
    device_for(db, user, row.data["device"]["id"])
    old, origins, changes = row.data["fields"], row.data["origins"].copy(), row.data["changes"].copy()
    for key, value in body.fields.items():
        if old[key] != value:
            changes.append(dict(field=key, before=old[key], after=value, actor=user.key, at=now(), revision=row.rev + 1))
            origins[key] = "经办人填写"
    pending = row.data.get("pendingExternalNumber", "") if body.number is None else body.number.strip()
    number_changed = pending != row.data.get("pendingExternalNumber", "")
    fields_changed = len(changes) != len(row.data["changes"])
    if fields_changed:
        row.change("draft", fields={**old, **body.fields}, origins=origins, changes=changes, snapshot=None,
                   pendingExternalNumber=pending)
    elif number_changed:
        row.change(pendingExternalNumber=pending)
    if fields_changed or number_changed:
        audit(db, user.org, user, "编辑工单草稿", row.id, request.state.request_id)
    return view(db, user, row)


@router.post("/drafts/{draft_id}/attachments", status_code=201)
def attach(draft_id: str, revisionId: int, request: Request, file: UploadFile = File(), user=Depends(actor),
           db=Depends(get_db, scope="function")):
    row = own(db, user, "draft", draft_id, lock=True)
    revision(row, revisionId)
    mutable(row)
    device_for(db, user, row.data["device"]["id"])
    if len(attachments(db, user.org, row.id)) >= 10:
        fail(422, "ATTACHMENT_LIMIT", "每个草稿最多 10 件附件")
    filename = Path((file.filename or "").replace("\\", "/")).name[:180]
    spec = ATTACHMENTS.get(Path(filename).suffix.lower())
    blob = file.file.read(20 * 1024 * 1024 + 1)
    if not spec or not blob.startswith(spec[1]) or len(blob) > 20 * 1024 * 1024:
        fail(422, "INVALID_ATTACHMENT", "附件须为不超过 20 MiB 的 PDF、PNG 或 JPEG")
    if cfg.clamav_command:
        with TemporaryDirectory() as temp:
            path = Path(temp) / "attachment"
            path.write_bytes(blob)
            try:
                check = subprocess.run([cfg.clamav_command, "--no-summary", str(path)], timeout=30, capture_output=True)
            except (OSError, subprocess.TimeoutExpired):
                fail(503, "SCAN_UNAVAILABLE", "附件扫描服务暂不可用")
            if check.returncode:
                fail(422, "SCAN_FAILED", "附件扫描未通过")
    elif not user.data.get("demo"):
        fail(503, "SCAN_UNAVAILABLE", "请配置附件扫描服务")
    add(db, user.org, "attachment", owner=row.id, status="ready", filename=filename,
        media=spec[0], bytes=len(blob), sha256=digest(blob), blob=blob)
    row.change("draft", snapshot=None)
    audit(db, user.org, user, "添加工单附件", row.id, request.state.request_id)
    db.flush()
    return view(db, user, row)


@router.get("/drafts/{draft_id}/attachments/{attachment_id}")
def read_attachment(draft_id: str, attachment_id: str, user=Depends(actor), db=Depends(get_db, scope="function")):
    row = own(db, user, "draft", draft_id)
    device_for(db, user, row.data["device"]["id"])
    asset = need(find(db, user.org, "attachment", id=attachment_id))
    if asset.owner != draft_id:
        fail(404, "NOT_FOUND", "附件不存在")
    return file_response(asset.blob, asset.data["filename"], asset.data["media"])


@router.delete("/drafts/{draft_id}/attachments/{attachment_id}")
def remove_attachment(draft_id: str, attachment_id: str, revisionId: int, request: Request, user=Depends(actor),
                      db=Depends(get_db, scope="function")):
    row = own(db, user, "draft", draft_id, lock=True)
    revision(row, revisionId)
    mutable(row)
    device_for(db, user, row.data["device"]["id"])
    asset = need(find(db, user.org, "attachment", id=attachment_id))
    if asset.owner != draft_id:
        fail(404, "NOT_FOUND", "附件不存在")
    db.delete(asset)
    row.change("draft", snapshot=None)
    audit(db, user.org, user, "移除工单附件", row.id, request.state.request_id)
    db.flush()
    return view(db, user, row)


@router.post("/drafts/{draft_id}/confirm")
def confirm(draft_id: str, body: Confirm, request: Request, user=Depends(actor), db=Depends(get_db, scope="function")):
    row = own(db, user, "draft", draft_id, lock=True)
    supplied = body.number if body.number is not None else row.data.get("pendingExternalNumber", "")
    if row.status == "linked" and supplied.strip().upper() == row.data.get("externalNumber", "").upper():
        device_for(db, user, row.data["device"]["id"])
        return view(db, user, row)
    revision(row, body.revision)
    mutable(row)
    legacy = row.status in {"confirmed", "exported"}
    if legacy and not supplied.strip():
        return view(db, user, row)
    device = device_for(db, user, row.data["device"]["id"])
    missing = [FIELDS[key] for key in REQUIRED if not row.data["fields"][key].strip()]
    if missing:
        fail(422, "REQUIRED_FIELDS", "请填写：" + "、".join(missing))
    number = ticket_number(supplied) if supplied.strip() else ""
    if not number and not (cfg.workorder_enabled and cfg.workorder_url and not user.data.get("demo")):
        fail(422, "TICKET_REQUIRED", "请先填写外部工单号再确认")
    if number:
        link_ticket(db, row, number)
    if legacy:
        row.change("linked", externalNumber=number, pendingExternalNumber="")
        audit(db, user.org, user, "登记外部工单号", row.id, request.state.request_id)
        return view(db, user, row)
    references = view(db, user, row)["references"]
    snapshot = dict(draftId=row.id, confirmedAt=now(), confirmedBy=user.key, version=row.rev + 1,
                    device={key: device[key] for key in ("id", "name", "number", "model", "batch", "location")},
                    fields=row.data["fields"], fieldOrigins=row.data["origins"],
                    references=[{key: ref[key] for key in ("sourceId", "number", "version", "position", "id", "referenceUrl")
                                 if ref.get(key) is not None} for ref in references],
                    attachments=[item.view() for item in attachments(db, user.org, row.id)])
    row.change("linked" if number else "confirmed", snapshot=snapshot, pendingExternalNumber="",
               **({"externalNumber": number} if number else {}))
    audit(db, user.org, user, "确认工单快照", row.id, request.state.request_id)
    if number:
        audit(db, user.org, user, "登记外部工单号", row.id, request.state.request_id)
    return view(db, user, row)


@router.post("/drafts/{draft_id}/export")
def export(draft_id: str, body: Revision, request: Request, user=Depends(actor), db=Depends(get_db, scope="function")):
    row = own(db, user, "draft", draft_id, lock=True)
    revision(row, body.revision)
    device_for(db, user, row.data["device"]["id"])
    if row.status not in {"confirmed", "exported", "linked"}:
        fail(409, "CONFIRM_REQUIRED", "请先人工确认工单")
    if row.status == "confirmed":
        row.change("exported")
    snapshot = {**row.data["snapshot"]}
    if row.data.get("externalNumber"):
        snapshot["externalNumber"] = row.data["externalNumber"]
    current = {ref["id"] for ref in view(db, user, row)["references"]}
    snapshot["references"] = [ref for ref in snapshot["references"] if ref["id"] in current]
    audit(db, user.org, user, "导出工单", row.id, request.state.request_id)
    return file_response(json.dumps(snapshot, ensure_ascii=False, indent=2).encode(), f"工单-{row.id[:8]}.json",
                         "application/json")


@router.post("/drafts/{draft_id}/register")
def register(draft_id: str, body: Register, request: Request, user=Depends(actor), db=Depends(get_db, scope="function")):
    row = own(db, user, "draft", draft_id, lock=True)
    revision(row, body.revision)
    device_for(db, user, row.data["device"]["id"])
    if row.status not in {"confirmed", "exported"}:
        fail(409, "CONFIRM_REQUIRED", "请先确认工单")
    number = ticket_number(body.number)
    link_ticket(db, row, number)
    row.change("linked", externalNumber=number, pendingExternalNumber="")
    audit(db, user.org, user, "登记外部工单号", row.id, request.state.request_id)
    return view(db, user, row)


@router.post("/drafts/{draft_id}/submit", status_code=202)
def submit(draft_id: str, body: Revision, request: Request, user=Depends(actor), db=Depends(get_db, scope="function")):
    row = own(db, user, "draft", draft_id, lock=True)
    device_for(db, user, row.data["device"]["id"])
    existing = find(db, user.org, "submission", key=row.id)
    if existing:
        return existing.view("payload")
    revision(row, body.revision)
    if not cfg.workorder_enabled or not cfg.workorder_url or user.data.get("demo"):
        fail(409, "GATE_CLOSED", "正式工单回写尚未启用，请登记外部工单号")
    if row.status not in {"confirmed", "exported"}:
        fail(409, "CONFIRM_REQUIRED", "请先确认工单")
    snapshot = {**row.data["snapshot"]}
    current = {ref["id"] for ref in view(db, user, row)["references"]}
    snapshot["references"] = [ref for ref in snapshot["references"] if ref["id"] in current]
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
    submission = add(db, user.org, "submission", key=row.id, owner=user.key, status="queued",
                     requestId=uid(), digest=digest(payload.encode()), payload=snapshot)
    row.change("submitting")
    enqueue(db, user.org, "submit", submission.id)
    audit(db, user.org, user, "提交外部工单", row.id, request.state.request_id)
    return submission.view("payload")


@router.get("/drafts/{draft_id}/submission")
def get_submission(draft_id: str, user=Depends(actor), db=Depends(get_db, scope="function")):
    row = own(db, user, "draft", draft_id)
    return need(find(db, user.org, "submission", key=row.id)).view("payload")


@router.post("/drafts/{draft_id}/complete")
def complete(draft_id: str, body: Revision, request: Request, user=Depends(actor), db=Depends(get_db, scope="function")):
    row = own(db, user, "draft", draft_id, lock=True)
    revision(row, body.revision)
    device_for(db, user, row.data["device"]["id"])
    if row.status != "linked":
        fail(409, "LINK_REQUIRED", "请先关联外部工单")
    if not row.data.get("completedAt"):
        row.change(completedAt=now())
        audit(db, user.org, user, "确认外部结单", row.id, request.state.request_id)
    return view(db, user, row)

