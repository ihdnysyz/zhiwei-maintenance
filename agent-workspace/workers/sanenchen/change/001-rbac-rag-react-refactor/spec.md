# rbac-rag-react-refactor

类型：变更（change）　编号：001　负责人：sanenchen　状态：已完成

## 约束检查

对照 [通用开发约束](../../../../skills/common/sdd/constraints.md)（C 编号）和 [个人约束](../../specs/constraints.md)（P 编号）逐条填写；不符合的写明理由，并经用户确认后再实施。两者冲突时以通用约束为准。

+ C1：是。新增 `permissions.py`、`model_config.py`、`rag.py`、`agent.py` 按单向分层依赖调用（路由 → agent/rag/model_config → db/permissions），无反向依赖。
+ C2：是。沿用现有 `backend/maintenance/` 和 `frontend/src/` 目录与命名规范，未引入重型外部依赖。
+ C3：是。已通过 RBAC、标准 RAG 切片/嵌入/混合检索、ReAct SSE 流式问答自动化验证与浏览器端到端巡检。
+ C4：是。API Key 仅存服务端并脱敏展示；修复了 `redact()` 身份证脱敏漏洞；沿用现有表结构无需破坏性迁移。
+ C5：是。已完成 SDD 文档与总结记录。
+ P1：是。模块按职责拆分为清晰小函数。
+ P2：是。完成了前端构建、后端自动化测试与浏览器实测。
+ P3：是。按 spec → plan → tasks → 实现推进。

## 验收标准

- [x] C-1：建立统一 RBAC 权限点矩阵（`permissions.py`），主管理员/平台管理员拥有全量权限，4 类角色权限边界清晰且由 `profile.permissions` 统一驱动前后端。
- [x] C-2：支持在「系统管理 → 模型与 RAG」在线配置 OpenAI 兼容 Chat + Embedding 模型及切片/检索参数（兼容 `.env`），API Key 脱敏回显，支持在线连通性测试。
- [x] C-3：知识库实现标准 RAG 流水线（解析 → 结构化递归切片含 `chunkSize`/`chunkOverlap` → 向量嵌入含远程 Embedding 与本地 256 维向量兜底 → 混合索引），支持单份与全量重建索引并在详情页展示流水线状态。
- [x] C-4：问答引擎重构为标准 ReAct Agent 循环（`Thought → Action/Tool → Observation` 多步增强检索、查询改写、邻域上下文扩展与历史案例对照 → 综合生成带出处回答）。
- [x] C-5：问答支持 SSE 流式输出（`POST /api/v1/sessions/{session_id}/turns/stream`），前端实时展示可折叠 ReAct 推理轨迹、流式回答与引用出处，历史会话持久化保留轨迹。
- [x] C-6：已有功能不回归，前端构建与浏览器端到端巡检通过。
