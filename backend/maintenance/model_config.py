"""Runtime AI & RAG settings store (DB override with .env fallback) and OpenAI-compatible HTTP client."""
import json
import os
import ssl
import sys
from math import isfinite

import httpx

from .db import add, cfg, find

SETTINGS_KEY = "ai"


def _tls_verification():
    if sys.platform == "win32" and not (os.getenv("SSL_CERT_FILE") or os.getenv("SSL_CERT_DIR")):
        return ssl.create_default_context()
    return True


_http = httpx.Client(verify=_tls_verification())


def _mask_secret(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "***"
    return f"{secret[:4]}***{secret[-4:]}"


def _default_settings() -> dict:
    has_env_model = bool(cfg.model_url and cfg.chat_model and (cfg.allow_model_data or cfg.model_key))
    return {
        "enabled": bool(cfg.allow_model_data and cfg.model_url and cfg.chat_model) if cfg.model_url else has_env_model,
        "chatUrl": cfg.model_url.strip(),
        "chatApiKey": cfg.model_key.strip(),
        "chatModel": cfg.chat_model.strip(),
        "embeddingUrl": cfg.model_url.strip(),
        "embeddingApiKey": cfg.model_key.strip(),
        "embeddingModel": cfg.embedding_model.strip(),
        "chunkSize": 600,
        "chunkOverlap": 100,
        "topK": 6,
        "maxReactSteps": 5,
    }


def get_ai_settings(db, org: str) -> dict:
    base = _default_settings()
    row = find(db, org, "settings", key=SETTINGS_KEY) if db is not None and org else None
    if row and isinstance(row.data, dict):
        saved = row.data
        base.update({
            "enabled": bool(saved.get("enabled", base["enabled"])),
            "chatUrl": str(saved.get("chatUrl", base["chatUrl"])).strip(),
            "chatApiKey": str(saved.get("chatApiKey", base["chatApiKey"])).strip(),
            "chatModel": str(saved.get("chatModel", base["chatModel"])).strip(),
            "embeddingUrl": str(saved.get("embeddingUrl", "")).strip(),
            "embeddingApiKey": str(saved.get("embeddingApiKey", "")).strip(),
            "embeddingModel": str(saved.get("embeddingModel", base["embeddingModel"])).strip(),
            "chunkSize": max(200, min(2000, int(saved.get("chunkSize", 600)))),
            "chunkOverlap": max(0, min(400, int(saved.get("chunkOverlap", 100)))),
            "topK": max(2, min(15, int(saved.get("topK", 6)))),
            "maxReactSteps": max(1, min(8, int(saved.get("maxReactSteps", 5)))),
        })
    if base["chunkOverlap"] >= base["chunkSize"]:
        base["chunkOverlap"] = max(0, base["chunkSize"] // 4)
    return base


def save_ai_settings(db, org: str, payload: dict) -> dict:
    current = get_ai_settings(db, org)
    updated = {
        "enabled": bool(payload.get("enabled", current["enabled"])),
        "chatUrl": str(payload.get("chatUrl", current["chatUrl"])).strip(),
        "chatApiKey": current["chatApiKey"] if payload.get("chatApiKey") in (None, "") else str(payload["chatApiKey"]).strip(),
        "chatModel": str(payload.get("chatModel", current["chatModel"])).strip(),
        "embeddingUrl": str(payload.get("embeddingUrl", current["embeddingUrl"])).strip(),
        "embeddingApiKey": current["embeddingApiKey"] if payload.get("embeddingApiKey") in (None, "") else str(payload["embeddingApiKey"]).strip(),
        "embeddingModel": str(payload.get("embeddingModel", current["embeddingModel"])).strip(),
        "chunkSize": max(200, min(2000, int(payload.get("chunkSize", current["chunkSize"])))),
        "chunkOverlap": max(0, min(400, int(payload.get("chunkOverlap", current["chunkOverlap"])))),
        "topK": max(2, min(15, int(payload.get("topK", current["topK"])))),
        "maxReactSteps": max(1, min(8, int(payload.get("maxReactSteps", current["maxReactSteps"])))),
    }
    if payload.get("clearChatApiKey"):
        updated["chatApiKey"] = ""
    if payload.get("clearEmbeddingApiKey"):
        updated["embeddingApiKey"] = ""
    if updated["chunkOverlap"] >= updated["chunkSize"]:
        updated["chunkOverlap"] = max(0, updated["chunkSize"] // 4)
    row = find(db, org, "settings", key=SETTINGS_KEY, lock=True)
    if row:
        row.change(**updated)
    else:
        add(db, org, "settings", key=SETTINGS_KEY, **updated)
    return updated


def public_ai_settings(settings: dict) -> dict:
    return {
        "enabled": settings["enabled"],
        "chatUrl": settings["chatUrl"],
        "hasChatApiKey": bool(settings["chatApiKey"]),
        "chatApiKeyMask": _mask_secret(settings["chatApiKey"]),
        "chatModel": settings["chatModel"],
        "embeddingUrl": settings["embeddingUrl"],
        "hasEmbeddingApiKey": bool(settings["embeddingApiKey"]),
        "embeddingApiKeyMask": _mask_secret(settings["embeddingApiKey"]),
        "embeddingModel": settings["embeddingModel"],
        "chunkSize": settings["chunkSize"],
        "chunkOverlap": settings["chunkOverlap"],
        "topK": settings["topK"],
        "maxReactSteps": settings["maxReactSteps"],
        "chatReady": chat_ready(settings),
        "embeddingReady": embedding_ready(settings),
    }


def chat_ready(settings: dict) -> bool:
    return bool(settings.get("enabled") and settings.get("chatUrl") and settings.get("chatModel"))


def embedding_ready(settings: dict) -> bool:
    base = settings.get("embeddingUrl") or settings.get("chatUrl")
    return bool(settings.get("enabled") and base and settings.get("embeddingModel"))


def _endpoint_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


class _ThinkTagSplitter:
    """Incrementally split inline <think>...</think> tags across streaming content chunks."""

    _OPEN = "<think>"
    _CLOSE = "</think>"

    def __init__(self) -> None:
        self._in_think = False
        self._buf = ""

    def _safe_prefix_len(self, text: str, tag: str) -> int:
        max_keep = min(len(tag) - 1, len(text))
        for keep in range(max_keep, 0, -1):
            if text.endswith(tag[:keep]):
                return len(text) - keep
        return len(text)

    def feed(self, chunk: str) -> list[tuple[str, str]]:
        if not chunk:
            return []
        self._buf += chunk
        out: list[tuple[str, str]] = []
        while self._buf:
            tag = self._CLOSE if self._in_think else self._OPEN
            kind = "reasoning" if self._in_think else "content"
            idx = self._buf.find(tag)
            if idx != -1:
                if idx > 0:
                    out.append((kind, self._buf[:idx]))
                self._buf = self._buf[idx + len(tag):]
                self._in_think = not self._in_think
                continue
            cut = self._safe_prefix_len(self._buf, tag)
            if cut > 0:
                out.append((kind, self._buf[:cut]))
                self._buf = self._buf[cut:]
            break
        return out

    def flush(self) -> list[tuple[str, str]]:
        if not self._buf:
            return []
        kind = "reasoning" if self._in_think else "content"
        leftover = self._buf
        self._buf = ""
        return [(kind, leftover)]


def extract_thinking_text(message: dict) -> tuple[str, str, str]:
    """Extract (clean_content, reasoning_text, raw_reasoning_field) from a non-streaming assistant message."""
    raw_reasoning = (
        message.get("reasoning_content")
        or message.get("reasoning")
        or message.get("thinking")
        or ""
    )
    if not isinstance(raw_reasoning, str):
        raw_reasoning = ""
    raw_content = message.get("content") or ""
    if not isinstance(raw_content, str):
        raw_content = ""
    splitter = _ThinkTagSplitter()
    content_parts: list[str] = []
    reasoning_parts: list[str] = [raw_reasoning] if raw_reasoning else []
    for kind, text in [*splitter.feed(raw_content), *splitter.flush()]:
        if kind == "reasoning":
            reasoning_parts.append(text)
        else:
            content_parts.append(text)
    return "".join(content_parts).strip(), "".join(reasoning_parts).strip(), raw_reasoning


def chat_completion(settings: dict, messages: list[dict], *, tools: list[dict] | None = None,
                    temperature: float = 0.2, max_tokens: int = 1600,
                    response_format: dict | None = None, timeout: float = 60.0,
                    ignore_enabled: bool = False) -> dict:
    if not (settings.get("chatUrl") and settings.get("chatModel")) or (not ignore_enabled and not settings.get("enabled")):
        raise RuntimeError("对话模型未启用或配置不完整")
    payload: dict = {
        "model": settings["chatModel"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if response_format:
        payload["response_format"] = response_format
    url = _endpoint_url(settings["chatUrl"], "chat/completions")
    response = _http.post(url, headers=_headers(settings.get("chatApiKey", "")),
                          json=payload, timeout=timeout, follow_redirects=False)
    response.raise_for_status()
    return response.json()


def chat_completion_stream_events(settings: dict, messages: list[dict], *,
                                  tools: list[dict] | None = None,
                                  temperature: float = 0.2,
                                  max_tokens: int = 1800,
                                  timeout: float = 120.0):
    """Stream reasoning deltas, content deltas, and accumulated tool_calls from an OpenAI-compatible SSE endpoint."""
    if not chat_ready(settings):
        raise RuntimeError("对话模型未启用或配置不完整")
    payload: dict = {
        "model": settings["chatModel"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    url = _endpoint_url(settings["chatUrl"], "chat/completions")
    splitter = _ThinkTagSplitter()
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    raw_reasoning_parts: list[str] = []
    tool_calls_by_idx: dict[int, dict] = {}

    with _http.stream("POST", url, headers=_headers(settings.get("chatApiKey", "")),
                      json=payload, timeout=timeout, follow_redirects=False) as response:
        response.raise_for_status()
        for raw_line in response.iter_lines():
            if not raw_line:
                continue
            line = raw_line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                packet = json.loads(data)
            except ValueError:
                continue
            choices = packet.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}

            r_chunk = delta.get("reasoning_content") or delta.get("reasoning") or delta.get("thinking")
            if isinstance(r_chunk, str) and r_chunk:
                raw_reasoning_parts.append(r_chunk)
                reasoning_parts.append(r_chunk)
                yield {"type": "reasoning", "delta": r_chunk}

            c_chunk = delta.get("content")
            if isinstance(c_chunk, str) and c_chunk:
                for kind, seg in splitter.feed(c_chunk):
                    if not seg:
                        continue
                    if kind == "reasoning":
                        reasoning_parts.append(seg)
                        yield {"type": "reasoning", "delta": seg}
                    else:
                        content_parts.append(seg)
                        yield {"type": "content", "delta": seg}

            for tc in delta.get("tool_calls") or []:
                idx = int(tc.get("index", 0))
                slot = tool_calls_by_idx.setdefault(idx, {
                    "id": "",
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                })
                if tc.get("id"):
                    slot["id"] = str(tc["id"])
                if tc.get("type"):
                    slot["type"] = str(tc["type"])
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] += str(fn["name"])
                if fn.get("arguments"):
                    slot["function"]["arguments"] += str(fn["arguments"])

    for kind, seg in splitter.flush():
        if not seg:
            continue
        if kind == "reasoning":
            reasoning_parts.append(seg)
            yield {"type": "reasoning", "delta": seg}
        else:
            content_parts.append(seg)
            yield {"type": "content", "delta": seg}

    ordered_calls = [tool_calls_by_idx[k] for k in sorted(tool_calls_by_idx) if tool_calls_by_idx[k]["function"]["name"]]
    yield {
        "type": "finish",
        "content": "".join(content_parts).strip(),
        "reasoning": "".join(reasoning_parts).strip(),
        "raw_reasoning": "".join(raw_reasoning_parts),
        "tool_calls": ordered_calls,
    }


def chat_completion_stream(settings: dict, messages: list[dict], *, temperature: float = 0.2,
                           max_tokens: int = 1800, timeout: float = 120.0):
    """Yield incremental content delta strings from an OpenAI-compatible SSE stream."""
    for ev in chat_completion_stream_events(settings, messages, temperature=temperature,
                                            max_tokens=max_tokens, timeout=timeout):
        if ev["type"] == "content":
            yield ev["delta"]


def embed_texts_remote(settings: dict, texts: list[str], *, timeout: float = 30.0,
                       ignore_enabled: bool = False) -> list[list[float]]:
    base = settings.get("embeddingUrl") or settings.get("chatUrl")
    key = settings.get("embeddingApiKey") or settings.get("chatApiKey", "")
    model = settings.get("embeddingModel", "")
    if not (base and model) or (not ignore_enabled and not settings.get("enabled")):
        raise RuntimeError("向量模型未启用或配置不完整")
    url = _endpoint_url(base, "embeddings")
    response = _http.post(url, headers=_headers(key), json={"model": model, "input": texts},
                          timeout=timeout, follow_redirects=False)
    response.raise_for_status()
    rows = response.json()["data"]
    vectors = [row["embedding"] for row in sorted(rows, key=lambda item: item["index"])]
    if len(vectors) != len(texts):
        raise ValueError("向量服务返回数量不一致")
    for vec in vectors:
        if (not isinstance(vec, list) or not 1 <= len(vec) <= 16000
                or any(type(x) not in {int, float} or not isfinite(x) for x in vec)
                or not any(vec)):
            raise ValueError("向量服务返回无效向量")
    return vectors
