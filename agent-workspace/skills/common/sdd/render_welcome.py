#!/usr/bin/env python3
"""将欢迎语中的 Markdown 表格渲染为中文对齐的终端表格；hook 可复用无 ANSI 的文本。"""
import argparse
import os
import re
import shutil
import sys
import unicodedata
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def paint(text, code, color):
    return f"\033[{code}m{text}\033[0m" if color else text


def char_width(char):
    if unicodedata.combining(char) or unicodedata.category(char) in {"Mn", "Me", "Cf"}:
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


def display_width(text):
    return sum(char_width(char) for char in ANSI.sub("", text))


def wrap_cell(text, width):
    lines, line, used = [], "", 0
    for word in re.findall(r"[!-~]+|.", text):
        if line and display_width(word) <= width and used + display_width(word) > width:
            lines.append(line.rstrip())
            line, used = "", 0
        for char in word:
            size = char_width(char)
            if line and used + size > width:
                lines.append(line.rstrip())
                line, used = "", 0
            if not line and char == " ":
                continue
            line += char
            used += size
    return [*lines, line]


def cells(line):
    return [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]


def render_table(rows, columns, color):
    widths = [max(display_width(row[i]) for row in rows) for i in range(len(rows[0]))]
    budget = max(2 * len(widths), columns - (3 * len(widths) + 1))
    while sum(widths) > budget:
        index = max(range(len(widths)), key=lambda i: (widths[i], i))
        widths[index] -= 1

    def border(left, middle, right):
        return paint(left + middle.join("─" * (width + 2) for width in widths) + right, "2", color)

    output = [border("┌", "┬", "┐")]
    for row_index, row in enumerate(rows):
        wrapped = [wrap_cell(cell, width) for cell, width in zip(row, widths)]
        for part in range(max(map(len, wrapped))):
            values = []
            for i, (lines, width) in enumerate(zip(wrapped, widths)):
                value = lines[part] if part < len(lines) else ""
                value += " " * (width - display_width(value))
                code = "1" if row_index == 0 else "36" if i == 0 else "32" if i == 2 else "0"
                values.append(" " + paint(value, code, color) + " ")
            edge = paint("│", "2", color)
            output.append(edge + edge.join(values) + edge)
        if row_index == 0:
            output.append(border("├", "┼", "┤"))
    output.append(border("└", "┴", "┘"))
    return "\n".join(output)


def render_version(line, color):
    failed = any(word in line for word in ("失败", "找不到", "风险"))
    code = "31" if failed else "32" if "已是最新" in line else "33"
    icon = "⚠" if failed else "✅" if "已是最新" in line else "🔄"
    match = re.fullmatch(r"AgentDevWorkSpace 版本：本地 (.+)，远端 (.+)（(.+)）", line)
    if match:
        local, remote, state = match.groups()
        return (f"📦 本地 {paint(local, '1;36', color)}  ·  远端 {paint(remote, '1;36', color)}"
                f"  ·  {icon} {paint(state, code, color)}")
    return f"{icon} {paint(line, code, color)}"


def render_welcome(text, version="", color=False, columns=80):
    lines = text.strip().splitlines()
    output = []
    if lines and lines[0].startswith("# "):
        output.append(paint(lines.pop(0)[2:], "1;36", color))
        while lines and not lines[0].strip():
            lines.pop(0)
    if version:
        output.extend(render_version(line, color) for line in version.splitlines())
    index = 0
    while index < len(lines):
        line = lines[index]
        if (line.strip().startswith("|") and index + 1 < len(lines)
                and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells(lines[index + 1]))
                and len(cells(line)) == len(cells(lines[index + 1]))):
            rows = [cells(line)]
            index += 2
            while index < len(lines) and lines[index].strip().startswith("|") and len(cells(lines[index])) == len(rows[0]):
                rows.append(cells(lines[index]))
                index += 1
            output.append(render_table(rows, columns, color))
            continue
        if line.startswith("## "):
            line = paint(line[3:], "1;35", color)
        elif line.startswith("💡"):
            line = paint(line, "2", color)
        output.append(line)
        index += 1
    return "\n".join(output).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--version-line", default="")
    args = parser.parse_args()
    color = sys.stdout.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"
    text = args.file.read_text(encoding="utf-8")
    print("\n" + render_welcome(text, args.version_line, color, shutil.get_terminal_size().columns) + "\n")


if __name__ == "__main__":
    main()
