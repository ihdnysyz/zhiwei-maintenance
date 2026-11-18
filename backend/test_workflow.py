import json
import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def services(tmp_path_factory):
    database = tmp_path_factory.mktemp("zhiwei") / "test.db"
    provided = os.environ.get("ZHIWEI_TEST_DATABASE_URL")
    keys = {"DATABASE_URL": provided or "sqlite:///" + database.as_posix(), "DEMO_MODE": "true",
            "ADMIN_ORG": "another_org", "ADMIN_PASSWORD": "local-test-password",
            "ALLOW_MODEL_DATA": "false", "WORKORDER_ENABLED": "false",
            "API_INIT": "false" if provided else "true"}
    previous = {key: os.environ.get(key) for key in keys}
    os.environ.update(keys)
    from maintenance.main import app
    from maintenance import worker
    with TestClient(app) as client:
        yield client, worker
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def post(client, path, payload=None, code=200):
    response = client.post("/api/v1" + path, json=payload)
    assert response.status_code == code, response.text
    return response.json() if response.headers.get("content-type", "").startswith("application/json") else response


def get(client, path, code=200):
    response = client.get("/api/v1" + path)
    assert response.status_code == code, response.text
    return response.json()


def do_job(worker, kind):
    job = worker.claim()
    assert job and job.kind == kind
    worker.run(job)


def upload(client, info, text):
    response = client.post("/api/v1/sources/import", data={"metadata": json.dumps([info])},
                           files={"files": ("manual.txt", text.encode(), "text/plain")})
    assert response.status_code == 202, response.text
    return response.json()


def test_import_review_ask_and_close_workorder(services, monkeypatch):
    client, worker = services
    info = dict(number="MAN-TEST", title="校验用检修手册", version="1.0",
                model="IM-200", batch="A01", category="manual")
    original = "验收样本。K81 表示示例记录提示。核对面板状态和问题发生时间。"
    get(client, "/sources", 401)
    assert get(client, "/openapi.json")["openapi"] == "3.1.1"
    post(client, "/auth/demo", {"role": "library"})
    batch = upload(client, info, original)
    source_id = batch["items"][0]["sourceId"]
    duplicate = upload(client, info, original)["items"][0]
    assert duplicate["status"] == "duplicate" and duplicate["sourceId"] == source_id
    conflicting = upload(client, info, "不同摘要。")["items"][0]
    assert conflicting["status"] == "conflict"
    conflict = next(
        item for group in get(client, "/imports") for item in group["items"] if item.get("sourceId") == conflicting["sourceId"])
    post(client, f"/conflicts/{conflict['sourceId']}/resolve", dict(revision=conflict["revision"], action="discard"))
    do_job(worker, "parse")
    assert get(client, f"/imports/{batch['id']}")["items"][0]["status"] == "calibration"
    source = get(client, f"/sources/{source_id}")
    assert source["chunks"][0]["text"] == original
    source = post(client, f"/sources/{source_id}/actions", dict(revision=source["revision"], action="review", verified=True))
    post(client, f"/sources/{source_id}/actions", dict(revision=source["revision"], action="approve"), 403)
    post(client, "/auth/demo", {"role": "expert"})
    source = post(client, f"/sources/{source_id}/actions", dict(revision=source["revision"], action="approve"))
    post(client, "/auth/demo", {"role": "library"})
    source = post(client, f"/sources/{source_id}/actions", dict(revision=source["revision"], action="publish"))
    post(client, "/auth/login", dict(org="another_org", username="admin", password="local-test-password"))
    assert get(client, "/sources") == []
    get(client, f"/sources/{source_id}", 404)
    post(client, "/auth/demo", {"role": "tech"})
    first, second = get(client, "/devices")
    assert get(client, f"/sources/{source_id}?deviceId={first['id']}")["active"]
    get(client, f"/sources/{source_id}?deviceId={second['id']}", 404)
    session = post(client, "/sessions", dict(deviceId=first["id"]), 201)
    url = f"/sessions/{session['id']}/turns"
    message = {"text": "K81 示例记录提示怎么核对？", "clientMessageId": uuid4().hex}
    turn = post(client, url, message, 202)
    assert post(client, url, message, 202)["id"] == turn["id"]
    post(client, url, {**message, "text": "同编号异内容"}, 409)
    do_job(worker, "turn")
    turn = get(client, f"{url}/{turn['id']}")
    assert turn["decision"] == "evidence" and turn["citations"][0]["sourceId"] == source_id
    assert client.get(f"/api/v1/sources/{source_id}/file", params={
        "deviceId": first["id"], "chunkId": turn["citations"][0]["id"]}).text == original
    from maintenance import conversations
    monkeypatch.setattr(conversations.cfg, "chat_model", "validation-chat")
    monkeypatch.setattr(conversations.cfg, "allow_model_data", True)
    monkeypatch.setattr(conversations, "model_call", lambda *_: {
        "choices": [{"message": {"content": json.dumps({"claims": [{"id": 1, "quote": "K81 表示示例记录提示。"}]})}}]})
    next_turn = post(client, url, {"text": "K81 提示是什么？", "clientMessageId": uuid4().hex}, 202)
    do_job(worker, "turn")
    result = get(client, f"{url}/{next_turn['id']}")
    assert result["decision"] == "answer" and "K81 表示示例记录提示。" in result["answer"]
    task = post(client, f"{url}/{turn['id']}/escalate", dict(note="请复核演示告警"), 201)
    assert task["assignee"] == "expert"
    draft = post(client, "/drafts", {"sessionId": session["id"]}, 201)
    draft_url = f"/drafts/{draft['id']}"
    post(client, draft_url + "/confirm", {"revision": draft["revision"]}, 422)
    fields = dict(cause="演示原因", actions="演示处置", result="演示结果")
    response = client.patch("/api/v1" + draft_url, json={"fields": fields, "revision": draft["revision"]})
    assert response.status_code == 200, response.text
    draft = response.json()
    assert draft["origins"]["cause"] == "经办人填写"
    draft = post(client, draft_url + "/confirm", {"revision": draft["revision"]})
    assert draft["status"] == "confirmed"
    post(client, draft_url + "/submit", {"revision": draft["revision"]}, 409)
    export = post(client, draft_url + "/export", {"revision": draft["revision"]})
    assert export["fields"]["result"] == fields["result"]
    draft = get(client, draft_url)
    draft = post(client, draft_url + "/register", {"revision": draft["revision"], "number": "CHECK-001"})
    draft = post(client, draft_url + "/complete", {"revision": draft["revision"]})
    assert draft["completedAt"]
    locked = client.post(f"/api/v1{draft_url}/attachments?revisionId={draft['revision']}",
                         files={"file": ("sample.pdf", b"%PDF-sample", "application/pdf")})
    assert locked.status_code == 409
    post(client, "/auth/demo", {"role": "expert"})
    task = post(client, f"/tasks/{task['id']}/actions", {"revision": task["revision"], "action": "reply", "note": "班组已复核。"})
    assert task["status"] == "answered"
    assert get(client, "/completed")[0]["id"] == draft["id"]
    case = post(client, draft_url + "/case", {"text": "已核实适用条件和处置结果。样本联系方式 13812345678 应当脱敏。"})
    do_job(worker, "parse")
    case = get(client, f"/sources/{case['id']}")
    assert "13812345678" not in case["chunks"][0]["text"]
    post(client, "/auth/demo", {"role": "library"})
    post(client, f"/sources/{source_id}/actions", {"revision": source["revision"], "action": "block"})
    post(client, "/auth/demo", {"role": "tech"})
    assert get(client, f"{url}/{turn['id']}")["decision"] == "expired"


def test_pdf_csv_parsers(services):
    import pymupdf

    from maintenance import knowledge
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((60, 60), "Maintenance records for the fictional sample device X72")
    blocks = knowledge.parse_pdf(pdf.tobytes())
    assert blocks[0][0] == "第 1 页" and not blocks[0][2]
    rows = knowledge.parse_csv("工单号,现象,结果\nWK-001,温度告警,已观察", "workorder")
    assert rows[0][0] == "记录 WK-001"


def test_formal_writeback_recovers_by_original_request(services, monkeypatch):
    import httpx
    from maintenance import drafts

    client, worker = services
    monkeypatch.setattr(drafts.cfg, "workorder_enabled", True)
    monkeypatch.setattr(drafts.cfg, "workorder_url", "https://workorders.invalid/tickets")
    post(client, "/auth/login", dict(org="another_org", username="admin", password="local-test-password"))
    device = post(client, "/devices", dict(number="PILOT-001", model="PILOT", name="验收台账"))
    session = post(client, "/sessions", {"deviceId": device["id"]}, 201)
    post(client, f"/sessions/{session['id']}/turns", {"text": "验收设备异常记录", "clientMessageId": uuid4().hex}, 202)
    do_job(worker, "turn")
    draft = post(client, "/drafts", {"sessionId": session["id"]}, 201)
    url = "/drafts/" + draft["id"]
    response = client.patch("/api/v1" + url, json={
        "revision": draft["revision"], "fields": {"cause": "验收原因", "actions": "验收处置", "result": "验收结果"},
    })
    assert response.status_code == 200, response.text
    confirmed = post(client, url + "/confirm", {"revision": response.json()["revision"]})
    submission = post(client, url + "/submit", {"revision": confirmed["revision"]}, 202)
    assert post(client, url + "/submit", {"revision": confirmed["revision"]}, 202)["id"] == submission["id"]

    requests = []

    def create(target, **kwargs):
        requests.append(("create", kwargs["headers"]["Idempotency-Key"]))
        raise httpx.ReadTimeout("synthetic response loss")

    def query(target, **kwargs):
        request_id = target.rsplit("/", 1)[-1]
        requests.append(("query", request_id))
        return httpx.Response(200, request=httpx.Request("GET", target),
                              json={"requestId": request_id, "status": "created", "number": "FORMAL-TEST-01"})

    monkeypatch.setattr(worker.httpx, "post", create)
    monkeypatch.setattr(worker.httpx, "get", query)
    do_job(worker, "submit")
    assert get(client, url + "/submission")["status"] == "unknown"
    instant = worker.timestamp()
    monkeypatch.setattr(worker, "timestamp", lambda: instant + 31)
    do_job(worker, "check")
    assert get(client, url)["externalNumber"] == "FORMAL-TEST-01"
    assert requests == [("create", submission["requestId"]), ("query", submission["requestId"])]


def test_semantic_retrieval_and_rls(services, monkeypatch):
    from sqlalchemy import select, text
    from maintenance import knowledge
    from maintenance.db import Chunk, DB, find, set_org

    client, _ = services
    with DB.begin() as db:
        if db.bind.dialect.name == "postgresql":
            assert db.scalar(text("SELECT rolbypassrls OR rolsuper FROM pg_roles WHERE rolname = current_user")) is False
            assert db.scalars(select(Chunk)).first() is None
        set_org(db, "demo")
        row = find(db, "demo", "source", key="MAN-001:2.1")
        chunk = db.scalar(select(Chunk).where(Chunk.org == "demo", Chunk.source == row.id))
        chunk.embedding = [1.0, 0.0, 0.0]
        chunk.embedding_model = "validation-vector"
        expected = row.id
    monkeypatch.setattr(knowledge.cfg, "allow_model_data", True)
    monkeypatch.setattr(knowledge.cfg, "embedding_model", "validation-vector")
    monkeypatch.setattr(knowledge, "model_call", lambda *_: {"data": [{"index": 0, "embedding": [1.0, 0.0, 0.0]}]})
    post(client, "/auth/demo", {"role": "tech"})
    device = get(client, "/devices")[0]
    ranked = get(client, f"/knowledge/search?deviceId={device['id']}&q=checkVector")
    assert ranked[0]["sourceId"] == expected

