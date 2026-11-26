# 知维 · 设备维修知识库

面向工业现场的设备维修知识库与智能诊断工作台。支持 PDF / DOCX / TXT / MD / CSV / TSV 资料导入、自动切片与向量索引、多轮工具检索增强问答（支持思考过程流式展示与引用溯源）、工单草稿确认以及专家协作审核闭环。

## 项目结构

```text
.
├── backend/                    # FastAPI + SQLAlchemy 后端与后台 RAG Worker
│   ├── maintenance/            # 业务模块（agent / rag / model_config / permissions 等）
│   └── migrations/             # Alembic 数据库迁移脚本
├── frontend/                   # Vue 3 + Vite + Element Plus 前端
│   └── src/
│       ├── views/              # 页面视图（概览 / 问答 / 知识库 / 工单 / 专家 / 管理）
│       └── components/         # 通用组件（MarkdownView / SourceDetail / OperationHistory）
├── knowledge/                  # 公开试点资料与设备维修手册样例
├── docs/                       # 需求分析与技术路线文档
├── compose.yaml                # PostgreSQL + pgvector + API + Worker + Web 容器编排
├── start.ps1                   # Windows 本地一键启动脚本
└── .env.example                # 精简环境变量模板
```

## 快速启动（本地 SQLite）

本地开发默认使用 SQLite（`.local/pilot.db`），内置 BM25 + 256 维本地向量混合检索，无需额外部署数据库。

- **环境要求**：Python 3.12+、Node.js 22+
- **默认管理员**：账号 `admin`，密码 `admin`（组织 `pilot`），可在 `.env` 中通过 `ADMIN_PASSWORD` 指定初始强密码。

### macOS / Linux

```bash
# 1. 安装依赖
python3.12 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
npm --prefix frontend ci

# 2. 启动后端 API、后台索引 Worker 与前端
PYTHONPATH=backend .venv/bin/python -m uvicorn maintenance.main:app --host 127.0.0.1 --port 8421
PYTHONPATH=backend .venv/bin/python -m maintenance.worker
API_PROXY=http://127.0.0.1:8421 npm --prefix frontend run dev -- --port 5173
```

### Windows (PowerShell)

```powershell
.\start.ps1
```

访问 [http://127.0.0.1:5173/](http://127.0.0.1:5173/) 使用 `admin` / `admin` 登录。

## 模型与 RAG 配置

大模型与向量参数无需写在 `.env` 中，登录主管理员账号后进入 **「系统管理 → 模型与 RAG」** 即可在线配置并测试连接：

- **对话模型**：支持任意 OpenAI 兼容接口（DeepSeek / OpenAI / 通义千问 / Ollama / 硅基流动等），支持推理模型的思考流实时展示。
- **向量模型（Embedding）**：配置远程 Embedding 模型后可一键全量重建语义索引；留空时自动使用内置本地向量。
- **扫描件 PDF OCR**：文字版 PDF 开箱即用；如需解析纯图片扫描件 PDF，需在本机安装 `tesseract` 及中英文语言包（`OCR_LANG=chi_sim+eng`）。

## 生产部署（PostgreSQL + pgvector）

复制 `.env.example` 为 `.env`，设置 `POSTGRES_PASSWORD`、`APP_DB_PASSWORD` 和至少 12 位的 `ADMIN_PASSWORD`，然后执行：

```bash
docker compose up -d --build
docker compose ps
```

默认监听本机 [http://127.0.0.1:8080/](http://127.0.0.1:8080/)。挂载 HTTPS 反向代理时，请将 `.env` 中的 `SECURE_COOKIE` 设为 `true`。

## 运维与校验

- 接口文档：`/api/v1/docs`（契约见 [backend/openapi.json](backend/openapi.json)），健康检查：`/api/v1/health`。
- 备份与恢复（需设置 `PYTHONPATH=backend`）：

```bash
python -m maintenance.ops backup <快照路径>
python -m maintenance.ops restore <快照路径>
python -m maintenance.ops retention <组织编号>
```

- 前端构建校验：`npm --prefix frontend run build`
- 后端回归测试：`PYTHONPATH=backend pytest -q backend/test_workflow.py`
