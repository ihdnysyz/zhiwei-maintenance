import logging
import os
import time

import httpx
from sqlalchemy import delete, or_, select, text

from .common import permitted_device
from .conversations import answer_turn, expert_task
from .db import DB, Chunk, Job, add, audit, cfg, find, init_db, set_org, timestamp, uid
from .knowledge import embeddings, parse_file, split_blocks

log = logging.getLogger("maintenance.worker")


def begin_write(db):
    if db.bind.dialect.name == "sqlite":
        db.execute(text("BEGIN IMMEDIATE"))


def claim():
    instant = timestamp()
    with DB.begin() as db:
        begin_write(db)
        job = db.scalar(select(Job).where(
            or_(Job.state == "pending", (Job.state == "running") & (Job.leased_until < instant)),
            Job.available <= instant,
        ).order_by(Job.available).limit(1).with_for_update(skip_locked=True))
        if job:
            job.state = "running"
            job.lease = uid()
            job.leased_until = instant + (3600 if job.kind == "parse" else 180)
            job.attempts += 1
        return job


def external_headers():
    return {"Authorization": f"Bearer {cfg.integration_token}"} if cfg.integration_token else {}


def external_result(job):
    with DB.begin() as db:
        begin_write(db)
        row = find(db, job.org, "submission", id=job.target, lock=True)
        if not row or row.status == "created":
            return None
        create = job.kind == "submit" and row.status in {"queued", "retry_queued"}
        if create:
            draft = find(db, job.org, "draft", id=row.key)
            user = find(db, job.org, "user", key=row.owner)
            device = find(db, job.org, "device", id=draft.data["device"]["id"]) if draft else None
            if (not cfg.workorder_enabled or not cfg.workorder_url or not user or user.status != "active"
                    or not device or not permitted_device(user, device.data)):
                return "manual_review", "", False
            row.change("creating")
        request_id, payload, summary = [row.data[k] for k in ("requestId", "payload", "digest")]
    headers = external_headers()
    try:
        base = cfg.workorder_url.rstrip("/")
        if create:
            response = httpx.post(base, json={"requestId": request_id, "payload": payload, "digest": summary},
                                  headers={**headers, "Idempotency-Key": request_id}, timeout=12)
        else:
            response = httpx.get(f"{base}/{request_id}", headers=headers, timeout=12)
        response.raise_for_status()
        result = response.json()
        status = result.get("status", "unknown")
        if result.get("requestId") != request_id:
            return "unknown", "", False
        if status == "created" and isinstance(result.get("number"), str) and result["number"].strip():
            return "created", result["number"].strip()[:64], False
        if status == "not_created":
            return "not_created", "", result.get("sameKeyRetry") is True
        return "processing" if status == "processing" else "unknown", "", False
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return "unknown", "", False


def finish_external(db, job, result):
    if result is None:
        return
    row = find(db, job.org, "submission", id=job.target, lock=True)
    if not row or row.status == "created":
        return
    status, number, retry = result
    previous = row.status
    since = row.data.get("checkSince", timestamp())
    row.change(status, externalNumber=number, sameKeyRetry=retry, checkSince=since)
    if status == "created":
        draft = find(db, job.org, "draft", id=row.key, lock=True)
        ticket = find(db, job.org, "ticket", key=number.upper())
        if ticket and ticket.owner != draft.id:
            row.change("manual_review", note="外部单号与已有记录冲突")
        else:
            if not ticket:
                add(db, job.org, "ticket", key=number.upper(), owner=draft.id)
            draft.change("linked", externalNumber=number)
    elif status in {"unknown", "processing"}:
        if timestamp() - since >= 900:
            row.change("manual_review", note="查询超过 15 分钟，待人工核对")
        else:
            db.add(Job(org=job.org, kind="check", target=row.id, available=timestamp() + 30))
    if row.status != previous:
        labels = {"created": "外部工单已登记", "not_created": "外部系统确认未创建工单",
                  "manual_review": "外部工单待人工核对", "processing": "外部工单处理中", "unknown": "外部工单结果待确认"}
        audit(db, job.org, "worker", labels[row.status], row.id, job.id)


def parse_source(job):
    with DB() as db:
        row = find(db, job.org, "source", id=job.target)
        document = list(split_blocks(parse_file(row.data["filename"], row.blob, row.data["category"])))
    if not document:
        raise ValueError("没有可校核的文字")
    vectors = [None] * len(document)
    degraded = False
    if cfg.allow_model_data and cfg.embedding_model and not cfg.model_source_review:
        from .conversations import redact
        try:
            for start in range(0, len(document), 16):
                stop = min(len(document), start + 16)
                vectors[start:stop] = embeddings([redact(item[1][:1800]) for item in document[start:stop]])
        except (httpx.HTTPError, KeyError, ValueError, TypeError, RuntimeError):
            degraded = True
    return document, vectors, degraded


def run(job):
    try:
        if job.kind == "parse":
            result = parse_source(job)
        elif job.kind in {"submit", "check"}:
            result = external_result(job)
        elif job.kind == "turn":
            with DB() as db:
                row = find(db, job.org, "turn", id=job.target)
                session = find(db, job.org, "session", id=row.owner) if row else None
                result = (answer_turn(db, job.org, row)
                          if session and session.status != "deleted" and row.status == "queued" else None)
        else:
            raise ValueError("不支持的后台操作")
        with DB.begin() as db:
            begin_write(db)
            current = db.get(Job, job.id, with_for_update=True)
            if not current or current.lease != job.lease:
                return
            if job.kind == "parse":
                row = find(db, job.org, "source", id=job.target, lock=True)
                if row and row.status == "queued":
                    set_org(db, job.org)
                    document, vectors, degraded = result
                    db.execute(delete(Chunk).where(Chunk.org == job.org, Chunk.source == row.id))
                    for (position, body, ocr), vector in zip(document, vectors):
                        db.add(Chunk(org=job.org, source=row.id, position=position, body=body, ocr=ocr,
                                     embedding=vector, embedding_model=cfg.embedding_model if vector is not None else ""))
                    row.change("calibration", indexReady=True, chunkCount=len(document),
                               ocr=any(item[2] for item in document), keywordOnly=degraded)
                    audit(db, job.org, "worker", "资料解析完成，等待校核", row.id, job.id)
            elif job.kind == "turn":
                row = find(db, job.org, "turn", id=job.target)
                session = find(db, job.org, "session", id=row.owner, lock=True) if row else None
                if session and session.status != "deleted" and row.status == "queued" and result:
                    device = result.pop("_conflictDevice", None)
                    if device:
                        result["expertTaskId"] = expert_task(db, job.org, session, row, device, "多份手册命中同一故障码").id
                    row.change("done", **result)
                    current.error = "模型生成降级，已返回授权原文" if result.get("modelDegraded") else ""
            else:
                finish_external(db, job, result)
            current.state = "done"
    except Exception as exc:
        log.warning("job %s (%s): %s", job.id, job.kind, type(exc).__name__)
        with DB.begin() as db:
            begin_write(db)
            current = db.get(Job, job.id, with_for_update=True)
            if not current or current.lease != job.lease:
                return
            display = str(exc)[:250] if isinstance(exc, ValueError) else "后台处理失败，请核对文件及服务状态"
            current.error = display
            current.state = "failed"
            if job.kind == "parse":
                row = find(db, job.org, "source", id=job.target, lock=True)
                row.change("failed", error=display)
            elif job.kind == "turn":
                row = find(db, job.org, "turn", id=job.target, lock=True)
                if row and row.status == "queued":
                    row.change("done", decision="error", referenceIds=[],
                               answer="问答服务暂时不可用。请重试或将问题转交专家。")
            elif job.attempts < 3:
                current.kind = "check"
                current.state = "pending"
                current.available = timestamp() + 20 * job.attempts
            audit(db, job.org, "worker", "后台任务异常", job.target, job.id)


def main():
    logging.basicConfig(level=logging.INFO)
    if os.getenv("WORKER_INIT", "true").lower() == "true":
        init_db()
    while True:
        job = claim()
        if job:
            run(job)
        else:
            time.sleep(0.3)


if __name__ == "__main__":
    main()

