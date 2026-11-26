---
name: knowledge
description: 开发知识库检索。用户要搜索或回顾历史开发痕迹时使用：以前做过什么、某个功能 / bug / 变更的需求和方案、为什么这样设计或修复、某段代码或某笔提交的来龙去脉、某人做过哪些需求、有没有类似的先例、有哪些约束。说明 SDD 产物里有什么，以及如何检索和追溯。
---

# 开发知识库

项目的开发历史沉淀在 `agent-workspace/` 的 SDD 产物、Note 和 git 提交中，所有人的个人目录都随项目提交，可以检索整个团队的历史。
回答"以前怎么做的 / 为什么这样做"之类的问题时，先在这里检索，用检索到的文档作为依据，不要凭猜测回答。

## 知识库里有什么

```
agent-workspace/
├── skills/common/sdd/constraints.md   通用开发约束（项目定制时在 skills/custom/sdd/ 中），编号 C1、C2…
└── workers/<个人目录>/                         每人一个，目录名遵循 config.json5 的 personal/peronsal 与默认用户名规则
    ├── specs/constraints.md            个人约束，编号 P1、P2…
    ├── feature/NNN-<slug>/             新功能
    ├── bugfix/NNN-<slug>/              bug 修复
    ├── change/NNN-<slug>/              变更：重构、依赖升级、配置或行为调整
    ├── research/NNN-<slug>/            调研：资料收集、方案对比、实验验证与结论
    └── notes/<类型>/<YYYY-MM-DD>/HH-mm-ss-<topic>.md   Note：改动记录，类型为 git-commit-change / large-scale-file-change
```

feature / bugfix / change / research 的每个目录都有三个文件（`NNN` 是该类型内的编号）：

| 文件 | feature | bugfix | change | research |
|---|---|---|---|---|
| `spec.md` | 约束检查（C / P 逐条）、验收标准 R1… | 约束检查、验收标准（按复现操作得到期望结果、不回归） | 约束检查、验收标准 C-1… | 约束检查、调研问题与范围、验收标准 Q1… |
| `plan.md` | 方案概述、改动范围 | 方案概述（含根因）、改动范围 | 方案概述（含迁移与回滚）、改动范围 | 方案概述、调研与取证计划、改动范围 |
| `tasks.md` | 按顺序的任务 T1…，`[x]` 表示已完成 | 同左，第一项是复现测试 | 同左 | 收集资料、对比 / 实验、汇总结论 |

- 其他资料（调研、诊断、验证记录、脚本、截图等）按主题放在需求目录的 `NN-<主题>/` 子目录中，`summary.md`（可选）是需求的总结与验收说明；检索脚本会一并搜索子目录中的 `.md`。
- `spec.md` 第三行是头部：`类型：…　编号：…　负责人：…　状态：草稿 | 已确认 | 已完成`。状态为草稿的只是提案，不代表已经实现。
- Note 记录一次改动的结果：标题下有 `时间：`，`## Outcome` 写改了什么、现在的行为，`## Verification` 写验证过什么、哪些没覆盖，`## Git 提交` 列出对应的提交（hash + 提交信息），large-scale-file-change 的 `## 文件改动` 列出当时未提交的改动；`*-merged.md` 是同一天多条 Note 的合并，每条以 `<!-- source: <原文件名> -->` 开头。
- git 提交信息以 `[fix]` / `[feature]` / `[research]` / `[del]` / `[update]` 开头，多项改动用分号隔开。

## 如何检索

### 1. 关键词检索（先用这个）

在项目根目录执行：

```sh
sh agent-workspace/skills/common/knowledge/search.sh <关键词> [关键词…]
```

- 一次检索所有人的 feature / bugfix / change / research 文档、Note、个人和通用约束，以及 git 提交信息，按类别列出；
- 多个关键词须同时出现在同一个文件中；不区分大小写，按字面匹配；
- 每个命中显示路径、所属需求的头部（类型、编号、负责人、状态）和最多 3 行命中内容；
- 结果太多时加关键词缩小范围；没有结果时换同义词、英文名、模块名或文件名再试。

### 2. 按条件浏览

```sh
# 所有需求的头部（类型、编号、负责人、状态）
grep -H '^类型：' agent-workspace/workers/*/*/[0-9]*/spec.md
# 某人做过的需求
ls agent-workspace/workers/<个人目录>/feature agent-workspace/workers/<个人目录>/bugfix agent-workspace/workers/<个人目录>/change agent-workspace/workers/<个人目录>/research
# 还没完成的需求 / 未勾选的任务
grep -L '状态：已完成' agent-workspace/workers/*/*/[0-9]*/spec.md
grep -n '^- \[ \]' agent-workspace/workers/*/*/[0-9]*/tasks.md
# 最近的 Note
ls -d agent-workspace/workers/*/notes/*/*/ | sort -t/ -k5 -r | head
```

### 3. 追溯：提交、Note、需求、代码

- 提交 → Note：`grep -rl <提交 hash 前 7 位> agent-workspace/workers/*/notes/`
- Note → 代码改动：取 `## Git 提交` 中的 hash，`git show --stat <hash>`、`git show <hash>`
- 代码文件 → 历史：`git log --oneline --follow -- <文件>`，再用 hash 找 Note
- 某段代码何时引入：`git log -S '<代码片段>' --oneline`；逐行来源：`git blame <文件>`
- 需求 → 实现：需求目录下 `tasks.md` 的任务和 `plan.md` 的改动范围列出了涉及的模块和文件；再按文件查 git 历史和 Note
- 提交信息检索：`git log --oneline -i --grep=<关键词>`；按类型：`git log --oneline --grep='^\[fix\]'`

## 使用检索结果

- 回答时给出依据的文件路径（和提交 hash），简要概括内容；多个结果之间有冲突时，以较新的、状态为已完成的为准，并指出冲突；
- 区分"计划过"（草稿、已确认）和"已实现"（已完成、有对应提交和 Note）；
- 检索只读，不修改别人个人目录下的文件；
- 检索不到时直接说明没有找到，以及检索了哪些关键词，不要编造历史。

归档兼容：检索递归覆盖各类型的 `archived/NN/`、Note 主题附件和个人 `specs/**/*.md`，不沿目录符号链接；需求元信息从最近的 spec.md 获取。
