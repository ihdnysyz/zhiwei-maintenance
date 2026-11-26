---
name: sdd
description: Spec-Driven Development 流程。用户输入「创建feature:<名称>」「创建bugfix:<名称>」「创建change:<名称>」「创建research:<名称>」（或 new_feature / new_bugfix / new_change / new_research <名称>）时，或开始新功能、跨模块的改动前使用：先读通用开发约束和个人约束，再按 spec → plan → tasks → 执行 推进；说明四种类型的创建命令、目录、编号规则、约束、模板和每一步要写什么。
---

# SDD

新功能、bug 修复、变更和调研都可以按 spec → plan → tasks → 执行 推进。新功能和跨模块的改动必须走这个流程；
用户没有用快捷命令、且只是文案、小 bug 之类的小改动时可以跳过。

## 快捷命令

用户输入下面的命令时（冒号可以是全角，前后可以有空格，例如 `创建 bugfix：登录超时`），在项目根目录执行对应的脚本：

| 用户输入 | 类型 | 执行 |
|---|---|---|
| `创建feature:<名称>` 或 `new_feature <名称>` | 新功能 | `sh agent-workspace/skills/common/sdd/new_feature.sh "<名称>"` |
| `创建bugfix:<名称>` 或 `new_bugfix <名称>` | bug 修复 | `sh agent-workspace/skills/common/sdd/new_bugfix.sh "<名称>"` |
| `创建change:<名称>` 或 `new_change <名称>` | 变更：重构、依赖升级、配置或行为调整等 | `sh agent-workspace/skills/common/sdd/new_change.sh "<名称>"` |
| `创建research:<名称>` 或 `new_research <名称>` | 调研：资料收集、方案对比、实验验证与结论 | `sh agent-workspace/skills/common/sdd/new_research.sh "<名称>"` |

- 脚本在当前用户个人目录的对应类型文件夹下新建目录（见下面「目录」），复制该类型的 `spec.md`、`plan.md`、`tasks.md` 模板，填入名称、编号、负责人，最后输出目录的路径；
- 目录和模板由脚本创建，不要手工复制模板；同类型同名已存在时脚本报错，此时告诉用户已有的目录，不要另建；
- 创建后告诉用户目录的路径，然后从下面流程的第 0 步开始。

每次新对话开始或 `/clear` 时，SessionStart hook 输出这些快捷命令（`welcome.md`），显示时机取决于客户端；欢迎语同时运行只读 `agentdev check`，检测环境和历史 personal/Note，需迁移时提示 `agentdev fix`。
Codex、Claude、Antigravity（agy）仅通过官方 hook 回调或外置插件接入，欢迎语显示时机由客户端决定；
不得通过修改宿主启动脚本、shell 启动配置或同名命令转发来提前展示，也不要让 Agent 在首次回复复述欢迎语。
agy 1.5.13 的 SessionStart 在首条消息、首次模型调用前触发；受宿主显示接口问题影响，欢迎语/check 文本仅作为系统上下文加载，TUI 不显示欢迎语。不得把协议测试通过视为显示验收。

## 约束

有两层约束，所有类型都必须遵守：

通用开发约束：本 skill 目录下的 `constraints.md`，全项目共享。

- 位置：`agent-workspace/skills/custom/sdd/constraints.md`（项目定制过 sdd 时）或 `agent-workspace/skills/common/sdd/constraints.md`；
- 每条约束有编号（C1、C2…），按架构、编码、测试、安全与兼容、文档分节；初始内容是示例；
- `common/` 每次执行 setup 都会被模板包覆盖，项目要定制约束（或模板、欢迎语）时，先把整个 `common/sdd/` 复制到 `custom/sdd/` 再修改，之后加载和执行的都是 custom 中的版本；
- 修改约束会改变全项目的约定，须经用户（团队）确认，不在实现某个需求时顺手修改。

个人约束：`agent-workspace/workers/<个人目录>/specs/constraints.md`，个人的自定义约束，只对本人个人目录下的 feature / bugfix / change / research 生效。

- 由 `init.sh` 从本 skill 的 `personal-constraints.md` 复制，已有时不覆盖；初始内容是示例，按个人习惯修改；
- 编号用 P1、P2…，与通用约束的 C 编号区分；
- 与通用约束冲突时以通用约束为准，在 spec 的约束检查中说明。
- 独立 `load_constraints.py` SessionStart hook 将通用约束及当前用户 `specs/**/*.md` 加载到 Agent 上下文；Claude/Codex 支持启动、恢复、clear 和 compact，agy 使用官方新会话 SessionStart，经协议适配直接注入上下文。不依赖 welcome.py，读取错误明确报错。每个开发任务前仍须重新读取源文件，避免会话内修改后使用旧约束。

## 目录

```
agent-workspace/workers/<个人目录>/
├── specs/constraints.md     个人约束
├── feature/NNN-<slug>/      新功能
├── bugfix/NNN-<slug>/       bug 修复
├── change/NNN-<slug>/       变更
├── research/NNN-<slug>/     调研（与 feature 同级）
└── notes/                   Note（见 notes skill）
```

- 个人目录名优先取配置 `personal`（兼容 `peronsal`）；默认 Git 根目录取 `git config user.name`，非 Git 目录取电脑用户名，转小写并把空格和标点换成 `-`。修改命名不重命名已有目录；旧版顶层个人目录通过 `agentdev fix` 迁移到 `workers/`；
- `NNN` 是该类型文件夹内三位递增编号，每种类型单独编号，从 `001` 开始；编号及同名检查包含 `archived/NN/` 中的历史需求，不复用；
- slug 由名称生成，规则同个人目录名，中文保留，例如 `创建feature:Login Page` → `feature/001-login-page/`，`创建bugfix:登录 超时` → `bugfix/001-登录-超时/`；
- `agentdev archive` 可将较早的需求整体移入 `archived/01/` 等分组，默认保留最新 50 项、每组最多 100 项；布局检查和知识检索仍覆盖归档内容。
- 模板在本 skill 的 `templates/<类型>/` 下。
- research 的调研记录、方案对比、实验验证与结论提交使用 `[research]` 标签（见 git-commit skill）。

## 需求目录内的文件

需求目录 `NNN-<slug>/` 中的文件必须归类，后续补充的每个文件都按下面的规则放：

```
NNN-<slug>/
├── spec.md / plan.md / tasks.md   SDD 主文档（创建时生成）
├── summary.md                     可选：总结与验收说明（跨主题的结论、当前环境、回退方式）
├── 01-<主题>/                     例如 01-baseline/：基线、调研、诊断
├── 02-<主题>/                     例如 02-<批次或子问题>/：该主题的文档、证据、截图、脚本、数据放在一起
└── …
```

- 根目录只放 `spec.md`、`plan.md`、`tasks.md`、`summary.md`，不放其他任何文件；
- 其他文件放进一级子目录 `NN-<主题>/`：`NN` 是两位编号（按产生顺序，从 `01` 开始），主题用简短的英文或中文，规则同 slug；
  按主题或批次归类，同一主题的说明文档、证据（json、截图）、脚本和测试放在同一个子目录；子目录内部可以再分层；
- 新增文件前先判断属于哪个已有子目录，都不合适时新建下一个编号的子目录，不要放在根目录"以后再整理"；
- 移动文件后同步更新文档中的相对链接、脚本中的相对路径（脚本定位项目根目录时按 `.git` 向上查找，不要写死层级）；
- 跨主题的结构化汇总（例如各批次的验证数据）放进专门的子目录，在 `summary.md` 中引用；
- 校验：Stop hook 执行本 skill 的 `check_layout.py`，当前用户个人目录中新增的文件不合规时阻止结束并列出清单，按清单归类即可；
  手动检查全部需求目录（含已提交的旧文件）：`agent-workspace/.agent/env/venv/bin/python agent-workspace/skills/common/sdd/check_layout.py --all`。

## 流程

0. 读约束：先读通用开发约束（custom 优先）和自己的个人约束，spec 和 plan 都不得与之冲突。
1. spec：写「约束检查」和「验收标准」两节；有未决问题时先和用户确认，再进入下一步。
   - 约束检查：逐条对照通用约束（C）和个人约束（P），填写是否符合；有不符合的，写明理由并经用户确认后才能进入下一步；
   - 验收标准：只写"做什么"、怎样算完成，不写"怎么做"，每条都要能验证；feature 按 R1、R2… 编号，change 按 C-1、C-2… 编号，research 按 Q1、Q2… 编号，bugfix 写按复现操作得到期望结果、相关场景不回归；
   - bugfix 先确认能复现，再进入 plan。
   - research 写清调研问题和范围，验收以问题得到有证据支撑的回答、局限与未决问题被记录为准。
2. plan：写「方案概述」和「改动范围」（模块、接口、数据格式）两节；方案不得与约束冲突，涉及新的约束项时回到 spec 补充约束检查。
   - bugfix 在方案概述中写根因，说明修复为什么解决根因而不只是掩盖现象；
   - change 涉及兼容时在方案概述中写迁移和回滚方式。
   - research 写明资料来源、比较维度或实验方法，以及证据和产物的存放位置。
3. tasks：把 plan 拆成按顺序执行、可独立验证的任务，每项注明对应的需求和验证方式；bugfix 的第一项是写出能复现问题的测试。
4. 执行：按 tasks 逐项实现或开展调研，完成一项勾选一项；需求或方案有变化时，先更新 spec（含约束检查）/ plan，再继续。
   过程中产生的调研、诊断、验证记录、脚本和截图按「需求目录内的文件」归类到 `NN-<主题>/` 子目录；需求完成或交付验收时写 `summary.md`。
   research 的 summary.md 汇总结论、证据链接、局限、未决问题和后续建议。
