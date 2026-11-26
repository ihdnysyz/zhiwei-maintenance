# rbac-rag-react-refactor：任务

关联：[spec.md](spec.md)、[plan.md](plan.md)

按顺序执行，每项都能独立验证；完成一项勾选一项。变更内容有调整时，先改 spec / plan，再调整任务。

- [x] T1：实现 `permissions.py` 统一权限矩阵并在后端各路由与 `profile()` 中落地（对应 C-1；验证：自动化 RBAC 测试通过）
- [x] T2：实现 `model_config.py` 与 `/api/v1/settings/ai`、模型测试接口（对应 C-2；验证：API 读写脱敏测试通过）
- [x] T3：实现 `rag.py` 标准 RAG 切片/嵌入/BM25+向量 RRF 混合检索，接入 `worker.py` 与 `library.py` 重建索引接口（对应 C-3；验证：导入/切片/向量/检索单测通过）
- [x] T4：实现 `agent.py` 标准 ReAct 循环工具集与 SSE 流式接口 `/sessions/{id}/turns/stream`，并兼容原有同步/异步问答（对应 C-4、C-5；验证：流式与多步 ReAct 测试通过）
- [x] T5：更新前端权限驱动 UI、「模型与 RAG 配置」页、知识库 RAG 管道状态/重建索引、问答页实时 ReAct 轨迹与流式打字机展示（对应 C-1~C-5；验证：`npm --prefix frontend run build` 通过）
- [x] T6：回归验证与浏览器端到端巡检（对应 C-6；验证：后端全套自检 + 浏览器端到端验证）
