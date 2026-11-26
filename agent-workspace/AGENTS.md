# AGENTS.md

## 工作方式

- 只做需求所需的最小改动，保留与任务无关的改动。
- 只跑覆盖本次改动的检查，汇报实际跑过的命令。
- 未经用户要求，不 commit、不 push、不做破坏性操作。
- 每次开始开发任务前，按 config.json5 的 personal（兼容 peronsal）确定当前用户，读取 custom 优先的 sdd/constraints.md 和该用户 specs/ 下的 Markdown 约束；恢复/压缩会话或约束变更后重新读取，不能只依赖先前会话的摘要。不得借用其他用户的个人约束；spec/plan 逐条检查适用约束。
- Codex、Claude、Antigravity（agy）只能通过官方提供的 hook 回调或外置插件接入；不得修改宿主启动脚本、shell 启动配置，或通过同名函数、别名和命令包装器接管宿主启动。

## agent-workspace/ 目录

项目根目录下的 `agent-workspace/` 是 Agent 工作区，由 `scripts/command/init.sh` 创建，AgentDevWorkSpace 的文件都放在这里：

- `skills/`：Agent 加载的 skill，随项目提交；
  - `common/`：从 AgentDevWorkSpace 的 `skills/`（通用 skill 模板包）复制，每次执行 setup 都整体覆盖，不要在这里修改；
  - `custom/`：项目自己的 skill，setup 从不改动；与 `common/` 同名时优先加载 `custom/` 中的，定制通用 skill 时先把它从 `common/` 复制过来再改；
- `workers/<个人目录>/`：每人一个；旧版顶层个人目录通过 `agentdev fix` 显式迁移，修改 personal 名称不自动重命名已有目录；目录名由 `config.json5` 的 `personal` 指定（兼容 `peronsal`）；未指定时 Git 根目录取 `git config user.name`，非 Git 目录取电脑用户名，再转小写并将空格、标点换成 `-`，除 `specs/` 外随项目提交；
  - `specs/`：个人的自定义约束 `constraints.md`，补充通用开发约束（见 `sdd` skill）；setup 时写入 `.gitignore`，不提交；
  - `feature/`、`bugfix/`、`change/`、`research/`：每个新功能、bug 修复、变更、调研一个目录 `NNN-<slug>/`，四种类型同级且独立编号（编号包含 archived/NN/ 中的历史需求，不复用）；根目录只放 `spec.md`、`plan.md`、`tasks.md` 和可选的 `summary.md`，其他文件按主题归类到 `NN-<主题>/` 子目录（见 `sdd` skill「需求目录内的文件」，Stop hook 会检查）；
  - `notes/`：Note 记录（见 `notes` skill），按类型分 `git-commit-change/`、`large-scale-file-change/`，其下按 `YYYY-MM-DD/` 归档；只改动各人 `notes/` 的提交不会触发新的 Note；
- 六类记录目录可显式执行 `agentdev archive`，默认保留按名称排序最新的 50 项，较早项放入各目录的 `archived/01`、`02`…，每组最多 100 项；`config.json5` 的 `archive.threshold` / `archive.group_size` 可配置。
- `config/`：配置，`config.json5` 见 `notes` skill；不提交；
- `.agent/`：本机使用，不提交；
  - `env/`：`python-path` 记录全局安装的共享 Python 环境，skill 中的 Python 脚本用该解释器运行；macOS/Linux 的新项目保留 `venv/` 兼容链接，旧本地环境不删除；另有 Note 校验状态和 AgentDevWorkSpace 路径。全局安装由根目录 `setup.sh` 完成，项目接入用 `agentdev init`；
  - `tmp/`：Agent 的临时文件（中间结果、一次性脚本、抓取的日志和截图等），按任务建子目录 `tmp/<主题>/`；不要在项目根目录或其他位置新建 `.agents/`、`tmp/` 之类的临时目录；已写入 `.gitignore`，不提交，需要留作证据的内容整理后放进对应需求目录；
  - `profiles/`、`evals/`：预留。

## Skills

SDD 流程、Note、Git 提交规则和开发知识库检索封装成 skill，在 `agent-workspace/skills/common/` 下；`init.sh` 把每个 skill 链接到 `.claude/skills/` 和 `.agents/skills/`（客户端按需加载，同名时指向 `custom/`）。`.agents/` 是 agy 官方项目配置目录，不作为临时目录使用。用到时读取对应的 `SKILL.md`（`custom/` 中有同名 skill 时读 `custom/` 中的）：

- `sdd`（`agent-workspace/skills/common/sdd/SKILL.md`）：用户输入「创建feature:<名称>」「创建bugfix:<名称>」「创建change:<名称>」「创建research:<名称>」（或 new_feature / new_bugfix / new_change / new_research <名称>）时，按它创建对应的目录；新功能和跨模块的改动，必须先读通用开发约束和个人约束，再按它写 spec / plan / tasks，spec 中逐条做约束检查后才能实现。
- `notes`（`agent-workspace/skills/common/notes/SKILL.md`）：改变行为、架构、接口或数据格式的改动，必须写一条 Note；Stop hook 提示有提交尚未记录到 Note、或工作区有大规模改动时，按它创建。
- `knowledge`（`agent-workspace/skills/common/knowledge/SKILL.md`）：搜索或回顾历史开发痕迹（以前做过什么、某个需求或 bug 的方案、为什么这样设计、某笔提交的来龙去脉）时，按它检索，用检索到的文档作为依据回答。
- `git-commit`（`agent-workspace/skills/common/git-commit/SKILL.md`）：每笔提交信息必须以 `[fix]` / `[feature]` / `[research]` / `[del]` / `[update]` 标签开头；提交前按它编写提交信息。
