"""notes 脚本共用：git 调用、项目根目录定位、配置读取与未记录提交扫描。

脚本在工作区副本 <项目根目录>/agent-workspace/skills/common/notes/ 中运行（init.sh 从 AgentDevWorkSpace 的 skills/ 复制），
按自身位置找到项目根目录，按 agent-workspace/.agent/env/agentskill-path 找到 AgentDevWorkSpace。
"""
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
if not (HERE.parent / "_workspace_config.py").is_file():
    sys.path.insert(0, str(HERE.parent.parent / "common"))
from _workspace_config import load_config as workspace_config, personal_name, personal_dir
ROOT = HERE.parents[3]  # 项目根目录（本文件位于 agent-workspace/skills/common/notes/）
ENV = ROOT / "agent-workspace" / ".agent" / "env"

try:
    import json5
except ImportError:
    sys.exit(f"缺少依赖 json5：请执行 source setup.sh 和 agentdev init，并使用 {ENV / 'python-path'} 记录的解释器运行")

# hook 环境会导出 GIT_DIR，影响 git -C 的结果。
for var in ("GIT_DIR", "GIT_WORK_TREE"):
    os.environ.pop(var, None)


def git(*args):
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def project_root():
    """按本文件的位置取项目根目录；不在工作区副本中运行（例如直接运行 AgentDevWorkSpace 模板包中的脚本）时报错退出。"""
    if HERE.parent.name not in {"common", "custom"} or [x.name for x in HERE.parents[1:3]] != ["skills", "agent-workspace"]:
        sys.exit(f"{HERE} 不是工作区中的 notes skill：请运行 <项目根目录>/agent-workspace/skills/common/notes/ 下的脚本")
    return ROOT


def skill_dir():
    """AgentDevWorkSpace 仓库目录，由 init.sh 写入；文件缺失或目录不存在时返回 None。"""
    try:
        d = Path((ENV / "agentskill-path").read_text(encoding="utf-8").strip())
    except OSError:
        return None
    return d if (d / "scripts" / "setup.sh").is_file() else None


def python_path(root):
    """优先使用全局安装记录的解释器，兼容旧项目本地 venv。"""
    env = root / "agent-workspace" / ".agent" / "env"
    try:
        text = (env / "python-path").read_text(encoding="utf-8").strip()
        if text:
            return Path(text)
    except OSError:
        pass
    return env / "venv" / "bin/python"


def user_dir_name(root):
    """所有工作区命令共用 personal/peronsal、Git 和系统用户名规则。"""
    return personal_name(root)


# Note 类型：notes/ 下的一级目录。git-commit-change 因未记录的提交触发，large-scale-file-change 因工作区大规模改动触发。
COMMIT_TYPE = "git-commit-change"
LARGE_TYPE = "large-scale-file-change"
NOTE_TYPES = (COMMIT_TYPE, LARGE_TYPE)
DATE_DIR = re.compile(r"\d{4}-\d{2}-\d{2}")


def notes_dir(root):
    """当前用户的 Note 目录：agent-workspace/workers/<个人目录>/notes。"""
    return personal_dir(root) / "notes"


def day_dir(notes, note_type, day=None):
    """某类型某天的 Note 目录：notes/<类型>/<YYYY-MM-DD>；day 缺省取今天。"""
    return notes / note_type / f"{day or datetime.date.today():%Y-%m-%d}"


def day_dirs(notes):
    """当前用户各类型下的全部日期目录；notes/ 下旧的平铺 Note 不在其中。"""
    return [d for t in NOTE_TYPES if (notes / t).is_dir()
            for d in sorted((notes / t).iterdir()) if d.is_dir() and DATE_DIR.fullmatch(d.name)]


def all_notes(root):
    """所有人的 Note（含 notes/ 下旧的平铺 Note），用于判断提交是否已记录：别人的提交记在别人的个人目录中。"""
    ws = root / "agent-workspace"
    for base in (ws / "workers", ws):
        if not base.is_dir() or base.is_symlink():
            continue
        for personal in base.iterdir():
            if personal.is_symlink() or personal.name in {"workers", "skills", "config", ".agent"}:
                continue
            notes = personal / "notes"
            if not notes.is_symlink():
                yield from (p for p in notes.rglob("*.md") if not p.is_symlink())


def base_file(root):
    return ENV / "note-base"


def update_stamp_file(root):
    return ENV / "last-update"


def large_base_file(root):
    return ENV / "large-change-base"


def load_config(root):
    """读取统一 config.json5，兼容旧 notes.json5；保留自定义 notes 默认值。"""
    try:
        return workspace_config(root, HERE / "config.json5")
    except ValueError as error:
        sys.exit(f"配置文件格式错误：{error}")


def recorded_hashes(root):
    text = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in all_notes(root))
    return set(re.findall(r"\b[0-9a-f]{7,40}\b", text))


def unrecorded_commits(root, cfg):
    """按扫描配置返回基线之后、hash 未出现在任何 Note 中的提交，元素为 (完整 hash, git log --oneline 行)，从新到旧。

    返回 None 表示基线不可用（首次执行或基线已失效），调用方应把基线重置为 HEAD。
    """
    scan = cfg["scan"]
    if not scan.get("commits", True):
        return []
    state = base_file(root)
    if not state.exists():
        return None
    if not git("-C", str(root), "rev-parse", "-q", "--verify", "HEAD"):
        return []
    # 基线为空表示 setup 时仓库还没有提交，从第一笔提交算起。
    base = state.read_text().strip()
    if base and not git("-C", str(root), "rev-parse", "-q", "--verify", f"{base}^{{commit}}"):
        return None
    args = ["-C", str(root), "log", "--format=%H%x09%h %s", f"{base}..HEAD" if base else "HEAD"]
    if not scan.get("include_merges", False):
        args.append("--no-merges")
    args += pathspec(scan)
    recorded = recorded_hashes(root)
    result = []
    for line in filter(None, git(*args).splitlines()):
        full, oneline = line.split("\t", 1)
        if not any(full.startswith(h) for h in recorded):
            result.append((full, oneline))
    return result


def pathspec(scan):
    """按扫描配置生成 git pathspec（含开头的 --）。

    Note 随项目提交：各人 notes/ 不计入，否则提交 Note 本身又会触发新的 Note。
    """
    excludes = [*scan.get("exclude_paths", []), "agent-workspace/workers/*/notes/*", "agent-workspace/*/notes/*"]
    return ["--", *scan.get("paths", []), *(f":(exclude){p}" for p in excludes)]


EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def line_count(path):
    try:
        data = path.read_bytes()
    except OSError:
        return 0
    if b"\0" in data[:8000]:  # 二进制文件与 git numstat 一致，不计行数
        return 0
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


def working_changes(root, cfg):
    """未提交的改动（相对 HEAD，含未跟踪文件），按扫描配置过滤：{文件: 增删行数}。二进制文件记 0 行。"""
    spec = pathspec(cfg["scan"])
    head = "HEAD" if git("-C", str(root), "rev-parse", "-q", "--verify", "HEAD") else EMPTY_TREE
    result = {}
    out = git("-C", str(root), "diff", head, "--numstat", "--no-renames", "-z", *spec)
    for rec in filter(None, out.split("\0")):
        added, deleted, path = rec.split("\t", 2)
        result[path] = (int(added) if added.isdigit() else 0) + (int(deleted) if deleted.isdigit() else 0)
    out = git("-C", str(root), "ls-files", "--others", "--exclude-standard", "-z", *spec)
    for path in filter(None, out.split("\0")):
        result[path] = line_count(root / path)
    return result


def load_large_base(root):
    """上次记录的改动快照；不存在或损坏时返回 None。"""
    try:
        data = json.loads(large_base_file(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return {k: int(v) for k, v in data.items()} if isinstance(data, dict) else None


def save_large_base(root, changes):
    f = large_base_file(root)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(changes, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def large_change_growth(current, base):
    """相对快照新增的改动：(新增改动的文件数, 新增行数)。"""
    grown = {f: n - base.get(f, 0) for f, n in current.items() if f not in base or n > base[f]}
    return len(grown), sum(grown.values())


def reset_base(root):
    state = base_file(root)
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(git("-C", str(root), "rev-parse", "-q", "--verify", "HEAD") + "\n")
