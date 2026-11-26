---
name: notes
description: 写 Note（改动记录）。完成改变行为、架构、接口或数据格式的改动后使用；Stop hook 提示"有 N 笔提交尚未记录到 Note"时也用它。说明 Note 的路径、内容、创建命令、校验与配置。
---

# Note

改变行为、架构、接口或数据格式的改动，写一条 Note。

## 路径

`agent-workspace/workers/<个人目录>/notes/<类型>/<YYYY-MM-DD>/HH-mm-ss-<topic>.md`，按类型分目录、按天归档，随项目提交。

- 类型：`git-commit-change`（有未记录到 Note 的提交时写）、`large-scale-file-change`（工作区有大规模未提交的改动时写）；
- 个人目录名优先取配置 `personal`（兼容 `peronsal`）；默认 Git 根目录取 `git config user.name`，非 Git 目录取电脑用户名，转小写并把空格和标点换成 `-`。修改命名不迁移已有目录；
- 日期目录和文件名中的时间取创建时刻，文件名不再包含年月日；topic 用小写字母、数字加 `-`；
- 例如 `agent-workspace/workers/tony/notes/git-commit-change/2026-08-25/14-30-05-local-cli-status-restart.md`；
- 旧版本平铺的日期前缀文件或主题目录（两位/四位年份，可含时分秒）由用户显式运行 `agentdev fix [目录]`（或 `adev fix`、`agentdev --f`）迁移：正文有实际 Git 提交记录时放入 `git-commit-change/<YYYY-MM-DD>/`，否则放入 `large-scale-file-change/<YYYY-MM-DD>/`；目录及附件保留。目标已存在时逐项合并，逐字节确认内容相同后删除旧副本，搬空的源目录及日期和类型父目录删除，保留当前 notes 根目录；内容不同或路径不安全时保留源和目标，仅输出汇总提示。旧 fix 已错分到 Git 日期目录的非提交 Note 也会纠正。setup / init / update 不自动迁移；同时将所有可识别的旧个人目录迁到 workers/<个人目录>/，适配各用户 Note；原始内容不同时再比较已修正共享 skills 链接的内容，Note 分类后重新核算 personal 冲突并清理旧空目录，不沿符号链接迁移，可重复执行。
- `agentdev archive` 按日期目录（或直接文件）整理两个类型目录，超过 `archive.threshold`（默认 50）时保留最新 N 项，较早项放入该类型目录的 `archived/01/` 等分组，每组最多 `archive.group_size`（默认 100）项；历史提交 hash 和知识检索仍覆盖归档内容，不拆散日期目录。

## 创建

在项目根目录执行：

```sh
# 有未记录的提交（默认类型 git-commit-change）
agent-workspace/.agent/env/venv/bin/python agent-workspace/skills/common/notes/create.py <topic>
# 工作区有大规模未提交的改动
agent-workspace/.agent/env/venv/bin/python agent-workspace/skills/common/notes/create.py --type large-scale-file-change <topic>
```

脚本输出新建文件的路径，并自动填入标题下的 `时间：`，以及：

- `git-commit-change`：`## Git 提交`（尚未记录到任何 Note 的提交；没有时删除该节）；
- `large-scale-file-change`：`## 文件改动`（未提交的改动，每个文件的增删行数），并把这批改动记为已记录，之后只按新增的改动量判断。

## 内容

打开新建的文件，填写：

- 标题：一句话说明这次改动；
- `## Outcome`：改了什么、现在的行为；
- `## Verification`：跑了哪些检查、哪些没覆盖；没有时删除本节。

每条 Note 独立：后续改动写新 Note，不合并到已有 Note，同一 topic 可重复使用。

## 校验

每轮对话结束前运行（Claude Code 和 Codex 已通过 Stop hook 自动执行）：

```sh
agent-workspace/.agent/env/venv/bin/python agent-workspace/skills/common/notes/verify.py
```

- 基线之后有提交的 hash 没出现在任何人的 Note 中时，提示写 `git-commit-change` Note；只改动各人 `notes/` 的提交不计入；
- 未提交的改动（含未跟踪文件，不含各人 `notes/`）相对上次记录新增的文件数或行数达到 `scan.large_change` 的阈值时，提示写 `large-scale-file-change` Note；改动被提交或撤销后从记录中扣除，不会误触发；首次运行只建立记录；
- 当前用户某个类型某个日期目录的 Note 超过 `max_notes` 时，把最早的几条按时间顺序合并为一个 `<最早一条的 HH-mm-ss>-merged.md`，使数量回到上限：每条保留全部内容和来源文件名（`<!-- source: … -->`，标题降一级），合并文件可再次参与合并；不删除内容，不处理别人的 Note；
- 每次运行都调用 `update.py` 触发自动更新（检查到期时才检查），见下节。

## 自动更新

仅支持 macOS/Linux。全局安装使用 `source setup.sh`，项目初始化使用 `agentdev init`；实际更新拆到 `update.sh`，与 `agentdev update` 共用跨平台核心。

- `update.py` 只负责欢迎语/Stop hook 的版本查询、后台触发、项目锁和日志；不再执行全局 setup 或在项目副本中 git pull。
- 欢迎语 `--welcome` 每次检查，Stop 按 `auto_update_interval_hours` 到期检查；已有远端版本传给后台进程，避免重复查询。版本查询默认按 API → Raw 回退，每个接口独立超时 5 秒，不平分超时。
- 后台通过全局程序更新安装目录，并重新初始化当前工作区，保留 custom、个人约束和 Note。失败记录 update-ready，之后重试。
- 版本使用大版本.小版本.补丁版本的整数三元组比较，例如 0.9.10 > 0.9.2；兼容读取旧一、两段版本并补齐三段。唯一版本源是主仓库 master 的 publish/version，旧配置地址自动映射，init 会保留注释并迁移。发现更新时提示“发现远端版本更新，由本地0.3.0版本更新到0.3.1版本”。
- 后台进程使用 POSIX 会话分离与存活检查；全局程序使用安装级内核更新锁与安装互斥锁，多项目等待后复用已完成升级并刷新各自工作区。
- 手动和自动更新原子写入安装根目录（app/ 外）的 update-status.json，记录任务、阶段、起止时间、版本、结果及独立 logs/update-<id>.log 路径；--check/无升级不覆盖状态。CLI 可用 agentdev update --status [--watch] 查看。
- Claude Code、Codex、Antigravity（agy）和普通 CLI 统一使用 macOS osascript/Linux notify-send 系统通知，仅实际更新成功完成时发送一次；开始、中间阶段、失败、暂停、无需更新及只读检查均不通知。无需外置更新插件，不修改宿主启动脚本或全局客户端配置。保留 --notify/AGENTDEV_NOTIFY 开关；缺少通知环境不会影响升级，系统通知时长受桌面策略控制。新注册 hook 通过 --client 明确传播调用客户端。agy 的 Stop 使用 decision/reason JSON 适配 Note 校验的续跑与放行，后台未空闲时跳过。
- 欢迎语查询版本时同时检查受管理安装的 Git 状态：未提交文件、分支或 HEAD 偏移会显示风险并暂停自动覆盖；hook 的内部 --project 更新调用保留同样保护。手动 agentdev/adev update 会强制覆盖安装目录改动，同版本发现风险也重新安装；风险文案提示“执行[agentdev update]进行强制覆盖更新。”。agentdev check 只读检查安装改动并继续工作区检查，风险时返回非零，不查询远端。无 .git 的旧快照提示无法检查，后续更新可迁移到独立 master 工作树；当前业务项目的修改不作为安装风险。

skill 运行时优先使用 `agent-workspace/.agent/env/python-path` 记录的共享解释器；旧项目本地 venv 为兼容回退。新项目保留 `venv/` 兼容链接。

## 配置

`agent-workspace/config/config.json5`（JSON5，每个字段都有注释；缺少的字段取本 skill 中 `config.json5` 的默认值）：

- `archive`：显式分组归档配置，`threshold` 默认 50、`group_size` 默认 100，均为正整数；缺失字段自动合并默认值。
- `personal`：个人目录名，留空时 Git 根目录用 git user.name、非 Git 目录用电脑用户名；兼容 `peronsal`，非空 `personal` 优先。修改后不移动旧目录；
- `enabled`：是否开启 Note 校验；
- `max_notes`：每个日期目录（`notes/<类型>/<YYYY-MM-DD>/`）的 Note 上限，超过时合并最早的，默认 100；
- `auto_update`：是否开启自动更新，默认开启；与 `enabled` 相互独立；
- `auto_update_interval_hours`：每轮结束时版本检查的间隔（小时），默认 24，上次检查时间记在 `agent-workspace/.agent/env/last-update`；会话开始时每次都检查；
- `update_url`：远端版本号地址，默认主仓库 `master/publish/version` 的原始内容；
- `scan`：触发条件。`commits` 为 true 时存在未记录的提交即触发；`include_merges` 是否计入 merge 提交；`paths` / `exclude_paths` 只统计 / 不统计改动这些路径的提交和工作区改动（git pathspec）；`large_change`（`enabled`、`files` 默认 20、`lines` 默认 1000）是大规模改动的触发阈值，改动快照记在 `agent-workspace/.agent/env/large-change-base`。
