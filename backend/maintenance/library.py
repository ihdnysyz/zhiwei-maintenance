import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import Response
import httpx
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from .common import actor, device_for, fail, get_db, need, permitted_device, revision, role
from .db import Chunk, Record, Scope, add, audit, audit_events, cfg, current_scopes, digest, enqueue, find, records, scopes, set_org
from .knowledge import KINDS, applicability, authorized_citation, embeddings, model_approval_endpoint, retrieve

router = APIRouter()
CONTENT_TYPES = {"pdf": "application/pdf", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                 "txt": "text/plain; charset=utf-8", "md": "text/plain; charset=utf-8", "csv": "text/plain; charset=utf-8"}


class ImportInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    number: str = Field(min_length=1, max_length=90, pattern=r"^[^:\s]+$")
    title: str = Field(min_length=1, max_length=150)
    version: str = Field(min_length=1, max_length=40, pattern=r"^[^:\s]+$")
    model: str = Field(min_length=1, max_length=80)
    batch: str = Field(default="", max_length=80)
    category: Literal["manual", "workorder", "case"]
    referenceUrl: AnyHttpUrl | None = None

    @field_validator("referenceUrl")
    @classmethod
    def public_reference(cls, url):
        if url is not None and (url.username or url.password):
            raise ValueError("来源链接不得包含登录凭据")
        return url


class SourceAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int
    action: Literal["review", "approve", "reject", "publish", "block"]
    verified: bool = False
    allowExternalModel: bool = False
    note: str = Field(default="", max_length=1500)
    start: str = ""
    end: str = ""
    batch: str | None = Field(default=None, max_length=80)


class ResolveConflict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    revision: int
    action: Literal["discard", "new_version"]
    version: str = Field(default="", max_length=40, pattern=r"^[^:\s]*$")


class Calibration(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    revision: int
    text: str = Field(min_length=1, max_length=80000)


def queue_source(db, org, owner, info, filename, body):
    key = f"{info['number']}:{info['version']}"
    exists = find(db, org, "source", key=key)
    summary = digest(body + json.dumps(info, sort_keys=True, ensure_ascii=False).encode())
    if exists:
        if exists.data.get("importDigest", exists.data.get("digest")) == summary:
            return exists, "duplicate"
        return add(db, org, "conflict", owner=owner, status="conflict", **info, filename=filename,
                   digest=summary, originalId=exists.id, blob=body), "conflict"
    if not find(db, org, "family", key=info["number"]):
        add(db, org, "family", key=info["number"])
    record = add(db, org, "source", owner=owner, key=key, status="queued", **info, filename=filename,
                 digest=summary, importDigest=summary, indexReady=False, blob=body)
    enqueue(db, org, "parse", record.id)
    return record, "queued"


@router.get("/sources")
def list_sources(user=Depends(actor), db=Depends(get_db, scope="function")):
    can_manage = user.data["role"] in {"library", "expert"}
    sources = list(db.scalars(records(user.org, "source").order_by(Record.updated.desc())))
    if can_manage:
        active = {scope.source for scope in db.scalars(current_scopes(user.org).where(Scope.status == "bind"))}
        return [{**row.view("digest", "approvedModelEndpoint"), "active": row.id in active}
                for row in sources if permitted_device(user, row.data)]
    active, devices = {}, {}
    for row in db.scalars(records(user.org, "device").order_by(Record.key)):
        if permitted_device(user, row.data):
            devices.setdefault((row.data["model"], row.data["batch"]), row.id)
    for (model, batch), device_id in devices.items():
        applicable, _ = applicability(db, user.org, {"model": model, "batch": batch})
        for source_id in applicable:
            active.setdefault(source_id, device_id)
    return [{**row.view("digest", "verifiedBy", "reviewer", "note", "approvedDigest", "approvedModelEndpoint"),
             "active": True, "applicableDeviceId": active[row.id]}
            for row in sources if row.id in active]


@router.post("/sources/import", status_code=202)
def import_sources(request: Request, files: Annotated[list[UploadFile], File()],
                   metadata: Annotated[str, Form()], user=Depends(actor), db=Depends(get_db, scope="function")):
    role(user, "library")
    try:
        entries = [ImportInfo.model_validate(entry).model_dump(mode="json", exclude_none=True)
                   for entry in json.loads(metadata)]
    except (ValueError, TypeError):
        fail(422, "INVALID_METADATA", "请检查来源编号、版本、类型、设备范围和来源链接")
    if not 1 <= len(files) == len(entries) <= 30:
        fail(422, "INVALID_BATCH", "每批 1 至 30 个文件，每份文件需要一条资料信息")
    items = []
    for file, info in zip(files, entries):
        filename = Path((file.filename or "文件").replace("\\", "/")).name[:180]
        ext = Path(filename).suffix.lower().strip(".")
        if not (permitted_device(user, info) or info["model"] == "*" and not user.data.get("models")):
            items.append(dict(filename=filename, status="failed", error="超出管理员设备范围"))
            continue
        if ext not in CONTENT_TYPES:
            items.append(dict(filename=filename, status="failed", error="此格式不受支持"))
            continue
        raw = file.file.read(cfg.max_upload_mib * 1024 * 1024 + 1)
        if not raw or len(raw) > cfg.max_upload_mib * 1024 * 1024:
            items.append(dict(filename=filename, status="failed", error=f"文件为空或超过 {cfg.max_upload_mib} MiB"))
            continue
        row, status = queue_source(db, user.org, user.key, info, filename, raw)
        items.append(dict(filename=filename, sourceId=row.id, status=status))
        if status in {"queued", "conflict"}:
            audit(db, user.org, user, "导入资料" if status == "queued" else "发现资料版本冲突", row.id, request.state.request_id)
    batch = add(db, user.org, "import", owner=user.key, items=items)
    audit(db, user.org, user, "资料导入", batch.id, request.state.request_id)
    return batch.view()


def import_view(db, user, batch, histories):
    result = []
    for item in batch.data["items"]:
        display = {**item}
        if item["status"] == "queued":
            row = find(db, user.org, "source", id=item["sourceId"])
            display.update(status=row.status if row else "failed", error=row.data.get("error", "") if row else "资料已清理")
        elif item["status"] == "conflict":
            row = find(db, user.org, "conflict", id=item["sourceId"])
            if row:
                display.update(status=row.status, revision=row.rev, version=row.data["version"])
                if permitted_device(user, row.data):
                    display["operationHistory"] = histories.get(row.id, [])
        result.append(display)
    return {**batch.view(), "items": result, "operationHistory": histories.get(batch.id, [])}


def import_histories(db, user, batches):
    targets = {batch.id for batch in batches}
    targets.update(item["sourceId"] for batch in batches for item in batch.data["items"] if item["status"] == "conflict")
    histories = {}
    for event in audit_events(db, user.org, targets):
        histories.setdefault(event["target"], []).append(event)
    return histories


@router.get("/imports")
def imports(user=Depends(actor), db=Depends(get_db, scope="function")):
    role(user, "library")
    batches = list(db.scalars(records(user.org, "import").order_by(Record.updated.desc()).limit(30)))
    histories = import_histories(db, user, batches)
    return [import_view(db, user, row, histories) for row in batches]


@router.get("/imports/{batch_id}")
def get_import(batch_id: str, user=Depends(actor), db=Depends(get_db, scope="function")):
    role(user, "library")
    batch = need(find(db, user.org, "import", id=batch_id))
    return import_view(db, user, batch, import_histories(db, user, [batch]))


@router.post("/imports/{batch_id}/retry", status_code=202)
def retry_import(batch_id: str, request: Request, user=Depends(actor), db=Depends(get_db, scope="function")):
    role(user, "library")
    batch = need(find(db, user.org, "import", id=batch_id))
    retried = False
    for item in batch.data["items"]:
        if item["status"] == "queued":
            row = find(db, user.org, "source", id=item["sourceId"], lock=True)
            if row and row.status == "failed" and permitted_device(user, row.data):
                row.change("queued", error="")
                enqueue(db, user.org, "parse", row.id)
                audit(db, user.org, user, "重新解析资料", row.id, request.state.request_id)
                retried = True
    if retried:
        audit(db, user.org, user, "重新解析失败的资料", batch.id, request.state.request_id)
    return import_view(db, user, batch, import_histories(db, user, [batch]))


@router.post("/conflicts/{conflict_id}/resolve")
def resolve_conflict(conflict_id: str, body: ResolveConflict, request: Request,
                     user=Depends(actor), db=Depends(get_db, scope="function")):
    role(user, "library")
    row = need(find(db, user.org, "conflict", id=conflict_id, lock=True))
    revision(row, body.revision)
    if not permitted_device(user, row.data) or row.status != "conflict":
        fail(409, "CONFLICT_STATE", "当前记录不可裁定")
    if body.action == "new_version":
        number = row.data["number"]
        need(find(db, user.org, "family", key=number, lock=True))
        if not body.version or find(db, user.org, "source", key=f"{number}:{body.version}"):
            fail(409, "VERSION_EXISTS", "请输入尚未使用的新版本")
        info = {key: row.data[key] for key in ImportInfo.model_fields if key in row.data}
        info["version"] = body.version
        source, _ = queue_source(db, user.org, user.key, info, row.data["filename"], row.blob)
        row.change("resolved", replacementId=source.id)
        audit(db, user.org, user, "按新版本导入资料", source.id, request.state.request_id)
    else:
        row.change("discarded")
    audit(db, user.org, user, "裁定资料版本冲突", row.id, request.state.request_id)
    return row.view("digest")


@router.get("/sources/{source_id}")
def get_source(source_id: str, deviceId: str = "", user=Depends(actor), db=Depends(get_db, scope="function")):
    row = need(find(db, user.org, "source", id=source_id))
    manager = user.data["role"] in {"library", "expert"}
    if manager and not permitted_device(user, row.data):
        fail(403, "SCOPE_DENIED", "资料超出设备范围")
    if not manager:
        allowed, _ = applicability(db, user.org, device_for(db, user, deviceId))
        if row.id not in allowed:
            fail(404, "NOT_APPLICABLE", "此资料不在当前设备的生效范围内")
    set_org(db, user.org)
    chunks = list(db.scalars(select(Chunk).where(Chunk.org == user.org, Chunk.source == row.id)))
    history = [scope.view() for scope in db.scalars(scopes(user.org).where(
        Scope.number == row.data["number"]).order_by(Scope.start.desc()))] if manager else []
    active = db.scalar(current_scopes(user.org).where(Scope.source == row.id, Scope.status == "bind").limit(1)) is not None
    if manager:
        host, endpoint_key = model_approval_endpoint()
        fields = {**row.view("digest", "approvedModelEndpoint"), "modelApprovalHost": host,
                  "externalModelApproved": bool(endpoint_key and endpoint_key == row.data.get("approvedModelEndpoint")),
                  "operationHistory": audit_events(db, user.org, [row.id, *(chunk.id for chunk in chunks)])}
    else:
        fields = row.view("digest", "reviewer", "verifiedBy", "note", "approvedDigest", "approvedModelEndpoint")
    return {**fields, "active": active, "chunks": [chunk.view() for chunk in chunks], "scopes": history}


@router.get("/knowledge/search")
def search(deviceId: str, q: Annotated[str, Query(min_length=1, max_length=300)],
           user=Depends(actor), db=Depends(get_db, scope="function")):
    results, ambiguous = retrieve(db, user.org, device_for(db, user, deviceId), q, limit=12)
    if ambiguous:
        fail(409, "BATCH_REQUIRED", "请在设备台账中补全批次")
    return results


@router.patch("/sources/{source_id}/chunks/{chunk_id}")
def calibrate(source_id: str, chunk_id: str, body: Calibration, request: Request,
              user=Depends(actor), db=Depends(get_db, scope="function")):
    role(user, "library")
    row = need(find(db, user.org, "source", id=source_id, lock=True))
    revision(row, body.revision)
    if not permitted_device(user, row.data) or row.status not in {"calibration", "returned"}:
        fail(409, "CALIBRATION_STATE", "请仅校正待校核或退回的版本")
    set_org(db, user.org)
    chunks = list(db.scalars(select(Chunk).where(Chunk.org == user.org, Chunk.source == source_id)
                             .order_by(Chunk.id)))
    target = need(next((item for item in chunks if item.id == chunk_id), None))
    if target.body == body.text:
        return row.view("digest", "importDigest")
    target.body = body.text
    target.embedding = None
    target.embedding_model = ""
    keyword_only = row.data.get("keywordOnly", False)
    if cfg.allow_model_data and cfg.embedding_model and not cfg.model_source_review:
        from .conversations import redact
        try:
            target.embedding = embeddings([redact(body.text[:1800])])[0]
            target.embedding_model = cfg.embedding_model
        except (httpx.HTTPError, RuntimeError, ValueError, TypeError, KeyError):
            keyword_only = True
    original = row.data.get("importDigest", row.data["digest"])
    summary = digest(json.dumps([original, *[(item.position, item.body) for item in chunks]],
                                ensure_ascii=False).encode())
    row.change("calibration", digest=summary, importDigest=original, approvedDigest="", keywordOnly=keyword_only)
    audit(db, user.org, user, "校正解析片段", row.id, request.state.request_id)
    return row.view("digest", "importDigest")


def parse_date(value):
    try:
        date = datetime.fromisoformat(value)
        if date.tzinfo is None:
            raise ValueError()
        return date.astimezone(timezone.utc)
    except ValueError:
        fail(422, "INVALID_DATE", "生效时间需要带时区")


def apply_publication_scope(db, source, *, bind=True, start_at="", end_at="", batch_override=None):
    data = source.data
    org = source.org
    if not data.get("indexReady") or data.get("approvedDigest") != data.get("digest"):
        fail(409, "INDEX_REQUIRED", "已审核摘要或索引尚未就绪")
    need(find(db, org, "family", key=data["number"], lock=True))
    current_time = datetime.now(timezone.utc)
    start = parse_date(start_at) if start_at else current_time
    end = parse_date(end_at) if end_at else None
    if start < current_time or end and end <= start:
        fail(422, "SCOPE_INTERVAL", "生效时间须在当前或未来，且结束时间晚于开始时间")
    batch = data["batch"]
    if not bind and not batch and batch_override is not None:
        batch = batch_override.strip()
    matching = scopes(org).where(Scope.number == data["number"],
                                 Scope.model == data["model"], Scope.batch == batch,
                                 (Scope.end.is_(None) | (Scope.end > start)))
    if end:
        matching = matching.where(Scope.start < end)
    for scope in db.scalars(matching.with_for_update()):
        if scope.start.replace(tzinfo=scope.start.tzinfo or timezone.utc) >= start:
            fail(409, "SCOPE_OVERLAP", "存在冲突的预定生效区间")
        scope.end = start
    db.flush()
    db.add(Scope(org=org, status="bind" if bind else "block", number=data["number"],
                 source=source.id, model=data["model"], batch=batch, start=start, end=end))
    source.change("published" if bind or batch != data["batch"] else "withdrawn")


@router.post("/sources/{source_id}/actions")
def action(source_id: str, body: SourceAction, request: Request, user=Depends(actor), db=Depends(get_db, scope="function")):
    role(user, "expert" if body.action in {"approve", "reject"} else "library")
    source = need(find(db, user.org, "source", id=source_id, lock=True))
    revision(source, body.revision)
    data = source.data
    if not permitted_device(user, data):
        fail(403, "SCOPE_DENIED", "资料超出设备范围")
    if body.allowExternalModel and body.action != "approve":
        fail(422, "MODEL_APPROVAL_ACTION", "请在独立审核通过时授权回答模型")
    if body.action == "review" and source.status in {"calibration", "returned"}:
        if not body.verified:
            fail(422, "CALIBRATION_REQUIRED", "请先核对片段、故障码、关键数值和原图")
        source.change("review", verifiedBy=user.key, note=body.note)
    elif body.action in {"approve", "reject"} and source.status == "review":
        if user.key in {source.owner, data.get("verifiedBy")}:
            fail(403, "INDEPENDENT_REVIEW", "请由独立审核人处理")
        if body.action == "reject" and not body.note.strip():
            fail(422, "REVIEW_NOTE_REQUIRED", "请填写退回原因")
        approved = body.action == "approve"
        endpoint_key = model_approval_endpoint()[1] if body.allowExternalModel else ""
        if body.allowExternalModel and not endpoint_key:
            fail(409, "MODEL_APPROVAL_DISABLED", "当前未启用按资料授权的回答模型")
        source.change("approved" if approved else "returned", reviewer=user.key, note=body.note,
                      approvedDigest=data.get("digest") if approved else "", approvedModelEndpoint=endpoint_key)
    elif body.action in {"publish", "block"} and source.status in {"approved", "published", "withdrawn"}:
        apply_publication_scope(db, source, bind=body.action == "publish", start_at=body.start,
                                end_at=body.end, batch_override=body.batch)
    else:
        fail(409, "SOURCE_STATE", "当前资料状态不支持此操作")
    audit(db, user.org, user, "资料·" + body.action, source.id, request.state.request_id)
    if body.allowExternalModel:
        audit(db, user.org, user, "资料·授权回答模型", source.id, request.state.request_id)
    if body.action == "approve" and data["category"] == "case" and data.get("workorderLocator"):
        apply_publication_scope(db, source)
        audit(db, user.org, user, "资料·审核后自动发布", source.id, request.state.request_id)
    return source.view("digest", "approvedModelEndpoint")


def file_response(blob, filename, media, inline=False):
    disposition = "inline" if inline else "attachment"
    return Response(blob, media_type=media, headers={
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(filename)}",
        "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store",
    })


@router.get("/sources/{source_id}/file")
def source_file(source_id: str, deviceId: str = "", chunkId: str = "", user=Depends(actor), db=Depends(get_db, scope="function")):
    source = need(find(db, user.org, "source", id=source_id))
    if deviceId and chunkId:
        citation = authorized_citation(db, user.org, chunkId, device_for(db, user, deviceId))
        if not citation or citation["sourceId"] != source_id:
            fail(404, "EVIDENCE_EXPIRED", "此资料引用已失效")
    else:
        role(user, "library", "expert")
        if not permitted_device(user, source.data):
            fail(403, "SCOPE_DENIED", "资料超出设备范围")
    if not source.blob:
        fail(404, "NO_ORIGINAL", "此演示片段没有原件")
    filename = source.data["filename"]
    ext = Path(filename).suffix.lower().strip(".")
    return file_response(source.blob, filename, CONTENT_TYPES[ext], inline=ext == "pdf")


@router.get("/conflicts/{conflict_id}/file")
def conflict_file(conflict_id: str, user=Depends(actor), db=Depends(get_db, scope="function")):
    role(user, "library")
    row = need(find(db, user.org, "conflict", id=conflict_id))
    if not permitted_device(user, row.data):
        fail(403, "SCOPE_DENIED", "资料超出设备范围")
    filename = row.data["filename"]
    return file_response(row.blob, filename, CONTENT_TYPES[Path(filename).suffix.lower().strip(".")])

