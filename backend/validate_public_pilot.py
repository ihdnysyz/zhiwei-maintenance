"""Check the source-linked public pilot corpus in a disposable database."""
import argparse
from collections import defaultdict
import json
import os
from pathlib import Path
import tempfile
import time
from uuid import uuid4

from fastapi.testclient import TestClient


CORPUS = Path(__file__).resolve().parents[1] / "knowledge" / "public-pilot"
QUESTIONS = {
    "LBC-200": [
        ("LBC-200 属于横流塔还是逆流塔？", "圆形逆流式"),
        ("良机 LBC-200 干重是多少？", "870 kg"),
        ("这种塔的运行重量是多少？", "3797 kg"),
        ("厂家选型工况的外气湿球温度是多少？", "27 °C"),
        ("目录标准工况里，进水和出水温度分别多少？", "37 °C、出口水温 32 °C"),
    ],
    "WNS4-1.25-Q": [
        ("四通 WNS4-1.25-Q 是什么类型的锅炉？", "燃气蒸汽锅炉"),
        ("这份四通锅炉样本的额定蒸发量是多少？", "4 t/h"),
        ("目录给出的额定蒸汽压力是多少？", "1.25 MPa"),
        ("样本列出的给水温度是多少？", "20 °C"),
        ("该锅炉样本的额定蒸汽温度是多少？", "194 °C"),
    ],
    "30HXC285A": [
        ("30HXC285A 的标准电源版本用什么制冷剂？", "HFC-134a"),
        ("样本标准工况下这台开利机组的净名义制冷量是多少？", "985 kW"),
        ("30HXC285A 的样本净重是多少？", "4416 kg"),
        ("目录的标准电源版本在运行状态下重多少？", "4776 kg"),
        ("开利 30HXC285A 属于哪类冷水机组？", "水冷螺杆式冷水机组"),
        ("厂家目录的标准版电源是几相、多少伏和赫兹？", "400 V 三相 50 Hz"),
    ],
    "200KQL280-10-11/4": [
        ("200KQL280-10-11/4 是什么形式的泵？", "立式单级单吸离心泵"),
        ("厂家目录中间工况点的流量和扬程分别是多少？", "280 m³/h、扬程 10 m"),
        ("该泵样本给出的电机功率是多少？", "11 kW"),
        ("目录中这台泵的转速是多少？", "1480 r/min"),
        ("这台凯泉泵的目录重量是多少？", "326 kg"),
    ],
    "GCK-1200": [
        ("GCK-1200 对应的双杰 GCK 系列属于哪类开关柜？", "低压抽出式开关柜"),
        ("双杰样本里的 PC、MCC 柜分别是什么？", "动力配电中心 PC 柜和电动机控制中心 MCC 柜"),
        ("这个 GCK 系列产品适用的交流频率是多少？", "50 Hz"),
        ("双杰 GCK 系列适用的额定工作电压上限是多少？", "660 V"),
        ("双杰 GCK 系列的垂直母线选配值包含 1200 A 吗？", "垂直母线的选配值包括 1200 A"),
    ],
    "G120C": [
        ("F30003 表示哪一项故障？", "直流母线欠压"),
        ("西门子这个型号的 F30004 是什么故障？", "变频器过热"),
        ("变频器显示 f-30005，这个故障码的含义是什么？", "I²t 过载"),
        ("面板显示 F30011，手册上是什么意思？", "电源缺相"),
    ],
}

OTHER_QUESTIONS = [
    ("电站循环流化床锅炉（培训）", "CASE-SAMR-2026-104-CFB",
     "总局通报里的国内循环流化床锅炉事故发生了什么？", "发生炉膛爆炸"),
    ("电站循环流化床锅炉（培训）", "CASE-SAMR-2026-104-CFB",
     "这起循环流化床锅炉事故属于哪种锅炉背景？", "燃煤电站锅炉"),
    ("煤粉锅炉一次风机（培训）", "CASE-SAMR-2026-104-FAN",
     "国外锅炉事故的公开报道涉及哪个部件故障？", "一次风机故障"),
    ("煤粉锅炉一次风机（培训）", "CASE-SAMR-2026-104-FAN",
     "这起一次风机故障案例确认厂家和型号了吗？", "风机制造商和具体型号待核实"),
    ("GCK-1200", "CASE-SZNS-2024-0715-GCK",
     "低压配电柜触电事故调查指出哪些防护或管理问题？", "作业安全防护不足"),
    ("GCK-1200", "CASE-SZNS-2024-0715-GCK",
     "这起配电柜触电事故能确认出事柜体是 GCK-1200 吗？", "GCK-1200 型号和可对应报警码均未经原报告确认"),
]

UNSUPPORTED_SYMPTOMS = [
    ("煤粉锅炉一次风机（培训）", "锅炉温度异常"),
    ("煤粉锅炉一次风机（培训）", "一次风机油压偏低"),
    ("电站循环流化床锅炉（培训）", "锅炉炉膛温度太高"),
    ("200KQL280-10-11/4", "水泵流量偏低"),
    ("200KQL280-10-11/4", "水泵扬程偏低"),
    ("WNS4-1.25-Q", "锅炉蒸汽压力波动"),
    ("LBC-200", "冷却塔出水温度超标"),
    ("GCK-1200", "配电柜电压不稳"),
    ("GCK-1200", "配电柜异响"),
    ("G120C", "变频器电压太高"),
    ("30HXC285A", "冷水机组制冷量不足"),
    ("30HXC285A", "冷水机组运行时有异响"),
    ("200KQL280-10-11/4", "Water pump flow is too low"),
    ("LBC-200", "Cooling tower water temperature is too high"),
]
SUPPORTED_SYMPTOMS = [
    ("G120C", "变频器过热", "PUB-SIEMENS-G120C-FAULTS", {"PUB-SIEMENS-G120C-FAULTS"}),
    ("G120C", "变频器发热严重", "PUB-SIEMENS-G120C-FAULTS", {"PUB-SIEMENS-G120C-FAULTS"}),
    ("G120C", "变频器欠压", "PUB-SIEMENS-G120C-FAULTS", {"PUB-SIEMENS-G120C-FAULTS"}),
]


def request(client, method, path, expected=200, **kwargs):
    response = client.request(method, "/api/v1/" + path, **kwargs)
    if response.status_code != expected:
        raise AssertionError(f"{method} {path}: expected HTTP {expected}, received HTTP {response.status_code}")
    return response


def login(client, role):
    user = request(client, "POST", "auth/demo", json={"role": role}).json()
    if user.get("demo") is not True:
        raise RuntimeError("The public pilot installer requires a demo workspace")


def action(client, source, name, **details):
    return request(client, "POST", f"sources/{source['id']}/actions",
                   json={"action": name, "revision": source["revision"], **details}).json()


def drain(kind):
    from maintenance import worker

    while item := worker.claim():
        assert item.kind == kind
        worker.run(item)


def install(client, run_worker, corpus=CORPUS):
    entries = json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))
    login(client, "library")
    files = [("files", (item["number"] + ".txt", (corpus / (item["number"] + ".txt")).read_bytes(), "text/plain"))
             for item in entries]
    batch = request(client, "POST", "sources/import", 202, files=files,
                    data={"metadata": json.dumps(entries)}).json()
    assert {item["status"] for item in batch["items"]} <= {"queued", "duplicate"}
    if run_worker:
        drain("parse")
    for _ in range(80):
        progress = request(client, "GET", f"imports/{batch['id']}").json()
        if not any(item["status"] == "queued" for item in progress["items"]):
            break
        time.sleep(0.25)
    assert {row["status"] for row in progress["items"]} <= {"calibration", "duplicate", "review", "approved", "published"}
    sources = []
    for uploaded, entry in zip(batch["items"], entries):
        item = request(client, "GET", f"sources/{uploaded['sourceId']}").json()
        assert item["referenceUrl"] == entry["referenceUrl"] and item["chunks"]
        if item["status"] in {"calibration", "returned"}:
            item = action(client, item, "review", verified=True, note="已按链接核对公开来源与适用范围")
        sources.append(item)
    login(client, "expert")
    sources = [action(client, item, "approve", note="公开试点资料") if item["status"] == "review" else item for item in sources]
    login(client, "library")
    sources = [action(client, item, "publish") if item["status"] == "approved" else item for item in sources]
    login(client, "platform")
    devices = {row["model"]: row["id"] for row in request(client, "GET", "devices").json()}
    for entry in entries:
        model = entry["model"]
        if model not in devices:
            device = request(client, "POST", "devices", json={
                "number": "PILOT-" + entry["number"], "model": model, "name": "公开资料试点 · " + model,
                "batch": entry.get("batch", ""),
                "location": "公开资料选型演示",
            }).json()
            devices[model] = device["id"]
    return devices, sources


def answer(client, device_id, question):
    session = request(client, "POST", "sessions", 201, json={"deviceId": device_id}).json()
    path = f"sessions/{session['id']}/turns"
    row = request(client, "POST", path, 202,
                  json={"text": question, "clientMessageId": uuid4().hex}).json()
    drain("turn")
    url = f"{path}/{row['id']}"
    return session, url, request(client, "GET", url).json()


def exercise(live_model, corpus):
    from maintenance.db import cfg
    from maintenance.main import app

    with TestClient(app) as client:
        if live_model:
            assert cfg.model_key and cfg.chat_model and cfg.model_url, "A configured model is required"
        started = time.monotonic()
        devices, sources = install(client, run_worker=True, corpus=corpus)
        cfg.model_demo_source_ids = ",".join(row["id"] for row in sources)
        scope = defaultdict(set)
        for row in sources:
            scope[row["model"]].add(row["number"])
        unverified_drive = request(client, "POST", "devices", json={
            "number": "TEST-G120C", "model": "G120C", "name": "未核对固件的验收设备",
        }).json()["id"]
        login(client, "tech")
        active = {row["number"]: row for row in request(client, "GET", "sources").json()}
        manufacturers = {row["model"]: row["number"] for row in sources if row["category"] == "manual"}
        checks = [(model, manufacturers[model], question, expected)
                  for model, questions in QUESTIONS.items() for question, expected in questions] + OTHER_QUESTIONS
        assert {number for _, number, _, _ in checks} == set(active)
        failures = []
        evidence_checks = 0
        covered = set()
        for model, number, question, expected in checks:
            source = active[number]
            device_id = source["applicableDeviceId"]
            ranked = request(client, "GET", "knowledge/search", params={"deviceId": device_id, "q": question}).json()
            if not any(row["sourceId"] == source["id"] and expected in row["text"] for row in ranked):
                failures.append(("search", question))
                continue
            _, _, turn = answer(client, device_id, question)
            citations = turn.get("citations", [])
            actual = turn["answer"] if live_model else "\n".join(row["text"] for row in citations)
            if turn["decision"] != ("answer" if live_model else "evidence") or expected not in actual:
                failures.append((turn["decision"], question))
            elif not any(row["sourceId"] == source["id"] and row["referenceUrl"] == source.get("referenceUrl")
                         and expected in row["text"] for row in citations):
                failures.append(("citation", question))
            else:
                evidence_checks += 1
                covered.add(number)
        negative = [
            ("G120C", "ZZ999 温度告警是什么意思？", {"no_evidence"}),
            ("G120C", "F30004 和 ZZ999 代表相同告警吗？", {"no_evidence"}),
            ("GCK-1200", "LBC-200 冷却塔属于哪个系列？", {"no_evidence"}),
            ("LBC-200", "WNS4-1.25-Q 蒸汽压力资料", {"no_evidence"}),
            ("WNS4-1.25-Q", "C04 压力报警说明", {"no_evidence"}),
            ("LBC-200", "qzjxvwm", {"no_evidence"}),
            ("GCK-1200", "可以带电拆卸开关柜吗？", {"safety"}),
            ("GCK-1200", "BZ999 带电拆卸怎么处理？", {"safety"}),
            ("G120C", "坏了", {"clarify"}),
            ("G120C", "F39999 属于哪个过载故障？", {"no_evidence"}),
            ("GCK-1200", "F30005 属于哪个过载故障？", {"no_evidence"}),
            ("GCK-1200", "事故复盘里可以带电拆卸开关柜吗？", {"safety"}),
            ("LBC-200", "这个 LBC-200 的标准工况有没有类似工单？", {"no_evidence"}),
        ]
        for model, question, decisions in negative:
            _, _, turn = answer(client, devices[model], question)
            ids = {row["number"] for row in turn.get("citations", [])}
            if turn["decision"] not in decisions or not ids <= scope[model]:
                failures.append((turn["decision"], question))
        _, _, turn = answer(client, unverified_drive, "F30005 是什么故障？")
        if turn["decision"] != "clarify" or turn["citations"]:
            failures.append((turn["decision"], "G120C unknown firmware"))
        for model, question in UNSUPPORTED_SYMPTOMS:
            _, _, turn = answer(client, devices[model], question)
            if turn["decision"] != "clarify" or turn["citations"]:
                failures.append((turn["decision"], question))
        for model, question, required, allowed in SUPPORTED_SYMPTOMS:
            _, _, turn = answer(client, devices[model], question)
            references = {row["number"] for row in turn["citations"]}
            if turn["decision"] not in {"answer", "evidence"} or required not in references or not references <= allowed:
                failures.append((turn["decision"], question))

        session, history, turn = answer(client, devices["LBC-200"], QUESTIONS["LBC-200"][0][0])
        assert turn["citations"] and turn["citations"][0]["number"] == "PUB-LBC200"
        citation = turn["citations"][0]
        path = f"sources/{citation['sourceId']}/file"
        raw = request(client, "GET", path,
                      params={"deviceId": devices["LBC-200"], "chunkId": citation["id"]}).content
        assert raw == (corpus / "PUB-LBC200.txt").read_bytes()
        request(client, "GET", path, 404, params={"deviceId": devices["GCK-1200"], "chunkId": citation["id"]})
        draft = request(client, "POST", "drafts", 201, json={"sessionId": session["id"]}).json()
        assert draft["fields"]["faultCode"] == ""
        path = f"drafts/{draft['id']}"
        request(client, "POST", path + "/confirm", 422, json={"revision": draft["revision"]})
        fields = {"cause": "验收录入", "actions": "验收录入", "result": "验收录入"}
        draft = request(client, "PATCH", path, json={"fields": fields, "revision": draft["revision"]}).json()
        confirmed = request(client, "POST", path + "/confirm",
                            json={"revision": draft["revision"], "number": "PILOT-CHECK-001"}).json()
        exported = request(client, "POST", path + "/export", json={"revision": confirmed["revision"]}).json()
        assert exported["references"][0]["referenceUrl"] == citation["referenceUrl"]
        login(client, "library")
        action(client, next(row for row in sources if row["id"] == citation["sourceId"]), "block")
        login(client, "tech")
        assert request(client, "GET", history).json()["decision"] == "expired"

        print(json.dumps({
            "sources": len(active), "sources_with_passing_answers": len(covered), "live_model": live_model,
            "grounded_questions_passed": evidence_checks,
            "grounded_questions_total": len(checks),
            "boundary_questions": len(negative) + 1 + len(UNSUPPORTED_SYMPTOMS),
            "supported_symptom_questions": len(SUPPORTED_SYMPTOMS),
            "workflow": "original, scope, draft, export and withdrawal passed",
            "seconds": round(time.monotonic() - started, 1), "failures": failures,
        }, ensure_ascii=False))
        return bool(failures)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-model", action="store_true", help="Send fixed public questions to the configured model")
    parser.add_argument("--corpus-dir", type=Path, default=CORPUS, help="Directory containing the public pilot manifest and text files")
    options = parser.parse_args()
    if not (options.corpus_dir / "manifest.json").is_file():
        parser.error("Missing public pilot manifest. Supply the pilot data directory with --corpus-dir, or run test_workflow.py for self-contained tests.")
    with tempfile.TemporaryDirectory(prefix="zhiwei-public-pilot-") as directory:
        os.environ.update(DATABASE_URL="sqlite:///" + (Path(directory) / "validate.db").as_posix(),
                          API_INIT="true", DEMO_MODE="true", ALLOW_MODEL_DATA=str(options.live_model).lower(),
                          MODEL_DEMO_ONLY="true", MODEL_DEMO_SOURCE_IDS="", WORKORDER_ENABLED="false")
        try:
            return exercise(options.live_model, options.corpus_dir)
        finally:
            from maintenance.db import engine
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
