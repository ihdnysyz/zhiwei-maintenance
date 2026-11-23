# 知维 · 设备维修知识库

设备维修一期工作台。资料进入知识库后，维修人员可以按设备、批次和故障码查询带出处的答案，再把现场记录整理成经过人工确认的工单草稿。已经结单的工单可以脱敏后进入资料审核。

## 本地体验

在项目目录用 PowerShell 运行。首次启动需要 Python 3.12、Node.js 22.12 或以上和网络。

```powershell
.\start.ps1 -Demo
```

打开 [http://127.0.0.1:5173/](http://127.0.0.1:5173/)。发布了西门子 G120C 公开资料后，可从首页的「F30005 是什么故障？」直接试查厂家手册。内置的 IM-200 设备提供 E07 温度告警演示样例。点击右上角头像可以切换四个演示角色。演示数据存放在 `.local/maintenance.db`，正式空库默认使用 `.local/pilot.db`。演示模式默认使用本地检索，支持保留诊断、工单及导入状态。

### 在演示环境接入 DeepSeek

在项目目录的 `.env` 填写以下配置，将 `MODEL_KEY` 的值设为你的 DeepSeek API Key。

```dotenv
MODEL_URL=https://api.deepseek.com
CHAT_MODEL=deepseek-flash
MODEL_KEY=
EMBEDDING_MODEL=
```

用 `.\start.ps1 -Demo -EnableModel` 启动。在「系统管理 → 运行与审计」点击「测试模型连接」，连接测试使用固定测试内容。新增资料由知识管理员校核后送审。班组专家可在审核通过时勾选回答模型授权，发布后方可将该版本的相关摘录与提问发送到页面显示的模型地址。未勾选的资料可本地检索和查看原文。授权按资料版本和模型地址记录。内置演示和原先在 `MODEL_DEMO_SOURCE_IDS` 中配置的公开资料仍可用于问答。

### 公开资料试点

公开试点使用厂家选型资料、变频器故障码和监管部门的事故案例摘要。使用时按设备铭牌、控制器固件和资料版本核对适用范围。

如果本机已有公开试点清单和资料文件，在项目根目录运行以下命令，可以用临时数据库验证导入、审核、发布、引用及工单导出。配置好 DeepSeek 后，可加上 `--live-model` 检验实际模型答复。独立的代码回归可使用下方的 `test_workflow.py`。

```powershell
.\.venv\Scripts\python.exe -X utf8 backend/validate_public_pilot.py --corpus-dir knowledge/public-pilot
```

## 导入正式资料

1. 复制 `.env.example` 为 `.env`，设置至少 12 字符的 `ADMIN_PASSWORD`，保持 `DEMO_MODE=false`。使用 `.\start.ps1` 启动本地空库，或用下节的 PostgreSQL 部署。管理员初始账号为 `admin`，组织取 `ADMIN_ORG`。
2. 在「系统管理」维护设备编号、型号、批次和地点，创建维修人员、知识管理员和班组专家。按需限制账号可见的型号。
3. 知识管理员进入「知识库 → 批量导入」，添加 PDF、DOCX、TXT、MD 或 CSV，并填写来源编号、版本及适用范围。可填写发布链接，供维修人员通过引用查看来源。一个来源编号跨版本保持一致。历史工单 CSV 需要「工单号」，案例 CSV 需要「案例编号」，每行一条记录。
4. 进入「导入记录」看每个文件的结果。知识管理员核对原件、OCR 片段、故障码、关键数值和图表，必要时在资料详情校正片段，再送审。独立班组专家审核，知识管理员发布。未审核或未生效的版本不会参与维修检索。
5. 维修人员在「智能问答」选择设备并描述现象。点击出处核对原文，PDF 可按引用页打开。需要人工核实的题可转交专家。生成工单草稿后填写原因、实际动作与现场结果，再确认、导出和登记外部单号。

「诊断记录」中每条对话可单独删除。已经转交给专家或用于生成工单的轮次仍保留在相应业务记录中。专家待办、工单和知识库审核在详情下方显示已记录的操作、执行人和时间。

在「专家协作 → 案例回流」整理已结单工单并送知识库校核。知识管理员核对后送审，独立班组专家点击「审核通过并发布」，案例立即进入原设备型号和批次的检索范围。

问答加载[设备维修问答规则](backend/maintenance/skills/maintenance-answer/SKILL.md)。模型结合用户描述和获准使用的资料，解释故障含义，指出当前能核对的项目，并在需要时追问一项现场信息。历史记录单独标注，每条关键内容均可追溯到出处。

问答展示完整维修步骤时，已审核手册的同一片段需包含明确的故障码或「故障现象」标题，以及「排查步骤」或「处置步骤」下连续编号的步骤。有额外适用条件时，请与步骤放在同一片段。系统按原顺序展示该故障的手册步骤。资料不足时说明能核实的范围，涉及安全关键操作时引导核对原文或转交专家。

同页包含多个故障时，可用「故障码：E123」或 Markdown 标题 `## E123` 分开。纯数字故障码请在资料和提问里都标明「故障码」，便于和参数区分。也支持英文手册的 `Fault code`、`Troubleshooting` 和 `Step 1` 等写法。

智能问答会显示当前设备的型号和批次。当前设备缺少直接匹配时，如果其他可访问设备的已发布资料有明确匹配，页面会单独显示设备入口。选择入口后会带入问题，发送前可再核对型号和批次。

样例 CSV 可从下列字段开始。文件使用 UTF-8 或 GB18030。

```csv
工单号,现象,确认原因,实际处置,现场结果,待核实项
WO-001,录入现场现象,录入已确认原因,录入实际处置,录入现场结果,录入尚待核实项
```

## PostgreSQL 部署

`compose.yaml` 包含 PostgreSQL 16/pgvector、Alembic 初始化、FastAPI、持久化任务进程和 Nginx。将 `.env.example` 复制为 `.env` 后，设置不同的 URL 安全数据库密码 `POSTGRES_PASSWORD` 和 `APP_DB_PASSWORD`、初始管理员密码，再执行：

```sh
docker compose up -d --build
docker compose ps
```

默认只监听本机 [http://127.0.0.1:8080/](http://127.0.0.1:8080/)。企业接入时在本机监听端口前使用企业 HTTPS 网关，并将 `SECURE_COOKIE` 设为 `true`。容器中的 Tesseract 支持简体中文和英语。正式附件按 PDF、PNG、JPEG 做类型校验，并要求可用的 ClamAV 签名库。把受控更新的签名文件挂载到 `private/clamav`。在 Windows 本地使用扫描 PDF 时需另外安装 Tesseract 和对应语言包。

### 按环境配置集成

| 集成 | 配置与入口 |
| --- | --- |
| 回答与语义向量 | 配置获准的 OpenAI 兼容模型网关 `MODEL_URL`、`MODEL_KEY`、`CHAT_MODEL`、`EMBEDDING_MODEL`。网关实现 `/chat/completions`、`/embeddings`。批准资料传输范围后开启 `ALLOW_MODEL_DATA`。先配置模型，再导入需建立语义索引的资料。模型缺失或异常时展示授权原文。 |
| 统一身份 | `ID_USERINFO_URL` 接收企业令牌，返回 `org` 和 `sub`。平台管理员预先按该组织和账号分配角色。 |
| 设备台账 | `REGISTRY_URL` 的 GET 返回 `{"devices":[{"number":"…","model":"…","name":"…","batch":"…","location":"…"}]}`，由平台管理员发起同步。 |
| 工单回写 | `WORKORDER_URL` 接收 POST，以 `Idempotency-Key` 去重并校验载荷一致性。GET `/{requestId}` 返回原请求号和状态 `created`、`processing`、`not_created` 或 `unknown`。已创建时返回 `number`。只有外部书面确认支持有效原键重试时返回 `sameKeyRetry: true`。完成目标接口联调及准入后再开启 `WORKORDER_ENABLED`。 |

正式环境如需逐份授权回答模型，设置 `MODEL_REQUIRE_SOURCE_APPROVAL=true`。该模式使用本地关键词和故障码检索，发布后按审核授权发送相关摘录。

回写未准入时使用确认稿导出及外部单号登记。回写返回超时或普通 404 时，后台仅查询原请求。超过 15 分钟进入人工核对，可在「运行与审计」重新查询。原件及附件保存在企业私有数据库，下载和引用会按当前权限检查。

## 运维与验收

接口契约为 [OpenAPI 3.1.1](backend/openapi.json)。运行时文档位于 `/api/v1/docs`，健康检查位于 `/api/v1/health`。系统管理可查看后台任务、连续失败告警、回写结果和变更审计。定时生效、换版及回滚途中，生效范围按服务端时间判定。回滚时重新发布仍已获批的旧版本。

数据库 BLOB 包含原件与附件，一起备份。运行以下模块时需要将 `backend` 加入 `PYTHONPATH`。生产快照需使用受控 PostgreSQL 备份账号及客户端工具，输出保存到私有备份目录。

```text
python -m maintenance.ops backup <新快照路径>
python -m maintenance.ops restore <快照路径>
python -m maintenance.ops retention <组织编号>
```

恢复需设置 `RESTORE_DATABASE_URL` 为独立的空目标，恢复后旧登录会话失效。通过任务计划每日备份并按环境保留期轮换。清理命令移除过期认证会话与已结束且超过保留期的后台任务。设备、知识版本、工单及审计记录保留用于追溯。

本地回归命令：

```powershell
$env:PYTHONPATH = 'backend'
.venv\Scripts\python.exe -m pip install pytest
.venv\Scripts\python.exe -m pytest -q backend/test_workflow.py
npm --prefix frontend run build
```

试点上线需使用真实资料验收问答和扫描件质量、身份与台账隔离、正式回写准入、并发延迟及恢复指标。具体门槛见项目技术路线。

