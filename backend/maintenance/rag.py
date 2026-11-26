"""Standard RAG pipeline: structure-aware chunking, dense embeddings (remote + local fallback),
BM25 + vector cosine + fault-code RRF hybrid retrieval, and neighbor context expansion."""
from collections import Counter, defaultdict
import hashlib
import math
import re

import httpx
from pgvector.sqlalchemy import Vector
from sqlalchemy import func, select, type_coerce
from sqlalchemy.orm import defer

from .db import Chunk, FaultIndex, Record, cfg, find, records, set_org
from .knowledge import (
    applicability,
    citation,
    code_key,
    fault_codes,
    fault_sections,
    normalize_code_text,
    normalize_fault_tables,
)
from .model_config import embed_texts_remote, embedding_ready

LOCAL_EMBEDDING_MODEL = "local-tfidf-256"
LOCAL_EMBEDDING_DIM = 256

_MD_HEADING = re.compile(r"(?m)^(#{1,6})[ \t]+([^\r\n]+)$")
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？；.!?\n])")
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9._/-]*|[\u4e00-\u9fff]+")


def tokenize(text: str) -> list[str]:
    """Tokenize Chinese + English + fault codes for BM25 and local dense embeddings."""
    normalized = normalize_code_text(text).lower()
    tokens: list[str] = []
    for part in _TOKEN_RE.findall(normalized):
        if re.fullmatch(r"[\u4e00-\u9fff]+", part):
            if len(part) == 1:
                tokens.append(part)
            else:
                tokens.extend(part[i:i + 2] for i in range(len(part) - 1))
                # Include unigrams for short queries / rare single-character terms
                if len(part) <= 6:
                    tokens.extend(part)
        else:
            tokens.append(part)
            compact = part.replace("-", "").replace("_", "")
            if compact != part:
                tokens.append(compact)
    return tokens


def local_embed(text: str, dim: int = LOCAL_EMBEDDING_DIM) -> list[float]:
    """Deterministic L2-normalized signed feature-hashing vector (fallback when no remote embedding model)."""
    counts = Counter(tokenize(text[:2400]))
    if not counts:
        return [0.0] * dim
    vec = [0.0] * dim
    for token, tf in counts.items():
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        bucket = int.from_bytes(digest[:4], "little") % dim
        sign = 1.0 if (digest[4] & 1) == 0 else -1.0
        vec[bucket] += sign * (1.0 + math.log(tf))
    norm = math.sqrt(sum(v * v for v in vec))
    if norm <= 0:
        return vec
    return [round(v / norm, 6) for v in vec]


def _recursive_slice(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    units: list[str] = []
    for para in paragraphs:
        if len(para) <= chunk_size:
            units.append(para)
        else:
            for sent in _SENTENCE_SPLIT.split(para):
                sent = sent.strip()
                if not sent:
                    continue
                if len(sent) <= chunk_size:
                    units.append(sent)
                else:
                    step = max(1, chunk_size - chunk_overlap)
                    for i in range(0, len(sent), step):
                        piece = sent[i:i + chunk_size].strip()
                        if piece:
                            units.append(piece)

    chunks: list[str] = []
    buf = ""
    for unit in units:
        sep = "\n" if buf else ""
        if len(buf) + len(sep) + len(unit) <= chunk_size:
            buf = f"{buf}{sep}{unit}"
        else:
            if buf:
                chunks.append(buf)
                tail = buf[-chunk_overlap:].strip() if chunk_overlap > 0 else ""
                buf = f"{tail}\n{unit}".strip() if tail and len(tail) + len(unit) + 1 <= chunk_size else unit
            else:
                buf = unit
    if buf:
        chunks.append(buf)
    return chunks


def chunk_blocks(blocks, category: str = "manual", *, chunk_size: int = 600, chunk_overlap: int = 100):
    """Standard structure-aware recursive chunker yielding (position, body, ocr)."""
    chunk_size = max(200, min(2000, int(chunk_size)))
    chunk_overlap = max(0, min(chunk_size // 2, int(chunk_overlap)))

    for raw_pos, raw_body, ocr in blocks:
        body = (raw_body or "").strip()
        if not body:
            continue
        base_pos = str(raw_pos or "正文").strip()[:120]

        # Keep structured CSV/TSV rows intact unless huge
        if base_pos.startswith("记录 ") and len(body) <= chunk_size * 2:
            yield base_pos[:180], body, bool(ocr)
            continue

        if category == "manual":
            body = normalize_fault_tables(body)
            sections = fault_sections(body)
            if sections:
                preamble = body[:sections[0].start()].strip()
                for idx, sec in enumerate(sections):
                    end = sections[idx + 1].start() if idx + 1 < len(sections) else len(body)
                    sec_text = body[sec.start():end].strip()
                    full_sec = f"{preamble}\n{sec_text}".strip() if preamble and len(preamble) <= 200 else sec_text
                    label = f"{base_pos} · {sec['code'].strip()}"[:180]
                    if len(full_sec) <= int(chunk_size * 1.6):
                        yield label, full_sec, bool(ocr)
                    else:
                        pieces = _recursive_slice(full_sec, chunk_size, chunk_overlap)
                        for p_idx, piece in enumerate(pieces, start=1):
                            yield f"{label} ({p_idx}/{len(pieces)})"[:180], piece, bool(ocr)
                continue

        # Markdown heading-aware splitting
        headings = list(_MD_HEADING.finditer(body))
        if headings:
            lead = body[:headings[0].start()].strip()
            if lead:
                for p_idx, piece in enumerate(_recursive_slice(lead, chunk_size, chunk_overlap), start=1):
                    yield f"{base_pos}"[:180], piece, bool(ocr)
            for idx, h in enumerate(headings):
                end = headings[idx + 1].start() if idx + 1 < len(headings) else len(body)
                sec_title = h.group(2).strip()[:60]
                sec_body = body[h.start():end].strip()
                label = f"{base_pos} · {sec_title}"[:180]
                pieces = _recursive_slice(sec_body, chunk_size, chunk_overlap)
                for p_idx, piece in enumerate(pieces, start=1):
                    pos = f"{label} ({p_idx}/{len(pieces)})"[:180] if len(pieces) > 1 else label
                    yield pos, piece, bool(ocr)
            continue

        pieces = _recursive_slice(body, chunk_size, chunk_overlap)
        for p_idx, piece in enumerate(pieces, start=1):
            pos = f"{base_pos} · 片段 {p_idx}/{len(pieces)}"[:180] if len(pieces) > 1 else base_pos[:180]
            yield pos, piece, bool(ocr)


def embed_documents(settings: dict, texts: list[str]) -> tuple[list[list[float]], str, int, bool]:
    """Compute embeddings for a batch of chunk texts.
    Returns (vectors, model_name, dimensions, degraded_to_local)."""
    if not texts:
        return [], "", 0, False
    if embedding_ready(settings):
        try:
            vectors: list[list[float]] = []
            for start in range(0, len(texts), 16):
                batch = [t[:1800] for t in texts[start:start + 16]]
                vectors.extend(embed_texts_remote(settings, batch))
            dim = len(vectors[0]) if vectors else 0
            return vectors, settings["embeddingModel"], dim, False
        except (httpx.HTTPError, KeyError, ValueError, TypeError, RuntimeError):
            vectors = [local_embed(t) for t in texts]
            return vectors, LOCAL_EMBEDDING_MODEL, LOCAL_EMBEDDING_DIM, True
    vectors = [local_embed(t) for t in texts]
    return vectors, LOCAL_EMBEDDING_MODEL, LOCAL_EMBEDDING_DIM, False


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na > 0 and nb > 0 else 0.0


def _bm25_scores(query_tokens: list[str], docs: list[tuple[str, list[str]]]) -> dict[str, float]:
    """Standard Okapi BM25 over candidate chunks."""
    if not query_tokens or not docs:
        return {}
    n = len(docs)
    k1, b = 1.5, 0.75
    doc_lens = {doc_id: max(1, len(toks)) for doc_id, toks in docs}
    avgdl = sum(doc_lens.values()) / n
    df: Counter[str] = Counter()
    tfs: dict[str, Counter[str]] = {}
    for doc_id, toks in docs:
        counter = Counter(toks)
        tfs[doc_id] = counter
        for term in counter:
            df[term] += 1
    q_terms = Counter(query_tokens)
    scores: dict[str, float] = {}
    for doc_id, counter in tfs.items():
        dl = doc_lens[doc_id]
        score = 0.0
        for term, qf in q_terms.items():
            tf = counter.get(term, 0)
            if not tf:
                continue
            idf = math.log(1.0 + (n - df[term] + 0.5) / (df[term] + 0.5))
            denom = tf + k1 * (1.0 - b + b * dl / avgdl)
            score += qf * idf * (tf * (k1 + 1.0) / denom)
        if score > 0:
            scores[doc_id] = score
    return scores


def _vector_scores(db, org: str, source_ids: set[str], query: str, settings: dict) -> dict[str, float]:
    """Compute vector cosine similarity against remote embeddings (if present) and/or local embeddings."""
    set_org(db, org)
    # Inspect which embedding models exist in these sources
    models_present = set(db.scalars(
        select(Chunk.embedding_model).where(
            Chunk.org == org, Chunk.source.in_(source_ids), Chunk.embedding.is_not(None),
        ).distinct()
    ))
    if not models_present:
        return {}

    scores: dict[str, float] = {}
    remote_model = settings.get("embeddingModel", "")
    # 1) Remote neural embeddings
    if remote_model and remote_model in models_present and embedding_ready(settings):
        try:
            q_vec = embed_texts_remote(settings, [query[:1800]])[0]
            if db.bind.dialect.name == "postgresql":
                distance = type_coerce(Chunk.embedding, Vector()).cosine_distance(q_vec)
                rows = db.execute(select(Chunk.id, distance).where(
                    Chunk.org == org, Chunk.source.in_(source_ids),
                    Chunk.embedding.is_not(None), Chunk.embedding_model == remote_model,
                    func.vector_dims(Chunk.embedding) == len(q_vec),
                ).order_by(distance).limit(60)).all()
                for cid, dist in rows:
                    sim = 1.0 - float(dist)
                    if sim > 0.15:
                        scores[cid] = max(scores.get(cid, 0.0), sim)
            else:
                rows = db.execute(select(Chunk.id, Chunk.embedding).where(
                    Chunk.org == org, Chunk.source.in_(source_ids),
                    Chunk.embedding.is_not(None), Chunk.embedding_model == remote_model,
                )).all()
                for cid, emb in rows:
                    sim = _cosine(q_vec, emb)
                    if sim > 0.15:
                        scores[cid] = max(scores.get(cid, 0.0), sim)
        except (httpx.HTTPError, KeyError, ValueError, TypeError, RuntimeError):
            pass

    # 2) Local dense feature vectors (also covers chunks embedded locally or while remote is unconfigured)
    if LOCAL_EMBEDDING_MODEL in models_present:
        q_local = local_embed(query)
        rows = db.execute(select(Chunk.id, Chunk.embedding).where(
            Chunk.org == org, Chunk.source.in_(source_ids),
            Chunk.embedding.is_not(None), Chunk.embedding_model == LOCAL_EMBEDDING_MODEL,
        )).all()
        for cid, emb in rows:
            sim = _cosine(q_local, emb)
            if sim > 0.08:
                scores[cid] = max(scores.get(cid, 0.0), sim)

    return scores


def hybrid_search(db, org: str, device: dict, query: str, settings: dict, *,
                  category: str | None = None, fault_code: str = "", top_k: int = 6,
                  preview_source=None) -> tuple[list[dict], bool]:
    """Standard Hybrid RAG retrieval: BM25 + Dense Vector + FaultIndex fused with RRF."""
    set_org(db, org)
    ids, ambiguous = ({preview_source.id: 0}, False) if preview_source is not None else applicability(db, org, device)
    if ambiguous or not ids:
        return [], ambiguous

    sources = {row.id: row for row in db.scalars(records(org, "source").where(Record.id.in_(ids)))}
    if category:
        sources = {k: v for k, v in sources.items() if v.data.get("category") == category}
        ids = {k: ids[k] for k in sources}
        if not ids:
            return [], False

    source_id_set = set(ids.keys())
    chunks = list(db.scalars(
        select(Chunk).where(Chunk.org == org, Chunk.source.in_(source_id_set)).options(defer(Chunk.embedding))
    ))
    if not chunks:
        return [], False

    # Signal 1: Exact / normalized FaultIndex lookup
    requested_codes = {code_key(c) for c in fault_codes(query, device)}
    if fault_code.strip():
        requested_codes.add(code_key(fault_code.strip()))
    fault_hits: dict[str, float] = defaultdict(float)
    if requested_codes:
        matches = db.execute(select(FaultIndex.chunk, FaultIndex.declared).where(
            FaultIndex.org == org, FaultIndex.source.in_(source_id_set), FaultIndex.code.in_(requested_codes)
        )).all()
        for cid, declared in matches:
            fault_hits[cid] += 2.0 if declared else 1.0

    # Signal 2: Okapi BM25
    q_tokens = tokenize(query + (" " + fault_code if fault_code else ""))
    docs = []
    chunk_by_id: dict[str, Chunk] = {}
    for ch in chunks:
        chunk_by_id[ch.id] = ch
        src = sources[ch.source]
        # Repeat title & position once to weight structural metadata higher in BM25
        full_text = f"{src.data['title']} {ch.position} {src.data['title']} {ch.body}"
        docs.append((ch.id, tokenize(full_text)))
    bm25 = _bm25_scores(q_tokens, docs)

    # Signal 3: Dense Vector Similarity
    vec_sim = _vector_scores(db, org, source_id_set, query, settings)

    # Fuse via Reciprocal Rank Fusion (RRF)
    k = max(1, int(cfg.rrf_k or 60))
    bm25_rank = {cid: idx + 1 for idx, cid in enumerate(sorted(bm25, key=bm25.get, reverse=True))}
    vec_rank = {cid: idx + 1 for idx, cid in enumerate(sorted(vec_sim, key=vec_sim.get, reverse=True))}
    fault_rank = {cid: idx + 1 for idx, cid in enumerate(sorted(fault_hits, key=fault_hits.get, reverse=True))}

    candidate_ids = set(bm25_rank) | set(vec_rank) | set(fault_rank)
    if not candidate_ids:
        return [], False

    scored = []
    for cid in candidate_ids:
        ch = chunk_by_id.get(cid)
        if not ch:
            continue
        src = sources[ch.source]
        rrf = 0.0
        if cid in bm25_rank:
            rrf += 1.0 / (k + bm25_rank[cid])
        if cid in vec_rank:
            rrf += 1.0 / (k + vec_rank[cid])
        if cid in fault_rank:
            rrf += 2.5 / (k + fault_rank[cid])
        # Slight preference for more specific device/batch scope & manuals on ties
        scope_rank = ids[ch.source]
        scope_bonus = max(0.0, (4 - scope_rank) * 0.0005)
        manual_bonus = 0.0004 if src.data.get("category") == "manual" else 0.0
        total = rrf + scope_bonus + manual_bonus
        scored.append((total, ch, bm25.get(cid, 0.0), vec_sim.get(cid, 0.0), fault_hits.get(cid, 0.0)))

    scored.sort(key=lambda item: -item[0])
    seen = set()
    results = []
    for total, ch, bm25_val, vec_val, fault_val in scored:
        key = (ch.source, ch.position, ch.body)
        if key in seen:
            continue
        seen.add(key)
        item = citation(sources[ch.source], ch)
        item["score"] = round(total, 5)
        item["bm25"] = round(bm25_val, 3)
        item["vectorSim"] = round(vec_val, 3)
        item["faultMatch"] = bool(fault_val > 0)
        results.append(item)
        if len(results) >= top_k:
            break
    return results, False


def lookup_fault_code(db, org: str, device: dict, code: str, settings: dict, *,
                      top_k: int = 6, preview_source=None) -> tuple[list[dict], bool]:
    """Targeted exact + fuzzy fault-code retrieval."""
    clean = code.strip()
    if not clean:
        return [], False
    return hybrid_search(db, org, device, f"故障码 {clean}", settings, fault_code=clean,
                         top_k=top_k, preview_source=preview_source)


def get_chunk_context(db, org: str, device: dict, chunk_id: str, *, window: int = 1,
                      preview_source=None) -> list[dict]:
    """Expand a retrieved chunk by fetching its ±window neighboring chunks in the same source."""
    set_org(db, org)
    ids, ambiguous = ({preview_source.id: 0}, False) if preview_source is not None else applicability(db, org, device)
    if ambiguous or not ids:
        return []
    target = db.scalar(select(Chunk).where(Chunk.org == org, Chunk.id == chunk_id).options(defer(Chunk.embedding)))
    if not target or target.source not in ids:
        return []
    source = find(db, org, "source", id=target.source)
    if not source:
        return []
    siblings = list(db.scalars(
        select(Chunk).where(Chunk.org == org, Chunk.source == target.source).options(defer(Chunk.embedding))
    ))
    idx = next((i for i, ch in enumerate(siblings) if ch.id == target.id), -1)
    if idx < 0:
        return []
    start = max(0, idx - max(1, min(3, window)))
    end = min(len(siblings), idx + max(1, min(3, window)) + 1)
    return [citation(source, ch) for ch in siblings[start:end]]


def list_applicable_sources(db, org: str, device: dict, *, preview_source=None) -> tuple[list[dict], bool]:
    """List applicable published knowledge sources for a device so the ReAct agent knows what exists."""
    set_org(db, org)
    ids, ambiguous = ({preview_source.id: 0}, False) if preview_source is not None else applicability(db, org, device)
    if ambiguous or not ids:
        return [], ambiguous
    sources = list(db.scalars(records(org, "source").where(Record.id.in_(ids))))
    summary = []
    for row in sources:
        sample_codes = list(db.scalars(
            select(FaultIndex.code).where(FaultIndex.org == org, FaultIndex.source == row.id).distinct().limit(8)
        ))
        summary.append({
            "sourceId": row.id,
            "title": row.data.get("title", ""),
            "number": row.data.get("number", ""),
            "version": row.data.get("version", ""),
            "category": row.data.get("category", "manual"),
            "chunkCount": row.data.get("chunkCount", 0),
            "sampleFaultCodes": sample_codes,
        })
    return summary, False
