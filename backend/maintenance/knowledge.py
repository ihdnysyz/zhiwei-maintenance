import csv
import io
import re
import subprocess
from collections import Counter, defaultdict
from math import isfinite
from pathlib import Path
from time import sleep
from unicodedata import normalize
from urllib.parse import urlsplit
from zipfile import ZipFile

import httpx
from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader
from pgvector.sqlalchemy import Vector
from sqlalchemy import func, select, type_coerce
from sqlalchemy.orm import defer

from .db import Chunk, Record, Scope, cfg, current_scopes, digest, find, records, set_org

KINDS = {"manual": "维修手册", "workorder": "历史工单", "case": "故障案例"}
CODES = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{1,5}[-_]?\d{2,6}(?![A-Za-z0-9])")
CODE_LABEL = r"故障代码|报警代码|告警代码|故障码|报警码|告警码|错误码|fault code|alarm code|error code"
LABELLED_CODE = re.compile(
    rf"(?:{CODE_LABEL})[ \t]*(?:[：:]|为)?[ \t]*((?:[A-Za-z]{{1,5}}[-_]?)?\d{{1,6}})(?![A-Za-z0-9./-])", re.I
)
SAFETY = re.compile(
    r"停机|送电|断电|带电|拆卸|拆装|高压|旁路|短接|复位|急停|隔离电|安全联锁|重启|合闸"
    r"|\b(?:energized|energised|live circuit|high voltage|lockout|bypass|short[- ]circuit|disassembl\w*|"
    r"emergency stop|reset|restart|shut\s*down|power (?:off|on)|safety interlock)\b", re.I
)
MODEL_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")


def model_approval_endpoint():
    if not (cfg.model_source_review and cfg.allow_model_data and cfg.model_url and cfg.chat_model):
        return "", ""
    endpoint = cfg.model_url.rstrip("/")
    host = urlsplit(endpoint).hostname or ""
    return (host, digest(endpoint.encode())) if host else ("", "")


def model_evidence(db, org, candidates, primary):
    if not (cfg.allow_model_data and cfg.chat_model):
        return []
    leading = next((item for item in candidates if item["category"] == primary), None)
    if leading is None:
        return []
    if not cfg.model_source_review:
        return candidates

    _, endpoint_key = model_approval_endpoint()
    allowed = {value.strip() for value in cfg.model_demo_source_ids.split(",") if value.strip()} if cfg.model_demo_only else set()
    allowed.update(item["sourceId"] for item in candidates if item["demo"])
    pending = {item["sourceId"] for item in candidates} - allowed
    if endpoint_key and pending:
        for source in db.scalars(records(org, "source").where(Record.id.in_(pending))):
            if (source.data.get("approvedModelEndpoint") == endpoint_key and source.data.get("digest")
                    and source.data.get("approvedDigest") == source.data["digest"]):
                allowed.add(source.id)
    if leading["sourceId"] not in allowed:
        return []
    return [item for item in candidates if item["sourceId"] in allowed]


def parse_file(filename, raw, category="manual"):
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        return parse_pdf(raw)
    if suffix == ".docx":
        with ZipFile(io.BytesIO(raw)) as archive:
            if sum(item.file_size for item in archive.infolist()) > 80 * 1024 * 1024:
                raise ValueError("Word 解压后超过 80 MiB")
        doc = Document(io.BytesIO(raw))
        blocks, position = [], "文档正文"
        for block in doc.iter_inner_content():
            if isinstance(block, Paragraph):
                if block.style.name.startswith(("Heading", "标题")):
                    position = block.text.strip()[:180] or position
                text = block.text
            elif isinstance(block, Table):
                text = "\n".join(" | ".join(cell.text for cell in row.cells) for row in block.rows)
            else:
                continue
            if text.strip():
                if not blocks or position != blocks[-1][0]:
                    blocks.append([position, text, False])
                else:
                    blocks[-1][1] += "\n" + text
        return blocks
    if suffix in {".txt", ".md", ".csv"}:
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                content = raw.decode(encoding)
                if suffix == ".csv":
                    return parse_csv(content, category)
                return [["文本正文", content, False]]
            except UnicodeDecodeError:
                pass
        raise ValueError("无法识别文本编码")
    raise ValueError("仅支持 PDF、DOCX、TXT、MD、CSV")


def parse_csv(content, category):
    reader = csv.DictReader(io.StringIO(content))
    if not reader.fieldnames or len(reader.fieldnames) > 60:
        raise ValueError("CSV 需要字段表头，最多 60 列")
    keys = {key.strip().lower(): key for key in reader.fieldnames}
    identifiers = ("单号", "工单号", "案例编号", "案例号", "number", "id", "ticket_id", "case_id")
    number_key = next((keys[key] for key in identifiers if key in keys), None)
    if category != "manual" and not number_key:
        raise ValueError("历史工单或案例 CSV 需要「工单号」或「案例编号」列")
    blocks = []
    for index, row in enumerate(reader, start=2):
        if None in row:
            raise ValueError(f"CSV 第 {index} 行多于表头列数")
        number = (row.get(number_key) or "").strip()
        if number_key and not number:
            raise ValueError(f"CSV 第 {index} 行缺少单号或编号")
        body = "\n".join(f"{field.strip()}：{value.strip()}" for field, value in row.items() if value and value.strip())
        blocks.append((f"记录 {number}" if number else f"第 {index} 行", body, False))
    return blocks


def parse_pdf(raw):
    from pymupdf import open as open_pdf

    reader = PdfReader(io.BytesIO(raw))
    if reader.is_encrypted:
        raise ValueError("请先解密 PDF")
    if len(reader.pages) > 500:
        raise ValueError("单份 PDF 最多 500 页")
    blocks = []
    with open_pdf(stream=raw, filetype="pdf") as rendered:
        for index, page in enumerate(reader.pages):
            content = (page.extract_text(extraction_mode="layout") or "").strip()
            ocr = len(re.sub(r"\s", "", content)) < 25
            if ocr:
                pix = rendered[index].get_pixmap(dpi=240)
                try:
                    process = subprocess.run(
                        ["tesseract", "stdin", "stdout", "-l", cfg.ocr_lang],
                        input=pix.tobytes("png"), capture_output=True, check=True, timeout=90,
                    )
                    content = process.stdout.decode("utf-8").strip()
                except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                    raise ValueError(f"第 {index + 1} 页 OCR 失败，请检查 Tesseract 和语言包") from exc
                if not content:
                    raise ValueError(f"第 {index + 1} 页无可辨认文字，请重新扫描")
            blocks.append((f"第 {index + 1} 页", content, ocr))
    return blocks


def split_blocks(blocks):
    for position, body, ocr in blocks:
        body = body.strip()
        if not body:
            continue
        if len(body) <= 800 or position.startswith("记录 ") or re.search(r"(?m)^\s*(?:\d+[.、)]|步骤\s*\d+)", body):
            yield position[:180], body, ocr
            continue
        parts = re.split(r"(?<=[。！？；\n])", body)
        buffer = ""
        for part in parts:
            while len(part) > 700:
                if buffer:
                    yield position[:180], buffer, ocr
                yield position[:180], part[:700], ocr
                part = part[700:]
                buffer = ""
            if len(buffer) + len(part) > 800:
                yield position[:180], buffer, ocr
                tail = buffer[-100:]
                buffer = tail + part
            else:
                buffer += part
        if buffer.strip():
            yield position[:180], buffer, ocr


def model_call(endpoint, payload, *, connection_test=False):
    if not cfg.model_url or not (cfg.allow_model_data or connection_test):
        raise RuntimeError("模型数据传输未启用")
    if urlsplit(cfg.model_url).hostname == "api.deepseek.com":
        if not cfg.model_key:
            raise RuntimeError("DeepSeek API Key 未配置")
        if endpoint == "chat/completions":
            payload = {**payload, "thinking": {"type": "disabled"}}
    headers = {"Authorization": f"Bearer {cfg.model_key}"} if cfg.model_key else {}
    url = f"{cfg.model_url.rstrip('/')}/{endpoint}"
    attempts = 2 if endpoint == "chat/completions" and not connection_test else 1
    for attempt in range(attempts):
        try:
            response = httpx.post(url, headers=headers, json=payload, timeout=12, follow_redirects=False)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            if attempt + 1 == attempts or exc.response.status_code not in {408, 502, 503, 504}:
                raise
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
            if attempt + 1 == attempts:
                raise
        sleep(0.25)


def embeddings(texts):
    if not cfg.embedding_model:
        return [None] * len(texts)
    data = model_call("embeddings", {"model": cfg.embedding_model, "input": texts})["data"]
    embeddings = [row["embedding"] for row in sorted(data, key=lambda row: row["index"])]
    if len(embeddings) != len(texts):
        raise ValueError("向量服务返回数量不一致")
    for vector in embeddings:
        if (not isinstance(vector, list) or not 1 <= len(vector) <= 16000
                or (cfg.embedding_dimensions and len(vector) != cfg.embedding_dimensions)
                or any(type(x) not in {int, float} or not isfinite(x) for x in vector)
                or not any(vector)):
            raise ValueError("向量服务返回无效向量")
    return embeddings


def applicability(db, org, device):
    scopes = defaultdict(dict)
    for row in db.scalars(current_scopes(org).where(Scope.model.in_({"*", device["model"]}))):
        scopes[row.number][(row.model, row.batch)] = row
    ids, ambiguous = {}, False
    for family in scopes.values():
        model, batch = device["model"], device.get("batch", "")
        if not batch and any(selector for _, selector in family):
            ambiguous = True
            continue
        for rank, selector in enumerate([(model, batch), (model, ""), ("*", batch), ("*", "")]):
            if selector in family:
                row = family[selector]
                if row.status == "bind":
                    ids[row.source] = rank
                break
    return ids, ambiguous


def terms(value):
    value = value.lower()
    parts = re.findall(r"[a-z0-9_/-]+|[\u4e00-\u9fff]+", value)
    tokens = []
    for part in parts:
        if re.fullmatch(r"[\u4e00-\u9fff]{2,}", part):
            tokens.extend(part[i:i+2] for i in range(len(part)-1))
        else:
            tokens.append(part)
    return Counter(tokens)


def codes_in(value):
    value = normalize("NFKC", value).translate(str.maketrans("–—−", "---"))
    return {match.upper() for pattern in (CODES, LABELLED_CODE) for match in pattern.findall(value)}


def code_key(code):
    return code.replace("-", "").replace("_", "")


def fault_codes(value, device):
    identifiers = codes_in(f"{device.get('number', '')} {device.get('model', '')}")
    device_keys = {code_key(code) for code in identifiers}
    return {code for code in codes_in(value) if code_key(code) not in device_keys}


def retrieve(db, org, device, query, limit=6):
    set_org(db, org)
    mentioned = {token.lower().rstrip(".") for token in MODEL_TOKEN.findall(query)
                 if any(char.isdigit() for char in token)}
    mentioned.discard(device["model"].lower())
    if mentioned and db.scalar(records(org, "device").where(
            func.lower(Record.data["model"].as_string()).in_(mentioned)).limit(1)):
        return [], False
    ids, ambiguous = applicability(db, org, device)
    if ambiguous or not ids:
        return [], ambiguous
    sources = {r.id: r for r in db.scalars(records(org, "source").where(Record.id.in_(ids)))}
    tokens = terms(query)
    faults = {code_key(code) for code in fault_codes(query, device)}
    matched_faults = set()
    groups = defaultdict(list)
    base = select(Chunk).where(Chunk.org == org, Chunk.source.in_(ids))
    for chunk in db.scalars(base.options(defer(Chunk.embedding))):
        source = sources[chunk.source]
        full = f"{source.data['title']} {chunk.position} {chunk.body}"
        exact = faults.intersection(code_key(code) for code in codes_in(full))
        if faults and not exact:
            continue
        matched_faults.update(exact)
        lexical = sum((tokens & terms(full)).values())
        groups[ids[source.id], source.data["category"] == "manual"].append((chunk, len(exact), lexical))
    if faults - matched_faults:
        return [], False
    semantic = {}
    if cfg.allow_model_data and cfg.embedding_model and not cfg.model_source_review:
        try:
            from .conversations import redact
            vector = embeddings([redact(query)])[0]
            for chunk, distance in semantic_matches(db, vector, ids, org):
                semantic[chunk] = 1 - distance
        except (httpx.HTTPError, ValueError, KeyError, TypeError, RuntimeError):
            pass
    candidates = []
    for (rank, manual), group in groups.items():
        lexical = sorted(group, key=lambda entry: (-entry[1], -entry[2]))
        vector_ids = sorted((entry[0].id for entry in group if entry[0].id in semantic
                             and semantic[entry[0].id] >= cfg.embedding_min_similarity),
                            key=semantic.get, reverse=True)
        scores = {key: 1 / (cfg.rrf_k + 1 + index) for index, key in enumerate(vector_ids)}
        for index, (chunk, exact, matches) in enumerate(lexical):
            if exact or matches or chunk.id in scores:
                score = scores.get(chunk.id, 0) + (1 / (cfg.rrf_k + 1 + index) if matches else 0)
                candidates.append(((-exact, rank, -int(manual), -score), chunk))
    ranked = sorted(candidates, key=lambda candidate: candidate[0])
    seen, output = set(), []
    for _, chunk in ranked:
        source = sources[chunk.source]
        location = (chunk.source, chunk.position, chunk.body)
        if location in seen:
            continue
        seen.add(location)
        output.append(citation(source, chunk))
        if len(output) == limit:
            break
    if output and all(item["category"] == "manual" for item in output):
        historical = next((chunk for _, chunk in ranked if sources[chunk.source].data["category"] != "manual"), None)
        if historical:
            output[-1:] = [citation(sources[historical.source], historical)] if len(output) == limit else [
                output[-1], citation(sources[historical.source], historical)]
    return output, False


def semantic_matches(db, vector, ids, org):
    if db.bind.dialect.name == "postgresql":
        from sqlalchemy import func
        distance = type_coerce(Chunk.embedding, Vector()).cosine_distance(vector)
        return db.execute(select(Chunk.id, distance).where(
            Chunk.org == org, Chunk.source.in_(ids), Chunk.embedding.is_not(None),
            Chunk.embedding_model == cfg.embedding_model, func.vector_dims(Chunk.embedding) == len(vector),
        ).order_by(distance).limit(40)).all()
    from math import sqrt
    norm = sqrt(sum(x*x for x in vector))
    output = []
    rows = db.execute(select(Chunk.id, Chunk.embedding).where(
        Chunk.org == org, Chunk.source.in_(ids), Chunk.embedding.is_not(None),
        Chunk.embedding_model == cfg.embedding_model))
    for chunk_id, embedding in rows:
        if not embedding or len(embedding) != len(vector):
            continue
        denominator = norm * sqrt(sum(x*x for x in embedding))
        if denominator:
            output.append((chunk_id, 1 - sum(x*y for x, y in zip(vector, embedding)) / denominator))
    return output


def citation(source, chunk):
    return {**chunk.view(), "sourceId": source.id, "title": source.data["title"],
            "number": source.data["number"], "version": source.data["version"],
            "category": source.data["category"], "demo": source.data.get("demo", False),
            "referenceUrl": source.data.get("referenceUrl")}


def authorized_citation(db, org, chunk_id, device):
    set_org(db, org)
    chunk = db.scalar(select(Chunk).where(Chunk.org == org, Chunk.id == chunk_id))
    if not chunk:
        return None
    ids, _ = applicability(db, org, device)
    source = find(db, org, "source", id=chunk.source)
    return citation(source, chunk) if source and source.id in ids else None

