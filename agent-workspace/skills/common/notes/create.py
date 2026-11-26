#!/usr/bin/env python3
"""在项目根目录的 agent-workspace/workers/<个人目录>/notes/ 下创建 Note，并填入能从脚本获取的内容，在任意目录执行都可以。

用法（在项目根目录执行）：
  agent-workspace/.agent/env/venv/bin/python agent-workspace/skills/common/notes/create.py [--type <类型>] <topic-in-kebab-case>
类型：git-commit-change（默认，因未记录的提交触发）或 large-scale-file-change（因工作区大规模改动触发）。
文件名：agent-workspace/workers/<个人目录>/notes/<类型>/<YYYY-MM-DD>/HH-mm-ss-<topic>.md，文件名不含日期；个人目录名取统一 config.json5 的 personal/peronsal；默认 Git 用 git user.name，非 Git 用电脑用户名。

自动填充：
- `时间：` 摘要时间戳，取当前时间；
- `## Git 提交`：git-commit-change 填入尚未记录到任何 Note 的提交（git log --oneline，扫描范围同 verify.py）；没有提交或其他类型时删除该节；
- `## 文件改动`：large-scale-file-change 填入未提交的改动（文件和增删行数，扫描范围同 verify.py），并把这批改动记为已记录；其他类型时删除该节。
每条 Note 独立：每次执行都新建文件，不修改已有 Note，同一 topic 可重复使用。
标题、`## Outcome` / `## Verification` 由 Agent 填写。
"""
import argparse
import datetime
import re
import sys

sys.dont_write_bytecode = True  # 不在工作区副本中生成 __pycache__

from common import (COMMIT_TYPE, HERE, LARGE_TYPE, NOTE_TYPES, day_dir, load_config, notes_dir, project_root,
                    save_large_base, unrecorded_commits, working_changes)

TIME = re.compile(r"^时间：.*$", re.M)


def section(title):
    return re.compile(rf"^## {title}[ \t]*\n(?:(?!^## ).*\n?)*", re.M)


def fill_section(text, title, lines):
    """把 lines 写入 `## <title>` 节的代码块；lines 为空时删除该节。"""
    block = f"## {title}\n\n```\n" + "".join(f"{l}\n" for l in lines) + "```\n\n" if lines else ""
    return section(title).sub(lambda _: block, text, count=1)


def main():
    parser = argparse.ArgumentParser(description="创建 Note")
    parser.add_argument("--type", default=COMMIT_TYPE, choices=NOTE_TYPES, help=f"Note 类型，默认 {COMMIT_TYPE}")
    parser.add_argument("topic", help="小写字母、数字和 -")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", args.topic):
        sys.exit(f"topic 只能用小写字母、数字和 -：{args.topic}")

    root = project_root()
    cfg = load_config(root)
    now = datetime.datetime.now()
    folder = day_dir(notes_dir(root), args.type, now.date())
    folder.mkdir(parents=True, exist_ok=True)
    file = folder / f"{now:%H-%M-%S}-{args.topic}.md"
    if file.exists():
        sys.exit(f"Note 已存在，请稍后重试：{file}")

    commits, changes = [], {}
    if args.type == COMMIT_TYPE:
        commits = [oneline for _, oneline in (unrecorded_commits(root, cfg) or [])]
    else:
        changes = working_changes(root, cfg)
    text = TIME.sub(f"时间：{now:%Y-%m-%d %H:%M:%S}", (HERE / "template.md").read_text(encoding="utf-8"), count=1)
    text = fill_section(text, "Git 提交", commits)
    text = fill_section(text, "文件改动", [f"{n:>6}  {f}" for f, n in sorted(changes.items())])
    file.write_text(text.rstrip("\n") + "\n", encoding="utf-8")
    if args.type == LARGE_TYPE:
        save_large_base(root, changes)
    print(file)
    if commits:
        print(f"已填入 {len(commits)} 笔未记录的提交", file=sys.stderr)
    if changes:
        print(f"已填入 {len(changes)} 个文件、{sum(changes.values())} 行未提交的改动", file=sys.stderr)


if __name__ == "__main__":
    main()
