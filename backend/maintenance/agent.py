"""Standard ReAct (Reason + Act) RAG Agent with OpenAI-compatible tool calling and SSE streaming."""
import json
import re
from typing import Iterator

import httpx

from .conversations import redact
from .knowledge import KINDS, SAFETY, fault_codes
from .model_config import (
    chat_completion,
    chat_completion_stream_events,
    chat_ready,
    extract_thinking_text,
    get_ai_settings,
)
from .rag import get_chunk_context, hybrid_search, list_applicable_sources, lookup_fault_code

REACT_SYSTEM_PROMPT = """你是「知维」工业设备维修知识助手，会通过多轮思考与知识库检索工具协作完成解答。

对话与检索原则：
1. **自然交流与话题引导**：
   - 当用户只是打招呼（如“你好”“在吗”）、询问你是谁/能做什么、或闲聊时，**直接自然、友好地回复**，不要生硬套模板，也不要无意义地检索无关关键词。
   - 如果用户聊得太偏离设备运维主题（如天气、娱乐、闲聊等），礼貌轻松回应一句后，自然地把话题拉回到当前设备的故障排查、参数查询或维修资料上。
2. **故障/专业问题必须走知识库检索增强（RAG）**：
   - 只要涉及具体故障码、异常现象、设备参数、排查步骤、历史案例或手册内容，必须调用工具检索知识库，严禁凭空编造参数、故障定义或操作步骤。
   - 若首轮检索召回不足或偏题，主动改写关键词、拆分子问题或切换资料类别（手册 / 历史案例）再次检索，提高召回率。
   - 若命中片段疑被截断，调用 `read_chunk_context` 补全上下文；故障排查时可结合 `search_similar_cases` 对照历史案例。
   - 若多轮检索后确实未命中资料，如实、自然地告知当前知识库未收录相关内容，并引导用户补充故障码/现场工况或导入对应手册。
3. **输出约定**：
   - 决定调用工具时，可在 `content` 写一句简短的思考说明（1~2 句）；
   - 无需再调用工具时，直接给出面向用户的自然回复。

可用工具：
- `get_device_profile`：查看当前设备台账及已挂载的适用资料清单与示例故障码。
- `lookup_fault_code`：按故障码精确/模糊检索手册与案例。
- `search_knowledge`：BM25 + 向量混合语义检索（支持按 `manual`/`case`/`workorder` 过滤）。
- `search_similar_cases`：检索历史故障案例与历史工单经验。
- `read_chunk_context`：读取某个切片的上下文相邻切片。"""

SYNTHESIS_SYSTEM_PROMPT = """你是「知维」工业设备维修知识助手。请结合当前对话语境与已检索到的知识库证据，用自然、专业、清晰的中文回答用户。

要求：
1. **检索增强生成（RAG）**：结合当前设备型号/批次和现场描述进行综合归纳与研判，不要生硬整段照抄原文。
2. **引用溯源**：涉及手册事实、参数、故障定义或步骤时，用 `[1]`、`[2]` 标注对应证据编号（严格对应下方证据列表）。
3. **区分手册与历史案例**：手册作为操作与参数依据；历史案例/工单明确说明仅作本次排查对照。
4. **安全提醒**：涉及高压、带电、断电/送电、停机、拆装、旁路、短接、复位或安全联锁时，提醒按现场规程由有资质人员操作并核对原件。
5. **自然友好**：如果知识库里确实没有对应资料，如实说明并给出下一步排查或补充信息建议；如果用户在闲聊，轻松回应并引导回设备运维主题。"""

OPENAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_device_profile",
            "description": "获取当前设备的型号、批次、位置以及已关联的适用手册/案例清单与示例故障码。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_fault_code",
            "description": "按故障码精确或规范化检索手册中的故障定义、原因与排查步骤。",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "故障码，例如 F30003、E101、4110"},
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": "使用 BM25 + 向量语义混合检索知识库片段，支持关键词改写和按类别过滤。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索查询语句或改写后的关键词"},
                    "category": {
                        "type": "string",
                        "enum": ["", "manual", "case", "workorder"],
                        "description": "可选过滤：manual=维修手册，case=故障案例，workorder=历史工单，空串=全部",
                    },
                    "top_k": {"type": "integer", "description": "返回片段数（默认 6）"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_similar_cases",
            "description": "检索当前设备相关的历史故障案例与历史工单，用于对照原因与处置经验。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symptom": {"type": "string", "description": "故障现象、部位或故障码"},
                    "top_k": {"type": "integer", "description": "返回条数（默认 4）"},
                },
                "required": ["symptom"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_chunk_context",
            "description": "读取指定切片的相邻前后切片，用于补全跨切片的维修步骤或参数表。",
            "parameters": {
                "type": "object",
                "properties": {
                    "chunk_id": {"type": "string", "description": "前序检索返回的 chunk id"},
                    "window": {"type": "integer", "description": "前后扩展切片数量（1~2）"},
                },
                "required": ["chunk_id"],
            },
        },
    },
]

_TOOL_LABELS = {
    "get_device_profile": "读取设备档案与资料清单",
    "lookup_fault_code": "检索故障码",
    "search_knowledge": "混合检索知识库",
    "search_similar_cases": "检索历史案例与工单",
    "read_chunk_context": "扩展上下文切片",
}

_GREETING_RE = re.compile(
    r"^(?:你好|您好|哈喽|嗨|在吗|在不在|早上好|上午好|中午好|下午好|晚上好|"
    r"你是谁|介绍一下|你能干嘛|你能做什么|帮帮我|谢谢|感谢|辛苦了|好的|收到|ok|hi|hello|hey)[啊呀呢吗！!。.?？\s]*$",
    re.I,
)


class _EvidencePool:
    """Maintains deduplicated, stably numbered [1..N] evidence chunks collected during ReAct."""

    def __init__(self):
        self.items: list[dict] = []
        self._index: dict[str, int] = {}

    def add_many(self, chunks: list[dict]) -> list[tuple[int, dict]]:
        added = []
        for item in chunks:
            cid = item["id"]
            if cid not in self._index:
                self._index[cid] = len(self.items) + 1
                self.items.append(item)
            added.append((self._index[cid], item))
        return added

    def ids(self) -> list[str]:
        return [item["id"] for item in self.items]

    def format_for_llm(self, max_items: int = 10) -> str:
        lines = []
        for idx, item in enumerate(self.items[:max_items], start=1):
            kind = KINDS.get(item.get("category", "manual"), "资料")
            text = redact(item.get("text", ""))[:1400]
            lines.append(
                f"[{idx}] 《{item['title']}》（{kind} · V{item['version']} · {item['position']} · chunk_id={item['id']}）\n{text}"
            )
        return "\n\n".join(lines)


def _execute_tool(db, org: str, device: dict, settings: dict, pool: _EvidencePool,
                  name: str, args: dict, *, preview_source=None) -> tuple[str, dict]:
    top_k = max(2, min(12, int(args.get("top_k") or settings.get("topK", 6))))
    ambiguous = False

    if name == "get_device_profile":
        docs, ambiguous = list_applicable_sources(db, org, device, preview_source=preview_source)
        payload = {
            "device": {k: device.get(k, "") for k in ("number", "name", "model", "batch", "location")},
            "ambiguousScope": ambiguous,
            "sources": docs,
        }
        summary = f"设备 {device.get('model', '')} 关联 {len(docs)} 份适用资料" + ("（存在批次未补全冲突）" if ambiguous else "")
        return json.dumps(payload, ensure_ascii=False), {"summary": summary, "hitCount": len(docs), "ambiguous": ambiguous}

    if name == "lookup_fault_code":
        code = str(args.get("code", "")).strip()
        hits, ambiguous = lookup_fault_code(db, org, device, code, settings, top_k=top_k, preview_source=preview_source)
        numbered = pool.add_many(hits)
        obs = _format_tool_hits(numbered, ambiguous)
        summary = f"故障码 {code} 命中 {len(hits)} 个切片" if hits else f"未检索到故障码 {code} 的直接记录"
        return obs, {"summary": summary, "hitCount": len(hits), "ambiguous": ambiguous}

    if name == "search_similar_cases":
        symptom = str(args.get("symptom", "")).strip()
        cases, amb1 = hybrid_search(db, org, device, symptom, settings, category="case", top_k=top_k, preview_source=preview_source)
        wos, amb2 = hybrid_search(db, org, device, symptom, settings, category="workorder", top_k=max(2, top_k // 2), preview_source=preview_source)
        ambiguous = amb1 or amb2
        hits = (cases + wos)[:top_k]
        numbered = pool.add_many(hits)
        obs = _format_tool_hits(numbered, ambiguous)
        summary = f"召回 {len(hits)} 条历史案例/工单" if hits else "未找到匹配的历史案例或工单"
        return obs, {"summary": summary, "hitCount": len(hits), "ambiguous": ambiguous}

    if name == "read_chunk_context":
        chunk_id = str(args.get("chunk_id", "")).strip()
        window = max(1, min(2, int(args.get("window") or 1)))
        hits = get_chunk_context(db, org, device, chunk_id, window=window, preview_source=preview_source)
        numbered = pool.add_many(hits)
        obs = _format_tool_hits(numbered, False)
        summary = f"扩展读取 {len(hits)} 个连续上下文切片" if hits else "未找到指定切片的邻域上下文"
        return obs, {"summary": summary, "hitCount": len(hits), "ambiguous": False}

    query = str(args.get("query", "")).strip()
    cat = str(args.get("category", "")).strip() or None
    hits, ambiguous = hybrid_search(db, org, device, query, settings, category=cat, top_k=top_k, preview_source=preview_source)
    numbered = pool.add_many(hits)
    obs = _format_tool_hits(numbered, ambiguous)
    summary = f"混合检索命中 {len(hits)} 个相关片段" if hits else "本轮检索未命中相关片段"
    return obs, {"summary": summary, "hitCount": len(hits), "ambiguous": ambiguous}


def _format_tool_hits(numbered: list[tuple[int, dict]], ambiguous: bool) -> str:
    if ambiguous:
        return "【提示】当前设备型号存在按批次区分的资料，但设备台账缺少批次信息，无法确定适用版本。"
    if not numbered:
        return "未命中任何片段。如果是设备故障或参数问题，可尝试改写关键词重试；如果是日常对话则无需检索。"
    parts = []
    for ref_no, item in numbered:
        kind = KINDS.get(item.get("category", "manual"), "资料")
        score_info = f"score={item.get('score', 0)}, vec={item.get('vectorSim', 0)}, bm25={item.get('bm25', 0)}"
        parts.append(
            f"[{ref_no}] chunk_id={item['id']} | 《{item['title']}》（{kind} · {item['position']} · {score_info}）\n"
            f"{redact(item.get('text', ''))[:900]}"
        )
    return "\n\n".join(parts)


def _rewrite_query_local(question: str, device: dict) -> str:
    cleaned = re.sub(r"(?:请问|怎么|如何|怎样|怎么办|为什么|什么意思|排查|处理|维修|步骤|方法|一下|呢|吗|[？?！!。，,])", " ", question)
    cleaned = " ".join(cleaned.split())
    model = device.get("model", "")
    return f"{model} {cleaned}".strip() if cleaned and cleaned != question else f"{model} 故障 排查"


def _local_greeting_reply(db, org: str, device: dict, question: str) -> str:
    docs, _ = list_applicable_sources(db, org, device)
    dev_name = f"{device.get('name', '当前设备')}（{device.get('model', '')}）"
    if re.search(r"谢谢|感谢|辛苦|好的|收到|ok", question, re.I):
        return f"不客气！如果 {dev_name} 后续还有故障码、异常现象或参数需要核对，随时告诉我。"
    doc_hint = f"目前这台设备已关联 {len(docs)} 份资料" if docs else "目前这台设备还没有导入适用手册"
    codes = [c for d in docs for c in d.get("sampleFaultCodes", [])][:4]
    code_hint = f"（例如 {'、'.join(codes)}）" if codes else ""
    return (
        f"你好！我是知维设备维修助手，当前正在为你查看 **{dev_name}**。\n\n"
        f"{doc_hint}{code_hint}。你可以直接把面板故障码、异常现象或想查的参数发给我，我来帮你结合手册和历史案例分析排查。"
    )


def _synthesize_local_answer(question: str, device: dict, pool: _EvidencePool, ambiguous: bool) -> tuple[str, str]:
    if ambiguous:
        return (
            "当前型号存在按批次区分的资料，但设备台账尚未填写批次。请核对设备铭牌并联系管理员补全批次后再查询。",
            "clarify",
        )
    if not pool.items:
        return (
            f"在 **{device.get('name', '')}（{device.get('model', '')}）** 当前关联的资料里，暂时没有查到和这句话直接匹配的内容。\n\n"
            "如果你正在排查设备问题，可以补充具体的**故障码、面板提示或现场异常现象**，我再帮你继续查；如果需要人工支持，也可以直接转交班组专家。",
            "no_evidence",
        )

    manuals = [(i + 1, item) for i, item in enumerate(pool.items) if item.get("category") == "manual"]
    cases = [(i + 1, item) for i, item in enumerate(pool.items) if item.get("category") != "manual"]
    safety_hit = bool(SAFETY.search(question) or any(SAFETY.search(item.get("text", "")) for _, item in manuals[:3]))

    sections = []
    device_line = f"适用设备：{device.get('name', '')}（{device.get('model', '')}" + (
        f" · 批次 {device['batch']}）" if device.get("batch") else "）"
    )

    if manuals:
        lead_lines = []
        for ref_no, item in manuals[:3]:
            excerpt = redact(item.get("text", "")).strip()
            if len(excerpt) > 550:
                excerpt = excerpt[:550].rstrip() + "…"
            lead_lines.append(f"**依据 [{ref_no}]（{item['title']} · {item['position']}）**\n{excerpt}")
        sections.append("### 手册依据与排查要点\n\n" + "\n\n".join(lead_lines))

    if cases:
        case_lines = []
        for ref_no, item in cases[:2]:
            excerpt = redact(item.get("text", "")).strip()
            if len(excerpt) > 360:
                excerpt = excerpt[:360].rstrip() + "…"
            case_lines.append(f"- **[{ref_no}] {item['title']}**（{item['position']}）：\n  {excerpt}")
        sections.append("### 历史案例对照（仅供本次排查参考）\n" + "\n".join(case_lines))

    if safety_hit:
        sections.append(
            "### 安全提醒\n"
            "涉及停机、断电/送电、高压带电、拆装或联锁复位等安全关键操作时，请由有资质人员对照原件手册与现场安全规程执行。"
        )

    sections.append(device_line)
    decision = "safety" if safety_hit else "answer"
    return "\n\n".join(sections), decision


def _chunk_stream_text(text: str, step: int = 18) -> Iterator[str]:
    for i in range(0, len(text), step):
        yield text[i:i + step]


def run_react_agent_stream(db, org: str, device: dict, question: str, *,
                           prior_turns: list[dict] | None = None,
                           preview_source=None) -> Iterator[dict]:
    """Execute the standard ReAct loop (Thought -> Action -> Observation -> Final Streaming Answer).
    Handles natural conversation gracefully while grounding technical/fault questions via RAG tools."""
    settings = get_ai_settings(db, org)
    max_steps = max(1, min(8, int(settings.get("maxReactSteps", 5))))
    use_llm = chat_ready(settings) and preview_source is None
    engine = "llm_react" if use_llm else "local_react"

    yield {"type": "meta", "engine": engine, "model": settings.get("chatModel", "") if use_llm else "local-hybrid-react"}

    pool = _EvidencePool()
    react_steps: list[dict] = []
    ambiguous = False
    model_degraded = False

    def record_step(step_no: int, phase: str, **payload) -> dict:
        entry = {"step": step_no, "phase": phase, **payload}
        react_steps.append(entry)
        return {"type": "step", **entry}

    thought_entries: dict[str, dict] = {}

    def stream_thought_delta(step_no: int, thought_id: str, delta: str) -> dict | None:
        if not delta:
            return None
        entry = thought_entries.get(thought_id)
        if entry is None:
            entry = {"step": step_no, "phase": "thought", "thoughtId": thought_id, "content": ""}
            thought_entries[thought_id] = entry
            react_steps.append(entry)
        entry["content"] += delta
        return {"type": "thought_delta", "step": step_no, "thoughtId": thought_id, "delta": delta}

    if use_llm:
        history_messages: list[dict] = []
        for prev in (prior_turns or [])[-6:]:
            if prev.get("text"):
                history_messages.append({"role": "user", "content": redact(prev["text"])[:800]})
            if prev.get("answer"):
                history_messages.append({"role": "assistant", "content": redact(prev["answer"])[:800]})

        device_ctx = (
            f"当前选中设备：{device.get('name', '')} | 编号：{device.get('number', '')} | "
            f"型号：{device.get('model', '')} | 批次：{device.get('batch') or '未填写'} | "
            f"位置：{device.get('location') or '未填写'}"
        )
        messages: list[dict] = [
            {"role": "system", "content": REACT_SYSTEM_PROMPT},
            *history_messages,
            {"role": "user", "content": f"{device_ctx}\n\n用户消息：{redact(question)}"},
        ]

        tool_loop_ok = True
        direct_reply = ""

        for step_no in range(1, max_steps + 1):
            thought_id = f"loop-{step_no}"
            content = ""
            reasoning_text = ""
            raw_reasoning = ""
            tool_calls: list[dict] = []
            try:
                finished = None
                for ev in chat_completion_stream_events(
                    settings, messages, tools=OPENAI_TOOLS, temperature=0.25, max_tokens=1400
                ):
                    if ev["type"] == "reasoning":
                        pkt = stream_thought_delta(step_no, thought_id, ev["delta"])
                        if pkt:
                            yield pkt
                    elif ev["type"] == "finish":
                        finished = ev
                if finished is None:
                    raise ValueError("empty stream")
                content = finished["content"]
                reasoning_text = finished["reasoning"]
                raw_reasoning = finished["raw_reasoning"]
                tool_calls = finished["tool_calls"]
            except httpx.HTTPStatusError as exc:
                if exc.response is not None and exc.response.status_code in {400, 422}:
                    # Some older OpenAI-compatible gateways reject stream=true with tools; fallback cleanly.
                    try:
                        resp = chat_completion(settings, messages, tools=OPENAI_TOOLS, temperature=0.25, max_tokens=1400)
                        msg = ((resp.get("choices") or [{}])[0]).get("message") or {}
                        content, reasoning_text, raw_reasoning = extract_thinking_text(msg)
                        tool_calls = msg.get("tool_calls") or []
                        if reasoning_text:
                            for delta in _chunk_stream_text(reasoning_text, step=24):
                                pkt = stream_thought_delta(step_no, thought_id, delta)
                                if pkt:
                                    yield pkt
                    except (httpx.HTTPError, KeyError, ValueError, TypeError, RuntimeError) as inner_exc:
                        tool_loop_ok = False
                        model_degraded = True
                        yield record_step(step_no, "thought", content=f"模型工具调用异常（{type(inner_exc).__name__}），切换为本地增强检索")
                        break
                else:
                    tool_loop_ok = False
                    model_degraded = True
                    yield record_step(step_no, "thought", content=f"模型工具调用异常（{type(exc).__name__}），切换为本地增强检索")
                    break
            except (httpx.HTTPError, KeyError, ValueError, TypeError, RuntimeError) as exc:
                tool_loop_ok = False
                model_degraded = True
                yield record_step(step_no, "thought", content=f"模型工具调用异常（{type(exc).__name__}），切换为本地增强检索")
                break

            if not tool_calls:
                # No more tool calls: content IS the assistant's direct reply (reasoning already streamed to thought)
                direct_reply = content
                break

            # Emit brief pre-tool content as a thought if present and distinct from reasoning stream
            if content and content != reasoning_text:
                yield record_step(step_no, "thought", content=content)

            assistant_msg: dict = {
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls,
            }
            if raw_reasoning:
                assistant_msg["reasoning_content"] = raw_reasoning
            messages.append(assistant_msg)

            for call in tool_calls[:3]:
                fn = call.get("function") or {}
                tool_name = fn.get("name", "search_knowledge")
                raw_args = fn.get("arguments", "{}")
                try:
                    parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                except ValueError:
                    parsed_args = {"query": question}
                if not isinstance(parsed_args, dict):
                    parsed_args = {"query": question}

                yield record_step(
                    step_no, "action",
                    tool=tool_name,
                    label=_TOOL_LABELS.get(tool_name, tool_name),
                    args=parsed_args,
                )
                obs_text, meta = _execute_tool(db, org, device, settings, pool, tool_name, parsed_args,
                                               preview_source=preview_source)
                ambiguous = ambiguous or meta.get("ambiguous", False)
                yield record_step(step_no, "observation", summary=meta["summary"], hitCount=meta["hitCount"])
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", f"call_{step_no}"),
                    "content": obs_text,
                })

        # Fallback retrieval only if the LLM tool call itself crashed
        if not tool_loop_ok and not pool.items and not _GREETING_RE.match(question.strip()):
            step_no = len(react_steps) + 1
            yield record_step(step_no, "action", tool="search_knowledge",
                              label=_TOOL_LABELS["search_knowledge"], args={"query": question})
            _, meta = _execute_tool(db, org, device, settings, pool, "search_knowledge",
                                    {"query": question}, preview_source=preview_source)
            ambiguous = ambiguous or meta.get("ambiguous", False)
            yield record_step(step_no, "observation", summary=meta["summary"], hitCount=meta["hitCount"])

        # Decide final output cleanly:
        # 1) If knowledge chunks were retrieved -> stream grounded RAG synthesis (including model thinking stream!)
        # 2) If conversational / no chunks needed and LLM already produced a direct reply -> stream that reply!
        # 3) If loop ended without direct_reply and without chunks -> stream natural LLM response (including thinking!)
        if ambiguous:
            answer, decision = _synthesize_local_answer(question, device, pool, True)
            for delta in _chunk_stream_text(answer):
                yield {"type": "token", "delta": delta}
        elif pool.items and tool_loop_ok:
            synth_messages = [
                {"role": "system", "content": SYNTHESIS_SYSTEM_PROMPT},
                *history_messages,
                {
                    "role": "user",
                    "content": (
                        f"{device_ctx}\n\n"
                        f"用户消息：{redact(question)}\n\n"
                        f"已检索并核验的知识库证据：\n\n{pool.format_for_llm(10)}\n\n"
                        "请综合以上证据自然、清晰地回答，并在关键依据后标注 `[1]`、`[2]` 等引用编号。"
                    ),
                },
            ]
            chunks_out: list[str] = []
            synth_step = len(react_steps) + 1
            synth_thought_id = f"synth-{synth_step}"
            try:
                for ev in chat_completion_stream_events(settings, synth_messages, temperature=0.25, max_tokens=2000):
                    if ev["type"] == "reasoning":
                        pkt = stream_thought_delta(synth_step, synth_thought_id, ev["delta"])
                        if pkt:
                            yield pkt
                    elif ev["type"] == "content":
                        chunks_out.append(ev["delta"])
                        yield {"type": "token", "delta": ev["delta"]}
                answer = "".join(chunks_out).strip()
                if not answer:
                    raise ValueError("empty stream")
                safety_hit = bool(SAFETY.search(question) or any(SAFETY.search(item.get("text", "")) for item in pool.items[:3]))
                decision = "safety" if safety_hit else "answer"
            except (httpx.HTTPError, ValueError, RuntimeError):
                model_degraded = True
                answer, decision = _synthesize_local_answer(question, device, pool, False)
                for delta in _chunk_stream_text(answer):
                    yield {"type": "token", "delta": delta}
        elif direct_reply:
            answer = direct_reply
            decision = "answer"
            for delta in _chunk_stream_text(answer, step=16):
                yield {"type": "token", "delta": delta}
        elif tool_loop_ok:
            fallback_messages = [
                {"role": "system", "content": SYNTHESIS_SYSTEM_PROMPT},
                *history_messages,
                {
                    "role": "user",
                    "content": (
                        f"{device_ctx}\n\n"
                        f"用户消息：{redact(question)}\n\n"
                        "（本轮未从知识库召回直接匹配片段。如果是日常交流或离题闲聊，请自然回应并引导回设备维修；"
                        "如果是具体设备故障/参数问题，请如实说明知识库暂未收录并引导用户补充信息或核对手册。）"
                    ),
                },
            ]
            chunks_out = []
            fb_step = len(react_steps) + 1
            fb_thought_id = f"fallback-{fb_step}"
            try:
                for ev in chat_completion_stream_events(settings, fallback_messages, temperature=0.35, max_tokens=1200):
                    if ev["type"] == "reasoning":
                        pkt = stream_thought_delta(fb_step, fb_thought_id, ev["delta"])
                        if pkt:
                            yield pkt
                    elif ev["type"] == "content":
                        chunks_out.append(ev["delta"])
                        yield {"type": "token", "delta": ev["delta"]}
                answer = "".join(chunks_out).strip()
                decision = "answer"
            except (httpx.HTTPError, ValueError, RuntimeError):
                model_degraded = True
                answer, decision = _synthesize_local_answer(question, device, pool, False)
                for delta in _chunk_stream_text(answer):
                    yield {"type": "token", "delta": delta}
        else:
            answer, decision = _synthesize_local_answer(question, device, pool, False)
            for delta in _chunk_stream_text(answer):
                yield {"type": "token", "delta": delta}
    else:
        # Local deterministic multi-step ReAct loop (when no external Chat LLM is configured)
        if _GREETING_RE.match(question.strip()) and preview_source is None:
            answer = _local_greeting_reply(db, org, device, question)
            decision = "answer"
            for delta in _chunk_stream_text(answer, step=16):
                yield {"type": "token", "delta": delta}
        else:
            codes = sorted(fault_codes(question, device))
            step_no = 1
            if codes:
                yield record_step(step_no, "thought", content=f"识别到故障码 {'、'.join(codes)}，优先按故障码精确检索适用手册与索引。")
                for code in codes[:2]:
                    yield record_step(step_no, "action", tool="lookup_fault_code",
                                      label=_TOOL_LABELS["lookup_fault_code"], args={"code": code})
                    _, meta = _execute_tool(db, org, device, settings, pool, "lookup_fault_code",
                                            {"code": code}, preview_source=preview_source)
                    ambiguous = ambiguous or meta.get("ambiguous", False)
                    yield record_step(step_no, "observation", summary=meta["summary"], hitCount=meta["hitCount"])
            else:
                yield record_step(step_no, "thought", content="启动 BM25 + 向量混合语义检索。")
                yield record_step(step_no, "action", tool="search_knowledge",
                                  label=_TOOL_LABELS["search_knowledge"], args={"query": question})
                _, meta = _execute_tool(db, org, device, settings, pool, "search_knowledge",
                                        {"query": question}, preview_source=preview_source)
                ambiguous = ambiguous or meta.get("ambiguous", False)
                yield record_step(step_no, "observation", summary=meta["summary"], hitCount=meta["hitCount"])

            if not ambiguous and max_steps >= 2:
                step_no = 2
                if not pool.items:
                    rewritten = _rewrite_query_local(question, device)
                    yield record_step(step_no, "thought", content=f"首轮未命中直接片段，尝试改写查询词为「{rewritten}」扩大召回。")
                    yield record_step(step_no, "action", tool="search_knowledge",
                                      label=_TOOL_LABELS["search_knowledge"], args={"query": rewritten})
                    _, meta = _execute_tool(db, org, device, settings, pool, "search_knowledge",
                                            {"query": rewritten}, preview_source=preview_source)
                    yield record_step(step_no, "observation", summary=meta["summary"], hitCount=meta["hitCount"])
                else:
                    top_manual = next((item for item in pool.items if item.get("category") == "manual"), None)
                    if top_manual and len(top_manual.get("text", "")) < 450:
                        yield record_step(step_no, "thought", content="首轮命中核心手册片段，扩展相邻上下文切片以防步骤截断。")
                        yield record_step(step_no, "action", tool="read_chunk_context",
                                          label=_TOOL_LABELS["read_chunk_context"], args={"chunk_id": top_manual["id"], "window": 1})
                        _, meta = _execute_tool(db, org, device, settings, pool, "read_chunk_context",
                                                {"chunk_id": top_manual["id"], "window": 1}, preview_source=preview_source)
                        yield record_step(step_no, "observation", summary=meta["summary"], hitCount=meta["hitCount"])
                        step_no += 1

                    has_case = any(item.get("category") != "manual" for item in pool.items)
                    if not has_case and step_no <= max_steps and preview_source is None:
                        yield record_step(step_no, "thought", content="进一步检索该设备的历史故障案例与工单，提供现场对照经验。")
                        yield record_step(step_no, "action", tool="search_similar_cases",
                                          label=_TOOL_LABELS["search_similar_cases"], args={"symptom": question[:80]})
                        _, meta = _execute_tool(db, org, device, settings, pool, "search_similar_cases",
                                                {"symptom": question[:80], "top_k": 3}, preview_source=preview_source)
                        yield record_step(step_no, "observation", summary=meta["summary"], hitCount=meta["hitCount"])

            answer, decision = _synthesize_local_answer(question, device, pool, ambiguous)
            for delta in _chunk_stream_text(answer):
                yield {"type": "token", "delta": delta}

    used_indices = {int(m) for m in re.findall(r"\[(\d+)\]", answer)}
    if pool.items and used_indices:
        max_ref = min(len(pool.items), max(used_indices))
        ref_ids = [item["id"] for item in pool.items[:max_ref]] if max_ref > 0 else pool.ids()[:8]
    elif pool.items:
        ref_ids = pool.ids()[:8]
    else:
        ref_ids = []

    yield {
        "type": "complete",
        "result": {
            "decision": decision,
            "answer": answer,
            "referenceIds": ref_ids,
            "reactSteps": react_steps,
            "engine": engine,
            "modelDegraded": model_degraded,
            "hits": pool.items[:10],
            "requestedCodes": sorted(fault_codes(question, device)),
        },
    }


def run_react_agent(db, org: str, device: dict, question: str, *,
                    prior_turns: list[dict] | None = None, preview_source=None) -> dict:
    """Synchronous wrapper around run_react_agent_stream returning the final turn result."""
    final = None
    for event in run_react_agent_stream(db, org, device, question,
                                        prior_turns=prior_turns, preview_source=preview_source):
        if event["type"] == "complete":
            final = event["result"]
    return final or {
        "decision": "no_evidence",
        "answer": "当前未检索到可用依据。",
        "referenceIds": [],
        "reactSteps": [],
        "engine": "local_react",
        "modelDegraded": False,
        "hits": [],
        "requestedCodes": [],
    }
