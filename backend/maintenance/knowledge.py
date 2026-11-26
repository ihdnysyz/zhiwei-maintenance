import csv
import io
import json
import os
import re
import ssl
import subprocess
import sys
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

from .common import permitted_device
from .db import Chunk, FaultIndex, Record, Scope, cfg, current_scopes, digest, find, records, set_org

KINDS = {"manual": "维修手册", "workorder": "历史工单", "case": "故障案例"}
CODES = re.compile(r"(?<![A-Za-z0-9.])[A-Za-z]{1,5}[-_]?\d{2,6}(?![A-Za-z0-9]|[._-][A-Za-z0-9])")
CODE_LABEL = r"故障代码|报警代码|告警代码|错误代码|故障编号|故障码|报警码|告警码|错误码|故障号|报码|fault code|alarm code|error code"
CODE_TOKEN = r"[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*"
_SOURCE_CODE_TOKEN = r"[A-Za-z0-9Ａ-Ｚａ-ｚ０-９]+(?:[-_.－＿．–—−][A-Za-z0-9Ａ-Ｚａ-ｚ０-９]+)*"
LABELLED_CODE = re.compile(
    rf"(?:{CODE_LABEL})[ \t]*(?:[：:]|为)?[ \t]*({CODE_TOKEN})(?![A-Za-z0-9/_\-]|\.[A-Za-z0-9])", re.I
)
FAULT_SECTION = re.compile(
    rf"(?im)^[ \t]*(?:(?:#{{1,6}}[ \t]+)?(?:\*\*|__|`)?(?:{CODE_LABEL})[ \t]*[：:]?[ \t]*|#{{1,6}}[ \t]+)"
    rf"(?:\*\*|__|`)?(?P<code>{_SOURCE_CODE_TOKEN})(?:\*\*|__|`)?"
    rf"(?![A-Za-z0-9Ａ-Ｚａ-ｚ０-９/_\-－＿]|[.．][A-Za-z0-9Ａ-Ｚａ-ｚ０-９])(?P<name>[^\r\n]*)"
)
_SECTION_EVIDENCE = re.compile(r"故障(?:含义|原因|现象|名称|排查|处理|处置)|(?:排查|处理|处置|维修|修复|解决|检查|排除)(?:步骤|方法|措施|流程|办法)|"
                               r"\b(?:fault meaning|cause|troubleshooting|corrective action|remedy|what to do)\b", re.I)
# A number can be a fault identifier in a question without being one everywhere in a manual.
_CODE_CANDIDATE = re.compile(rf"(?<![A-Za-z0-9./+_\-])({CODE_TOKEN})(?![A-Za-z0-9/_\-]|\.[A-Za-z0-9]|,\d{{3}}(?:\D|$))")
_NUMERIC_FAULT_PREFIX = re.compile(
    r"(?:报(?:了|出|错|的是|的)?|报警(?:为)?|告警(?:为)?|显示(?:了|的是)?|出现(?:了)?|提示|问(?:了|的|的是)?|"
    r"不是[^。！？!?]{0,30}[，,；;]\s*(?:而?是)|(?:不对|更正|改成|应为|正确的是)(?:[，,：:]\s*)?(?:是|为)?|实际(?:是|为)|应该是|"
    r"(?:fault|alarm|error)(?:\s+code)?|(?:shows?|reported)|what\s+is)\s*[:：]?\s*$", re.I
)
_NUMERIC_FAULT_SUFFIX = re.compile(
    r"^\s*(?:[，,：:]\s*)?(?:分别)?(?:的)?(?:是(?:什么|啥|哪(?:一)?[个种项类])(?:故障|问题|错误|告警|报警)?|"
    r"什么意思|什么故障|什么原因|怎么回事|(?:表示|对应|代表)(?:什么|哪)|故障(?:原因|含义|怎么|如何|是什么)?|(?:告警|报警|报错)|"
    r"(?:怎么|如何|怎样)(?:办|查|修|维修|排障|解决|处理|处置|排除|排查|检查|消除)|"
    r"(?:维修|排障|处理|排查|解决)(?:方法|步骤|流程|办法|措施)|(?:有什么|有哪些)(?:解决|维修|处理|排查)(?:方法|办法|措施|步骤)|"
    r"(?:有(?:没有|哪些)?|查询|查找)?(?:类似|历史|过往|已结单)?(?:故障案例|案例|工单|历史记录|复盘)|"
    r"(?:能否|可否|能不能|可以|是否|需要|要不要|能)(?:带电|拆卸|维修|排查|处理|复位|重启|解决)|"
    r"\b(?:means?|what|how|fault|alarm|error)\b)", re.I
)
_NON_FAULT_NUMBER_PREFIX = re.compile(
    r"(?:参数(?:号|编号|值)?|电压|电流|功率|温度|压力|流量|扬程|转速|频率|重量|数量|年份|日期|页码|型号|编号|"
    r"parameter|voltage|current|power|temperature|pressure|flow|speed|frequency|page|model)"
    r"\s*(?:为|是|值|达到|等于|[:：=])?\s*$", re.I
)
_NON_FAULT_NUMBER_UNIT = re.compile(
    r"^\s*(?:%|°|℃|℉|[年月日时秒个台页元件米克伏安瓦吨]|分(?!别)|毫秒|毫米|千[克瓦伏]|"
    r"兆帕|千帕|摄氏度|转/分|m[³3]/h|(?:kW|W|kV|V|mA|A|Hz|kHz|RPM|MPa|kPa|Pa|kg|mm|cm|ms)\b)", re.I
)
_UNIT_CODE = re.compile(r"\d+(?:\.\d+)?(?:kW|W|kV|V|mA|A|Hz|kHz|RPM|MPa|kPa|Pa|kg|mm|cm|ms)", re.I)
_NUMBER_LIST_SEPARATOR = re.compile(r"\s*(?:[、,，]|和|与|及|或|还是)\s*")
SAFETY = re.compile(
    r"停机|送电|断电|带电|拆卸|拆装|高压|旁路|短接|复位|急停|隔离电|安全联锁|重启|合闸"
    r"|\b(?:energized|energised|live circuit|high voltage|lockout|bypass|short[- ]circuit|disassembl\w*|"
    r"emergency stop|reset|restart|shut\s*down|power (?:off|on)|safety interlock)\b", re.I
)
MODEL_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")


def model_tls_verification():
    # Windows services need the OS trust store, including trusted enterprise CAs.
    if sys.platform == "win32" and not (os.getenv("SSL_CERT_FILE") or os.getenv("SSL_CERT_DIR")):
        return ssl.create_default_context()
    return True


_model_http = httpx.Client(verify=model_tls_verification())


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
        return parse_pdf(raw, category)
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
                rows = [[cell.text for cell in row.cells] for row in block.rows]
                text = field_value_fault_table(rows) if category == "manual" else ""
                if not text:
                    text = "\n".join(" | ".join(row) for row in rows)
            else:
                continue
            if text.strip():
                if not blocks or position != blocks[-1][0]:
                    blocks.append([position, text, False])
                else:
                    blocks[-1][1] += "\n" + text
        return blocks
    if suffix in {".txt", ".md", ".csv", ".tsv"}:
        encodings = ("utf-16",) if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "gb18030")
        for encoding in encodings:
            try:
                content = raw.decode(encoding)
                if suffix in {".csv", ".tsv"}:
                    return parse_csv(content, category, delimiter="\t" if suffix == ".tsv" else ",")
                return [["文本正文", content, False]]
            except UnicodeDecodeError:
                pass
        raise ValueError("无法识别文本编码")
    raise ValueError("仅支持 PDF、DOCX、TXT、MD、CSV、TSV")


def parse_csv(content, category, *, delimiter=","):
    reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)
    kind = "TSV" if delimiter == "\t" else "CSV"
    if not reader.fieldnames or len(reader.fieldnames) > 60:
        raise ValueError(f"{kind} 需要字段表头，最多 60 列")
    keys = {key.strip().lower(): key for key in reader.fieldnames}
    identifiers = ("单号", "工单号", "案例编号", "案例号", "number", "id", "ticket_id", "case_id")
    number_key = next((keys[key] for key in identifiers if key in keys), None)
    if category != "manual" and not number_key:
        raise ValueError(f"历史工单或案例 {kind} 需要「工单号」或「案例编号」列")
    fields = fault_table_fields(reader.fieldnames) if category == "manual" else []
    blocks = []
    for index, row in enumerate(reader, start=2):
        if None in row:
            raise ValueError(f"{kind} 第 {index} 行多于表头列数")
        number = (row.get(number_key) or "").strip()
        if number_key and not number:
            raise ValueError(f"{kind} 第 {index} 行缺少单号或编号")
        body = fault_table_row(fields, [(row.get(key) or "").strip() for key in reader.fieldnames]) if fields else ""
        if not body:
            body = "\n".join(f"{field.strip()}：{value.strip()}" for field, value in row.items() if value and value.strip())
        blocks.append((f"记录 {number}" if number else f"第 {index} 行", body, False))
    return blocks


def parse_pdf(raw, category="manual"):
    from pymupdf import open as open_pdf

    reader = PdfReader(io.BytesIO(raw))
    if reader.is_encrypted:
        raise ValueError("请先解密 PDF")
    if len(reader.pages) > 500:
        raise ValueError("单份 PDF 最多 500 页")
    blocks = []
    with open_pdf(stream=raw, filetype="pdf") as rendered:
        for index, page in enumerate(reader.pages):
            rendered_page = rendered[index]
            content = (page.extract_text(extraction_mode="layout") or "").strip()
            ocr = len(re.sub(r"\s", "", content)) < 25
            if ocr:
                native = rendered_page.get_text(sort=True).strip()
                if len(re.sub(r"\s", "", native)) > len(re.sub(r"\s", "", content)):
                    content = native
                has_large_image = any(
                    (image["bbox"][2] - image["bbox"][0]) * (image["bbox"][3] - image["bbox"][1])
                    >= rendered_page.rect.get_area() * 0.45 for image in rendered_page.get_image_info())
                ocr = has_large_image and len(re.sub(r"\s", "", content)) < 25
            if ocr:
                pix = rendered_page.get_pixmap(dpi=240)
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
            elif category == "manual" and re.search(rf"{CODE_LABEL}|\b(?:code|fault number|alarm number)\b|代码|编码", content, re.I):
                content = extract_pdf_fault_tables(rendered_page) or content
            blocks.append((f"第 {index + 1} 页", content, ocr))
    return blocks


def split_blocks(blocks, category="manual"):
    for position, body, ocr in blocks:
        body = body.strip()
        if not body:
            continue
        if category == "manual":
            body = normalize_fault_tables(body)
            sections = fault_sections(body)
            if sections:
                preamble = body[:sections[0].start()]
                for index, section in enumerate(sections):
                    end = sections[index + 1].start() if index + 1 < len(sections) else len(body)
                    yield position[:180], (preamble + body[section.start():end]).strip(), ocr
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
    deepseek = urlsplit(cfg.model_url).hostname == "api.deepseek.com"
    if deepseek:
        if not cfg.model_key:
            raise RuntimeError("DeepSeek API Key 未配置")
        if endpoint == "chat/completions":
            payload = {**payload, "thinking": {"type": "disabled"}}
    headers = {"Authorization": f"Bearer {cfg.model_key}"} if cfg.model_key else {}
    url = f"{cfg.model_url.rstrip('/')}/{endpoint}"
    attempts = 2 if endpoint == "chat/completions" else 1
    expects_json = deepseek and payload.get("response_format") == {"type": "json_object"}
    for attempt in range(attempts):
        try:
            response = _model_http.post(url, headers=headers, json=payload,
                                        timeout=5 if connection_test else 12, follow_redirects=False)
            response.raise_for_status()
            data = response.json()
            choices = data.get("choices") if isinstance(data, dict) else None
            if expects_json and attempt + 1 < attempts and isinstance(choices, list) and choices:
                choice = choices[0]
                message = choice.get("message") if isinstance(choice, dict) else None
                content = message.get("content") if isinstance(message, dict) else None
                if isinstance(content, str) and choice.get("finish_reason") == "stop":
                    try:
                        json.loads(content)
                    except ValueError:
                        sleep(0.25)
                        continue
            return data
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


def normalize_code_text(value):
    return normalize("NFKC", value).translate(str.maketrans("–—−", "---"))


def valid_code(value):
    return (len(value) <= 24 and bool(re.fullmatch(CODE_TOKEN, value)) and any(char.isdigit() for char in value)
            and (value.isdecimal() or any(char.isalpha() for char in value)))


def letter_code(value):
    return bool(re.fullmatch(r"[A-Za-z]{2,6}", value))


def fault_sections(body):
    rows = list(FAULT_SECTION.finditer(body))
    sections = []
    for index, row in enumerate(rows):
        code = normalize_code_text(row["code"])
        labelled = bool(LABELLED_CODE.search(normalize_code_text(row.group())))
        end = rows[index + 1].start() if index + 1 < len(rows) else len(body)
        if ((labelled and (valid_code(code) or letter_code(code))) or
                (valid_code(code) and _SECTION_EVIDENCE.search(body[row.end():end]))):
            sections.append(row)
    return sections


def table_cells(line):
    if "|" in line:
        return [cell.strip() for cell in line.strip().strip("|").split("|")]
    return re.split(r"\t+| {2,}", line.strip())


def table_field(header):
    header = normalize_code_text(header).lower().strip()
    if re.fullmatch(rf"(?:{CODE_LABEL}|(?:fault|alarm|error)\s+(?:number|no\.?)|code|代码|编码)(?:\s*\([^)]*\))?", header, re.I):
        return "故障码"
    if re.fullmatch(r"(?:故障(?:含义|名称|现象|描述|说明)?|告警(?:名称|含义)?|报警(?:名称|含义)?|含义|名称|现象|说明|"
                    r"name|meaning|fault(?: name| description| text)?|alarm(?: name| description| text)?|symptom|description)", header):
        return "故障含义"
    if re.fullmatch(r"原因|故障原因|可能原因|cause|possible causes?|cause of problem", header):
        return "故障原因"
    if re.fullmatch(r"(?:故障)?(?:处理|处置|排查|维修|解决|排除)(?:方法|措施|步骤|办法|方案|意见)?|措施|建议措施|纠正措施|"
                    r"remed(?:y|ies)|what to do|corrective actions?|recommended actions?|troubleshooting(?: steps)?|solution", header):
        return "处理措施"
    return header


def fault_table_row(fields, cells):
    code_column = fields.index("故障码")
    if len(cells) != len(fields):
        return ""
    code = normalize_code_text(cells[code_column])
    if not (valid_code(code) or letter_code(code)):
        return ""
    record = [f"故障码：{cells[code_column]}"]
    record.extend(f"{field}：{cell}" for column, (field, cell) in enumerate(zip(fields, cells))
                  if column != code_column and field and cell)
    return "\n".join(record) if len(record) > 1 else ""


def fault_table_fields(headers):
    fields = [table_field(header or "") for header in headers]
    return fields if fields.count("故障码") == 1 and set(fields).intersection({"故障含义", "故障原因", "处理措施"}) else []


def field_value_fault_table(rows):
    if not rows or any(len(row) != 2 for row in rows):
        return ""
    pairs = [(table_field(label), value.strip()) for label, value in rows]
    if (pairs[0][0], pairs[0][1].casefold()) in (("field", "value"), ("字段", "内容")):
        pairs = pairs[1:]
    records = []
    for field, value in pairs:
        if field == "故障码" and (valid_code(normalize_code_text(value)) or letter_code(normalize_code_text(value))):
            records.append([(field, value)])
        elif records and field in {"故障含义", "故障原因", "处理措施"} and value:
            records[-1].append((field, value))
        else:
            return ""
    if not records or any(len(record) < 2 for record in records):
        return ""
    return "\n\n".join("\n".join(f"{field}：{value}" for field, value in record) for record in records)


def extract_pdf_fault_tables(page):
    """Use actual PDF cells for grid tables, retaining surrounding page text."""
    try:
        tables = page.find_tables().tables
    except (RuntimeError, ValueError, TypeError, AttributeError):
        return ""
    accepted = []
    for table in tables:
        try:
            rows = table.extract()
            headers, data = (table.header.names, rows) if table.header.external else (rows[0], rows[1:])
        except (RuntimeError, ValueError, TypeError, AttributeError, IndexError):
            continue
        if not rows:
            continue
        fields = fault_table_fields(headers)
        if not fields:
            continue
        records = [fault_table_row(fields, [re.sub(r"\s+", " ", cell or "").strip() for cell in row]) for row in data]
        if not records or not all(records):
            continue
        left, top, right, bottom = table.bbox
        if table.header.external:
            header = table.header.bbox
            left, top, right, bottom = min(left, header[0]), min(top, header[1]), max(right, header[2]), max(bottom, header[3])
        accepted.append(((left, top, right, bottom), "\n\n".join(records)))
    if not accepted:
        return ""
    # Table cells already contain their full text. Keep lines outside those cells once.
    items = [(bounds[1], bounds[0], records) for bounds, records in accepted]
    for block in page.get_text("dict", sort=True)["blocks"]:
        for line in block.get("lines", []):
            left, top, right, bottom = line["bbox"]
            x, y = (left + right) / 2, (top + bottom) / 2
            if any(bounds[0] <= x <= bounds[2] and bounds[1] <= y <= bounds[3] for bounds, _ in accepted):
                continue
            text = "".join(span["text"] for span in line["spans"]).strip()
            if text:
                items.append((top, left, text))
    return "\n".join(text for _, _, text in sorted(items))


def normalize_fault_tables(body):
    """Label complete, recognizable fault-table rows without inventing any cell contents."""
    if "|" not in body and not re.search(r"\t| {2,}", body):
        return body
    lines = body.splitlines()
    output = []
    index = 0
    while index < len(lines):
        fields = fault_table_fields(table_cells(lines[index]))
        if not fields:
            output.append(lines[index])
            index += 1
            continue
        end = index + 1
        records = []
        while end < len(lines) and lines[end].strip():
            if re.fullmatch(r"[\s|:\-]+", lines[end]):
                end += 1
                continue
            cells = table_cells(lines[end])
            record = fault_table_row(fields, cells)
            if not record:
                break
            records.append(record)
            end += 1
        # A wrapped or ambiguous next row can belong to the preceding remedy. Keep the
        # original table in that case so that review can resolve its boundaries.
        if records and (end == len(lines) or not lines[end].strip() or FAULT_SECTION.match(lines[end])):
            output.extend(records)
            index = end
        else:
            output.append(lines[index])
            index += 1
    return "\n".join(output)


def codes_in(value):
    value = normalize_code_text(normalize_fault_tables(value))
    codes = {match for match in CODES.findall(value) if valid_code(match)}
    codes.update(match for match in LABELLED_CODE.findall(value) if valid_code(match) or letter_code(match))
    codes.update(row["code"] for row in fault_sections(value))
    return {match.upper() for match in codes}


def chunk_fault_codes(position, body):
    normalized = normalize_code_text(normalize_fault_tables(body))
    declared = fault_sections(normalized)
    if declared:
        return {row["code"].upper() for row in declared}
    return codes_in(position + "\n" + body)


def code_key(code):
    return normalize_code_text(code).upper().replace("-", "").replace("_", "")


def fault_index_entries(org, source, chunk, position, body):
    sections = fault_sections(normalize_code_text(normalize_fault_tables(body)))
    codes = {code_key(row["code"]) for row in sections} if sections else {
        code_key(code) for code in codes_in(position + "\n" + body)}
    return [dict(org=org, source=source, chunk=chunk, code=code, declared=bool(sections))
            for code in sorted(codes) if code]


def standalone_fault_code(value):
    text = normalize_code_text(value).strip().rstrip("?。!").rstrip()
    match = re.fullmatch(
        rf"(?:那(?:么)?|请问)?[ \t]*(?:(?P<label>{CODE_LABEL})[ \t]*(?:[:为][ \t]*)?)?"
        rf"(?P<code>{CODE_TOKEN})(?:[ \t]*[呢吗嘛])?", text, re.I)
    if match:
        code = match["code"]
        if ((valid_code(code) and not _UNIT_CODE.fullmatch(code)) or
                (letter_code(code) and (code.isupper() or match["label"]))):
            return code.upper()
    return ""


def question_codes(value):
    codes = set()
    for line in normalize_code_text(value).splitlines():
        standalone = standalone_fault_code(line)
        if standalone:
            codes.add(standalone)
            continue
        explicit = codes_in(line)
        numbers = []
        for token in _CODE_CANDIDATE.finditer(line):
            number = token.group().upper()
            before, after = line[:token.start()], line[token.end():]
            prefixed, suffixed = _NUMERIC_FAULT_PREFIX.search(before), _NUMERIC_FAULT_SUFFIX.match(after)
            alpha = letter_code(number) and (token.group().isupper() or number in explicit or
                    any(cue and re.search(r"[\u4e00-\u9fff]", cue.group()) for cue in (prefixed, suffixed)))
            if not (valid_code(number) or alpha):
                continue
            if number not in explicit and (_NON_FAULT_NUMBER_PREFIX.search(before) or _NON_FAULT_NUMBER_UNIT.match(after)
                                           or _UNIT_CODE.fullmatch(number)):
                continue
            if number in explicit or prefixed or suffixed:
                codes.add(number)
            numbers.append(token)
        # In a comparison of several codes, a diagnostic cue can apply to the whole list.
        for left, right in reversed(list(zip(numbers, numbers[1:]))):
            if _NUMBER_LIST_SEPARATOR.fullmatch(line[left.end():right.start()]) and (
                    left.group().upper() in codes or right.group().upper() in codes):
                codes.update((left.group().upper(), right.group().upper()))
    return codes


def fault_codes(value, device, *, infer_numeric=True):
    device_labels = (device.get("number", ""), device.get("model", ""))
    identifiers = codes_in(" ".join(device_labels)) | {normalize_code_text(label).upper() for label in device_labels
                                                        if valid_code(normalize_code_text(label))}
    device_keys = {code_key(code) for code in identifiers}
    codes = codes_in(value)
    if infer_numeric:
        codes |= question_codes(value)
        explicit = {match.upper() for match in LABELLED_CODE.findall(normalize_code_text(value))}
        codes -= non_fault_numbers(value) - explicit
    return {code for code in codes if code_key(code) not in device_keys}


def non_fault_numbers(value):
    normalized = normalize_code_text(value)
    numbers = set()
    for token in _CODE_CANDIDATE.finditer(normalized):
        word = token.group()
        if _UNIT_CODE.fullmatch(word):
            numbers.add(re.match(r"\d+(?:\.\d+)?", word).group())
        elif (valid_code(word) or re.fullmatch(r"\d+\.\d+", word)) and (_NON_FAULT_NUMBER_PREFIX.search(normalized[:token.start()])
                                  or _NON_FAULT_NUMBER_UNIT.match(normalized[token.end():])):
            numbers.add(word.upper())
    return numbers


_LOOKUP_FILLER = re.compile(
    r"有没有|能否|是否有|怎么处理|怎么办|出现了?|发生了?|故障码|故障|异常|复盘|案例|历史工单|"
    r"厂家目录|维修手册|手册|目录|资料|请问|核对|查询|相关|记录|什么|哪[些个]|怎么|如何|一下|的|吗|了"
)
_DIAGNOSIS_CUE = re.compile(r"故障|异常|告警|报警|振动|异响|复盘|案例|工单|损坏")
_NO_FLOW = r"不出水|无出水|不供水|没有出水|没出水|没水出(?:来)?|出不了水|打不出水|无流量|流量为零|流量为0|\bno water\b"
_OVERHEAT = r"过热|超温|发热|发烫|烫手|烫得厉害|\boverheat\w*\b"
_PHASE_LOSS = r"缺相|相位丢失|\b(?:phase loss|loss of (?:a )?phase)\b"
_SYMPTOM_CUE = re.compile(
    r"异常|不正常|告警|报警|波动|(?:过|偏|太)[高低大小]|超限|越限|超标|超出|超过|"
    r"升高|增大|下降|骤降|不稳|忽大忽小|不足|欠压|过压|过流|间歇|突变|漏液|漏油|漏水|漏气|渗漏|异响|"
    r"开不了机|起不来|不启动|(?:无法|不能)(?:启动|起动)|启动失败"
    rf"|{_NO_FLOW}|{_OVERHEAT}|{_PHASE_LOSS}"
    r"|\b(?:abnormal|alarm|warning|unstable|too (?:high|low)|leak\w*|excessive|"
    r"fluctuat\w*|insufficient|won.t start|fails? to start|start(?:up)? failure)\b", re.I
)
_DIAGNOSTIC_CONTEXT = re.compile(
    r"排查|诊断|故障现象|检查|查看|核对|建议记录|\b(?:troubleshoot\w*|diagnos\w*|inspect\w*|check|monitor)\b", re.I
)
_NOMINAL_CONTEXT = re.compile(
    r"额定|名义|选型|标准工况|样本|目录|正常值|正常范围|\b(?:rated|nominal|catalog(?:ue)?|specification)\b", re.I
)
_SUPPORT_LINES = re.compile(r"[。！？；;\n]|(?<=[.!?])\s+")
_SIGNAL_TERMS = {
    "temperature": re.compile(rf"温度|温升|过冷|高温|低温|水温|油温|炉温|壁温|气温|烟温|\btemperatur\w*\b|{_OVERHEAT}", re.I),
    "pressure": re.compile(r"压力|压差|气压|油压|水压|炉压|负压|\bpressure\b", re.I),
    "motion": re.compile(r"振动|震动|抖动|晃动|\b(?:vibrat\w*|shak\w*)\b", re.I),
    "noise": re.compile(r"噪声|噪音|异响|杂音|\b(?:noise|noisy)\b", re.I),
    "flow": re.compile(rf"流量|流速|风量|水量|\b(?:flow|airflow)\b|{_NO_FLOW}", re.I),
    "head": re.compile(r"扬程|\b(?:pump head|total head|discharge head)\b", re.I),
    "capacity": re.compile(r"制冷量|冷量|\bcooling capacity\b", re.I),
    "voltage": re.compile(r"电压|欠压|过压|\b(?:voltage|overvoltage|undervoltage)\b", re.I),
    "current": re.compile(r"电流|过流|\b(?:current|overcurrent)\b", re.I),
    "phase": re.compile(_PHASE_LOSS, re.I),
    "leak": re.compile(r"泄漏|渗漏|漏液|漏油|漏水|漏气|\bleak\w*\b", re.I),
    "start": re.compile(r"开不了机|起不来|不启动|(?:无法|不能)(?:启动|起动)|启动失败"
                        r"|\b(?:won.t start|fails? to start|start(?:up)? failure)\b", re.I),
}
_MEASURING_POINTS = {
    "bearing": re.compile(r"轴承|轴瓦|\bbearing\w*\b", re.I),
    "winding": re.compile(r"绕组|线圈|\b(?:winding\w*|coil\w*)\b", re.I),
    "furnace": re.compile(r"炉膛|\b(?:furnace|combustion chamber)\b", re.I),
    "inlet": re.compile(r"入口|进口|进水|进气|吸入口|\b(?:inlet|intake|suction)\b", re.I),
    "outlet": re.compile(r"出口|出水|排气口|出风口|\b(?:outlet|discharge)\b", re.I),
    "flue": re.compile(r"烟气|烟温|烟道|\bflue(?: gas)?\b", re.I),
}
_SYMPTOM_DIRECTIONS = (
    re.compile(r"(?:过|偏|太)[高大]|升高|增大|高温|过压|过流"
               rf"|\b(?:high|too large|overvoltage|overcurrent|excessive)\b|{_OVERHEAT}", re.I),
    re.compile(r"(?:过|偏|太)[低小]|下降|骤降|低温|欠压|不足"
               r"|\b(?:low|too small|undervoltage|insufficient)\b", re.I),
    re.compile(_NO_FLOW, re.I),
    re.compile(r"波动|不稳|忽高忽低|忽大忽小|\b(?:unstable|fluctuat\w*)\b", re.I),
)
_PHASE_POINTS = (
    re.compile(r"(?:输入(?:电源)?|进线)缺相|\b(?:input|supply) phase loss\b", re.I),
    re.compile(r"(?:输出(?:电源)?|出线)缺相|\boutput phase loss\b", re.I),
)


def symptom_signals(value):
    if not _SYMPTOM_CUE.search(value):
        return ()
    return tuple(pattern for pattern in _SIGNAL_TERMS.values() if pattern.search(value))


def symptom_details(value):
    return tuple(pattern for pattern in (*_MEASURING_POINTS.values(), *_SYMPTOM_DIRECTIONS, *_PHASE_POINTS)
                 if pattern.search(value))


def supports_symptoms(position, body, signals, details):
    if not signals:
        return True
    lines = [line for line in _SUPPORT_LINES.split(f"{position}\n{body}")
             if _SYMPTOM_CUE.search(line) or
             (_DIAGNOSTIC_CONTEXT.search(line) and not _NOMINAL_CONTEXT.search(line))]
    return all(any(signal.search(line) and all(detail.search(line) for detail in details) for line in lines)
               for signal in signals)


def suggested_device_refs(db, user, device, query, candidates, *, signals=(), details=(), category=None):
    if len(query) > 100 or SAFETY.search(query):
        return []
    faults = {code_key(code) for code in fault_codes(query, device)}
    if not faults and not _DIAGNOSIS_CUE.search(query):
        return []
    subject = _LOOKUP_FILLER.sub(" ", query.strip())
    phrases = set(re.findall(r"[\u4e00-\u9fff]{2,}", subject))
    if not faults and not phrases:
        return []

    def score(title, position, body, *, current=False):
        if faults:
            codes = {code_key(code) for code in chunk_fault_codes(position, body)}
            return len(faults) if faults <= codes else 0
        if not supports_symptoms(position, body, signals, details):
            return 0
        location = f"{title} {position} {body}" if current else title
        return max((len(phrase) for phrase in phrases if phrase in location), default=0)

    if any(score(item["title"], item["position"], item["text"], current=True) for item in candidates):
        return []

    current_ids, _ = applicability(db, user.org, device)
    devices = {}
    for row in db.scalars(records(user.org, "device").order_by(Record.key)):
        pair = row.data["model"], row.data["batch"]
        if pair != (device["model"], device["batch"]) and permitted_device(user, row.data):
            devices.setdefault(pair, row)
    if not devices:
        return []

    by_source = defaultdict(list)
    for scope in db.scalars(current_scopes(user.org).where(
            Scope.status == "bind", Scope.model.in_({model for model, _ in devices} | {"*"}))):
        if scope.source not in current_ids:
            by_source[scope.source].append(scope)
    if not by_source:
        return []

    ranked = []
    chunks = select(Chunk, Record.data).join(Record, Record.id == Chunk.source).where(
        Chunk.org == user.org, Chunk.source.in_(by_source), Record.org == user.org).options(defer(Chunk.embedding))
    if category:
        chunks = chunks.where(Record.data["category"].as_string() == category)
    for chunk, data in db.execute(chunks):
        relevance = score(data["title"], chunk.position, chunk.body)
        if relevance:
            ranked.append((relevance, data["category"] == "manual", data["title"], chunk))

    suggestions, checked = {}, {}
    for _, _, _, chunk in sorted(ranked, key=lambda row: (-row[0], -row[1], row[2], row[3].id)):
        for scope in by_source[chunk.source]:
            for (model, batch), target in devices.items():
                if target.id in suggestions or scope.model not in {"*", model} or scope.batch not in {"", batch}:
                    continue
                if target.id not in checked:
                    checked[target.id] = applicability(db, user.org, target.data)
                ids, ambiguous = checked[target.id]
                if not ambiguous and chunk.source in ids:
                    suggestions[target.id] = {"deviceId": target.id, "chunkId": chunk.id}
                if len(suggestions) == 3:
                    return list(suggestions.values())
    return list(suggestions.values())


def historical_numeric_matches(db, base, sources, ranks, faults, limit, trace):
    if len(faults) != 1:
        return []
    number = next(iter(faults))
    if not re.fullmatch(r"[0-9]+", number):
        return []
    history = [source.id for source in sources.values() if source.data["category"] in {"case", "workorder"}]
    if not history:
        return []
    full_width = number.translate(str.maketrans("0123456789", "０１２３４５６７８９"))
    query = base.where(Chunk.source.in_(history), Chunk.body.contains(number) | Chunk.body.contains(full_width))
    field = re.compile(rf"(?im)^[ \t]*(?:故障现象|现场现象|现象|symptom)[ \t]*:[ \t]*"
                       rf"(?:(?:面板(?:显示|提示)|报(?:了|出)?)[ \t]*:?[ \t]*)?{number}[ \t.。]*\r?$")
    matches = [chunk for chunk in db.scalars(query.options(defer(Chunk.embedding)))
               if field.search(normalize_code_text(chunk.body))]
    matches.sort(key=lambda chunk: (ranks[chunk.source], sources[chunk.source].data["category"] != "case", chunk.id))
    results = [{**citation(sources[chunk.source], chunk), "matchedObservation": number} for chunk in matches[:limit]]
    if trace is not None:
        trace.update(retrievalPoolSize=len(matches), rankedChunks=[
            dict(id=item["id"], scopeRank=ranks[item["sourceId"]], exactCodeMatches=0, matchKind="historical_observation")
            for item in results])
    return results


_FAULT_NAME_ENDING = re.compile(
    r"(?:是(?:什么|啥)(?:故障|问题|意思)|怎么回事|是什么原因|是什么意思|"
    r"(?:怎么|如何|怎样)(?:修(?:复|理)?|维修|处理|处置|解决|排查|检查|排除|排障|消除)|怎么办|呢|吗)$", re.I)
_FAULT_NAME_FIELD = re.compile(
    r"(?im)^[ \t]*(?:故障(?:含义|名称|说明)|报警名称|告警名称|fault (?:name|meaning))\s*(?:[:：]|为|是)\s*([^\r\n]+)")


def normalize_fault_name(value):
    text = normalize_code_text(value).strip(" \t:：[]【】。?!-")
    text = re.sub(r"(?<=输入)电源(?=缺相)", "", text)
    return " ".join(text.casefold().split())


def named_fault_matches(db, org, device, question, preview_source=None):
    subject = normalize_code_text(question).strip(" \t?。! ")
    subject = re.sub(r"^(?:请问|那么|那(?=\S))\s*", "", subject)
    subject = _FAULT_NAME_ENDING.sub("", subject).strip()
    name = normalize_fault_name(subject)
    if len(name) < 2 or len(name) > 48 or not re.fullmatch(r"[\w\u4e00-\u9fff .()/-]+", name):
        return []
    if preview_source is None:
        applicable, ambiguous = applicability(db, org, device)
        if ambiguous or not applicable:
            return []
        ids = set(db.scalars(records(org, "source").where(
            Record.id.in_(applicable), Record.data["category"].as_string() == "manual"
        ).with_only_columns(Record.id)))
    elif preview_source.data["category"] == "manual":
        ids = {preview_source.id}
    else:
        return []
    if not ids:
        return []
    set_org(db, org)
    chunks = db.scalars(select(Chunk).where(Chunk.org == org, Chunk.source.in_(ids),
        Chunk.position.icontains(name, autoescape=True) | Chunk.body.icontains(name, autoescape=True))
        .order_by(Chunk.source, Chunk.position, Chunk.id).options(defer(Chunk.embedding)))
    matches = {}
    for chunk in chunks:
        body = normalize_code_text(normalize_fault_tables(chunk.body))
        sections = fault_sections(body)
        for i, section in enumerate(sections):
            end = sections[i + 1].start() if i + 1 < len(sections) else len(body)
            labels = [section["name"].split(" · ", 1)[0], *_FAULT_NAME_FIELD.findall(body[section.end():end])]
            if name in {normalize_fault_name(label) for label in labels}:
                code = section["code"].upper()
                matches.setdefault(code_key(code), (code, chunk.id))
    return list(matches.values())


def retrieve(db, org, device, query, limit=6, *, signals=(), details=(), preview_source=None, trace=None, category=None):
    set_org(db, org)
    if trace is not None:
        trace.update(retrievalPoolSize=0, rankedChunks=[])
    mentioned = {token.lower().rstrip(".") for token in MODEL_TOKEN.findall(query)
                 if any(char.isdigit() for char in token)}
    mentioned.discard(device["model"].lower())
    if mentioned and db.scalar(records(org, "device").where(
            func.lower(Record.data["model"].as_string()).in_(mentioned)).limit(1)):
        return [], False
    ids, ambiguous = ({preview_source.id: 0}, False) if preview_source is not None else applicability(db, org, device)
    if ambiguous or not ids:
        return [], ambiguous
    sources = {r.id: r for r in db.scalars(records(org, "source").where(Record.id.in_(ids)))}
    if category:
        sources = {key: row for key, row in sources.items() if row.data["category"] == category}
        ids = {key: ids[key] for key in sources}
        if not ids:
            return [], False
    tokens = terms(query)
    faults = {code_key(code) for code in fault_codes(query, device)}
    parameter_numbers = non_fault_numbers(query) if not faults and not signals else set()
    matched_faults = set()
    groups = defaultdict(list)
    base = select(Chunk).where(Chunk.org == org, Chunk.source.in_(ids))
    indexed_codes = defaultdict(set)
    if faults:
        matches = db.execute(select(FaultIndex.chunk, FaultIndex.code, FaultIndex.source, FaultIndex.declared).where(
            FaultIndex.org == org, FaultIndex.source.in_(ids), FaultIndex.code.in_(faults))).all()
        declared = {(source, code) for _, code, source, is_declared in matches if is_declared}
        for chunk_id, code, source, is_declared in matches:
            if is_declared or (source, code) not in declared:
                indexed_codes[chunk_id].add(code)
        if not indexed_codes:
            return historical_numeric_matches(db, base, sources, ids, faults, limit, trace), False
        base = base.where(Chunk.id.in_(select(FaultIndex.chunk).where(
            FaultIndex.org == org, FaultIndex.source.in_(ids), FaultIndex.code.in_(faults))))
    for chunk in db.scalars(base.options(defer(Chunk.embedding))):
        source = sources[chunk.source]
        if not supports_symptoms(chunk.position, chunk.body, signals, details):
            continue
        full = f"{source.data['title']} {chunk.position} {chunk.body}"
        if parameter_numbers and not parameter_numbers <= non_fault_numbers(chunk.body):
            continue
        exact = indexed_codes.get(chunk.id, ())
        if faults and not exact:
            continue
        matched_faults.update(exact)
        lexical = sum((tokens & terms(full)).values()) + len(signals)
        groups[ids[source.id], source.data["category"] == "manual"].append((chunk, len(exact), lexical))
    if not groups or faults - matched_faults:
        return [], False
    semantic = {}
    if preview_source is None and cfg.allow_model_data and cfg.embedding_model and not cfg.model_source_review:
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
    if trace is not None:
        scores = {chunk.id: rank for rank, chunk in ranked}
        trace.update(retrievalPoolSize=len(ranked), rankedChunks=[
            dict(id=item["id"], exactCodeMatches=-scores[item["id"]][0], scopeRank=scores[item["id"]][1],
                 rrfScore=round(-scores[item["id"]][3], 6))
            for item in output])
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

