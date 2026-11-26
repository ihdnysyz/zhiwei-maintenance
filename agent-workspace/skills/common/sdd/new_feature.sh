#!/bin/sh
# 用法（在项目根目录执行）：sh agent-workspace/skills/common/sdd/new_feature.sh <名称>
# 新建功能需求：agent-workspace/workers/<个人目录>/feature/NNN-<slug>/，详见 new.sh。
exec sh "$(dirname "$0")/new.sh" feature "$@"
