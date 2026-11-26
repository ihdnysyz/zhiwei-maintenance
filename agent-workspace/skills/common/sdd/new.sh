#!/bin/sh
# 用法（在项目根目录执行）：sh agent-workspace/skills/common/sdd/new.sh <feature|bugfix|change|research> <名称>
# 通常通过 new_feature.sh / new_bugfix.sh / new_change.sh / new_research.sh 调用。
# 在当前用户的个人目录下新建 agent-workspace/workers/<个人目录>/<类型>/NNN-<slug>/（类型为 feature、bugfix、change、research），
# 复制该类型的 spec.md、plan.md、tasks.md 模板，填入名称、编号、负责人，最后输出目录的路径。
# - 个人目录名取 config.json5 的 personal/peronsal；默认 Git 根目录取 git user.name，非 Git 取电脑用户名，统一规范化；
# - slug 由名称按同样规则生成，例如 "Login Page" → login-page，中文原样保留；
# - NNN 取该类型目录下已有的最大编号加一，每种类型单独编号，从 001 开始；同类型同名已存在时报错；
# - 模板和约束规范优先取 skills/custom/sdd/，没有时取 skills/common/sdd/。
set -e
unset GIT_DIR GIT_WORK_TREE

# 与 init.sh 一致的名称规范化。
slug() {
  printf '%s' "$1" | LC_ALL=C tr '[:upper:]' '[:lower:]' |
    LC_ALL=C sed -e 's/[[:space:][:punct:][:cntrl:]]/-/g' -e 's/--*/-/g' -e 's/^-//' -e 's/-$//'
}
# 转义 sed 替换串中的特殊字符（分隔符用 |）。
esc() { printf '%s' "$1" | sed 's/[\\|&]/\\&/g'; }

type=$1
case "$type" in feature|bugfix|change|research) shift ;; *)
  echo "用法：sh $0 <feature|bugfix|change|research> <名称>" >&2
  exit 1 ;;
esac
name=$*
if [ -z "$name" ]; then
  echo "用法：sh $(dirname "$0")/new_${type}.sh <名称>" >&2
  exit 1
fi
feature=$(slug "$name")
if [ -z "$feature" ]; then
  echo "需求名称中没有可用字符：$name" >&2
  exit 1
fi

# 本脚本位于 <项目根目录>/agent-workspace/skills/<common|custom>/sdd/。
ws=$(cd "$(dirname "$0")/../../.." && pwd -P)
if [ "$(basename "$ws")" != agent-workspace ]; then
  echo "$0 不在工作区中：请运行 <项目根目录>/agent-workspace/skills/common/sdd/new_${type}.sh" >&2
  exit 1
fi
root=$(dirname "$ws")

py="$ws/.agent/env/venv/bin/python"
[ ! -f "$ws/.agent/env/python-path" ] || py=$(cat "$ws/.agent/env/python-path")
helper="$ws/skills/common/_workspace_config.py"
user_dir=$("$py" "$helper" "$root" --personal)
user_name=$("$py" "$helper" "$root" --owner)

if [ -d "$ws/skills/custom/sdd/templates" ]; then sdd=custom; else sdd=common; fi
templates="$ws/skills/$sdd/sdd/templates/$type"
if [ ! -f "$ws/workers/$user_dir/specs/constraints.md" ]; then
  mkdir -p "$ws/workers/$user_dir/specs"
  cp "$ws/skills/$sdd/sdd/personal-constraints.md" "$ws/workers/$user_dir/specs/constraints.md"
fi
type_dir="$ws/workers/$user_dir/$type"
mkdir -p "$type_dir"

# 同类型同名已存在时报错，不重复创建。
for d in "$type_dir"/[0-9][0-9][0-9]-"$feature" "$ws/$user_dir/$type"/[0-9][0-9][0-9]-"$feature" "$type_dir"/archived/[0-9]*/[0-9][0-9][0-9]-"$feature" "$ws/$user_dir/$type"/archived/[0-9]*/[0-9][0-9][0-9]-"$feature"; do
  if [ -d "$d" ]; then
    echo "已存在：${d#"$root"/}" >&2
    exit 1
  fi
done

# 已有的最大编号加一。
last=0
for d in "$type_dir"/[0-9][0-9][0-9]-* "$ws/$user_dir/$type"/[0-9][0-9][0-9]-* "$type_dir"/archived/[0-9]*/[0-9][0-9][0-9]-* "$ws/$user_dir/$type"/archived/[0-9]*/[0-9][0-9][0-9]-*; do
  [ -d "$d" ] || continue
  n=$(basename "$d" | cut -c1-3)
  n=$(expr "$n" + 0)
  [ "$n" -le "$last" ] || last=$n
done
num=$(printf '%03d' $((last + 1)))

dir="$type_dir/$num-$feature"
mkdir "$dir"
for f in spec plan tasks; do
  sed -e "s|<需求名称>|$(esc "$name")|g" \
      -e "s|<NNN>|$num|g" \
      -e "s|<git user.name>|$(esc "$user_name")|g" \
      -e "s|^\(.*状态：\)草稿 \| 已确认 \| 已完成$|\1草稿|" \
      -e "s|skills/common/sdd/constraints.md|skills/$sdd/sdd/constraints.md|g" \
      "$templates/$f.md" > "$dir/$f.md"
done
echo "${dir#"$root"/}"
