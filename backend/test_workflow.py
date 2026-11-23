import json
import os
from io import BytesIO
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


def model_line(payload, text):
    context = json.loads(payload["messages"][-1]["content"])
    return next(row["line"] for item in context["evidence"] for row in item["lines"] if row["text"] == text)


def upload(client, info, text):
    response = client.post("/api/v1/sources/import", data={"metadata": json.dumps([info])},
                           files={"files": ("manual.txt", text.encode(), "text/plain")})
    assert response.status_code == 202, response.text
    return response.json()


def test_import_review_ask_and_close_workorder(services, monkeypatch):
    client, worker = services
    info = dict(number="MAN-TEST", title="校验用检修手册", version="1.0",
                model="IM-200", batch="A01", category="manual", referenceUrl="https://example.invalid/catalog")
    original = "验收样本。K81 表示示例记录提示。核对面板状态和问题发生时间。"
    get(client, "/sources", 401)
    assert get(client, "/openapi.json")["openapi"] == "3.1.1"
    post(client, "/auth/demo", {"role": "library"})
    invalid_url = client.post("/api/v1/sources/import",
        data={"metadata": json.dumps([{**info, "referenceUrl": "javascript:alert(1)"}])},
        files={"files": ("manual.txt", b"test", "text/plain")})
    assert invalid_url.status_code == 422
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
    listed = next(row for row in get(client, "/sources") if row["id"] == source_id)
    assert listed["applicableDeviceId"] == first["id"]
    published = get(client, f"/sources/{source_id}?deviceId={first['id']}")
    assert published["active"] and "operationHistory" not in published
    get(client, f"/sources/{source_id}?deviceId={second['id']}", 404)
    other_session = post(client, "/sessions", {"deviceId": second["id"]}, 201)
    other_turns = f"/sessions/{other_session['id']}/turns"
    other_turn = post(client, other_turns, {"text": "K81", "clientMessageId": uuid4().hex}, 202)
    do_job(worker, "turn")
    hint_url = f"{other_turns}/{other_turn['id']}"
    hint = get(client, hint_url)
    assert hint["decision"] == "no_evidence" and not hint["citations"]
    assert [row["id"] for row in hint["deviceSuggestions"]] == [first["id"]]
    assert hint["deviceSuggestions"][0]["title"] == info["title"]
    session = post(client, "/sessions", dict(deviceId=first["id"]), 201)
    url = f"/sessions/{session['id']}/turns"
    message = {"text": "K81 示例记录提示怎么核对？", "clientMessageId": uuid4().hex}
    turn = post(client, url, message, 202)
    assert post(client, url, message, 202)["id"] == turn["id"]
    post(client, url, {**message, "text": "同编号异内容"}, 409)
    do_job(worker, "turn")
    turn = get(client, f"{url}/{turn['id']}")
    assert turn["decision"] == "evidence" and turn["citations"][0]["sourceId"] == source_id
    assert turn["citations"][0]["referenceUrl"] == info["referenceUrl"]
    assert client.get(f"/api/v1/sources/{source_id}/file", params={
        "deviceId": first["id"], "chunkId": turn["citations"][0]["id"]}).text == original
    from maintenance import conversations
    monkeypatch.setattr(conversations.cfg, "chat_model", "validation-chat")
    monkeypatch.setattr(conversations.cfg, "allow_model_data", True)
    def model_reply(_, payload):
        context = json.loads(payload["messages"][-1]["content"])
        category = context["requestedSource"]
        text = ("K81 已核实适用条件和处置结果。" if category == "case" else "K81 表示示例记录提示。")
        claim = {"line": model_line(payload, text)}
        return {"choices": [{"message": {"content": json.dumps({"claims": [claim]})}}]}

    monkeypatch.setattr(conversations, "model_call", model_reply)
    next_turn = post(client, url, {"text": "K81 提示是什么？", "clientMessageId": uuid4().hex}, 202)
    do_job(worker, "turn")
    result = get(client, f"{url}/{next_turn['id']}")
    assert result["decision"] == "answer" and "K81 表示示例记录提示。" in result["answer"]
    with monkeypatch.context() as protected:
        protected.setattr(conversations.cfg, "model_demo_only", True)
        protected.setattr(conversations.cfg, "model_demo_source_ids", "")
        mock_model = conversations.model_call

        def unexpected_egress(*args, **kwargs):
            pytest.fail("Imported documents must stay local in the model demo")

        protected.setattr(conversations, "model_call", unexpected_egress)
        protected_turn = post(client, url, {"text": "K81 样例用途？", "clientMessageId": uuid4().hex}, 202)
        do_job(worker, "turn")
        assert get(client, f"{url}/{protected_turn['id']}")["decision"] == "evidence"
        protected.setattr(conversations.cfg, "model_demo_source_ids", source_id)
        protected.setattr(conversations, "model_call", mock_model)
        allowed_turn = post(client, url, {"text": "K81 记录提示是什么？", "clientMessageId": uuid4().hex}, 202)
        do_job(worker, "turn")
        assert get(client, f"{url}/{allowed_turn['id']}")["decision"] == "answer"
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
    assert post(client, draft_url + "/confirm", {"revision": draft["revision"]}, 422)["code"] == "TICKET_REQUIRED"
    response = client.patch("/api/v1" + draft_url, json={
        "revision": draft["revision"], "fields": {}, "number": " CHECK-001 ",
    })
    assert response.status_code == 200
    draft = response.json()
    assert get(client, draft_url)["pendingExternalNumber"] == "CHECK-001"
    confirmation = {"revision": draft["revision"], "number": "CHECK-001"}
    draft = post(client, draft_url + "/confirm", confirmation)
    assert draft["status"] == "linked" and draft["externalNumber"] == "CHECK-001"
    assert post(client, draft_url + "/confirm", confirmation)["revision"] == draft["revision"]
    post(client, draft_url + "/submit", {"revision": draft["revision"]}, 409)
    post(client, "/auth/demo", {"role": "library"})
    assert all(row["id"] != draft["id"] for row in get(client, "/completed"))
    post(client, "/auth/demo", {"role": "tech"})
    draft = post(client, draft_url + "/complete", {"revision": draft["revision"]})
    assert draft["completedAt"]
    assert draft["caseProgress"] == {"status": "not_submitted"}
    assert all(event["action"] != "导出工单" for event in draft["operationHistory"])
    export = post(client, draft_url + "/export", {"revision": draft["revision"]})
    assert export["externalNumber"] == "CHECK-001"
    assert export["fields"]["result"] == fields["result"]
    assert export["references"][0]["referenceUrl"] == info["referenceUrl"]
    assert get(client, draft_url)["completedAt"] == draft["completedAt"]
    post(client, "/auth/demo", {"role": "library"})
    assert any(row["id"] == draft["id"] for row in get(client, "/completed"))
    post(client, "/auth/demo", {"role": "tech"})
    locked = client.post(f"/api/v1{draft_url}/attachments?revisionId={draft['revision']}",
                         files={"file": ("sample.pdf", b"%PDF-sample", "application/pdf")})
    assert locked.status_code == 409
    post(client, "/auth/demo", {"role": "expert"})
    task = post(client, f"/tasks/{task['id']}/actions", {"revision": task["revision"], "action": "reply", "note": "班组已复核。"})
    assert task["status"] == "answered"
    assert [(event["action"], event["actor"]) for event in task["operationHistory"]] == [
        ("转交专家", "tech"), ("专家待办·reply", "expert"),
    ]
    assert all(event["actorName"] and event["at"] for event in task["operationHistory"])
    assert get(client, "/completed")[0]["id"] == draft["id"]
    post(client, "/auth/demo", {"role": "library"})
    case_input = {"text": "K81 已核实适用条件和处置结果。样本联系方式 13812345678 应当脱敏。"}
    case = post(client, draft_url + "/case", case_input)
    retried = post(client, draft_url + "/case", case_input)
    assert retried["id"] == case["id"] and retried["revision"] == case["revision"]
    conflict = post(client, draft_url + "/case", {"text": "K81 重试输入改动了既有案例，不应生成隐形冲突。"}, 409)
    assert conflict["code"] == "CASE_ALREADY_SUBMITTED"
    do_job(worker, "parse")
    case = get(client, f"/sources/{case['id']}")
    assert "13812345678" not in case["chunks"][0]["text"]
    assert get(client, "/completed")[0]["caseSource"] == dict(id=case["id"], status="review", active=False)
    assert any(event["action"] == "案例校验完成，已送独立审核" and event["actor"] == "worker"
               for event in case["operationHistory"])
    post(client, "/auth/demo", {"role": "tech"})
    search_url = f"/knowledge/search?deviceId={first['id']}&q=K81"
    assert all(row["sourceId"] != case["id"] for row in get(client, search_url))
    assert get(client, draft_url)["caseProgress"] == {"status": "review", "sourceId": None}
    case_url = f"/sources/{case['id']}/actions"
    post(client, "/auth/demo", {"role": "expert"})
    case = post(client, case_url, dict(action="reject", revision=case["revision"], note="补充验收记录"))
    assert case["status"] == "returned"
    assert get(client, "/completed")[0]["caseSource"] == dict(id=case["id"], status="returned", active=False)
    post(client, "/auth/demo", {"role": "library"})
    case = post(client, case_url, dict(action="review", revision=case["revision"], verified=True))
    post(client, "/auth/demo", {"role": "expert"})
    case = post(client, case_url, dict(action="approve", revision=case["revision"]))
    assert case["status"] == "published"
    assert get(client, "/completed")[0]["caseSource"] == dict(id=case["id"], status="published", active=True)
    post(client, "/auth/demo", {"role": "library"})
    post(client, f"/sources/{source_id}/actions", {"revision": source["revision"], "action": "block"})
    post(client, "/auth/demo", {"role": "tech"})
    assert any(row["sourceId"] == case["id"] for row in get(client, search_url))
    assert not get(client, f"/knowledge/search?deviceId={second['id']}&q=K81")
    assert not get(client, hint_url)["deviceSuggestions"]
    assert get(client, f"{url}/{turn['id']}")["decision"] == "expired"
    case_session = post(client, "/sessions", {"deviceId": first["id"]}, 201)
    case_turns = f"/sessions/{case_session['id']}/turns"
    case_turn = post(client, case_turns, {"text": "K81 有哪些已结单案例？", "clientMessageId": uuid4().hex}, 202)
    do_job(worker, "turn")
    assert [row["sourceId"] for row in get(client, f"{case_turns}/{case_turn['id']}")["citations"]] == [case["id"]]
    preserved = get(client, draft_url)
    assert preserved["sessionAvailable"]
    assert preserved["caseProgress"] == {"status": "published", "sourceId": case["id"]}
    visible_case = next(row for row in get(client, "/sources") if row["id"] == case["id"])
    assert visible_case["category"] == "case"
    duplicate_draft = post(client, "/drafts", {"sessionId": case_session["id"]}, 201)
    duplicate_url = f"/drafts/{duplicate_draft['id']}"
    edited_duplicate = client.patch("/api/v1" + duplicate_url,
                                    json={"revision": duplicate_draft["revision"], "fields": fields}).json()
    assert post(client, duplicate_url + "/confirm",
                {"revision": edited_duplicate["revision"], "number": "check-001"}, 409)["code"] == "DUPLICATE_TICKET"
    assert get(client, duplicate_url)["status"] == "draft"
    assert any(event["action"] == "确认外部结单" and event["actor"] == "tech"
               for event in preserved["operationHistory"])
    other = post(client, "/sessions", {"deviceId": first["id"]}, 201)
    pending = post(client, f"/sessions/{other['id']}/turns",
                   {"text": "将被删除的临时诊断", "clientMessageId": uuid4().hex}, 202)
    post(client, "/auth/demo", {"role": "library"})
    assert client.delete("/api/v1/sessions/" + session["id"]).status_code == 404
    case_history = get(client, case_url.removesuffix("/actions"))["operationHistory"]
    assert [(event["action"], event["actor"]) for event in case_history][-2:] == [
        ("资料·approve", "expert"), ("资料·审核后自动发布", "expert"),
    ]
    post(client, "/auth/demo", {"role": "tech"})
    for session_id in (session["id"], other["id"], session["id"]):
        assert client.delete("/api/v1/sessions/" + session_id).status_code == 204
        get(client, "/sessions/" + session_id, 404)
    do_job(worker, "turn")
    assert {row["id"] for row in get(client, "/sessions")}.isdisjoint({session["id"], other["id"]})
    post(client, "/drafts", {"sessionId": session["id"]}, 404)
    post(client, url, {"text": "删除后不可继续", "clientMessageId": uuid4().hex}, 404)
    draft_after = get(client, draft_url)
    assert not draft_after["sessionAvailable"]
    for key in ("id", "status", "completedAt", "fields", "revision", "references", "operationHistory"):
        assert draft_after[key] == preserved[key]
    assert get(client, f"/tasks/{task['id']}")["reply"] == "班组已复核。"
    assert get(client, "/devices") == [first, second]
    assert any(item["sourceId"] == case["id"] for item in get(client, search_url))
    from maintenance.db import DB, find
    with DB() as db:
        assert find(db, "demo", "turn", id=pending["id"]) is None


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


def test_deepseek_connection_probe_uses_fixed_prompt(services, monkeypatch):
    import httpx
    from maintenance import knowledge

    client, _ = services
    post(client, "/auth/demo", {"role": "tech"})
    post(client, "/integrations/model/test", code=403)
    post(client, "/auth/demo", {"role": "platform"})
    monkeypatch.setattr(knowledge.cfg, "model_url", "https://api.deepseek.com")
    monkeypatch.setattr(knowledge.cfg, "chat_model", "deepseek-flash")
    monkeypatch.setattr(knowledge.cfg, "model_key", "")
    post(client, "/integrations/model/test", code=409)
    monkeypatch.setattr(knowledge.cfg, "model_key", "local-test-key")
    monkeypatch.setattr(knowledge.cfg, "allow_model_data", False)
    outbound = []

    def send(url, *, headers, json, **options):
        outbound.append(json)
        assert url == "https://api.deepseek.com/chat/completions"
        assert headers["Authorization"] == "Bearer local-test-key"
        return httpx.Response(200, request=httpx.Request("POST", url),
                              json={"choices": [{"message": {"content": '{"ok":true}'}}]})

    monkeypatch.setattr(knowledge._model_http, "post", send)
    assert post(client, "/integrations/model/test") == {"status": "ok", "model": "deepseek-flash"}
    assert outbound[0]["messages"] == [{"role": "user", "content": 'Return this JSON object exactly: {"ok":true}'}]
    assert outbound[0]["thinking"] == {"type": "disabled"}


def test_unknown_fault_code_does_not_return_unrelated_alarm(services):
    client, worker = services
    post(client, "/auth/demo", {"role": "tech"})
    device = next(row for row in get(client, "/devices") if row["model"] == "IM-200")
    session = post(client, "/sessions", {"deviceId": device["id"]}, 201)
    url = f"/sessions/{session['id']}/turns"
    turn = post(client, url, {"clientMessageId": uuid4().hex, "text": "ZZ999 温度告警是什么意思？"}, 202)
    do_job(worker, "turn")
    answer = get(client, f"{url}/{turn['id']}")
    assert answer["decision"] == "no_evidence" and answer["citations"] == []


def test_device_model_in_question_is_not_copied_as_fault_code(services):
    client, worker = services
    post(client, "/auth/demo", {"role": "tech"})
    device = next(row for row in get(client, "/devices") if row["model"] == "IM-200")
    session = post(client, "/sessions", {"deviceId": device["id"]}, 201)
    url = f"/sessions/{session['id']}/turns"
    post(client, url, {"clientMessageId": uuid4().hex, "text": "IM-200 型号有哪些温度告警资料？"}, 202)
    do_job(worker, "turn")
    draft = post(client, "/drafts", {"sessionId": session["id"]}, 201)
    assert draft["fields"]["faultCode"] == ""


def test_grounding_rejects_missing_lines_and_keeps_qualifiers(services, monkeypatch):
    from maintenance import conversations

    evidence = [{"category": "manual", "text": "禁止跨型号使用保护参数。"}]
    monkeypatch.setattr(conversations, "model_call", lambda *_: {
        "choices": [{"message": {"content": json.dumps({
            "claims": [{"line": 9, "text": "跨型号使用保护参数。"}],
        })}}]})
    with pytest.raises(ValueError):
        conversations.grounded_answer("可否跨型号使用保护参数？", evidence)
    assert conversations.finding_text(evidence[0]["text"], "跨型号可以使用保护参数。") == evidence[0]["text"]
    assert conversations.finding_text("额定压力为 1.25 MPa。", "额定压力为 2.5 MPa。") == "额定压力为 1.25 MPa。"
    assert conversations.finding_text("E07 表示温度告警。", "E07 确认是传感器损坏。") == "E07 表示温度告警。"


def test_answer_shows_only_the_sources_it_cites(services, monkeypatch):
    from maintenance import conversations

    client, worker = services
    post(client, "/auth/demo", {"role": "tech"})
    device = next(row for row in get(client, "/devices") if row["model"] == "IM-200")
    text = "ｅ－０７ 温度告警是什么？"
    candidates = get(client, f"/knowledge/search?deviceId={device['id']}&q={text}")
    assert {row["number"] for row in candidates} == {"MAN-001", "WO-001"}
    monkeypatch.setattr(conversations.cfg, "allow_model_data", True)
    monkeypatch.setattr(conversations.cfg, "chat_model", "validation-chat")
    def model_reply(_, payload):
        message = json.loads(payload["messages"][-1]["content"])
        assert message["normalizedFaultCodes"] == ["E07"]
        return {"choices": [{"message": {"content": json.dumps({
            "claims": [{"line": model_line(payload, "示例故障码 E07 表示温度告警。")}],
        })}}]}

    monkeypatch.setattr(conversations, "model_call", model_reply)
    session = post(client, "/sessions", {"deviceId": device["id"]}, 201)
    url = f"/sessions/{session['id']}/turns"
    turn = post(client, url, {"text": text, "clientMessageId": uuid4().hex}, 202)
    do_job(worker, "turn")
    result = get(client, f"{url}/{turn['id']}")
    assert result["decision"] == "answer" and "示例故障码 E07 表示温度告警。 [1]" in result["answer"]
    assert [row["number"] for row in result["citations"]] == ["MAN-001"]


def test_maintenance_skill_uses_context_to_guide_troubleshooting(services, monkeypatch):
    from maintenance import conversations

    client, worker = services
    post(client, "/auth/demo", {"role": "tech"})
    device = next(row for row in get(client, "/devices") if row["model"] == "IM-200")
    session = post(client, "/sessions", {"deviceId": device["id"]}, 201)
    url = f"/sessions/{session['id']}/turns"
    monkeypatch.setattr(conversations.cfg, "allow_model_data", True)
    monkeypatch.setattr(conversations.cfg, "chat_model", "validation-chat")
    first = "电机连续运行一段时间后出现 E07。"
    second = "这种情况下一步先查什么？"
    prompts = []

    def model_reply(_, payload):
        assert payload["messages"][0]["content"] == conversations._ANSWER_SKILL
        message = json.loads(payload["messages"][-1]["content"])
        prompts.append(message)
        evidence = {item["type"]: item for item in message["evidence"]}
        manual = evidence["manual"]
        claims = [{"line": model_line(payload, "示例故障码 E07 表示温度告警。"),
                   "text": "示例中的 E07 是温度告警。"}]
        if message["question"] == second:
            cause = "已确认原因：示例环境中通风区域积尘。"
            claims.extend([{"line": manual["checkLines"][0]},
                           {"line": model_line(payload, cause), "text": "配合手册确认本次是积尘。"}])
        reply = {"claims": claims}
        if message["question"] != first:
            reply["followUp"] = "load"
        return {"choices": [{"message": {"content": json.dumps(reply)}}]}

    monkeypatch.setattr(conversations, "model_call", model_reply)
    for text in (first, second):
        turn = post(client, url, {"text": text, "clientMessageId": uuid4().hex}, 202)
        do_job(worker, "turn")
    result = get(client, f"{url}/{turn['id']}")
    assert prompts[-1]["question"] == second
    assert prompts[-1]["previousQuestions"] == [first]
    assert prompts[-1]["troubleshooting"] and not prompts[-1]["fullProcedureAvailable"]
    assert result["decision"] == "answer"
    assert result["answer"].startswith("示例中的 E07 是温度告警。 [1]")
    assert result["answer"].index("记录告警时间") < result["answer"].index("历史记录（仅供对照）\n已确认原因")
    assert "确认本次是积尘" not in result["answer"]
    assert "不足以确认完整维修步骤" in result["answer"]
    assert "处于什么负载或工况" in result["answer"]
    assert [row["number"] for row in result["citations"]] == ["MAN-001", "WO-001"]
    third = post(client, url, {"text": "满负载时才出现", "clientMessageId": uuid4().hex}, 202)
    do_job(worker, "turn")
    clarified = get(client, f"{url}/{third['id']}")
    assert prompts[-1]["lastFollowUp"] == "load" and prompts[-1]["troubleshooting"]
    assert clarified["decision"] == "answer"
    assert "处于什么负载或工况" not in clarified["answer"]


def test_chat_retries_temporary_provider_failure(services, monkeypatch):
    import httpx
    from maintenance import knowledge

    monkeypatch.setattr(knowledge.cfg, "model_url", "https://example.com")
    monkeypatch.setattr(knowledge.cfg, "allow_model_data", True)
    codes = iter([503, 200, 503, 503])

    def send(url, **kwargs):
        return httpx.Response(next(codes), request=httpx.Request("POST", url), json={"choices": []})

    monkeypatch.setattr(knowledge._model_http, "post", send)
    assert knowledge.model_call("chat/completions", {}) == {"choices": []}
    with pytest.raises(httpx.HTTPStatusError):
        knowledge.model_call("chat/completions", {})
    assert next(codes, None) is None


def test_symptom_search_requires_evidence_for_the_reported_signal(services, monkeypatch):
    from maintenance import conversations

    client, worker = services
    monkeypatch.setattr(conversations.cfg, "allow_model_data", False)
    post(client, "/auth/demo", {"role": "tech"})
    device = next(row for row in get(client, "/devices") if row["number"] == "MTR-001")

    def new_thread():
        session = post(client, "/sessions", {"deviceId": device["id"]}, 201)
        return f"/sessions/{session['id']}/turns"

    def ask(path, question):
        turn = post(client, path, {"text": question, "clientMessageId": uuid4().hex}, 202)
        do_job(worker, "turn")
        return get(client, f"{path}/{turn['id']}")

    path = new_thread()
    result = ask(path, "电机压力异常")
    assert result["decision"] == "clarify" and result["citations"] == []
    assert device["name"] in result["answer"] and "测点" in result["answer"]
    result = ask(path, "出口测点，没有告警")
    assert result["decision"] == "no_evidence" and result["citations"] == []
    assert "转交班组专家" in result["answer"]
    for text in ("电机轴承过热", "电机振动和压力异常"):
        result = ask(new_thread(), text)
        assert result["decision"] == "clarify" and result["citations"] == []
    result = ask(new_thread(), "电机启动后温升异常")
    assert result["decision"] == "evidence"
    assert "MAN-001" in {row["number"] for row in result["citations"]}


def test_chat_outage_keeps_the_scoped_references(services, monkeypatch):
    import httpx
    from maintenance import conversations

    client, worker = services
    post(client, "/auth/demo", {"role": "tech"})
    device = next(row for row in get(client, "/devices") if row["model"] == "IM-200")
    session = post(client, "/sessions", {"deviceId": device["id"]}, 201)
    url = f"/sessions/{session['id']}/turns"
    monkeypatch.setattr(conversations.cfg, "allow_model_data", True)
    monkeypatch.setattr(conversations.cfg, "chat_model", "local-test-model")

    def unavailable(*args):
        raise httpx.ReadTimeout("simulated provider outage")

    monkeypatch.setattr(conversations, "model_call", unavailable)
    turn = post(client, url, {"clientMessageId": uuid4().hex, "text": "E07 温度告警可能与什么有关？"}, 202)
    do_job(worker, "turn")
    result = get(client, f"{url}/{turn['id']}")
    assert result["decision"] == "evidence" and result["modelDegraded"]
    assert result["citations"] and all("E07" in row["text"] for row in result["citations"])


def test_new_documents_can_be_approved_for_model_answers(services, monkeypatch):
    from maintenance import conversations

    client, worker = services
    monkeypatch.setattr(conversations.cfg, "allow_model_data", True)
    monkeypatch.setattr(conversations.cfg, "model_demo_only", True)
    monkeypatch.setattr(conversations.cfg, "model_demo_source_ids", "")
    monkeypatch.setattr(conversations.cfg, "model_url", "https://model.example.invalid")
    monkeypatch.setattr(conversations.cfg, "chat_model", "validation-chat")
    post(client, "/auth/demo", {"role": "platform"})
    model = "QRM-610"
    new_device = post(client, "/devices", {"number": model, "name": "验收用新设备", "model": model})

    quote = "验收设备的观察窗口直径为 64 毫米。"
    private = "验收设备的观察窗口仅作为内部检查使用。"
    outbound = []

    def fake_model(endpoint, payload):
        context = json.loads(payload["messages"][-1]["content"])
        outbound.append(context)
        assert [line["text"] for item in context["evidence"] for line in item["lines"]] == [quote]
        return {"choices": [{"message": {"content": json.dumps({
            "claims": [{"line": model_line(payload, quote)}],
        })}}]}

    monkeypatch.setattr(conversations, "model_call", fake_model)

    def ask(text):
        post(client, "/auth/demo", {"role": "tech"})
        session = post(client, "/sessions", {"deviceId": new_device["id"]}, 201)
        url = f"/sessions/{session['id']}/turns"
        turn = post(client, url, {"text": text, "clientMessageId": uuid4().hex}, 202)
        do_job(worker, "turn")
        return get(client, f"{url}/{turn['id']}")

    def publish(number, body, allow, category):
        post(client, "/auth/demo", {"role": "library"})
        metadata = dict(number=number, version="1.0", title=number, model=model, category=category)
        source_id = upload(client, metadata, body)["items"][0]["sourceId"]
        do_job(worker, "parse")
        path = f"/sources/{source_id}"
        source = get(client, path)
        post(client, path + "/actions", dict(revision=source["revision"], action="review",
             verified=True, allowExternalModel=True), 422)
        source = post(client, path + "/actions", dict(revision=source["revision"], action="review", verified=True))
        post(client, path + "/actions", dict(revision=source["revision"], action="approve", allowExternalModel=allow), 403)
        post(client, "/auth/demo", {"role": "expert"})
        assert get(client, path)["modelApprovalHost"] == "model.example.invalid"
        source = post(client, path + "/actions", dict(revision=source["revision"], action="approve", allowExternalModel=allow))
        assert get(client, path)["externalModelApproved"] is allow
        post(client, "/auth/demo", {"role": "library"})
        post(client, path + "/actions", dict(revision=source["revision"], action="publish"))
        return source_id

    publish("PRIVATE-QRM", private, False, "case")
    assert ask("验收设备的观察窗口有哪些历史案例？")["decision"] == "evidence"
    assert outbound == []
    approved_id = publish("MAN-QRM-610", quote, True, "manual")
    result = ask("验收设备的观察窗口直径是多少？")
    assert result["decision"] == "answer" and quote in result["answer"]
    assert [row["sourceId"] for row in result["citations"]] == [approved_id]
    assert len(outbound) == 1
    monkeypatch.setattr(conversations.cfg, "model_url", "https://replacement.example.invalid")
    assert ask("验收设备的观察窗口直径是多少？")["decision"] == "evidence"
    assert len(outbound) == 1


def test_repair_questions_require_applicable_numbered_manual_steps(services, monkeypatch):
    from maintenance import conversations

    client, worker = services
    original = ("L92 记录校验提示\n"
                "适用条件：验收台处于记录核对状态。\n"
                "排查步骤：\n1. 核对样本记录编号。\n2. 对照台账中的记录版本。")
    for incomplete in (original.replace("2. 对照", "3. 对照"), original + "\n第 3 步："):
        assert conversations.procedure_excerpt([{"position": "文本正文", "text": incomplete}], {}, {"L92"}) is None
    by_symptom = ("故障现象：泵运行但不出水（流量为零）\n"
                  "排查步骤：\n1. 核对出口指示。\n2. 核对入口条件。\n"
                  "故障现象：泵流量波动\n排查步骤：\n1. 核对入口记录。")
    question = "泵运行但不出水怎么查？"
    entry = {"position": "文本正文", "text": by_symptom}
    extracted = conversations.procedure_excerpt([entry], {}, set(),
        signals=conversations.symptom_signals(question), details=conversations.symptom_details(question))
    assert extracted and "核对入口条件" in extracted[1] and "核对入口记录" not in extracted[1]
    monkeypatch.setattr(conversations.cfg, "allow_model_data", False)
    post(client, "/auth/demo", {"role": "library"})
    info = dict(number="L92-PROCEDURE", version="1.0", title="记录核对验收手册",
                model="IM-200", batch="A01", category="manual")
    source_id = upload(client, info, original)["items"][0]["sourceId"]
    do_job(worker, "parse")
    action = f"/sources/{source_id}/actions"
    source = get(client, f"/sources/{source_id}")
    source = post(client, action, dict(action="review", revision=source["revision"], verified=True))
    post(client, "/auth/demo", {"role": "expert"})
    source = post(client, action, dict(action="approve", revision=source["revision"]))
    post(client, "/auth/demo", {"role": "library"})
    source = post(client, action, dict(action="publish", revision=source["revision"]))
    post(client, "/auth/demo", {"role": "tech"})
    devices = {row["batch"]: row["id"] for row in get(client, "/devices") if row["model"] == "IM-200"}

    def new_conversation(batch):
        session = post(client, "/sessions", {"deviceId": devices[batch]}, 201)
        return f"/sessions/{session['id']}/turns"

    def ask(url, text):
        turn = post(client, url, {"clientMessageId": uuid4().hex, "text": text}, 202)
        do_job(worker, "turn")
        return get(client, f"{url}/{turn['id']}")

    turns = new_conversation("A01")
    ask(turns, "L92 提示是什么意思？")
    result = ask(turns, "怎么排查？")
    assert result["decision"] == "answer" and original in result["answer"]
    assert [row["sourceId"] for row in result["citations"]] == [source_id]
    bare_code = ask(new_conversation("A01"), "Ｌ９２")
    assert bare_code["decision"] == "answer" and original in bare_code["answer"]
    wrong_batch = ask(new_conversation("A02"), "L92 怎么排查？")
    assert wrong_batch["decision"] == "no_evidence" and not wrong_batch["citations"]
    only_definition = ask(new_conversation("A01"), "E07 怎么修复？")
    assert only_definition["decision"] == "evidence" and "不足以确认" in only_definition["answer"]
    assert [row["number"] for row in only_definition["citations"]] == ["MAN-001"]
    safety = ask(new_conversation("A01"), "L92 怎么带电拆卸处理？")
    assert safety["decision"] == "safety" and original not in safety["answer"]

    post(client, "/auth/demo", {"role": "library"})
    post(client, action, dict(action="block", revision=source["revision"]))
    post(client, "/auth/demo", {"role": "tech"})
    expired = get(client, f"{turns}/{result['id']}")
    assert expired["decision"] == "expired" and original not in expired["answer"]


@pytest.mark.parametrize("suffix,category,body,question,expected,position", [
    ("txt", "manual", "故障码：104\n适用条件：通过记录核对。\n解决办法：\n1. 核对样本登记名。\n2. 对照归档编号。",
     "故障码 104 怎么处理？", "对照归档编号", "文本正文"),
    ("md", "manual", "验收阶段仅核对记录。\n## F23\n处理步骤：\n1. 查验样本批号。\n\n## F24\n"
     "处置步骤：\n1. 核对抄录日期。\n2. 核对封存编号。",
     "Ｆ－２４怎么排查？", "核对封存编号", "文本正文"),
    ("docx", "manual", "D63 记录校验提示\n故障排查\n第一步：核对登记名称。\n第二步：核对归档日期。",
     "D63 的排查方法是什么？", "核对归档日期", "D63 记录校验提示"),
    ("pdf", "manual", "Fault code: T74\nTroubleshooting\nStep 1: Check the sample label.\nStep 2: Compare the record number.",
     "How do I troubleshoot T74?", "Compare the record number.", "第 1 页"),
    ("csv", "case", "案例编号,现象,结果\nQA8,观察窗标签模糊,归档标记已经更正",
     "观察窗标签模糊的案例处理结果是什么？", "归档标记已经更正", "记录 QA8"),
])
def test_independent_import_formats(services, monkeypatch, suffix, category, body, question, expected, position):
    import pymupdf
    from docx import Document
    from maintenance import conversations

    raw = body.encode("utf-8")
    if suffix == "docx":
        document = Document()
        title, *lines = body.splitlines()
        document.add_heading(title, level=1)
        for line in lines:
            document.add_paragraph(line)
        stream = BytesIO()
        document.save(stream)
        raw = stream.getvalue()
    elif suffix == "pdf":
        with pymupdf.open() as document:
            page = document.new_page()
            for index, line in enumerate(body.splitlines()):
                page.insert_text((55, 55 + 19 * index), line)
            raw = document.tobytes()

    client, worker = services
    monkeypatch.setattr(conversations.cfg, "allow_model_data", False)
    post(client, "/auth/demo", {"role": "platform"})
    model = f"FORMAT-{suffix.upper()}"
    device = post(client, "/devices", {"number": model, "name": "独立验收设备", "model": model})
    post(client, "/auth/demo", {"role": "library"})
    metadata = dict(number=model, version="1.0", title="独立验收资料", model=model, category=category)
    response = client.post("/api/v1/sources/import", data={"metadata": json.dumps([metadata])},
                           files={"files": (f"sample.{suffix}", raw, "application/octet-stream")})
    assert response.status_code == 202
    source_id = response.json()["items"][0]["sourceId"]
    do_job(worker, "parse")
    path = f"/sources/{source_id}"
    source = get(client, path)
    source = post(client, path + "/actions", dict(action="review", revision=source["revision"], verified=True))
    post(client, "/auth/demo", {"role": "expert"})
    source = post(client, path + "/actions", dict(action="approve", revision=source["revision"]))
    post(client, "/auth/demo", {"role": "library"})
    post(client, path + "/actions", dict(action="publish", revision=source["revision"]))
    post(client, "/auth/demo", {"role": "tech"})
    session = post(client, "/sessions", {"deviceId": device["id"]}, 201)
    path = f"/sessions/{session['id']}/turns"

    def ask(text):
        turn = post(client, path, {"clientMessageId": uuid4().hex, "text": text}, 202)
        do_job(worker, "turn")
        return get(client, f"{path}/{turn['id']}")

    answer = ask(question)
    citations = answer.get("citations", [])
    wanted = "answer" if category == "manual" else "evidence"
    assert answer["decision"] == wanted, answer["answer"]
    assert [row["sourceId"] for row in citations] == [source_id]
    assert citations[0]["position"] == position
    assert expected in (answer["answer"] if category == "manual" else citations[0]["text"])
    if suffix == "md":
        assert "查验样本批号" not in answer["answer"] and "验收阶段仅核对记录。" in answer["answer"]
        ask("F23 是什么提示？")
        followup = ask("那怎么处理？")
        assert "查验样本批号" in followup["answer"] and "核对封存编号" not in followup["answer"]
    if suffix == "pdf":
        safety = ask("How do I fix T74 while energized?")
        assert safety["decision"] == "safety" and expected not in safety["answer"]


def test_completed_filter_applies_before_list_limit(services):
    from maintenance.db import DB, add

    client, _ = services
    with DB.begin() as db:
        closed = add(db, "demo", "draft", owner="tech", status="linked",
                     device={"model": "IM-200", "batch": "A01"}, completedAt="2020-01-01T00:00:00Z")
        closed.updated = "2020-01-01T00:00:00Z"
        closed_id = closed.id
        for _ in range(100):
            add(db, "demo", "draft", owner="tech", status="linked",
                device={"model": "IM-200", "batch": "A01"})
    post(client, "/auth/demo", {"role": "library"})
    assert closed_id in {row["id"] for row in get(client, "/completed")}

