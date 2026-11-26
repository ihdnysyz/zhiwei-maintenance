import json
import os
from io import BytesIO
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient


TEST_MANUAL_FACT = "验收故障码 E07 表示温度告警。"
TEST_WORKORDER_CAUSE = "已确认原因：验收状态位与输入记录不一致。"


@pytest.fixture(scope="module")
def services(tmp_path_factory):
    database = tmp_path_factory.mktemp("zhiwei") / "test.db"
    provided = os.environ.get("ZHIWEI_TEST_DATABASE_URL")
    keys = {"DATABASE_URL": provided or "sqlite:///" + database.as_posix(), "DEMO_MODE": "true",
            "ADMIN_ORG": "another_org", "ADMIN_PASSWORD": "local-test-password",
            "ALLOW_MODEL_DATA": "false", "WORKORDER_ENABLED": "false",
            "MODEL_DEMO_ONLY": "false", "MODEL_REQUIRE_SOURCE_APPROVAL": "false",
            "API_INIT": "false" if provided else "true"}
    previous = {key: os.environ.get(key) for key in keys}
    os.environ.update(keys)
    from maintenance.main import app
    from maintenance import worker
    with TestClient(app) as client:
        prepare_test_workspace(client, worker)
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


def prepare_test_workspace(client, worker):
    post(client, "/auth/demo", {"role": "library"})
    assert get(client, "/devices") == []
    assert get(client, "/sources") == []
    post(client, "/auth/demo", {"role": "platform"})
    for index in (1, 2):
        post(client, "/devices", {"number": f"TEST-DEVICE-{index}", "model": "IM-200",
                                  "name": f"回归测试设备 {index}", "batch": f"A0{index}"})
    cases = [
        ("TEST-MAN-001", "manual", TEST_MANUAL_FACT + "记录告警时间和测试负载，核对采样记录。"),
        ("TEST-WO-001", "workorder", "验收场景中，电机运行后出现 E07 温度告警。" + TEST_WORKORDER_CAUSE),
        ("TEST-CASE-001", "case", "验收场景中，电机振动增大，同时出现间歇性异响。现场记录了试验时间。"),
    ]
    for number, category, body in cases:
        post(client, "/auth/demo", {"role": "library"})
        info = dict(number=number, title=number, version="1.0", model="IM-200", category=category)
        source_id = upload(client, info, body)["items"][0]["sourceId"]
        do_job(worker, "parse")
        assert get(client, f"/sources/{source_id}")["status"] == "published"
    client.cookies.clear()


def test_platform_batch_creates_accounts_atomically(services):
    client, _ = services
    path = "/accounts/batch"
    username = "batch-repair"
    password = "batch-test-password"
    account = dict(username=username, name="批量维修账号", role="tech", password=password, models=["IM-200"])
    second = dict(username="batch-library", name="批量知识账号", role="library", active=False)

    client.cookies.clear()
    post(client, path, {"accounts": [account]}, 401)
    for role in ("tech", "expert", "library"):
        post(client, "/auth/demo", {"role": role})
        post(client, path, {"accounts": [account]}, 403)

    post(client, "/auth/demo", {"role": "platform"})
    original = {item["key"]: item for item in get(client, "/accounts")}
    post(client, path, {"accounts": []}, 422)
    post(client, path, {"accounts": [account] * 51}, 422)
    post(client, path, {"accounts": [account, {**second, "username": "invalid name"}]}, 422)
    assert post(client, path, {"accounts": [account, account]}, 409)["code"] == "DUPLICATE_ACCOUNT"
    assert post(client, path, {"accounts": [account, {**account, "username": "batch-z", "password": "short"}]},
                422)["code"] == "WEAK_PASSWORD"
    assert post(client, path, {"accounts": [account, {**second, "username": "tech"}]}, 409)["code"] == "ACCOUNT_EXISTS"
    assert {item["key"]: item for item in get(client, "/accounts")} == original

    assert post(client, path, {"accounts": [account, second]}, 201) == {"count": 2}
    result = {item["key"]: item for item in get(client, "/accounts")}
    assert result[username]["models"] == ["IM-200"] and result[username]["status"] == "active"
    assert result[second["username"]]["role"] == "library" and result[second["username"]]["status"] == "disabled"
    assert "password" not in result[username]
    expected_targets = {result[key]["id"] for key in (username, second["username"])}
    events = [item for item in get(client, "/operations")["audit"] if item["action"] == "批量创建账号"]
    assert {item["target"] for item in events} == expected_targets
    assert all(item["actor"] == "platform" and item["created"] for item in events)
    assert post(client, path, {"accounts": [account, second]}, 409)["code"] == "ACCOUNT_EXISTS"

    login = post(client, "/auth/login", dict(org="demo", username=username, password=password))
    assert login["role"] == "tech" and {item["model"] for item in get(client, "/devices")} == {"IM-200"}
    post(client, "/auth/login", dict(org="another_org", username="admin", password="local-test-password"))
    assert username not in {item["key"] for item in get(client, "/accounts")}
    assert post(client, path, {"accounts": [account]}, 201) == {"count": 1}
    client.cookies.clear()


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
    assert get(client, f"/imports/{batch['id']}")["items"][0]["status"] == "published"
    source = get(client, f"/sources/{source_id}")
    assert source["chunks"][0]["text"] == original
    first, second = get(client, "/devices")
    assert any(row["id"] == source_id and row["active"]
               for row in get(client, f"/sources?deviceId={first['id']}"))
    assert source_id not in {row["id"] for row in get(client, f"/sources?deviceId={second['id']}")}
    assert any(event["action"] == "资料解析完成，已自动生效" and event["actor"] == "worker"
               for event in source["operationHistory"])
    post(client, f"/sources/{source_id}/actions", dict(revision=source["revision"], action="approve"), 403)
    post(client, "/auth/login", dict(org="another_org", username="admin", password="local-test-password"))
    assert get(client, "/sources") == []
    get(client, f"/sources/{source_id}", 404)
    post(client, "/auth/demo", {"role": "tech"})
    first, second = get(client, "/devices")
    listed = next(row for row in get(client, "/sources") if row["id"] == source_id)
    assert listed["applicableDeviceId"] == first["id"]
    matching = get(client, f"/sources?deviceId={first['id']}")
    other = get(client, f"/sources?deviceId={second['id']}")
    assert source_id in {row["id"] for row in matching} - {row["id"] for row in other}
    assert {row["category"] for row in other} == {"manual", "case", "workorder"}
    assert {row["applicableDeviceId"] for row in other} == {second["id"]}
    get(client, "/sources?deviceId=missing", 404)
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
    # Already-published documents can still contain an unsplit table from the old parser.
    from maintenance.conversations import procedure_excerpt
    old_table = ("Code (hex)    Meaning                 What to do\n"
                 "0xCA          Record missing          Check the sample record.\n"
                 "0xCB          Label mismatch          Compare the sample label.")
    assert knowledge.chunk_fault_codes("第 8 页", old_table) == {"0XCA", "0XCB"}
    entry = {"position": "第 8 页", "text": old_table}
    excerpt = procedure_excerpt([entry], {}, {"0XCA"})
    assert excerpt and "Check the sample record." in excerpt[1] and "Compare the sample label." not in excerpt[1]
    assert entry["text"] == old_table


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
        row = find(db, "demo", "source", key="TEST-MAN-001:1.0")
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


def test_numeric_fault_queries_are_precise_and_answer_without_external_model(services, monkeypatch):
    from docx import Document
    from maintenance import conversations
    from maintenance.knowledge import fault_codes

    client, worker = services
    monkeypatch.setattr(conversations.cfg, "allow_model_data", False)
    post(client, "/auth/demo", {"role": "platform"})
    device = post(client, "/devices", {"number": "TEST-NUMERIC-DEVICE", "model": "NUMERIC-QA",
                                      "name": "数字故障码回归设备", "batch": "A01"})

    document = Document()
    for heading, paragraphs in (
        ("故障码 4110 记录异常", ("故障含义：登记记录缺失。", "排查步骤：", "1. 核对样本记录。", "2. 对照归档编号。")),
        ("故障码 4210 计数异常", ("故障含义：计数记录不一致。", "排查步骤：", "1. 核对计数记录。")),
        ("故障码 5070", ("故障含义：校验窗口未找到。",)),
        ("故障码 D7 目录异常", ("故障含义：目录页缺失。", "处理措施：核对目录页。")),
        ("故障码 A3A1 校验不一致", ("故障含义：登记值不一致。", "处理措施：核对登记值。")),
        ("故障码 50A0 版本错误", ("故障含义：记录版本不一致。", "处理措施：核对记录版本。")),
        ("故障码 8802 样本编号无效", ("适用验收记录。",)),
        ("普通参数", ("额定功率为 4110 W，参数 4110 用于台账核对。", "样本干重为 17 kg。运行重量为 23 kg。")),
    ):
        document.add_heading(heading, level=1)
        for paragraph in paragraphs:
            document.add_paragraph(paragraph)
    stream = BytesIO()
    document.save(stream)
    post(client, "/auth/demo", {"role": "library"})
    metadata = dict(number="NUMERIC-MANUAL-QA", version="1", title="数字故障码回归手册",
                    model=device["model"], batch=device["batch"], category="manual")
    imported = client.post("/api/v1/sources/import", data={"metadata": json.dumps([metadata])},
                           files={"files": ("numeric.docx", stream.getvalue(), "application/octet-stream")})
    assert imported.status_code == 202
    source_id = imported.json()["items"][0]["sourceId"]
    do_job(worker, "parse")
    assert get(client, f"/sources/{source_id}")["active"]
    post(client, "/auth/demo", {"role": "tech"})

    def thread():
        session = post(client, "/sessions", {"deviceId": device["id"]}, 201)
        return f"/sessions/{session['id']}/turns"

    def ask(path, text):
        turn = post(client, path, {"text": text, "clientMessageId": uuid4().hex}, 202)
        do_job(worker, "turn")
        return get(client, f"{path}/{turn['id']}")

    path = thread()
    for question in ("4110", "４１１０！", "4110是什么故障", "4110什么意思？", "面板报了4110", "面板报4110",
                     "4110的故障含义", "What is 4110?", "4110怎么回事？", "4110什么原因", "4110怎么维修", "4110解决办法",
                     "4110有哪些排查措施？", "报码：4110", "4110故障如何消除？", "4110对应什么",
                     "故障码 4110 怎么排查？", "我只问了4110，为什么你回复其他的故障码内容？"):
        assert fault_codes(question, device) == {"4110"}
        result = ask(path, question)
        assert result["decision"] == "answer", (question, result["answer"])
        assert "登记记录缺失" in result["answer"] and "对照归档编号" in result["answer"]
        assert [row["position"] for row in result["citations"]] == ["故障码 4110 记录异常"]
        ranked = client.get("/api/v1/knowledge/search", params={"deviceId": device["id"], "q": question})
        assert ranked.status_code == 200 and [row["position"] for row in ranked.json()] == ["故障码 4110 记录异常"]

    path = thread()
    assert ask(path, "4110")["decision"] == "answer"
    assert "对照归档编号" in ask(path, "样本记录正常，下一步怎么查？")["answer"]
    draft = post(client, "/drafts", {"sessionId": path.split("/")[2]}, 201)
    assert draft["fields"]["faultCode"] == "4110" and draft["origins"]["faultCode"] == "当前诊断上下文"
    switched = ask(path, "4210是什么故障？")
    assert "计数记录不一致" in switched["answer"] and "对照归档编号" not in switched["answer"]
    assert [row["position"] for row in switched["citations"]] == ["故障码 4210 计数异常"]
    corrected_path = thread()
    ask(corrected_path, "4110")
    corrected = ask(corrected_path, "不是这个，是4210")
    assert "计数记录不一致" in corrected["answer"] and "登记记录缺失" not in corrected["answer"]
    corrected_draft = post(client, "/drafts", {"sessionId": corrected_path.split("/")[2]}, 201)
    assert corrected_draft["fields"]["faultCode"] == "4210"
    multiple = thread()
    ask(multiple, "4110和4210分别是什么故障？")
    assert not post(client, "/drafts", {"sessionId": multiple.split("/")[2]}, 201)["fields"]["faultCode"]
    for question in ("5070", "5070是什么故障？"):
        response = ask(thread(), question)
        assert response["decision"] == "answer" and "校验窗口未找到" in response["answer"]
        assert len(response["citations"]) == 1
    for code, expected in (("D7", "核对目录页"), ("A3A1", "核对登记值"), ("50A0", "核对记录版本"),
                           ("8802", "样本编号无效")):
        for question in (code, f"现在报的是{code}", f"{code}是哪一类故障？"):
            response = ask(thread(), question)
            assert response["decision"] == "answer" and expected in response["answer"], (question, response["answer"])
            assert len(response["citations"]) == 1 and f"故障码 {code} " in response["citations"][0]["position"]
    combined = ask(thread(), "4110和4210分别是什么故障？")
    assert combined["decision"] == "answer" and "登记记录缺失" in combined["answer"] and "计数记录不一致" in combined["answer"]
    assert {row["position"] for row in combined["citations"]} == {"故障码 4110 记录异常", "故障码 4210 计数异常"}
    for question, expected in (("参数 4110 是什么意思？", "台账核对"), ("4110 W 是什么功率？", "额定功率为 4110 W")):
        response = ask(thread(), question)
        assert response["decision"] == "answer" and expected in response["answer"], (question, response["answer"])
        assert [row["position"] for row in response["citations"]] == ["普通参数"]
    weights = thread()
    ask(weights, "干重是多少？")
    running = ask(weights, "运行重量呢？")
    assert running["decision"] == "answer" and "23 kg" in running["answer"] and "17 kg" not in running["answer"]
    for question in ("9999", "9999是什么故障？"):
        response = ask(thread(), question)
        assert response["decision"] == "no_evidence" and not response["citations"]
    for question in ("参数 4110 是什么意思？", "额定功率为 4110 W", "电压显示4110伏", "2024年发生什么故障？",
                     "2024-11-24", "温度为4110℃", "4110 W 是什么功率？", "3.14 是什么数值？"):
        assert not fault_codes(question, device), question
    assert fault_codes("4110和4210分别是什么故障？", device) == {"4110", "4210"}
    assert fault_codes("故障码 4110 和 4210", device) == {"4110", "4210"}
    unsafe = ask(thread(), "4110可以带电拆卸吗？")
    assert unsafe["decision"] == "safety" and "对照归档编号" not in unsafe["answer"]
    assert [row["position"] for row in unsafe["citations"]] == ["故障码 4110 记录异常"]


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
    assert {row["number"] for row in candidates} == {"TEST-MAN-001", "TEST-WO-001"}
    monkeypatch.setattr(conversations.cfg, "allow_model_data", True)
    monkeypatch.setattr(conversations.cfg, "chat_model", "validation-chat")
    def model_reply(_, payload):
        message = json.loads(payload["messages"][-1]["content"])
        assert message["normalizedFaultCodes"] == ["E07"]
        return {"choices": [{"message": {"content": json.dumps({
            "claims": [{"line": model_line(payload, TEST_MANUAL_FACT)}],
        })}}]}

    monkeypatch.setattr(conversations, "model_call", model_reply)
    session = post(client, "/sessions", {"deviceId": device["id"]}, 201)
    url = f"/sessions/{session['id']}/turns"
    turn = post(client, url, {"text": text, "clientMessageId": uuid4().hex}, 202)
    do_job(worker, "turn")
    result = get(client, f"{url}/{turn['id']}")
    assert result["decision"] == "answer" and f"{TEST_MANUAL_FACT} [1]" in result["answer"]
    assert [row["number"] for row in result["citations"]] == ["TEST-MAN-001"]


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
        claims = [{"line": model_line(payload, TEST_MANUAL_FACT), "text": "验收中的 E07 是温度告警。"}]
        if message["question"] == second:
            claims.extend([{"line": manual["checkLines"][0]},
                           {"line": model_line(payload, TEST_WORKORDER_CAUSE), "text": "把旧工单原因当作本次结论。"}])
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
    assert result["answer"].startswith("验收中的 E07 是温度告警。 [1]")
    assert result["answer"].index("记录告警时间") < result["answer"].index("历史记录（仅供对照）\n已确认原因")
    assert "把旧工单原因当作本次结论" not in result["answer"]
    assert "不足以确认完整维修步骤" in result["answer"]
    assert "处于什么负载或工况" in result["answer"]
    assert [row["number"] for row in result["citations"]] == ["TEST-MAN-001", "TEST-WO-001"]
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
    codes = iter([503, 200, 503, 503, None, 200, 401])
    monkeypatch.setattr(knowledge, "sleep", lambda _: None)

    def send(url, **kwargs):
        code = next(codes)
        if code is None:
            raise httpx.ConnectError("simulated transient disconnect")
        return httpx.Response(code, request=httpx.Request("POST", url), json={"choices": []})

    monkeypatch.setattr(knowledge._model_http, "post", send)
    assert knowledge.model_call("chat/completions", {}) == {"choices": []}
    with pytest.raises(httpx.HTTPStatusError):
        knowledge.model_call("chat/completions", {})
    assert knowledge.model_call("chat/completions", {}, connection_test=True) == {"choices": []}
    with pytest.raises(httpx.HTTPStatusError) as unauthorized:
        knowledge.model_call("chat/completions", {}, connection_test=True)
    assert unauthorized.value.response.status_code == 401
    assert next(codes, None) is None


def test_windows_model_tls_keeps_verification_and_custom_ca_overrides(services, monkeypatch):
    import ssl
    from maintenance import knowledge

    default_context = ssl.create_default_context()
    monkeypatch.setattr(knowledge.ssl, "create_default_context", lambda: default_context)
    monkeypatch.setattr(knowledge.sys, "platform", "win32")
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    context = knowledge.model_tls_verification()
    assert context is default_context
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    monkeypatch.setenv("SSL_CERT_FILE", "operator-selected-ca.pem")
    assert knowledge.model_tls_verification() is True


def test_deepseek_retries_invalid_json_but_preserves_refusal(services, monkeypatch):
    import httpx
    from maintenance import knowledge

    monkeypatch.setattr(knowledge.cfg, "model_url", "https://api.deepseek.com")
    monkeypatch.setattr(knowledge.cfg, "model_key", "local-test-key")
    monkeypatch.setattr(knowledge, "sleep", lambda _: None)
    responses = iter([("stop", "  "), ("stop", '{"ok":true}'),
                      ("stop", "invalid JSON"), ("stop", '{"ok":true}'), ("content_filter", "")])

    def send(url, **kwargs):
        reason, body = next(responses)
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "choices": [{"finish_reason": reason, "message": {"content": body}}],
        })

    monkeypatch.setattr(knowledge._model_http, "post", send)
    payload = {"response_format": {"type": "json_object"}}
    for _ in range(2):
        result = knowledge.model_call("chat/completions", payload, connection_test=True)
        assert result["choices"][0]["message"]["content"] == '{"ok":true}'
    filtered = knowledge.model_call("chat/completions", payload, connection_test=True)
    assert filtered["choices"][0]["finish_reason"] == "content_filter"
    assert next(responses, None) is None


def test_symptom_search_requires_evidence_for_the_reported_signal(services, monkeypatch):
    from maintenance import conversations

    client, worker = services
    monkeypatch.setattr(conversations.cfg, "allow_model_data", False)
    post(client, "/auth/demo", {"role": "tech"})
    device = next(row for row in get(client, "/devices") if row["number"] == "TEST-DEVICE-1")

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
    assert "TEST-MAN-001" in {row["number"] for row in result["citations"]}


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
        assert source["status"] == "published" and not source["externalModelApproved"]
        post(client, path + "/actions", dict(revision=source["revision"], action="review",
             verified=True, allowExternalModel=True), 422)
        post(client, path + "/actions", dict(revision=source["revision"], action="authorize_model"), 403)
        post(client, "/auth/demo", {"role": "expert"})
        assert get(client, path)["modelApprovalHost"] == "model.example.invalid"
        if allow:
            source = post(client, path + "/actions", dict(revision=source["revision"], action="authorize_model"))
            assert source["status"] == "published"
        assert get(client, path)["externalModelApproved"] is allow
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
    assert get(client, f"/sources/{source_id}")["active"]
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
    follow_up = ask(turns, "样本记录编号已确认正常，然后呢？")
    assert follow_up["decision"] == "answer" and "下一步" in follow_up["answer"]
    assert "对照台账中的记录版本" in follow_up["answer"] and "核对样本记录编号" not in follow_up["answer"]
    colloquial = ask(turns, "样本记录编号没问题，下一步去查哪里？")
    assert colloquial["answer"] == follow_up["answer"]
    uncertain = ask(turns, "如果样本记录编号正常，然后呢？")
    assert uncertain["decision"] == "answer" and "核对样本记录编号" in uncertain["answer"]
    switched = ask(turns, "电机温升异常")
    assert switched["decision"] == "evidence"
    assert "TEST-MAN-001" in {row["number"] for row in switched["citations"]}
    assert source_id not in {row["sourceId"] for row in switched["citations"]}
    bare_code = ask(new_conversation("A01"), "Ｌ９２")
    assert bare_code["decision"] == "answer" and original in bare_code["answer"]
    reported_code = ask(new_conversation("A01"), "刚才报了 L92，接下来呢？")
    assert reported_code["decision"] == "answer" and original in reported_code["answer"]
    wrong_batch = ask(new_conversation("A02"), "L92 怎么排查？")
    assert wrong_batch["decision"] == "no_evidence" and not wrong_batch["citations"]
    only_definition = ask(new_conversation("A01"), "E07 怎么修复？")
    assert only_definition["decision"] == "evidence" and "不足以确认" in only_definition["answer"]
    assert [row["number"] for row in only_definition["citations"]] == ["TEST-MAN-001"]
    safety = ask(new_conversation("A01"), "L92 怎么带电拆卸处理？")
    assert safety["decision"] == "safety" and original not in safety["answer"]

    post(client, "/auth/demo", {"role": "library"})
    source_path = f"/sources/{source_id}"
    post(client, source_path + "/actions", dict(action="block", revision=get(client, source_path)["revision"]))
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
    ("csv", "workorder", "工单号,现象,原因,实际处置,现场结果\nQA-REC,验收编号模糊,记录未登记,对照登记编号,记录已复核",
     "验收编号模糊的历史工单结果是什么？", "记录已复核", "记录 QA-REC"),
    ("md", "manual", "## ０７１７\n故障含义：样本标签缺失。\n排查步骤：\n1. 核对标签记录。\n\n"
     "## ０７１８\n故障含义：目录版本不一致。\n处置步骤：\n1. 核对目录版本。",
     "0717怎么排查？", "核对标签记录", "文本正文"),
    ("md", "manual", "| 故障号 | 说明 | 处置办法 |\n| --- | --- | --- |\n| 1B20 | 登记项缺失 | 核对登记项 |\n"
     "| 1B21 | 样本号缺失 | 核对样本号 |", "1B20", "核对登记项", "文本正文"),
    ("docx", "manual", "| 故障码 | 原因 | 处理措施 |\n| 0xA9 | 样本编号不符 | 对照样本编号 |\n"
     "| 0xAA | 日期不符 | 核对归档日期 |", "0xA9是什么故障？", "对照样本编号", "文档正文"),
    ("csv", "manual", "故障号,含义,解决方案\n0xB6,登记项无效,核对登记表\n0xB7,编号异常,核对编号\n",
     "0xB6怎么处理？", "核对登记表", "第 2 行"),
    ("pdf", "manual", "Code (hex)     Meaning                    What to do\n"
     "9123                 Record index missing        Check the index record.\n"
     "9124                 Sample label mismatch       Compare the sample label.",
     "9123", "Check the index record.", "第 1 页"),
    ("pdf", "manual", "| Code (hex) | Description | What to do |\n"
     "| 1B88 | Record missing | Check the sample record.<br>Compare the archive index. |\n"
     "| 1B89 | Label mismatch | Check the label version. |",
     "1B88怎么处理？", "Compare the archive index.", "第 1 页"),
    ("md", "manual", "## **C73**\n故障含义：样本页缺失。\n排查步骤：\n1. 核对样本页。\n\n"
     "## **C74**\n故障含义：登记项缺失。\n排查步骤：\n1. 核对登记项。", "C73怎么处理？", "核对样本页", "文本正文"),
    ("txt", "manual", "故障码 A1.2\n故障含义：档号缺失。\n排查步骤：\n1. 核对档号。\n\n"
     "故障码 A1.3\n故障含义：批号缺失。\n排查步骤：\n1. 核对批号。", "A1.2是什么故障？", "核对档号", "文本正文"),
    ("csv", "manual", "故障号,说明,处理\n7819,日期记录有误,对照归档日期\n7820,清单记录有误,核对清单版本\n",
     "7819怎么处理？", "对照归档日期", "第 2 行"),
])
def test_independent_import_formats(services, monkeypatch, suffix, category, body, question, expected, position):
    import pymupdf
    from docx import Document
    from maintenance import conversations

    raw = body.encode("utf-8")
    if suffix == "docx":
        document = Document()
        if body.startswith("|"):
            rows = [line.strip().strip("|").split("|") for line in body.splitlines()]
            table = document.add_table(rows=len(rows), cols=len(rows[0]))
            for row, fields in zip(table.rows, rows):
                for cell, value in zip(row.cells, fields):
                    cell.text = value.strip()
        else:
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
            if body.startswith("|"):
                columns = (42, 127, 263, 550)
                for index, line in enumerate(body.splitlines()):
                    values = [cell.strip() for cell in line.strip().strip("|").split("|")]
                    for column, value in enumerate(values):
                        top = 45 + index * 54
                        page.draw_rect(pymupdf.Rect(columns[column], top, columns[column + 1], top + 54))
                        for offset, part in enumerate(value.split("<br>")):
                            page.insert_text((columns[column] + 5, top + 14 + 14 * offset), part, fontsize=9)
            else:
                for index, line in enumerate(body.splitlines()):
                    page.insert_text((55, 55 + 19 * index), line)
            raw = document.tobytes()

    client, worker = services
    monkeypatch.setattr(conversations.cfg, "allow_model_data", False)
    post(client, "/auth/demo", {"role": "platform"})
    model = f"FORMAT-{suffix.upper()}-{uuid4().hex[:8]}"
    device = post(client, "/devices", {"number": model, "name": "独立验收设备", "model": model})
    post(client, "/auth/demo", {"role": "library"})
    metadata = dict(number=model, version="1.0", title="独立验收资料", model=model, category=category)
    response = client.post("/api/v1/sources/import", data={"metadata": json.dumps([metadata])},
                           files={"files": (f"sample.{suffix}", raw, "application/octet-stream")})
    assert response.status_code == 202
    source_id = response.json()["items"][0]["sourceId"]
    do_job(worker, "parse")
    assert get(client, f"/sources/{source_id}")["active"]
    post(client, "/auth/demo", {"role": "tech"})
    session = post(client, "/sessions", {"deviceId": device["id"]}, 201)
    path = f"/sessions/{session['id']}/turns"

    def ask(text):
        turn = post(client, path, {"clientMessageId": uuid4().hex, "text": text}, 202)
        do_job(worker, "turn")
        return get(client, f"{path}/{turn['id']}")

    answer = ask(question)
    citations = answer.get("citations", [])
    assert answer["decision"] == "answer", answer["answer"]
    assert [row["sourceId"] for row in citations] == [source_id]
    assert citations[0]["position"] == position
    assert expected in answer["answer"] and expected in citations[0]["text"]
    if suffix == "md" and "F23" in body:
        assert "查验样本批号" not in answer["answer"] and "验收阶段仅核对记录。" in answer["answer"]
        ask("F23 是什么提示？")
        followup = ask("那怎么处理？")
        assert "查验样本批号" in followup["answer"] and "核对封存编号" not in followup["answer"]
    if suffix == "pdf" and "T74" in body:
        safety = ask("How do I fix T74 while energized?")
        assert safety["decision"] == "safety" and expected not in safety["answer"]


def test_source_trial_indexes_utf16_tsv_on_automatic_publication(services):
    client, worker = services
    post(client, "/auth/demo", {"role": "library"})
    info = dict(number="TEST-TSV-TRIAL", title="试问索引", version="1", model="IM-200", batch="A01", category="manual")
    content = 'Fault number\tFault text\tRemedies\r\nCNF\t反馈超时\t"核对反馈间隔\n记录面板状态\n出处：原厂续表"\r\n'
    upload_result = client.post("/api/v1/sources/import", data={"metadata": json.dumps([info])},
                                files={"files": ("legacy.tsv", content.encode("utf-16"), "text/tab-separated-values")})
    assert upload_result.status_code == 202, upload_result.text
    source = "/sources/" + upload_result.json()["items"][0]["sourceId"]
    devices = {row["batch"]: row for row in get(client, "/devices") if row["model"] == "IM-200"}
    query = dict(deviceId=devices["A01"]["id"], question="cnf 怎么处理")
    post(client, source + "/trial", query, 409)
    do_job(worker, "parse")
    detail = get(client, source)
    assert detail["indexedCodeExamples"] == ["CNF"]
    trial = post(client, source + "/trial", query)
    assert trial["decision"] == "answer" and "核对反馈间隔" in trial["answer"]
    assert trial["requestedCodes"] == ["CNF"] and trial["referenceIds"] == [trial["hits"][0]["id"]]
    assert trial["availableInAsk"]
    assert post(client, source + "/trial", {**query, "deviceId": devices["A02"]["id"]})["hits"] == []
    post(client, "/auth/demo", {"role": "tech"})
    post(client, source + "/trial", query, 403)
    post(client, "/auth/demo", {"role": "library"})
    saved = client.patch("/api/v1" + source + "/chunks/" + detail["chunks"][0]["id"],
                         json=dict(revision=detail["revision"], text="故障码：CNW\n故障含义：反馈超时\n处理措施：核对反馈间隔"))
    assert saved.status_code == 409, saved.text
    assert get(client, source)["indexedCodeExamples"] == ["CNF"]


def test_safety_procedure_preserves_fault_meaning_and_followup_topic(services, monkeypatch, caplog):
    import logging

    from maintenance import conversations

    client, worker = services
    monkeypatch.setattr(conversations.cfg, "rag_trace", True)
    monkeypatch.setattr(conversations.cfg, "allow_model_data", True)
    monkeypatch.setattr(conversations.cfg, "embedding_model", "")
    monkeypatch.setattr(conversations.cfg, "chat_model", "test-model")

    def unexpected_model_call(*args, **kwargs):
        pytest.fail("The exact/no-evidence/safety route should not require a model call")

    monkeypatch.setattr(conversations, "model_call", unexpected_model_call)
    caplog.set_level(logging.INFO, logger="maintenance.worker")
    post(client, "/auth/demo", {"role": "platform"})
    device = post(client, "/devices", {"number": "QA-SAFETY-CABINET", "name": "电气问答隔离验收设备",
                                      "model": "QA-SAFETY-CABINET", "batch": "A01"})
    post(client, "/auth/demo", {"role": "library"})
    info = dict(number="TEST-SAFETY-114514", title="验收设备故障码定义", version="1", model=device["model"],
                batch=device["batch"], category="manual")
    body = ("故障码：114514\n故障：温度异常\n修复方法：先断电，然后测试阻抗\n\n"
            "故障码：114515\n故障：标签记录不符\n处理措施：核对标签记录")
    source = upload(client, info, body)["items"][0]["sourceId"]
    do_job(worker, "parse")
    post(client, "/auth/demo", {"role": "tech"})
    session = post(client, "/sessions", {"deviceId": device["id"]}, 201)
    path = f"/sessions/{session['id']}/turns"
    def ask(question, *, has_source=True):
        turn = post(client, path, {"text": question, "clientMessageId": uuid4().hex}, 202)
        do_job(worker, "turn")
        result = get(client, f"{path}/{turn['id']}")
        assert [row["sourceId"] for row in result["citations"]] == ([source] if has_source else [])
        return result

    definition = ask("114514是什么情况")
    assert definition["decision"] == "answer" and "温度异常 [1]" in definition["answer"]
    assert "安全关键" not in definition["answer"] and "测试阻抗" not in definition["answer"]
    repair = ask("告诉我怎么修复")
    assert repair["decision"] == "safety"
    assert "温度异常 [1]" in repair["answer"] and "具体测点" in repair["answer"]
    assert "测试阻抗" not in repair["answer"] and "标签记录" not in repair["answer"]
    correction = ask("不对，是114515，怎么处理？")
    assert correction["decision"] == "answer" and "核对标签记录" in correction["answer"]
    missing = ask("114513是什么情况", has_source=False)
    assert missing["decision"] == "no_evidence" and "114513" in missing["answer"] and "114514" not in missing["answer"]
    short = ask("114514呢")
    assert short["decision"] == "answer" and "温度异常 [1]" in short["answer"]
    for question in ("温度异常这么解决", "温度异常怎么解决"):
        symptom = ask(question)
        assert symptom["decision"] == "safety" and "故障码 114514" in symptom["answer"]
        assert "核对面板" in symptom["answer"] and "测试阻抗" not in symptom["answer"]
    events = [json.loads(row.getMessage().removeprefix("rag_trace ")) for row in caplog.records
              if row.getMessage().startswith("rag_trace ")]
    assert [row["previousTurns"] for row in events] == [0, 1, 0, 0, 0, 0, 0]
    assert events[1]["contextStart"] == 1
    assert events[3]["rankedChunks"] == []
    assert all(events[i]["rankedChunks"][0]["exactCodeMatches"] == 1 for i in (0, 1, 2, 4))
    assert "告诉我怎么修复" not in caplog.text and "测试阻抗" not in caplog.text


def test_short_named_fault_uses_authorized_manual_heading_and_carries_context(services, monkeypatch):
    from maintenance import conversations

    client, worker = services
    monkeypatch.setattr(conversations.cfg, "allow_model_data", False)
    post(client, "/auth/demo", {"role": "platform"})
    devices = {}
    for batch in ("A01", "A02"):
        devices[batch] = post(client, "/devices", dict(number=f"QA-NAMED-{batch}", name=f"短故障名验收 {batch}",
                                                      model="QA-NAMED-DRIVE", batch=batch))["id"]
    post(client, "/auth/demo", {"role": "library"})
    info = dict(title="输入缺相验收手册", version="1", model="QA-NAMED-DRIVE", category="manual")
    references = {}
    for batch, code, step in (("A01", "3130", "核对进线验收记录。"), ("A02", "9130", "核对 A02 的交接表。")):
        text = (f"# 故障码 {code} 输入缺相 · 第 537 页\n故障原因：输入电源缺相。\n排查步骤：\n1. {step}\n\n"
                "故障码 3220 直流回路欠压\n故障原因：可能与供电缺相有关。\n排查步骤：\n1. 核对欠压登记。")
        references[batch] = upload(client, dict(info, batch=batch, number=f"QA-NAMED-MANUAL-{batch}"), text)["items"][0]["sourceId"]
        do_job(worker, "parse")
    post(client, "/auth/demo", {"role": "tech"})

    def ask(path, question):
        turn = post(client, path, dict(text=question, clientMessageId=uuid4().hex), 202)
        do_job(worker, "turn")
        return get(client, f"{path}/{turn['id']}")

    for batch, code in (("A01", "3130"), ("A02", "9130")):
        session = post(client, "/sessions", {"deviceId": devices[batch]}, 201)
        path = f"/sessions/{session['id']}/turns"
        ask(path, "3220")
        for text in ("那输入缺相呢？", "那怎么处理？", "输入电源缺相怎么处理？"):
            result = ask(path, text)
            assert result["decision"] == "answer" and f"故障码 {code}" in result["answer"]
            assert "3220" not in result["answer"] and "欠压登记" not in result["answer"]
            assert [row["sourceId"] for row in result["citations"]] == [references[batch]]
        missing = ask(path, "输出缺相怎么处理？")
        assert missing["decision"] in {"clarify", "no_evidence"} and missing["citations"] == []


def test_bare_number_can_find_legacy_case_observation_in_scope(services):
    client, worker = services
    post(client, "/auth/demo", {"role": "library"})
    devices = {row["batch"]: row for row in get(client, "/devices") if row["number"] in {"TEST-DEVICE-1", "TEST-DEVICE-2"}}
    info = dict(title="历史现象验收", version="1", model="IM-200", category="case")
    matching = None
    for label, batch, body in (
        ("MATCH", "A01", "现象：５３４６４５６４\r\n确认原因：其他历史记录。"),
        ("OTHER", "A02", "现象：53464564\n结果：另一批次记录。"),
        ("SUBSTRING", "A01", "现象：153464564\n结果：53464564"),
    ):
        row = upload(client, dict(info, number="TEST-NUM-CASE-" + label, batch=batch), body)["items"][0]
        do_job(worker, "parse")
        if label == "MATCH":
            matching = row["sourceId"]
    post(client, "/auth/demo", {"role": "tech"})
    path = "/sessions/" + post(client, "/sessions", {"deviceId": devices["A01"]["id"]}, 201)["id"] + "/turns"
    for question in ("53464564", "53464564是什么故障", "53464564 处理方法", "53464564 有历史案例吗？"):
        turn = post(client, path, {"text": question, "clientMessageId": uuid4().hex}, 202)
        do_job(worker, "turn")
        answer = get(client, f"{path}/{turn['id']}")
        assert answer["decision"] == "evidence" and "「现象」栏为 53464564" in answer["answer"]
        assert [row["sourceId"] for row in answer["citations"]] == [matching]
    post(client, "/auth/demo", {"role": "library"})
    manual = upload(client, dict(info, number="TEST-NUM-MANUAL", batch="A01", category="manual"),
                    "故障码：53464564\n故障含义：独立记录未登记。\n处理措施：核对独立记录。")["items"][0]["sourceId"]
    do_job(worker, "parse")
    post(client, "/auth/demo", {"role": "tech"})
    for question, decision, source, expected in (
        ("53464564", "answer", manual, "独立记录未登记"),
        ("53464564 有历史案例吗？", "evidence", matching, "「现象」栏为 53464564"),
        ("53464564 历史案例怎么处理？", "evidence", matching, "历史现象"),
    ):
        turn = post(client, path, {"text": question, "clientMessageId": uuid4().hex}, 202)
        do_job(worker, "turn")
        answer = get(client, f"{path}/{turn['id']}")
        assert answer["decision"] == decision and expected in answer["answer"]
        assert [row["sourceId"] for row in answer["citations"]] == [source]


def test_automatic_import_preserves_scheduled_change_and_rejects_failed_file(services):
    from datetime import datetime, timedelta, timezone

    client, worker = services
    post(client, "/auth/demo", {"role": "library"})
    info = dict(number="TEST-AUTO-SCOPE", title="自动生效验收资料", version="1", model="IM-200", batch="A01", category="manual")
    old = upload(client, info, "验收文字版本一。")["items"][0]["sourceId"]
    do_job(worker, "parse")
    path = f"/sources/{old}"
    scheduled = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    post(client, path + "/actions", dict(action="publish", revision=get(client, path)["revision"], start=scheduled))
    new = upload(client, {**info, "version": "2"}, "验收文字版本二。")["items"][0]["sourceId"]
    do_job(worker, "parse")
    details = get(client, f"/sources/{new}")
    assert details["active"] and not get(client, path)["active"]
    assert any(scope["sourceId"] == new and scope["end"] == scheduled for scope in details["scopes"])
    assert any(scope["sourceId"] == old and scope["start"] == scheduled for scope in details["scopes"])
    rejected = upload(client, {**info, "version": "3"}, "  \n ")["items"][0]["sourceId"]
    do_job(worker, "parse")
    failed = get(client, f"/sources/{rejected}")
    assert failed["status"] == "failed" and not failed["active"]
    assert get(client, f"/sources/{new}")["active"]


def test_case_retains_manually_entered_fault_code_and_answers_in_local_mode(services, monkeypatch):
    from maintenance import conversations

    client, worker = services
    monkeypatch.setattr(conversations.cfg, "allow_model_data", False)
    post(client, "/auth/demo", {"role": "platform"})
    device = post(client, "/devices", {"number": "CASE-FIELD-QA", "model": "CASE-FIELD-QA", "name": "案例字段验收"})
    post(client, "/auth/demo", {"role": "tech"})
    session = post(client, "/sessions", {"deviceId": device["id"]}, 201)
    turn_path = f"/sessions/{session['id']}/turns"
    post(client, turn_path, {"text": "记录异常，需要核对现场。", "clientMessageId": uuid4().hex}, 202)
    do_job(worker, "turn")
    draft = post(client, "/drafts", {"sessionId": session["id"]}, 201)
    draft_path = f"/drafts/{draft['id']}"
    fields = {"faultCode": "Q8A1", "cause": "登记编号不符", "actions": "对照验收台账", "result": "已确认记录一致"}
    changed = client.patch("/api/v1" + draft_path, json={"revision": draft["revision"], "number": "QA-CASE-FIELD", "fields": fields})
    assert changed.status_code == 200
    draft = post(client, draft_path + "/confirm", {"revision": changed.json()["revision"], "number": "QA-CASE-FIELD"})
    draft = post(client, draft_path + "/complete", {"revision": draft["revision"]})
    assert draft["completedAt"] and draft["fields"]["faultCode"] == "Q8A1"
    post(client, "/auth/demo", {"role": "library"})
    case_body = {"text": "现象：记录异常。\n确认原因：登记编号不符。\n实际处置：对照验收台账。\n结果：已确认记录一致。"}
    case = post(client, draft_path + "/case", case_body)
    assert post(client, draft_path + "/case", case_body)["id"] == case["id"]
    do_job(worker, "parse")
    case_path = f"/sources/{case['id']}"
    case = get(client, case_path)
    assert case["status"] == "review"
    assert "故障码：Q8A1" in case["chunks"][0]["text"]
    post(client, "/auth/demo", {"role": "expert"})
    case = post(client, case_path + "/actions", {"revision": case["revision"], "action": "approve"})
    assert case["status"] == "published"
    post(client, "/auth/demo", {"role": "library"})
    unrelated = upload(client, dict(number="CASE-Q8A2", title="另一条验收案例", version="1", model=device["model"], category="case"),
                       "故障码：Q8A2\n结果：另一条独立记录。")
    do_job(worker, "parse")
    unrelated_path = f"/sources/{unrelated['items'][0]['sourceId']}"
    assert get(client, unrelated_path)["active"]
    post(client, "/auth/demo", {"role": "tech"})
    hits = client.get("/api/v1/knowledge/search", params={"deviceId": device["id"], "q": "Q8A1"}).json()
    assert [item["sourceId"] for item in hits] == [case["id"]]
    history = post(client, "/sessions", {"deviceId": device["id"]}, 201)
    path = f"/sessions/{history['id']}/turns"
    for text, expected in (("Q8A1", "对照验收台账"), ("Q8A1 有哪些已结单案例？", "对照验收台账"),
                           ("Q8A1 的历史案例处理结果是什么？", "已确认记录一致"),
                           ("Q8A1的历史案例怎么处理？", "对照验收台账")):
        turn = post(client, path, {"text": text, "clientMessageId": uuid4().hex}, 202)
        do_job(worker, "turn")
        answer = get(client, f"{path}/{turn['id']}")
        assert answer["decision"] == "answer" and expected in answer["answer"]
        assert "历史记录供复盘参考" in answer["answer"] and [item["sourceId"] for item in answer["citations"]] == [case["id"]]
        assert "另一条独立记录" not in answer["answer"]


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

