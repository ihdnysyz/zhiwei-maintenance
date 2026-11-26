# rbac-rag-react-refactor：变更方案

关联：[spec.md](spec.md)

## 方案概述

分四层对现有系统做结构化重构，保留底层表结构兼容并消除权限混乱与非标准检索逻辑：

1. **统一 RBAC 权限体系（`permissions.py`）**：定义 10 个原子权限点（`ask:use`、`draft:use`、`knowledge:read/manage/review`、`expert:task`、`case:feed`、`device:manage`、`account:manage`、`system:manage`），将 `platform`（主管理员/平台管理员）授予全量权限，`library`、`expert`、`tech` 按职责收敛；`profile()` 下发 `permissions` 列表驱动前端按钮与路由可见性。
2. **在线 AI 与 RAG 配置（`model_config.py`）**：在 `records(kind="settings", key="ai")` 持久化组织级 OpenAI 兼容 Chat/Embedding 地址、脱敏密钥、模型名以及 `chunkSize`、`chunkOverlap`、`topK`、`maxReactSteps`，无数据库配置时平滑回退 `.env`；提供在线保存与连通性测试接口。
3. **标准 RAG 流水线（`rag.py`）**：将文档导入拆分为 `parsing → chunked → embedding → indexed` 四阶段，采用结构感知递归切片（按故障节/标题/段落/句子 + 可配置 `chunkOverlap`），接入外部 OpenAI 兼容 `/embeddings` 并提供确定性本地 256 维 L2 归一化特征向量兜底，检索侧采用 **Okapi BM25 + 向量余弦相似度 + 故障码精确索引 + RRF 融合 + 邻域切片扩展**，并支持手工触发单文档/全库重建索引。
4. **标准 ReAct Agent 与 SSE 流式输出（`agent.py` + `conversations.py`）**：封装 5 个工具（`get_device_profile`、`lookup_fault_code`、`search_knowledge`、`search_similar_cases`、`read_chunk_context`），在配置外部 Chat LLM 时运行标准 `Thought → Action(Tool Call) → Observation` 多轮循环并流式综合生成带 `[1][2]` 出处的答案；未配置外部 LLM 时运行同构本地 ReAct 多步检索增强状态机并流式输出。通过 `POST /api/v1/sessions/{session_id}/turns/stream` 推送 `step`、`token`、`done` SSE 事件，原有非流式接口与 worker 复用同一 Agent 核心以保证兼容与可回退。

## 改动范围

- 模块 / 文件：
  - 新增：`backend/maintenance/permissions.py`、`backend/maintenance/model_config.py`、`backend/maintenance/rag.py`、`backend/maintenance/agent.py`
  - 修改：`backend/maintenance/common.py`、`backend/maintenance/admin.py`、`backend/maintenance/library.py`、`backend/maintenance/worker.py`、`backend/maintenance/conversations.py`、`backend/maintenance/drafts.py`
  - 前端：`frontend/src/store.ts`、`frontend/src/App.vue`、`frontend/src/Home.vue`、`frontend/src/Ask.vue`、`frontend/src/Library.vue`、`frontend/src/SourceDetail.vue`、`frontend/src/Expert.vue`、`frontend/src/Admin.vue`、`frontend/src/style.css`
- 接口：
  - 新增：`GET /api/v1/settings/ai`、`PUT /api/v1/settings/ai`、`POST /api/v1/sources/{source_id}/reindex`、`POST /api/v1/sources/reindex-all`、`POST /api/v1/sessions/{session_id}/turns/stream`
  - 增强：`/api/v1/auth/state` 及 `profile` 增加 `permissions`；`/api/v1/integrations/model/test` 支持在线配置测试 Chat + Embedding；`turn` 返回增加 `reactSteps` 与 `engine`
- 数据格式：
  - 复用 `records`、`chunks`、`fault_index`、`effective_scopes` 表；`source.data` 增加 `ragStage`、`embeddedCount`、`embeddingModel`、`embeddingDim`、`chunkSize`、`chunkOverlap`；`turn.data` 增加 `reactSteps`、`engine`；`records(kind="settings", key="ai")` 存储模型与 RAG 参数。
