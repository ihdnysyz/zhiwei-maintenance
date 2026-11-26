# 知维 · 设备维修知识库

设备维修一期工作台。资料进入知识库后，维修人员可以按设备、批次和故障码查询带出处的答案，再把现场记录整理成经过人工确认的工单草稿。已经结单的工单可以脱敏后进入资料审核。

## 本地体验

在项目目录用 PowerShell 运行。首次启动需要 Python 3.12、Node.js 22.12 或以上和网络。

```powershell
.\start.ps1 -Demo
```

打开 [http://127.0.0.1:5173/](http://127.0.0.1:5173/)。首次启动创建空的设备台账和知识库。用右上角头像切换角色，先以平台管理员身份添加设备，再以知识管理员身份导入自备资料。演示工作区使用 `.local/maintenance.db`，正式工作区默认使用 `.local/pilot.db`。

### 在演示环境接入 DeepSeek

在项目目录的 `.env` 填写以下配置，将 `MODEL_KEY` 的值设为你的 DeepSeek API Key。

```dotenv
MODEL_URL=https://api.deepseek.com
CHAT_MODEL=deepseek-flash
MODEL_KEY=
EMBEDDING_MODEL=
```

用 `.\start.ps1 -Demo -EnableModel` 启动。在「系统管理 → 运行与审计」点击「测试模型连接」，连接测试使用固定测试内容。知识管理员导入的资料解析成功后自动生效，可在本地检索和引用。需要让外部模型参与回答时，班组专家可在资料详情选择「授权回答模型」。授权按资料版本和模型地址记录。

Windows 上的模型请求默认使用系统可信证书库。使用自定义可信证书时，可由管理员配置 `SSL_CERT_FILE` 或 `SSL_CERT_DIR`。

### 公开资料试点

如果本机自备对应的公开资料试点清单和文件，可用下列命令验证导入、审核、发布、引用及工单导出。配置好 DeepSeek 后，可加上 `--live-model` 检验实际模型答复。常规代码回归使用 `test_workflow.py`，会在测试数据库中生成验收记录。

```powershell
.\.venv\Scripts\python.exe -X utf8 backend/validate_public_pilot.py --corpus-dir knowledge/public-pilot
```

## 导入正式资料

1. 复制 `.env.example` 为 `.env`，设置至少 12 字符的 `ADMIN_PASSWORD`，保持 `DEMO_MODE=false`。使用 `.\start.ps1` 启动本地空库，或用下节的 PostgreSQL 部署。管理员初始账号为 `admin`，组织取 `ADMIN_ORG`。
2. 在「系统管理」维护设备编号、型号、批次和地点，创建维修人员、知识管理员和班组专家。按需限制账号可见的型号。
3. 知识管理员进入「知识库 → 批量导入」，添加 PDF、DOCX、TXT、MD、CSV 或 TSV，并填写来源编号、版本及适用范围。可填写发布链接，供维修人员通过引用查看来源。一个来源编号跨版本保持一致。历史工单表格需要「工单号」，案例表格需要「案例编号」，每行一条记录。
4. 在「导入记录」查看每份文件的结果。解析成功后，资料立即在适用设备范围内生效。管理员可以在资料详情试问、下架，或导入修正后的新版本。解析失败的文件可以在导入记录重试。
5. 维修人员在「智能问答」选择设备并描述现象。点击出处核对原文，PDF 可按引用页打开。需要人工核实的题可转交专家。生成工单草稿后填写外部单号、原因、实际动作与现场结果，再预览并确认。外部工单实际结单后可直接确认结单，工单确认后可按需导出。

「诊断记录」中每条对话可单独删除。已经转交给专家或用于生成工单的轮次仍保留在相应业务记录中。专家待办、工单和知识库审核在详情下方显示已记录的操作、执行人和时间。

在「专家协作 → 案例回流」整理已结单工单并送知识库校验。校验通过后自动进入独立审核。班组专家可在案例回流页打开详情并审核通过，案例立即发布到原设备型号和批次的检索范围。

问答加载[设备维修问答规则](backend/maintenance/skills/maintenance-answer/SKILL.md)。模型结合用户描述和获准使用的资料，解释故障含义，指出当前能核对的项目，并在需要时追问一项现场信息。历史记录单独标注，每条关键内容均可追溯到出处。

问答展示完整维修步骤时，已审核手册的同一片段需包含明确的故障码或「故障现象」标题，以及「排查步骤」或「处置步骤」下连续编号的步骤。有额外适用条件时，请与步骤放在同一片段。系统按原顺序展示该故障的手册步骤。资料不足时说明能核实的范围，涉及安全关键操作时引导核对原文或转交专家。

同页包含多个故障时，可用「故障码：E123」或 Markdown 标题 `## E123` 分开。纯数字故障码请在资料和提问里都标明「故障码」，便于和参数区分。也支持英文手册的 `Fault code`、`Troubleshooting` 和 `Step 1` 等写法。

智能问答会显示当前设备的型号和批次。当前设备缺少直接匹配时，如果其他可访问设备的已发布资料有明确匹配，页面会单独显示设备入口。选择入口后会带入问题，发送前可再核对型号和批次。

导入历史工单时，可使用下列 CSV 表头并填入自己的工单。文件使用 UTF-8 或 GB18030。

```csv
工单号,现象,确认原因,实际处置,现场结果,待核实项
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

正式环境如需逐份授权回答模型，设置 `MODEL_REQUIRE_SOURCE_APPROVAL=true`。该模式使用本地关键词和故障码检索，生效后按专家授权发送相关摘录。

排查问答时可临时开启 `RAG_TRACE=true`。后台日志按问答轮次记录继承上下文的范围、命中片段和排序分、实际交给模型的片段以及最终决策。日志仅记录诊断元数据。

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

