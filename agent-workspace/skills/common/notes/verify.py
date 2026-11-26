#!/usr/bin/env python3
"""每轮对话结束时执行：判断是否需要触发 Note，并按上限合并旧 Note。Note 的内容由 create.py 填充。

用法（在项目根目录执行）：agent-workspace/.agent/env/venv/bin/python agent-workspace/skills/common/notes/verify.py
作为 Claude Code / Codex 的 Stop hook 时从 stdin 读取事件 JSON；需要写 Note 时以退出码 2 把提示交回 Agent。

Note 位于 agent-workspace/workers/<个人目录>/notes/<类型>/<YYYY-MM-DD>/HH-mm-ss-<topic>.md，类型为 git-commit-change 或 large-scale-file-change。

配置：agent-workspace/config/config.json5（JSON5，字段含义见其中的注释；缺少的键取本 skill 中 config.json5 的默认值）
- enabled：false 时不触发 Note、不合并。
- max_notes：当前用户每个日期目录中的 Note 超过该数量时，把最早的几条按时间顺序合并为一条，使数量回到上限；
  合并保留全部内容（含提交 hash），不删除内容，不处理别人的 Note。
- auto_update / auto_update_interval_hours / update_url：每次运行都调用 update.py 的 trigger()，检查到期时在后台检查版本，
  需要更新时立即拉取并执行 setup.sh，不阻塞本 hook；与 enabled 相互独立。
- scan：触发条件。commits 为 true 时，基线之后存在未记录到任何 Note 的提交即触发 git-commit-change；
  include_merges 是否计入 merge 提交；paths / exclude_paths 只统计 / 不统计改动这些路径的提交和工作区改动（git pathspec）；
  large_change：未提交的改动相对上次记录（agent-workspace/.agent/env/large-change-base）新增的文件数或行数达到阈值时触发 large-scale-file-change。

全部提交都已记录时，基线（agent-workspace/.agent/env/note-base）前移到 HEAD，此后合并旧 Note 也不会让这些提交重新触发。
改动快照每次运行都下调到不超过当前改动（提交或撤销后不误触发）；写 large-scale-file-change Note 时由 create.py 更新为当前改动。
"""
import json
import re
import select
import sys

sys.dont_write_bytecode = True  # 不在工作区副本中生成 __pycache__

import update
from common import (LARGE_TYPE, day_dirs, large_change_growth, load_large_base, load_config, notes_dir, project_root,
                    reset_base, save_large_base, unrecorded_commits, working_changes)

BLOCK = 2  # Claude Code / Codex 的 Stop hook：退出码 2 阻止结束，stderr 交给 Agent 继续处理


def hook_input():
    """Stop hook 会立即写入事件 JSON；手动执行时 stdin 可能是终端或一直不关闭的管道，等不到数据就跳过，避免卡住。"""
    try:
        if sys.stdin is None or sys.stdin.isatty() or not select.select([sys.stdin], [], [], 1)[0]:
            return {}
        return json.loads(sys.stdin.read() or "{}")
    except (OSError, ValueError):
        return {}


MERGED_MARK = "<!-- merged-note -->"
SOURCE = re.compile(r"^<!-- source: .* -->$", re.M)
CREATE = "agent-workspace/.agent/env/venv/bin/python agent-workspace/skills/common/notes/create.py"


def demote(text):
    """标题降一级（代码块内的行不动），使原 Note 的 # 标题成为合并文件中的 ## 小节。"""
    out, fence = [], False
    for line in text.splitlines():
        if line.startswith("```"):
            fence = not fence
        out.append("#" + line if not fence and re.match(r"#+ ", line) else line)
    return "\n".join(out).strip("\n")


def entries(path):
    """一条 Note 在合并文件中的内容；本身是合并文件时取出其中已有的各条，不再降级。"""
    text = path.read_text(encoding="utf-8")
    if MERGED_MARK not in text:
        return [f"<!-- source: {path.name} -->\n\n{demote(text)}"]
    m = SOURCE.search(text)
    if not m:
        return []
    parts = re.split(r"\n+---\n+(?=<!-- source: )", text[m.start():])
    return [x.strip("\n") for x in parts]


def merge(folder, limit):
    """日期目录中的 Note 超过 limit 时，把最早的 n - limit + 1 条按时间顺序合并为一条。先写合并文件，再删除被合并的原文件。"""
    existing = sorted(folder.glob("*.md"))
    if limit < 1 or len(existing) <= limit:
        return
    group = existing[: len(existing) - limit + 1]
    prefix = group[0].name[:8]  # HH-mm-ss：合并文件按最早一条的时间排序
    target, i = folder / f"{prefix}-merged.md", 2
    while target.exists() and target not in group:
        target, i = folder / f"{prefix}-merged-{i}.md", i + 1
    items = [e for f in group for e in entries(f)]
    body = "\n\n---\n\n".join(items)
    target.write_text(f"# 合并的 Note\n\n{MERGED_MARK}\n"
                      f"日期目录中的 Note 超过上限时由 verify.py 合并生成，按时间顺序收录 {len(items)} 条 Note，内容保持原样（标题降一级）。\n\n"
                      f"---\n\n{body}\n", encoding="utf-8")
    for f in group:
        if f != target:
            f.unlink()
    print(f"{folder} 的 Note 超过 {limit} 条，已把最早的 {len(group)} 条合并到 {target.name}", file=sys.stderr)


def large_change(root, cfg):
    """未提交的改动相对快照新增的量达到阈值时返回 (文件数, 行数)，否则返回 None。快照不存在时以当前改动建立快照、不触发。"""
    lc = cfg["scan"]["large_change"]
    if not lc.get("enabled", True):
        return None
    current = working_changes(root, cfg)
    base = load_large_base(root)
    if base is None:
        save_large_base(root, current)
        return None
    # 已提交或撤销的改动从快照中收缩掉，之后重新增长的部分照常计入。
    base = {f: min(n, current[f]) for f, n in base.items() if f in current}
    save_large_base(root, base)
    files, lines = large_change_growth(current, base)
    max_files, max_lines = int(lc.get("files", 0)), int(lc.get("lines", 0))
    if (max_files > 0 and files >= max_files) or (max_lines > 0 and lines >= max_lines):
        return files, lines
    return None


def check_notes(root, cfg, event):
    """判断是否需要写 Note、合并超过上限的 Note；返回 hook 的退出码。"""
    notes = notes_dir(root)
    notes.mkdir(parents=True, exist_ok=True)

    pending = unrecorded_commits(root, cfg)
    if not pending:
        # 全部已记录，或基线不可用（首次执行、rebase 等）：基线前移到 HEAD。
        reset_base(root)
    large = large_change(root, cfg)
    for folder in day_dirs(notes):
        merge(folder, int(cfg["max_notes"]))
    if not pending and not large:
        return 0
    if pending:
        print(f"有 {len(pending)} 笔提交尚未记录到 Note，请按 notes skill（agent-workspace/skills/common/notes/SKILL.md）写 Note，在项目根目录执行：\n"
              f"{CREATE} <topic>", file=sys.stderr)
    if large:
        print(f"工作区有大规模未提交的改动（新增 {large[0]} 个文件、{large[1]} 行），请按 notes skill 写 Note，在项目根目录执行：\n"
              f"{CREATE} --type {LARGE_TYPE} <topic>", file=sys.stderr)
    # Stop hook 已经因本脚本续跑过一轮时不再阻止，避免循环。
    return 0 if event.get("stop_hook_active") else BLOCK


def main():
    event = hook_input()
    root = project_root()
    cfg = load_config(root)
    # 检查到期时在后台更新，不阻塞本 hook。
    update.trigger(root, cfg)
    return check_notes(root, cfg, event) if cfg["enabled"] else 0


if __name__ == "__main__":
    sys.exit(main())
