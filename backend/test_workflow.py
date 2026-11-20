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
    assert turn["citations"][0]["referenceUrl"] == info["referenceUrl"]
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
    draft = post(client, draft_url + "/confirm", {"revision": draft["revision"]})
    assert draft["status"] == "confirmed"
    post(client, draft_url + "/submit", {"revision": draft["revision"]}, 409)
    export = post(client, draft_url + "/export", {"revision": draft["revision"]})
    assert export["fields"]["result"] == fields["result"]
    assert export["references"][0]["referenceUrl"] == info["referenceUrl"]
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

    monkeypatch.setattr(knowledge.httpx, "post", send)
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


def test_grounding_rejects_quote_that_omits_negation(services, monkeypatch):
    from maintenance import conversations

    evidence = [{"category": "manual", "text": "禁止跨型号使用保护参数。"}]
    monkeypatch.setattr(conversations, "model_call", lambda *_: {
        "choices": [{"message": {"content": json.dumps({"claims": [{"id": 1, "quote": "跨型号使用保护参数。"}]})}}]})
    with pytest.raises(ValueError):
        conversations.grounded_answer("可否跨型号使用保护参数？", evidence)


def test_answer_shows_only_the_sources_it_cites(services, monkeypatch):
    from maintenance import conversations

    client, worker = services
    post(client, "/auth/demo", {"role": "tech"})
    device = next(row for row in get(client, "/devices") if row["model"] == "IM-200")
    text = "E07 温度告警是什么？"
    candidates = get(client, f"/knowledge/search?deviceId={device['id']}&q={text}")
    assert {row["number"] for row in candidates} == {"MAN-001", "WO-001"}
    monkeypatch.setattr(conversations.cfg, "allow_model_data", True)
    monkeypatch.setattr(conversations.cfg, "chat_model", "validation-chat")
    monkeypatch.setattr(conversations, "model_call", lambda *_: {
        "choices": [{"message": {"content": json.dumps({
            "claims": [{"id": 1, "quote": "示例故障码 E07 表示温度告警。"}],
        })}}]})
    session = post(client, "/sessions", {"deviceId": device["id"]}, 201)
    url = f"/sessions/{session['id']}/turns"
    turn = post(client, url, {"text": text, "clientMessageId": uuid4().hex}, 202)
    do_job(worker, "turn")
    result = get(client, f"{url}/{turn['id']}")
    assert result["decision"] == "answer" and "示例故障码 E07 表示温度告警。 [1]" in result["answer"]
    assert [row["number"] for row in result["citations"]] == ["MAN-001"]


def test_chat_retries_temporary_provider_failure(services, monkeypatch):
    import httpx
    from maintenance import knowledge

    monkeypatch.setattr(knowledge.cfg, "model_url", "https://example.com")
    monkeypatch.setattr(knowledge.cfg, "allow_model_data", True)
    codes = iter([503, 200, 503, 503])

    def send(url, **kwargs):
        return httpx.Response(next(codes), request=httpx.Request("POST", url), json={"choices": []})

    monkeypatch.setattr(knowledge.httpx, "post", send)
    assert knowledge.model_call("chat/completions", {}) == {"choices": []}
    with pytest.raises(httpx.HTTPStatusError):
        knowledge.model_call("chat/completions", {})
    assert next(codes, None) is None


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
        assert [item["text"] for item in context["evidence"]] == [quote]
        return {"choices": [{"message": {"content": json.dumps({"claims": [{"id": 1, "quote": quote}]})}}]}

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

