# SemanticaAdapter v1 能力矩阵

状态含义：`supported` 表示直接使用并已做真实集成测试；`mapped` 表示经过稳定领域模型或兼容映射；`deferred` 表示 Semantica 提供能力，但本适配器 v1 尚未暴露。

| 能力 | Semantica 模块/API | 状态 | v1 行为与边界 |
|---|---|---:|---|
| HTTP 治理服务 | SemanticaAdapter FastAPI | supported | Agent 通过带 API Key 的稳定 `/v1` 接口完成完整治理生命周期 |
| Agent HTTP 客户端 | SemanticaAdapter + `httpx` | supported | 不导入 Semantica；完成领域模型解码、稳定错误映射和审计包校验 |
| 服务端独立依赖 | PyPI `semantica==0.6.6` | supported | Semantica 仅属于 `semantica`/`server` extra，不依赖本地源码目录 |
| 决策上下文图 | `semantica.context.ContextGraph`、`DecisionRecorder` | supported | 保存审计、画像、证据、决策及关联边 |
| 确定性推理 | `semantica.reasoning.Reasoner` | mapped | 业务事实转为事实字符串，使用前向链并返回规则 ID、结论、前提 |
| Agent 画像 | 适配器领域模型 + Context Graph | mapped | Semantica `AgentContext` 不是版本化治理画像；版本、规则、本体和审批策略由适配层固化 |
| 来源追踪 | `semantica.provenance.ProvenanceManager` | supported | 记录画像、证据、决策来源及内容哈希 |
| W3C PROV-O | `ProvenanceManager.export_prov()` | supported | 与 Context Graph RDF 一同写入 Turtle 审计文件 |
| 本体结构验证 | `semantica.ontology.OntologyValidator` | mapped | 调用结构验证并增加 v1 输入字段类型约束；完整 SHACL 数据图验证延期 |
| 人工审批 | `ApprovalChain`、`ContextGraph` | mapped | 公司工作流先授权；0.6.6 内存图兼容分支使用公共图 API 保存审批节点和边 |
| 政策例外 | `PolicyException`、`ContextGraph` | mapped | 公司工作流先授权，并保存例外、政策和决策关联 |
| JSON 导出 | `semantica.export.JSONExporter` | supported | 导出稳定 `AuditTrace` |
| RDF/Turtle 导出 | `semantica.export.RDFExporter` | supported | 导出上下文图并追加 PROV-O 来源数据 |
| 决策级数据隔离 | 适配器作用域过滤 | supported | Trace、JSON、RDF 和 PROV-O 仅包含目标决策关联节点 |
| 离线完整性 | 适配器 manifest/哈希链 | supported | JSON/RDF 必需文件、SHA-256、链绑定清单、原子发布/回滚；可传外部可信链头 |
| 文件/网页/数据库/API 采集 | `semantica.ingest` | deferred | 后续作为独立入口适配，不改变治理接口 |
| 清洗和标准化 | `semantica.normalize` | deferred | 由上游业务系统提供标准化输入 |
| GraphRAG 分块 | `semantica.split` | deferred | 不属于 v1 决策治理闭环 |
| NER/关系/事件/三元组抽取 | `semantica.semantic_extract` | deferred | 不属于金额核对首个案例 |
| 冲突检测和实体去重 | `semantica.conflicts`、`deduplication` | deferred | v1 接收上游明确的决策关键冲突并失败关闭 |
| 图分析/链路预测 | `semantica.kg` | deferred | Context Graph 已使用，中心性/社区/链路预测尚未暴露 |
| 向量检索 | `semantica.vector_store` | deferred | 治理决策 v1 不依赖向量数据库 |
| Pipeline DSL | `semantica.pipeline` | deferred | 当前由 `AgentGovernanceService` 显式编排 |
| 可视化工作台 | `semantica.visualization` | deferred | v1 输出 JSON/RDF 供后续监管界面使用 |
| 双时态/时间旅行 | Temporal Intelligence | deferred | 仅记录事件时间，尚无双时态查询接口 |
| 多 Agent 共享上下文 | Multi-Agent/Agno | deferred | 当前每次审计绑定一个 Agent 画像版本 |

## 可替换性保证

新后端只需实现 `GovernanceBackend`，并通过 `tests/contract/test_backend_contract.py`。Agent 业务代码只调用稳定 HTTP API，不接触 Semantica 对象；更换图数据库、规则引擎或企业治理平台时，保留 HTTP 契约、领域模型和 `AgentGovernanceService` 即可。
