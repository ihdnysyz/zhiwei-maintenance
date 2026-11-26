import json
import logging
import httpx
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .common import actor, device_for, fail, get_db, need, own, permitted_device, role
from .db import Record, add, audit, cfg, enqueue, find, records
from .knowledge import (SAFETY, applicability, authorized_citation, chunk_fault_codes, code_key, codes_in, fault_codes,
                        fault_sections, model_call, model_evidence, named_fault_matches, non_fault_numbers, normalize_code_text,
                        normalize_fault_tables, retrieve, suggested_device_refs, supports_symptoms,
                        standalone_fault_code, symptom_details, symptom_signals, terms)

router = APIRouter()


class DeviceSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deviceId: str


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    clientMessageId: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    text: str = Field(min_length=1, max_length=3000)


class Feedback(BaseModel):
    note: str = Field(default="", max_length=1500)


def session_turns(db, org, session):
    return list(db.scalars(records(org, "turn").where(
        Record.owner == session.id).order_by(Record.created, Record.key)))


def active_session(db, user, session_id, *, lock=False):
    session = own(db, user, "session", session_id, lock=lock)
    if session.status == "deleted":
        fail(404, "SESSION_DELETED", "此对话已删除")
    return session


@router.get("/sessions")
def sessions(user=Depends(actor), db=Depends(get_db, scope="function")):
    return [row.view() for row in db.scalars(records(user.org, "session").where(
        Record.owner == user.key, Record.status != "deleted").order_by(Record.updated.desc()).limit(100))]


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session(session_id: str, request: Request, user=Depends(actor), db=Depends(get_db, scope="function")):
    session = own(db, user, "session", session_id, lock=True)
    if session.status != "deleted":
        draft = find(db, user.org, "draft", key=session.id)
        retained = {draft.data.get("turnId")} if draft else set()
        retained.update(row.data.get("turnId") for row in db.scalars(records(user.org, "expert").where(
            Record.data["sessionId"].as_string() == session.id)))
        for turn in session_turns(db, user.org, session):
            if turn.id not in retained:
                db.delete(turn)
            elif turn.status == "queued":
                turn.change("done", decision="expired", referenceIds=[], answer="来源对话已删除，未完成的回答已取消。")
        session.change("deleted", title="", sequence=0)
        audit(db, user.org, user, "删除诊断对话", session.id, request.state.request_id)
    return Response(status_code=204)


@router.post("/sessions", status_code=201)
def new_session(body: DeviceSelection, user=Depends(actor), db=Depends(get_db, scope="function")):
    device = device_for(db, user, body.deviceId)
    return add(db, user.org, "session", owner=user.key, title="新建诊断", deviceId=device["id"],
               deviceName=device["name"], deviceNumber=device["number"], sequence=0).view()


def turn_view(db, user, turn, session):
    result = turn.view("referenceIds", "claims", "suggestedDeviceRefs", "diagnosticFollowUp", "contextStart")
    if turn.status != "done":
        return result
    try:
        device = device_for(db, user, session.data["deviceId"])
    except Exception as exc:
        from .common import Problem
        if not isinstance(exc, Problem):
            raise
        return {**result, "answer": "设备访问范围已变更，关联证据已隐藏。", "decision": "expired", "citations": []}
    citations = [authorized_citation(db, user.org, chunk, device) for chunk in turn.data.get("referenceIds", [])]
    if None in citations:
        return {**result, "answer": "资料版本或访问范围已变更。请重新提问以获取当前依据。",
                "decision": "expired", "citations": [cue for cue in citations if cue]}
    suggestions = []
    for hint in turn.data.get("suggestedDeviceRefs", []):
        target = find(db, user.org, "device", id=hint["deviceId"])
        if not target or not permitted_device(user, target.data):
            continue
        citation = authorized_citation(db, user.org, hint["chunkId"], target.data)
        if citation:
            suggestions.append({
                "id": target.id, "name": target.data["name"], "number": target.data["number"],
                "model": target.data["model"], "batch": target.data["batch"],
                "title": citation["title"], "category": citation["category"],
            })
    return {**result, "citations": citations, "deviceSuggestions": suggestions}


@router.get("/sessions/{session_id}")
def get_session(session_id: str, user=Depends(actor), db=Depends(get_db, scope="function")):
    session = active_session(db, user, session_id)
    return {**session.view(), "turns": [turn_view(db, user, row, session) for row in session_turns(db, user.org, session)]}


@router.post("/sessions/{session_id}/turns", status_code=202)
def send(session_id: str, body: Message, request: Request, user=Depends(actor), db=Depends(get_db, scope="function")):
    session = active_session(db, user, session_id, lock=True)
    device_for(db, user, session.data["deviceId"])
    for previous in session_turns(db, user.org, session):
        if previous.data["clientMessageId"] == body.clientMessageId:
            if previous.data["text"] != body.text:
                fail(409, "IDEMPOTENCY_CONFLICT", "消息编号已经用于其他内容")
            return turn_view(db, user, previous, session)
        if previous.status == "queued":
            fail(409, "TURN_PENDING", "请等待当前问题完成")
    position = session.data["sequence"] + 1
    session.change(title=body.text[:40] if position == 1 else session.data["title"], sequence=position)
    turn = add(db, user.org, "turn", key=f"{session.id}:{position:08}", owner=session.id, status="queued",
               text=body.text, clientMessageId=body.clientMessageId, sequence=position)
    enqueue(db, user.org, "turn", turn.id)
    audit(db, user.org, user, "提交问答", turn.id, request.state.request_id)
    return turn.view()


@router.get("/sessions/{session_id}/turns/{turn_id}")
def get_turn(session_id: str, turn_id: str, user=Depends(actor), db=Depends(get_db, scope="function")):
    session = active_session(db, user, session_id)
    turn = need(find(db, user.org, "turn", id=turn_id))
    if turn.owner != session.id:
        fail(404, "NOT_FOUND", "轮次不存在")
    return turn_view(db, user, turn, session)


def expert_task(db, org, session, turn, device, note="", origin="escalate", operator="worker", request_id=""):
    key = f"{origin}:{turn.id}"
    task = find(db, org, "expert", key=key)
    if task:
        return task
    experts = list(db.scalars(records(org, "user").where(Record.status == "active").order_by(Record.key)))
    eligible = [row for row in experts if row.data["role"] == "expert"
                and (not row.data.get("models") or device["model"] in row.data["models"])]
    assignee = eligible[0].key if len(eligible) == 1 else ""
    deadline = datetime.now(ZoneInfo(cfg.org_timezone))
    holidays = set(cfg.org_holidays.split(","))
    for _ in range(max(1, cfg.expert_sla_days)):
        deadline += timedelta(days=1)
        while deadline.weekday() >= 5 or deadline.date().isoformat() in holidays:
            deadline += timedelta(days=1)
    task = add(db, org, "expert", key=key, owner=session.owner, status="assigned" if assignee else "open",
               title=turn.data["text"][:100], note=note, origin=origin, sessionId=session.id,
               turnId=turn.id, deviceId=device["id"], deviceName=device["name"], model=device["model"],
               assignee=assignee, reply="", dueAt=deadline.astimezone(timezone.utc).isoformat())
    action = "自动转交专家" if operator == "worker" else ("提交答复反馈" if origin == "feedback" else "转交专家")
    audit(db, org, operator, action, task.id, request_id)
    return task


@router.post("/sessions/{session_id}/turns/{turn_id}/escalate", status_code=201)
@router.post("/sessions/{session_id}/turns/{turn_id}/feedback", status_code=201)
def escalate(session_id: str, turn_id: str, body: Feedback, request: Request, user=Depends(actor), db=Depends(get_db, scope="function")):
    session = active_session(db, user, session_id, lock=True)
    turn = need(find(db, user.org, "turn", id=turn_id))
    if turn.owner != session.id:
        fail(404, "NOT_FOUND", "轮次不存在")
    device = device_for(db, user, session.data["deviceId"])
    origin = "feedback" if request.url.path.endswith("/feedback") else "escalate"
    if origin == "feedback" and not body.note.strip():
        fail(422, "FEEDBACK_REQUIRED", "请描述需核实的答复或引用问题")
    row = expert_task(db, user.org, session, turn, device, body.note.strip(), origin, user, request.state.request_id)
    return row.view()


def redact(value):
    value = re.sub(r"1[3-9]\d{9}|\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[已脱敏]", value)
    return re.sub(r"(?<!\d)\d{17}[\dXx](?!\w)", "[已脱敏]", value)


_SENTENCE_BREAK = re.compile(r"(?<=[。！？!?])|(?<=\.)\s+(?=[A-Z])|\n+")
_WORKORDER_QUESTION = re.compile(r"历史工单|历史记录|过往工单|类似工单|演示工单|工单里|工单记录")
_CASE_QUESTION = re.compile(r"事故|案例|复盘|通报")
_HISTORICAL_ACTION_LOOKUP = re.compile(
    r"(?:历史(?:案例|工单|记录)|已结单(?:案例|工单)|过往(?:案例|工单)|(?:案例|工单|复盘)(?:中|里))"
    r"(?:的|中|里|是|曾经|当时)*\s*(?:(?:怎么|如何|怎样)(?:修|维修|处理|处置|解决)|"
    r"(?:维修|处理|处置|解决)(?:步骤|方法|措施|过程|办法))"
)
_DIRECT_LOOKUP = re.compile(
    r"是什么意思|含义是什么|是什么(?:故障|告警|类型|形式)?[？?。]?$|什么故障|什么意思|表示(?:什么|哪)|属于哪|多少|几相"
    r"|\bwhat (?:does|is|are)\b|\bhow (?:much|many)\b", re.I
)
_FOCUSED_FACT_FOLLOWUP = re.compile(r"^(?:那(?:么)?\s*)?(?P<focus>[\u4e00-\u9fff]{2,20}?)呢[？?。！!]?$")
_FAULT_LOOKUP = re.compile(
    r"是什么意思|什么意思|怎么回事|是(?:什么|啥)[？?。]?$|(?:什么|啥)(?:情况|故障|告警|报警|错误|问题|提示|原因)|"
    r"(?:故障|告警|报警)(?:含义|原因|名称|是什么)|(?:表示|对应|代表)(?:什么|哪)|含义是什么|"
    r"(?:报了|报出|报错|报的是|出现|显示|只问了|只问的是)|(?:报|告警|报警|提示|请问)\s*[:：]?\s*(?:[A-Z]+\d|\d)|"
    r"不是[^。！？!?]{0,30}[，,；;]\s*(?:而?是)|(?:不对|更正|改成|应为|正确的是|实际是|实际为|应该是)|"
    r"哪(?:一)?[个种项类](?:故障|告警|报警|错误|问题|提示)|"
    r"(?:故障|告警|报警)[？?。]?$|"
    r"\bwhat (?:is|does)\b|\bmeans?\b", re.I
)
_PROCEDURE_QUESTION = re.compile(
    r"(?:怎么|如何|怎样)(?:进行)?(?:修|维修|排除|排障|处理|处置|解决|排查|检查|消除)"
    r"|这么(?:修(?:复|理)?|维修|解决|处理|处置|排查)[？?。！!]*$"
    r"|(?:维修|修复|排障|处理|处置|排查|解决)(?:步骤|方法|流程|办法|措施)"
    r"|怎么办|(?:先|下一步)(?:该|应该)?(?:查|检查|核对|看)(?:什么|哪些|哪里|哪项)"
    r"|(?:怎么|如何|怎样)查(?=[？?。！!\s]|$)"
    r"|\bhow\b[^?\n]{0,70}\b(?:repair|fix|troubleshoot|resolve)\b"
    r"|\b(?:repair|troubleshooting|corrective) (?:steps|procedure)\b", re.I
)
_CONTEXT_REFERENCE = re.compile(r"^(?:这(?:个|种|次|条)|那(?:么)?|接着|接下来|然后|继续|下一步|刚才|它|还有呢)|"
                                r"(?:^|[，。；,;\s])(?:(?:然后|接下来|下一步)(?:呢|要|怎么办|怎么|查|先|去|$)"
                                r"|先(?:查|看|检查|核对)(?:什么|哪些|哪里))")
_SHORT_PROCEDURE = re.compile(
    r"^(?:请问|(?:请)?告诉我|(?:我)?想知道)?\s*(?:怎么|如何|怎样)(?:进行)?"
    r"(?:修(?:复|理)?|维修|排除|排障|处理|处置|解决|排查|检查|查|消除)[？?。！!]*$"
)
_ADVANCE = re.compile(r"下一步|然后呢|接下来|之后呢|先查|先看|先检查|先核对")
_QUESTION_WORDS = re.compile(r"[？?]|什么|多少|为何|是否|为什么|是什么意思")
_OBSERVATION_REPORT = re.compile(r"出现|发生|启动|运行|负载|没(?:有|看|查|核对)|测点|确认|正常|看过|查过|故障码")
_NORMAL_CHECK = re.compile(
    r"^(?:目前|刚才)?(?P<subject>[\u4e00-\u9fff、]{2,24}?)(?:都)?(?:已(?:经)?(?:确认|核对|检查))?"
    r"(?:运行|运转)?(?:正常|无异常|没有异常|没(?:有)?问题|在(?:允许|规定)范围内)(?:了)?$"
)
_UNCERTAIN_CHECK = re.compile(r"如果|假如|要是|可能|怀疑|是否|能否|未|没(?:查|看|核对|检查|确认)|不正常|不在")
_PROCEDURE_HEADING = re.compile(
    r"(?im)^[ \t]*(?:#{1,6}[ \t]+)?(?:故障(?:排查|处理|处置)"
    r"|(?:维修|修复|处理|处置|检查|排查|排除|解决)(?:步骤|方法|流程|办法)"
    r"|troubleshooting(?: steps)?|corrective actions?|remedy)[：:]?[ \t]*$"
)
_PROCEDURE_STEP = re.compile(
    r"(?im)^[ \t]*(?:(?:(?:步骤|step\b)[ \t]*(\d{1,3})"
    r"|第[ \t]*(\d{1,3}|[一二三四五六七八九十])[ \t]*步)[ \t]*[：:、.．）)]?"
    r"|[(（]?(\d{1,3})[、.．）)](?!\d))[ \t]*([^\r\n]*)"
)
_PROCEDURE_FIELD = re.compile(
    r"(?im)^[ \t]*(?:故障(?:处理|排查|处置)|(?:处理|处置|排查|维修|解决|排除)(?:方法|措施|步骤|办法|方案|意见)?|"
    r"措施|建议措施|纠正措施|what to do|corrective actions?|recommended actions?|remedy|solution|troubleshooting)[：:][ \t]*([^\r\n]+)"
)
_HISTORY_FIELD = re.compile(
    r"(?im)^[ \t]*(?P<label>故障(?:码|现象|含义)|现象|适用设备|确认原因|已确认原因|故障原因|原因|"
    r"实际处置|处置过程|处理过程|维修过程|处置方法|处理措施|实际处理|现场结果|处置结果|处理结果|维修结果|整改结果|结果|结论|"
    r"定位|来源|出处|记录编号|symptom|confirmed cause|cause|actual actions?|actions?|result|outcome)[ \t]*[：:][ \t]*"
)
_HISTORY_CAUSE = re.compile(r"原因|为何|为什么|\bcause\b", re.I)
_HISTORY_RESULT = re.compile(r"结果|结论|效果|恢复|\b(?:result|outcome)\b", re.I)
_HISTORY_ACTION = re.compile(r"处置|处理|维修|措施|\b(?:action|repair|fix)\w*\b", re.I)
_SYMPTOM_SECTION = re.compile(r"(?im)^[ \t]*(?:#{1,6}[ \t]+)?(?:故障现象|现场现象|症状|symptom)[：:][^\r\n]+")
_CHINESE_STEP = dict(zip("一二三四五六七八九十", range(1, 11)))
_CHECK_START = re.compile(r"^(?:请)?(?:记录|核对|查看|查阅|对照|观察|检查(?:面板|记录|历史)|"
                          r"(?:record|note|review|compare|verify|observe|read)\b)", re.I)
_INTERVENTION = re.compile(r"更换|断开|拔出|重新接|拆除|拆开|调节|清理|清除|修改|调整|切换|启动|关闭"
                           r"|\b(?:replace|disconnect|remove|adjust|clean|switch|disable|activate)\b", re.I)
_QUALIFIERS = re.compile(r"禁止|严禁|不得|不可|没有|未|不足|不能|不应|不允许|不适用|仅|必须|至少|至多|可能"
                         r"|除非|适用|当(?:前)?|如果|只有|确认|原因|导致|引起|损坏|失效|示例|演示"
                         r"|\b(?:not|no|never|only|must|unless|when|if|may|at least|at most|cause|caused|confirmed)\b", re.I)
_ANSWER_VALUES = re.compile(r"[A-Z]{1,5}[-_]?\d{2,6}|[+−-]?\d+(?:[.,]\d+)*\s*"
                            r"(?:毫米|厘米|千克|公斤|兆帕|千帕|千瓦|千伏|伏|安培|赫兹|吨|摄氏度|"
                            r"°[CF]|℃|℉|%|[a-zA-Z]+\b)?|[<>≤≥]", re.I)
_FOLLOW_UP = {
    "onset": "告警是在启动时出现，还是运行一段时间后出现？",
    "load": "告警出现时，设备处于什么负载或工况？",
    "panel": "面板当时显示的完整故障码或提示是什么？",
    "trend": "告警前后，相关参数的历史趋势有什么变化？",
    "checked": "你已经核对了哪些项目，分别看到了什么？",
}
_ANSWER_SKILL = (Path(__file__).with_name("skills") / "maintenance-answer" / "SKILL.md").read_text(
    encoding="utf-8").split("---", 2)[2].strip()


def applicable_procedure_text(item, device, requested):
    body = normalize_fault_tables(item["text"]).strip()
    sections = fault_sections(body)
    if not sections:
        context = item["position"] + "\n" + body
        return body if {code_key(code) for code in fault_codes(context, device, infer_numeric=False)} == requested else ""
    excerpts = []
    preamble = body[:sections[0].start()]
    for index, section in enumerate(sections):
        if code_key(section["code"].upper()) in requested:
            end = sections[index + 1].start() if index + 1 < len(sections) else len(body)
            excerpts.append((preamble + body[section.start():end]).strip())
    return excerpts[0] if len(excerpts) == 1 else ""


def fault_definition(manuals, device, requested):
    if len(requested) != 1:
        return None
    available = []
    for item in manuals:
        scoped = applicable_procedure_text(item, device, requested)
        if not scoped:
            continue
        heading = _PROCEDURE_HEADING.search(scoped)
        description = scoped[:heading.start()] if heading else scoped
        section = next(iter(fault_sections(description)), None)
        if section:
            description = description[section.start():]
        lines = []
        for sentence in _SENTENCE_BREAK.split(description):
            line = sentence.strip()
            if not line or SAFETY.search(line) or _INTERVENTION.search(line):
                continue
            exact = {code_key(code) for code in codes_in(line)}
            inline_definition = requested <= exact and re.search(r"表示|对应|代表|指的是|意味着|\b(?:means?|indicates?)\b", line, re.I)
            labelled_definition = section and re.match(
                r"^(?:(?:故障|报警|告警)?(?:含义|名称|定义|原因|描述|说明|现象)|故障|meaning|cause|description)"
                r"(?:为|是|[：:])\s*\S", line, re.I)
            if inline_definition or labelled_definition:
                lines.append(line)
        if not lines and section:
            name = section["name"].split(" · ", 1)[0].strip(" \t:：-—·")
            if (len(name) >= 2 and re.search(r"[A-Za-z\u4e00-\u9fff]", name)
                    and not re.match(r"第\s*\d|页码|page\s+\d", name, re.I)
                    and not (SAFETY.search(name) or _INTERVENTION.search(name))):
                lines.append(f"故障码 {section['code']}：{name}")
        if lines:
            available.append((item, "\n".join(dict.fromkeys(lines))))
    return available[0] if len(available) == 1 else None


def symptom_procedure_text(body, signals, details):
    body = normalize_fault_tables(body)
    sections = sorted([*_SYMPTOM_SECTION.finditer(body), *fault_sections(body)], key=lambda match: match.start())
    excerpts = []
    for index, section in enumerate(sections):
        end = sections[index + 1].start() if index + 1 < len(sections) else len(body)
        text = body[section.start():end]
        heading = _PROCEDURE_HEADING.search(text)
        description = text[:heading.start()] if heading else section.group()
        if supports_symptoms("", description, signals, details):
            excerpts.append((body[:sections[0].start()] + body[section.start():end]).strip())
    return excerpts[0] if len(excerpts) == 1 else ""


def procedure_excerpt(manuals, device, requested, *, signals=(), details=()):
    if len(requested) > 1 or not (requested or signals):
        return None
    available = []
    for item in manuals:
        text = (applicable_procedure_text(item, device, requested) if requested
                else symptom_procedure_text(item["text"], signals, details))
        headings = list(_PROCEDURE_HEADING.finditer(text))
        procedure_text = re.sub(r"(?im)^[ \t]*(?:定位|出处|原文位置|source location)[：:][^\r\n]*", "", text)
        if len(text) > 1600 or re.search(r"续上|续下|续表|接上页|见下页|见下一页|转下页", procedure_text):
            continue
        if not headings:
            fields = list(_PROCEDURE_FIELD.finditer(text))
            if fields and all(not re.match(r"(?:参见|参考|见|详见|(?:see|refer to)\b)", field[1].strip(), re.I)
                              for field in fields):
                available.append((item, text))
            continue
        if len(headings) != 1:
            continue
        entries = _PROCEDURE_STEP.findall(text[headings[0].end():])
        if any(not instruction.strip() for *_, instruction in entries):
            continue
        numbers = [first or second or third for first, second, third, _ in entries]
        steps = [_CHINESE_STEP[number] if number in _CHINESE_STEP else int(number) for number in numbers]
        if steps and steps == list(range(1, len(steps) + 1)):
            available.append((item, text))
    return available[0] if len(available) == 1 else None


def next_procedure_check(procedure, observation):
    heading = _PROCEDURE_HEADING.search(procedure)
    if not heading:
        return None
    steps = list(_PROCEDURE_STEP.finditer(procedure, heading.end()))
    if len(steps) < 2:
        return None
    confirmed = set()
    for clause in re.split(r"[，,；;。！？?\n]", observation):
        match = _NORMAL_CHECK.fullmatch(clause.strip())
        if not match or _UNCERTAIN_CHECK.search(clause):
            continue
        for subject in re.split(r"[、和与及]", match["subject"]):
            matched = [index for index, step in enumerate(steps) if len(subject) >= 2 and subject in step[4]]
            if len(matched) == 1:
                confirmed.add(matched[0])
    next_index = next((index for index in range(len(steps)) if index not in confirmed), None)
    if next_index in {0, None}:
        return None
    end = steps[next_index + 1].start() if next_index + 1 < len(steps) else len(procedure)
    return procedure[steps[next_index].start():end].strip(), procedure[:heading.start()].strip()


def multiple_fault_answer(manuals, device, requested, wants_procedure):
    sections, references = [], []
    for code in sorted(requested):
        procedure = procedure_excerpt(manuals, device, {code})
        entry = procedure or fault_definition(manuals, device, {code})
        if not entry:
            return None
        citation, body = entry
        if citation["id"] not in references:
            references.append(citation["id"])
        link = references.index(citation["id"]) + 1
        section = f"故障码 {code} [{link}]\n\n{body}"
        if wants_procedure and not procedure:
            section += "\n\n手册仅提供故障说明，完整处置方法需要进一步核实。"
        sections.append(section)
    return sections, references


def numeric_parameter_answer(manuals, question):
    numbers = non_fault_numbers(question)
    if not numbers:
        return None
    tokens = terms(question)
    matches = {}
    for item in manuals:
        for sentence in _SENTENCE_BREAK.split(item["text"]):
            line = sentence.strip()
            if numbers <= non_fault_numbers(line) and not (SAFETY.search(line) or _INTERVENTION.search(line)):
                matches.setdefault(line, (sum((tokens & terms(line)).values()), item))
    if not matches:
        return None
    best = max(score for score, _ in matches.values())
    winners = [(item, line) for line, (score, item) in matches.items() if score == best]
    return winners[0] if len(winners) == 1 else None


def focused_fact_answer(manuals, question):
    match = _FOCUSED_FACT_FOLLOWUP.fullmatch(question.strip())
    if not match:
        return None
    focus = match["focus"]
    facts = {}
    for item in manuals:
        for sentence in _SENTENCE_BREAK.split(item["text"]):
            line = sentence.strip()
            if (focus in line and re.search(r"\d", line.partition(focus)[2]) and len(line) <= 320
                    and not (SAFETY.search(line) or _INTERVENTION.search(line) or _CHECK_START.search(line)
                             or _PROCEDURE_STEP.match(line) or _PROCEDURE_HEADING.match(line) or _PROCEDURE_FIELD.match(line))):
                facts.setdefault(line, item)
    if len(facts) == 1:
        fact, item = next(iter(facts.items()))
        return item, fact
    return None


def historical_answer(question, evidence):
    wanted = (_HISTORY_RESULT if _HISTORY_RESULT.search(question) else
              _HISTORY_CAUSE if _HISTORY_CAUSE.search(question) else
              _HISTORY_ACTION if _HISTORY_ACTION.search(question) else None)
    sections, references = [], []
    for item in evidence:
        text = redact(item["text"])
        fields = list(_HISTORY_FIELD.finditer(text))
        selected = []
        substantive = False
        for index, field in enumerate(fields):
            end = fields[index + 1].start() if index + 1 < len(fields) else len(text)
            value = text[field.end():end].strip()
            label = field["label"]
            if (value and len(value) <= 1200 and not SAFETY.search(value)
                    and label not in {"定位", "来源", "出处", "记录编号"} and (wanted is None or wanted.search(label))):
                selected.append(f"{label}：{value}")
                substantive |= label not in {"故障码", "适用设备"}
        if substantive:
            references.append(item["id"])
            sections.append(f"{item['title']} [{len(references)}]\n\n" + "\n".join(selected))
    return sections, references


def question_context(prior, text, new_subject):
    if not prior or new_subject:
        return [], False, ""
    last = prior[-1].data
    follow_up = last.get("diagnosticFollowUp", "")
    reporting = bool(follow_up and not _QUESTION_WORDS.search(text) and _OBSERVATION_REPORT.search(text))
    if not (reporting or _CONTEXT_REFERENCE.search(text) or _SHORT_PROCEDURE.fullmatch(text) or _ADVANCE.match(text)):
        return [], False, ""
    start = last.get("contextStart", last["sequence"])
    anchor = next((row for row in prior if row.data["sequence"] == start), prior[-1])
    recent = [row for row in prior[-2:] if row.data["sequence"] > anchor.data["sequence"]]
    return [anchor, *recent], reporting, follow_up


def finding_text(quote, text):
    if text is None:
        return quote
    if not isinstance(text, str):
        raise ValueError("invalid finding text")
    text = text.strip()
    if (not text or len(text) > 320 or "\n" in text or SAFETY.search(text) or _INTERVENTION.search(text)
            or _CHECK_START.search(text) or re.search(r"\[\d+\]|https?://", text, re.I)):
        return quote
    original = normalize_code_text(quote)
    proposed = normalize_code_text(text)
    if _QUALIFIERS.findall(original.casefold()) != _QUALIFIERS.findall(proposed.casefold()):
        return quote
    original_values = [re.sub(r"\s+", "", match) for match in _ANSWER_VALUES.findall(original)]
    proposed_values = [re.sub(r"\s+", "", match) for match in _ANSWER_VALUES.findall(proposed)]
    if original_values != proposed_values:
        return quote
    return text


def grounded_answer(question, evidence, primary="manual", fault_keys=(), *,
                    previous=(), last_follow_up="", inspection_texts=None, troubleshooting=False, full_procedure=False):
    available, lines = [], {}
    direct_lookup = primary == "manual" and not troubleshooting and bool(_DIRECT_LOOKUP.search(question))
    for index, item in enumerate(evidence, start=1):
        body = redact(item["text"])
        sentences = _SENTENCE_BREAK.split(body[:1600])
        if len(body) > 1600 and sentences[-1]:
            sentences.pop()
        allowed = [sentence.strip() for sentence in sentences if sentence.strip()]
        relevant = None if inspection_texts is None else redact(inspection_texts.get(item["id"], ""))
        scoped = allowed if relevant is None else [line.strip() for line in _SENTENCE_BREAK.split(relevant)]
        category = item["category"]
        entry = {"type": category, "lines": []}
        check_lines = []
        for text in allowed:
            number = len(lines) + 1
            observation = (category == "manual" and not (full_procedure or direct_lookup) and text in scoped
                           and bool(_CHECK_START.search(text)) and not (SAFETY.search(text) or _INTERVENTION.search(text)))
            lines[number] = (index, category, text, observation)
            entry["lines"].append({"line": number, "text": text})
            if observation:
                check_lines.append(number)
        if check_lines:
            entry["checkLines"] = check_lines
        available.append(entry)
    response = model_call("chat/completions", {
        "model": cfg.chat_model, "temperature": 0, "max_tokens": 900,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _ANSWER_SKILL},
            {"role": "user", "content": json.dumps({
                "question": redact(question), "previousQuestions": [redact(text) for text in previous],
                "normalizedFaultCodes": sorted(fault_keys), "requestedSource": primary,
                "troubleshooting": troubleshooting, "fullProcedureAvailable": full_procedure,
                "directLookup": direct_lookup, "lastFollowUp": last_follow_up, "evidence": available,
            }, ensure_ascii=False)},
        ],
    })
    reply = json.loads(response["choices"][0]["message"]["content"])
    claims = reply["claims"]
    if not isinstance(claims, list) or not 1 <= len(claims) <= 4:
        raise ValueError("invalid claims")
    findings, checks, history, references = [], [], [], {}
    for claim in claims:
        line = claim["line"]
        if type(line) is not int or line not in lines:
            raise ValueError("invalid citation")
        index, category, excerpt, observation = lines[line]
        if len(excerpt) < 8 or SAFETY.search(excerpt):
            continue
        if category != "manual":
            if not direct_lookup and not full_procedure:
                history.append((index, excerpt))
        elif observation:
            checks.append((index, excerpt))
        elif not (_CHECK_START.search(excerpt) or _PROCEDURE_STEP.match(excerpt)
                  or _PROCEDURE_HEADING.match(excerpt) or _INTERVENTION.search(excerpt)):
            findings.append((index, finding_text(excerpt, claim.get("text"))))
    if not any(available[index - 1]["type"] == primary for index, _ in findings + checks + history):
        raise ValueError("no citation from the requested source type")

    def cite(items):
        return [f"{text} [{references.setdefault(index, len(references) + 1)}]"
                for index, text in dict.fromkeys(items)]

    sections = cite(findings)
    if checks:
        sections.append("先核对\n" + "\n".join(f"• {entry}" for entry in cite(checks)))
    if history:
        label = "历史记录（仅供对照）" if primary == "manual" else "历史记录"
        sections.append(label + "\n" + "\n".join(cite(history)))
    follow_up = reply.get("followUp")
    if direct_lookup or not isinstance(follow_up, str) or follow_up not in _FOLLOW_UP or follow_up == last_follow_up:
        follow_up = ""
    return sections, [evidence[index - 1]["id"] for index in references], follow_up


def answer_turn(db, org, turn):
    session = need(find(db, org, "session", id=turn.owner))
    user = need(find(db, org, "user", key=session.owner))
    if user.status != "active":
        raise ValueError("用户访问已撤销")
    device = device_for(db, user, session.data["deviceId"])
    prior = [row for row in session_turns(db, org, session) if row.data["sequence"] < turn.data["sequence"]]
    trace = {} if cfg.rag_trace else None
    result = answer_question(db, org, user, device, turn.data["text"], prior=prior,
                             sequence=turn.data["sequence"], trace=trace)
    if trace is not None:
        logging.getLogger("maintenance.worker").info("rag_trace %s", json.dumps({
            "turnId": turn.id, **trace, "decision": result["decision"],
            "citations": result["referenceIds"], "modelDegraded": result.get("modelDegraded", False),
        }))
    return result


def answer_question(db, org, user, device, text, *, prior=(), sequence=1, preview_source=None, trace=None):
    current_codes = fault_codes(text, device)
    current_signals = symptom_signals(text)
    named = named_fault_matches(db, org, device, text, preview_source) if not current_codes else []
    context_rows, reporting_observations, last_follow_up = question_context(
        prior, text, current_codes or current_signals or named)
    context = [row.data["text"] for row in context_rows]
    query = "\n".join([*context, text])
    requested = {code_key(code) for code in fault_codes(query, device)}
    if not requested and not named and context:
        named = named_fault_matches(db, org, device, context[0], preview_source)
    if len(named) == 1 and not requested:
        query = f"故障码：{named[0][0]}\n{query}"
        requested = {code_key(named[0][0])}
    safety_question = bool(SAFETY.search(query))
    context_start = context_rows[0].data["sequence"] if context_rows else sequence
    if len(named) > 1 and not safety_question and preview_source is None:
        codes = "、".join(code for code, _ in named[:6])
        if trace is not None:
            trace.update(contextStart=context_start, previousTurns=len(context), faultCodeCount=0,
                         rankedChunks=[dict(id=chunk) for _, chunk in named[:6]])
        return dict(decision="clarify", referenceIds=list(dict.fromkeys(chunk for _, chunk in named[:6])),
                    answer=f"当前设备手册中有多条同名故障，报码包括 {codes}。请核对面板上的完整故障码。",
                    contextStart=context_start)
    wants_procedure = bool(_PROCEDURE_QUESTION.search(text) or reporting_observations or (
        _ADVANCE.search(text) and (requested or current_signals or any(
            _PROCEDURE_QUESTION.search(row.data["text"]) for row in prior
            if row.data["sequence"] >= context_start))))
    primary = "manual"
    if (not wants_procedure or _HISTORICAL_ACTION_LOOKUP.search(text)) and _WORKORDER_QUESTION.search(text):
        primary = "workorder"
    elif (not wants_procedure or _HISTORICAL_ACTION_LOOKUP.search(text)) and _CASE_QUESTION.search(text):
        primary = "case"
    if primary != "manual":
        wants_procedure = False
    signals, details = (), ()
    if primary == "manual" and not requested:
        signals = current_signals
        details = symptom_details(text)
        if not signals:
            for earlier_text in reversed(context):
                signals = symptom_signals(earlier_text)
                if signals:
                    details = tuple(dict.fromkeys((*symptom_details(earlier_text), *details)))
                    break
    category = primary if primary != "manual" else None
    candidates, ambiguous = retrieve(db, org, device, query, signals=signals, details=details,
                                     preview_source=preview_source, trace=trace, category=category)
    observation = next((item.get("matchedObservation") for item in candidates if item.get("matchedObservation")), "")
    if (primary == "manual" and requested and not wants_procedure and not safety_question
            and candidates and all(item["category"] != "manual" for item in candidates)):
        primary = "case" if any(item["category"] == "case" for item in candidates) else "workorder"
    data = dict(decision="clarify", referenceIds=[], answer="", contextStart=context_start)
    if trace is not None:
        trace.update(contextStart=context_start, previousTurns=len(context), faultCodeCount=len(requested),
                     procedure=wants_procedure, category=primary, ambiguousScope=ambiguous)
    if preview_source is not None:
        data.update(hits=candidates.copy(), requestedCodes=sorted(requested))
    if observation and not safety_question:
        history = [item for item in candidates if primary == "manual" or item["category"] == primary]
        if history:
            refs = " ".join(f"[{i}]" for i in range(1, len(history) + 1))
            data.update(decision="evidence", referenceIds=[item["id"] for item in history],
                        answer=f"在当前设备的历史记录中找到了「现象」栏为 {observation} 的资料。{refs}\n\n"
                               "这是已记录的历史现象。请以面板显示和适用手册核实实际故障码，历史记录供复盘参考。")
            return data
    suggested = [] if ambiguous or preview_source is not None else suggested_device_refs(
        db, user, device, query, candidates, signals=signals, details=details, category=category)
    if ambiguous:
        data["answer"] = "当前型号存在不同批次的资料。请核对设备铭牌，并联系管理员补全台账中的批次后再查询。"
    elif not candidates and safety_question:
        data.update(decision="safety", answer="缺少适用的已生效手册。请联系班组专家并遵循现场规程。")
    elif suggested:
        data.update(decision="no_evidence", suggestedDeviceRefs=suggested,
                    answer="当前设备暂无直接匹配的资料。请先核对台账，再选择下方匹配的设备继续查询。")
    elif signals and not candidates:
        current = f"在「{device['name']}」的资料中，没有找到支持该异常的依据。"
        if last_follow_up == "point":
            data.update(decision="no_evidence",
                        answer=current + "请核对台账中的设备编号和型号，或将现象转交班组专家。")
        else:
            data["answer"] = current + "请先确认异常发生在当前设备还是其他设备，并补充异常测点和面板告警。"
        data["diagnosticFollowUp"] = "point"
    elif len(text) < 6 and not (signals and candidates) and not requested and not context:
        data["answer"] = "请描述面板上的故障码、异常现象，以及异常发生时的运行状态。"
    elif not candidates:
        answer = ("当前没有找到适用的维修步骤。请核对设备型号、完整故障码和发生条件，或转交班组专家。"
                  if wants_procedure else "当前设备范围内没有匹配的已发布资料。可以补充故障码和发生条件，或转交班组专家。")
        if len(requested) == 1 and len(current_codes) == 1:
            answer = f"当前设备的已生效资料中未找到故障码 {next(iter(current_codes))}。请核对面板上的完整报码和设备型号。"
        if preview_source is not None:
            answer = "本资料未召回与这次提问匹配的片段。请对照原件检查故障码、字段和片段内容。"
        data.update(decision="no_evidence", answer=answer)
    else:
        data["referenceIds"] = [item["id"] for item in candidates]
        condition = f"适用设备：{device['model']}" + (f" · 批次 {device['batch']}" if device.get("batch") else "")
        manual = [item for item in candidates if item["category"] == "manual"]
        manual_codes = {item["id"]: {code_key(value) for value in chunk_fault_codes(item["position"], item["text"])}
                        for item in manual}
        by_code = {code: [item for item in manual if code in manual_codes[item["id"]]] for code in requested}
        matched_ids = {item["id"] for group in by_code.values() for item in group}
        matched = [item for item in manual if item["id"] in matched_ids]
        code_lookup = code_key(standalone_fault_code(text)) in requested or len(named) == 1
        direct_fault_lookup = (primary == "manual" and bool(requested) and not wants_procedure and
                               bool(code_lookup or _FAULT_LOOKUP.search(text)))
        excerpt = (procedure_excerpt(matched if requested else manual, device, requested, signals=signals, details=details)
                   if primary == "manual" and (wants_procedure or direct_fault_lookup) else None)
        if primary == "manual" and any(len({item["sourceId"] for item in group}) > 1 for group in by_code.values()):
            data.update(decision="conflict", answer="找到多份适用手册同时涉及该故障码。请对照出处，并由班组专家核实适用条件。")
            data["_conflictDevice"] = device
        elif safety_question or (primary == "manual" and any(SAFETY.search(item["text"]) for item in manual)):
            definition = (fault_definition(matched, device, requested)
                          if requested and not safety_question and primary == "manual" else None)
            if definition and direct_fault_lookup:
                citation, meaning = definition
                data.update(decision="answer", referenceIds=[citation["id"]], answer=f"{meaning} [1]\n\n{condition}")
                return data
            data["referenceIds"] = [item["id"] for item in manual]
            data.update(decision="safety", answer="这涉及安全关键操作。请核对下方适用的手册原文和图示，并按现场规程由有权限的人员处置。")
            if definition and wants_procedure:
                citation, meaning = definition
                data.update(referenceIds=[citation["id"]], answer=f"{meaning} [1]\n\n这涉及安全关键操作。"
                            "请由有权限的人员对照手册与现场规程，确认具体测点、操作条件和判断标准。")
            elif wants_procedure and signals and len(manual) == 1 and not requested and not safety_question:
                sections = fault_sections(normalize_fault_tables(manual[0]["text"]))
                if len(sections) == 1:
                    code = sections[0]["code"]
                    candidate = fault_definition(manual, device, {code_key(code)})
                    if candidate and supports_symptoms("", candidate[1], signals, details):
                        citation, meaning = candidate
                        data.update(referenceIds=[citation["id"]],
                                    answer=f"手册中与所述现象匹配的条目是故障码 {code}。{meaning} [1]\n\n"
                                    "请先核对面板是否显示该码，并记录异常测点、读数和当时的运行状态。"
                                    "涉及电气操作时，请由有权限的人员按现场规程核实。")
            elif wants_procedure and signals and len(manual) > 1 and not requested and not safety_question:
                data["answer"] = ("手册有多条记录与所述现象相关。请补充面板报码、异常测点和当时的运行状态，"
                                  "以便缩小范围。涉及安全关键操作，请由有权限的人员按现场规程处置。")
            if not manual:
                data["answer"] = "缺少适用的已生效手册。请将问题转交班组专家核实。"
        elif wants_procedure and not manual:
            data.update(decision="no_evidence", referenceIds=[],
                        answer="当前没有找到适用的维修步骤。请转交班组专家核实。")
        else:
            if primary == "manual" and not requested and not wants_procedure and not signals:
                fact = focused_fact_answer(manual, text)
                if fact:
                    citation, value = fact
                    data.update(decision="answer", referenceIds=[citation["id"]], answer=f"{value} [1]\n\n{condition}")
                    return data
            if primary == "manual" and len(requested) > 1 and (wants_procedure or direct_fault_lookup):
                combined = multiple_fault_answer(matched, device, requested, wants_procedure)
                if combined:
                    sections, references = combined
                    data.update(decision="answer", referenceIds=references,
                                answer="\n\n".join([*sections, "请分别核对各故障的适用条件。", condition]))
                    return data
            partial = wants_procedure and not excerpt
            limits = "当前手册片段不足以确认完整维修步骤。需要进一步处置时，请转交班组专家核实。"
            if primary != "manual":
                candidates = [item for item in candidates if item["category"] == primary]
                if not candidates:
                    data.update(decision="no_evidence", referenceIds=[], answer="当前设备范围内没有匹配的已发布历史记录或案例。")
                    return data
                data["referenceIds"] = [item["id"] for item in candidates]
                condition = "历史记录供复盘参考。本次现场原因和处置仍需核实。"
            approved_context = [] if excerpt or preview_source is not None else model_evidence(db, org, candidates, primary)
            sections, references, follow_up = [], [], ""
            inspection_texts = None
            if approved_context:
                if requested:
                    inspection_texts = {}
                    for item in matched:
                        sections_for_codes = [applicable_procedure_text(item, device, {code}) for code in sorted(requested)]
                        inspection_texts[item["id"]] = "\n\n".join(dict.fromkeys(filter(None, sections_for_codes)))
                if primary == "manual" and inspection_texts is not None:
                    approved_context = [item if item["category"] != "manual" else {**item, "text": inspection_texts[item["id"]]}
                                        for item in approved_context
                                        if item["category"] != "manual" or inspection_texts.get(item["id"])]
            if any(item["category"] == primary for item in approved_context):
                if trace is not None:
                    trace["modelChunks"] = [item["id"] for item in approved_context]
                try:
                    sections, references, follow_up = grounded_answer(
                        text, approved_context, primary, requested, previous=context, last_follow_up=last_follow_up,
                        inspection_texts=inspection_texts, troubleshooting=wants_procedure or code_lookup,
                        full_procedure=bool(excerpt))
                except (ValueError, RuntimeError, TypeError, KeyError, IndexError, httpx.HTTPError):
                    data["modelDegraded"] = True
            if excerpt:
                citation, original = excerpt
                if citation["id"] not in references:
                    references.append(citation["id"])
                link = references.index(citation["id"]) + 1
                next_check = next_procedure_check(original, text) if _ADVANCE.search(text) else None
                if next_check:
                    step, conditions = next_check
                    sections.append(f"下一步 [{link}]\n\n{step}\n\n手册适用条件与条目\n{conditions}")
                else:
                    sections.append(f"手册处置依据 [{link}]\n\n{original}")
                sections.append("请按出处核对完整章节、适用条件和现场规程。" if requested else
                                "这是按所述现象匹配的手册条目。请核对面板提示和适用条件。")
            elif not sections and direct_fault_lookup:
                definition = fault_definition(matched, device, requested)
                if definition:
                    citation, meaning = definition
                    sections.append(f"{meaning} [1]")
                    references = [citation["id"]]
            elif not sections and primary in {"case", "workorder"}:
                sections, references = historical_answer(text, candidates)
            elif (not sections and primary == "manual" and not requested and not wants_procedure and not signals
                  and (_DIRECT_LOOKUP.search(text) or _QUESTION_WORDS.search(text))):
                parameter = numeric_parameter_answer(manual, text)
                if parameter:
                    citation, fact = parameter
                    sections.append(f"{fact} [1]")
                    references = [citation["id"]]
            if sections:
                if partial:
                    sections.append(limits)
                if follow_up and primary == "manual":
                    sections.append(_FOLLOW_UP[follow_up])
                    data["diagnosticFollowUp"] = follow_up
                data.update(decision="answer", answer="\n\n".join([*sections, condition]), referenceIds=references)
            else:
                message = ("回答模型暂未返回可核验的答复。下方提供相关原文。"
                           if data.get("modelDegraded") else "已找到相关原文，请查看资料出处并核对适用条件。")
                if signals and not manual:
                    message = "当前只找到记录过此类异常的历史资料，可用于对照。请补充异常测点和面板告警，以便进一步核实。"
                if partial:
                    message = limits + " " + message
                    data["referenceIds"] = [item["id"] for item in manual]
                data.update(decision="evidence", answer=message + "\n\n" + condition)
    return data

