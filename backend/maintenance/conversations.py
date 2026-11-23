import json
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
from .knowledge import (CODE_LABEL, SAFETY, applicability, authorized_citation, code_key, codes_in, fault_codes,
                        model_call, model_evidence, normalize_code_text, retrieve, suggested_device_refs,
                        supports_symptoms, symptom_details, symptom_signals)

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
    result = turn.view("referenceIds", "claims", "suggestedDeviceRefs", "diagnosticFollowUp")
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
_DIRECT_LOOKUP = re.compile(
    r"是什么意思|含义是什么|是什么(?:故障|告警|类型|形式)?[？?。]?$|表示(?:什么|哪)|属于哪|多少|几相"
    r"|\bwhat (?:does|is|are)\b|\bhow (?:much|many)\b", re.I
)
_PROCEDURE_QUESTION = re.compile(
    r"(?:怎么|如何|怎样)(?:进行)?(?:修|维修|排除|排障|处理|处置|解决|排查|检查)"
    r"|(?:维修|修复|排障|处理|处置|排查)(?:步骤|方法|流程)"
    r"|怎么办|(?:先|下一步)(?:该|应该)?(?:查|检查|核对|看)(?:什么|哪些|哪里|哪项)"
    r"|(?:怎么|如何|怎样)查(?=[？?。！!\s]|$)"
    r"|\bhow\b[^?\n]{0,70}\b(?:repair|fix|troubleshoot|resolve)\b"
    r"|\b(?:repair|troubleshooting|corrective) (?:steps|procedure)\b", re.I
)
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
_FAULT_SECTION = re.compile(
    rf"(?im)^[ \t]*(?:(?:#{{1,6}}[ \t]+)?(?:{CODE_LABEL})[ \t]*[：:]?[ \t]*"
    r"(?:[A-Z]{1,5}[-_]?)?\d{1,6}|#{1,6}[ \t]+[A-Z]{1,5}[-_]?\d{1,6})"
    r"(?![A-Za-z0-9./-])[^\r\n]*"
)
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
    body = item["text"].strip()
    sections = list(_FAULT_SECTION.finditer(body))
    if not sections:
        context = item["position"] + "\n" + body
        return body if {code_key(code) for code in fault_codes(context, device)} == requested else ""
    excerpts = []
    preamble = body[:sections[0].start()]
    for index, section in enumerate(sections):
        if {code_key(code) for code in fault_codes(section.group(), device)} == requested:
            end = sections[index + 1].start() if index + 1 < len(sections) else len(body)
            excerpts.append((preamble + body[section.start():end]).strip())
    return excerpts[0] if len(excerpts) == 1 else ""


def symptom_procedure_text(body, signals, details):
    sections = list(_SYMPTOM_SECTION.finditer(body))
    excerpts = []
    for index, section in enumerate(sections):
        if supports_symptoms(section.group(), "", signals, details):
            end = sections[index + 1].start() if index + 1 < len(sections) else len(body)
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
        if len(headings) != 1 or len(text) > 1600 or re.search(r"续上|续下|续表|接上页|见下页|见下一页|转下页", text):
            continue
        entries = _PROCEDURE_STEP.findall(text[headings[0].end():])
        if any(not instruction.strip() for *_, instruction in entries):
            continue
        numbers = [first or second or third for first, second, third, _ in entries]
        steps = [_CHINESE_STEP[number] if number in _CHINESE_STEP else int(number) for number in numbers]
        if steps and steps == list(range(1, len(steps) + 1)):
            available.append((item, text))
    return available[0] if len(available) == 1 else None


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
    text = turn.data["text"]
    prior = [row for row in session_turns(db, org, session) if row.data["sequence"] < turn.data["sequence"]][-3:]
    earlier = [row.data["text"] for row in prior]
    context = []
    last_follow_up = ""
    query = text
    if len(text) <= 60 and earlier and not fault_codes(text, device):
        for index in range(len(earlier) - 1, -1, -1):
            if fault_codes(earlier[index], device):
                earlier = earlier[index:]
                break
        context = earlier
        query = " ".join([*context, text])
        last_follow_up = prior[-1].data.get("diagnosticFollowUp", "")
    reporting_observations = last_follow_up and not re.search(r"[？?]|什么|多少|为何|是否|为什么|是什么意思", text)
    wants_procedure = bool(_PROCEDURE_QUESTION.search(text) or reporting_observations)
    primary = "manual"
    if not wants_procedure and _WORKORDER_QUESTION.search(text):
        primary = "workorder"
    elif not wants_procedure and _CASE_QUESTION.search(text):
        primary = "case"
    signals, details = (), ()
    if primary == "manual" and not fault_codes(text, device):
        signals = symptom_signals(text)
        details = symptom_details(text)
        if not signals and reporting_observations and last_follow_up == "point":
            for earlier_text in reversed(context):
                signals = symptom_signals(earlier_text)
                if signals:
                    details = tuple(dict.fromkeys((*symptom_details(earlier_text), *details)))
                    break
    candidates, ambiguous = retrieve(db, org, device, query, signals=signals, details=details)
    data = dict(decision="clarify", referenceIds=[], answer="")
    suggested = [] if ambiguous else suggested_device_refs(db, user, device, query, candidates, signals=signals,
                                                         details=details)
    if ambiguous:
        data["answer"] = "当前型号存在不同批次的资料。请核对设备铭牌，并联系管理员补全台账中的批次后再查询。"
    elif not candidates and SAFETY.search(query):
        data.update(decision="safety", answer="缺少适用的已审核手册。请联系班组专家并遵循现场规程。")
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
    elif len(text) < 6 and not (signals and candidates) and not fault_codes(query, device) and not earlier:
        data["answer"] = "请描述面板上的故障码、异常现象，以及异常发生时的运行状态。"
    elif not candidates:
        answer = ("当前没有找到适用的维修步骤。请核对设备型号、完整故障码和发生条件，或转交班组专家。"
                  if wants_procedure else "当前设备范围内没有匹配的已发布资料。可以补充故障码和发生条件，或转交班组专家。")
        data.update(decision="no_evidence", answer=answer)
    else:
        data["referenceIds"] = [item["id"] for item in candidates]
        manual = [item for item in candidates if item["category"] == "manual"]
        requested = {code_key(code) for code in fault_codes(query, device)}
        matched = [item for item in manual
                   if requested.intersection(code_key(code) for code in codes_in(item["position"] + " " + item["text"]))]
        plain = re.sub(rf"^(?:{CODE_LABEL})\s*[:为]?\s*", "", normalize_code_text(text), flags=re.I).strip(" ?。")
        code_lookup = code_key(plain.upper()) in requested
        excerpt = (procedure_excerpt(matched if requested else manual, device, requested, signals=signals, details=details)
                   if wants_procedure or code_lookup else None)
        if primary == "manual" and len({item["sourceId"] for item in matched}) > 1:
            data.update(decision="conflict", answer="找到多份适用手册同时涉及该故障码。请对照出处，并由班组专家核实适用条件。")
            data["_conflictDevice"] = device
        elif SAFETY.search(query) or (primary == "manual" and any(SAFETY.search(item["text"]) for item in manual)):
            data["referenceIds"] = [item["id"] for item in manual]
            data.update(decision="safety", answer="这涉及安全关键操作。请核对下方适用的审核原文和图示，并按现场规程由有权限的人员处置。")
            if not manual:
                data["answer"] = "缺少适用的已审核手册。请将问题转交班组专家核实。"
        elif wants_procedure and not manual:
            data.update(decision="no_evidence", referenceIds=[],
                        answer="当前没有找到适用的已审核维修步骤。请转交班组专家核实。")
        else:
            condition = f"适用设备：{device['model']}" + (f" · 批次 {device['batch']}" if device.get("batch") else "")
            partial = wants_procedure and not excerpt
            limits = "当前手册片段不足以确认完整维修步骤。需要进一步处置时，请转交班组专家核实。"
            if primary != "manual":
                candidates = [item for item in candidates if item["category"] == primary]
                if not candidates:
                    return dict(decision="no_evidence", referenceIds=[], answer="当前设备范围内没有匹配的已发布历史记录或案例。")
                data["referenceIds"] = [item["id"] for item in candidates]
                condition = "历史记录供复盘参考。本次现场原因和处置仍需核实。"
            approved_context = model_evidence(db, org, candidates, primary)
            sections, references, follow_up = [], [], ""
            inspection_texts = None
            if approved_context:
                if excerpt:
                    procedure_source, procedure_text = excerpt
                    approved_context = [{**item, "text": procedure_text} for item in approved_context
                                        if item["id"] == procedure_source["id"]]
                elif requested:
                    inspection_texts = {item["id"]: applicable_procedure_text(item, device, requested) for item in matched}
                if wants_procedure and inspection_texts is not None:
                    approved_context = [item if item["category"] != "manual" else {**item, "text": inspection_texts[item["id"]]}
                                        for item in approved_context
                                        if item["category"] != "manual" or inspection_texts.get(item["id"])]
            if any(item["category"] == primary for item in approved_context):
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
                sections.extend([f"手册处置依据 [{link}]\n\n{original}", "请按出处核对完整章节、适用条件和现场规程。"])
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

