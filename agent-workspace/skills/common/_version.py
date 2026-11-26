"""主仓库版本文件和 MAJOR.MINOR.PATCH 版本规则；所有入口共用。"""
import base64
import json
import os
from pathlib import Path
import re
import urllib.parse
import urllib.request

PAGE_URL = "https://github.com/LumenVestige/AgentDevWorkSpace/blob/master/publish/version"
RAW_URL = "https://raw.githubusercontent.com/LumenVestige/AgentDevWorkSpace/master/publish/version"
API_URL = "https://api.github.com/repos/LumenVestige/AgentDevWorkSpace/contents/publish/version?ref=master"
DEFAULT_URL = API_URL
PART = r"(?:0|[1-9][0-9]*)"


def parse_version(text, allow_legacy=False):
    text = str(text).strip()
    pattern = rf"{PART}\.{PART}\.{PART}"
    if allow_legacy:
        pattern = rf"{PART}(?:\.{PART}){{0,2}}"
    if not re.fullmatch(pattern, text):
        raise ValueError(f"无效版本号 {text!r}：需要大版本.小版本.补丁版本（如 0.3.0）")
    parts = tuple(int(part) for part in text.split("."))
    return parts + (0,) * (3 - len(parts))


def format_version(value):
    if isinstance(value, str):
        value = parse_version(value, allow_legacy=True)
    if len(value) != 3 or any(type(part) is not int or part < 0 for part in value):
        raise ValueError("版本必须由三个非负整数组成")
    return ".".join(str(part) for part in value)


def read_version_file(file):
    return parse_version(Path(file).read_text(encoding="utf-8"), allow_legacy=True)


def next_patch(value):
    return value[0], value[1], value[2] + 1


def normalize_url(url):
    """旧配置只用于识别并映射，不会向已废弃服务发送请求。"""
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.casefold()
    if parsed.hostname in {"github.com", "raw.githubusercontent.com"} and path.startswith("/lumenvestige/versions/"):
        return DEFAULT_URL
    if parsed.hostname == "api.github.com" and path.startswith("/repos/lumenvestige/versions/"):
        return DEFAULT_URL
    if url == PAGE_URL:
        return DEFAULT_URL
    return url


def fetch_remote_version(url=None, timeout=5):
    """只有取得可解析的版本号才成功；失败尝试下一地址，timeout 为每个接口的超时秒数。"""
    url = normalize_url(url or DEFAULT_URL)
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if url in {RAW_URL, API_URL}:
        urls = [API_URL, RAW_URL]
    else:
        urls = [url]
    errors = []
    for address in urls:
        headers = {"Accept": "application/vnd.github.raw", "User-Agent": "AgentDevWorkSpace"}
        if token and address == API_URL:
            headers["Authorization"] = "Bearer " + token
        try:
            request = urllib.request.Request(address, headers=headers)
            with urllib.request.urlopen(request, timeout=max(0.1, timeout)) as response:
                body = response.read(8192).decode("utf-8")
            if body.lstrip().startswith("{"):
                payload = json.loads(body)
                content = payload.get("content")
                if not isinstance(content, str) or not content.strip():
                    raise ValueError(f"响应不包含版本内容：{payload.get('message') or '缺少有效 content'}")
                body = base64.b64decode(content).decode("utf-8")
            return parse_version(body, allow_legacy=True)
        except (OSError, ValueError, KeyError, TypeError) as error:
            errors.append(str(error))
    raise RuntimeError("读取主仓库 publish/version 失败：" + "; ".join(errors))
