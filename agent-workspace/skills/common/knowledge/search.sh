#!/bin/sh
# 用法（在项目根目录执行）：sh agent-workspace/skills/common/knowledge/search.sh <关键词> [关键词…]
# 在开发知识库中检索：所有人的 feature / bugfix / change / research 文档（含 NN-<主题>/ 子目录中的 .md）、Note、个人约束、通用约束，以及 git 提交信息。
# 多个关键词须同时出现在同一个文件（或同一条提交信息）中；不区分大小写，按字面匹配（不是正则）。
# 每个命中的文件列出路径、所属需求的 spec 头部（类型、编号、负责人、状态）和最多 3 行命中内容。
unset GIT_DIR GIT_WORK_TREE

if [ $# -eq 0 ]; then
  echo "用法：sh $0 <关键词> [关键词…]" >&2
  exit 1
fi

# 本脚本位于 <项目根目录>/agent-workspace/skills/<common|custom>/knowledge/。
ws=$(cd "$(dirname "$0")/../../.." && pwd -P)
if [ "$(basename "$ws")" != agent-workspace ]; then
  echo "$0 不在工作区中：请运行 <项目根目录>/agent-workspace/skills/common/knowledge/search.sh" >&2
  exit 1
fi
cd "$(dirname "$ws")" || exit 1

# 关键词逐行写入临时文件，供 grep -f 和逐个检查使用，避免引号、空格等字符的转义问题。
kwfile=$(mktemp) || exit 1
listfile=$(mktemp) || { rm -f "$kwfile"; exit 1; }
trap 'rm -f "$kwfile" "$listfile"' EXIT
for k in "$@"; do printf '%s\n' "$k" >> "$kwfile"; done

# 文件是否包含全部关键词。
has_all() {
  while IFS= read -r kw; do
    grep -qiF -e "$kw" "$1" || return 1
  done < "$kwfile"
}

# 打印一个命中的文件：路径、所属需求的 spec 头部、最多 3 行命中内容。
show() {
  echo "- $1"
  # 需求目录根下的文档，或已归类到 NN-<主题>/ 子目录中的文档（见 sdd skill「需求目录内的文件」）。
  d=$(dirname "$1")
  while [ "$d" != . ] && [ "$d" != agent-workspace ] && [ ! -f "$d/spec.md" ]; do
    d=$(dirname "$d")
  done
  spec=$d/spec.md
  if [ -f "$spec" ]; then
    head=$(grep -m1 '^类型：' "$spec") && echo "    [$head]"
  fi
  grep -niF -f "$kwfile" -m 3 "$1" | cut -c1-200 | sed 's/^/    /'
}

total=0
# 在一组文件中检索；有命中时打印标题和结果。
search() {
  title=$1; shift
  n=0
  for f in "$@"; do
    [ -f "$f" ] && has_all "$f" || continue
    [ "$n" -gt 0 ] || echo "== $title =="
    n=$((n + 1))
    show "$f"
  done
  [ "$n" -eq 0 ] || echo
  total=$((total + n))
}

# 递归检索 archived 分组和主题附件，不跟随目录符号链接；read 保留含空格的路径。
search_tree() {
  title=$1; shift
  : > "$listfile"
  for directory in "$@"; do
    [ ! -L "$directory" ] && [ -d "$directory" ] || continue
    find "$directory" -type f -name '*.md' -print >> "$listfile"
  done
  n=0
  while IFS= read -r f; do
    has_all "$f" || continue
    [ "$n" -gt 0 ] || echo "== $title =="
    n=$((n + 1))
    show "$f"
  done < "$listfile"
  [ "$n" -eq 0 ] || echo
  total=$((total + n))
}

for W in agent-workspace/workers agent-workspace; do
search_tree "功能 feature" $W/*/feature
search_tree "bug 修复 bugfix" $W/*/bugfix
search_tree "变更 change" $W/*/change
search_tree "调研 research" $W/*/research
search_tree "Note" $W/*/notes
search_tree "个人约束" $W/*/specs
done
search "通用约束" agent-workspace/skills/custom/sdd/constraints.md agent-workspace/skills/common/sdd/constraints.md

# 提交信息：全部关键词都出现的提交，最多 20 条。
if git rev-parse --git-dir >/dev/null 2>&1; then
  commits=$(
    set --
    while IFS= read -r kw; do set -- "$@" --grep="$kw"; done < "$kwfile"
    git log --oneline -i -F --all-match -n 20 "$@"
  )
  if [ -n "$commits" ]; then
    echo "== Git 提交（最多 20 条）=="
    echo "$commits" | sed 's/^/- /'
    echo
    total=$((total + $(echo "$commits" | wc -l)))
  fi
fi

[ "$total" -gt 0 ] || echo "未找到同时包含以下关键词的内容：$*"
