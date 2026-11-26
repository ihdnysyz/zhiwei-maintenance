#!/usr/bin/env python3
"""独立 SessionStart hook：把当前用户约束放入 Agent 上下文，不依赖欢迎语。"""
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
WS = HERE.parents[2]
sys.path.insert(0, str(WS / 'skills/common'))
from _workspace_config import personal_dir


def local_file(path):
    return all(not parent.is_symlink() for parent in (*reversed(path.parents), path) if parent == WS or WS in parent.parents) and path.is_file()


def context():
    root = WS.parent
    selected = personal_dir(root)
    common = WS / 'skills/custom/sdd/constraints.md'
    if not local_file(common):
        common = WS / 'skills/common/sdd/constraints.md'
    if not local_file(common):
        raise ValueError('通用约束不存在或路径包含符号链接')
    personal = selected / 'specs'
    files = [common]
    # specs 是约束目录，支持多个 Markdown；不沿链接加载其他目录中的私有信息。
    personal_files = []
    if all(not p.is_symlink() for p in (personal, personal.parent, personal.parent.parent)) and personal.is_dir():
        personal_files = sorted(p for p in personal.rglob('*.md') if local_file(p))
    files.extend(personal_files)
    parts = ['以下为当前工作区的开发约束。实施任务前逐条遵循，spec/plan 必须检查；个人约束补充通用约束，冲突时以通用约束为准。'
             '每次新任务、恢复或压缩会话后，重新读取这些源文件，不能把约束仅视为参考。']
    for file in files:
        parts.append(f'## {file.relative_to(root)}\n\n{file.read_text(encoding="utf-8-sig")}')
    if not personal_files:
        parts.append(f'未找到个人约束：{personal.relative_to(root)}/*.md；不要借用其他人的约束。')
    return '\n\n'.join(parts)


if __name__ == '__main__':
    try:
        print(json.dumps({'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext': context()}}, ensure_ascii=False))
    except (OSError, ValueError) as error:
        print(f'约束加载失败，请修正后重试：{error}', file=sys.stderr)
        sys.exit(1)
