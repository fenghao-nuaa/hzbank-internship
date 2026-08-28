# README 功能导航表补充设计

## 目标

在根 `README.md` 的每个核心功能说明后增加一张可直接定位实现的“功能导航表”，使读者无需全文搜索即可从业务概念跳转到对应源码。保留现有功能说明、接口、依赖、部署和快速入门，不恢复原先逐文件、逐测试的超细功能清单。

## 统一表格格式

每张表固定使用三列：

| 想定位的部分 | 这部分负责什么 | 源码入口 |
|---|---|---|

约束：

- 不设置“验证测试”列，也不在表格中链接测试文件。
- “想定位的部分”使用读者会搜索的业务或流程名称，不直接使用难懂的类名。
- “这部分负责什么”说明输入、主要处理和输出，使未阅读源码的人知道为什么进入该文件。
- “源码入口”必须是一个或多个可点击的仓库相对链接，链接文字同时说明文件职责。
- 一行对应一个可独立理解的处理阶段；辅助函数不单独占行。
- 原有散列的“关键实现”列表由导航表替代，避免同一批链接重复出现。

## short-term-memory

### 上下文压缩与原文召回优化

该表使用九个定位项：

1. 本轮上下文准备入口：链接 `service/context_coordinator.py`，说明如何读取活动上下文、计算预算并协调 L1/L2/L3/L4。
2. 活动上下文组装：链接 `compression/context_query.py`，说明如何用最新摘要覆盖旧历史并组合未覆盖 generation 与最近原文。
3. L1 微压缩：链接 `compression/micro_compact.py`，说明按时间清理陈旧工具结果和保留最近消息。
4. L2 自动压缩判断：链接 `compression/auto_compact.py` 与 `compression/policy.py`，说明 token 水位、模型窗口和保留输出预算判断。
5. L4 Session Memory：链接 `compression/session_memory_compact.py`、`compression/session_memory_prompt.py` 和 `jobs/session_memory_worker.py`，说明结构化摘要的提示、后台生成和 revision 提交。
6. L3 连续性摘要：链接 `compression/traditional_compact.py` 与 `compression/compact_prompt.py`，说明基于上一版摘要和新增上下文生成新摘要。
7. Headroom generation：链接 `jobs/compression_worker.py` 与 `compression/generations.py`，说明较早原文压缩、marker 保存和 generation 替换。
8. 两条原文召回路径：分别链接 `compression/ccr_recall.py`、`transcript/grep_tool.py` 和 `transcript/read_tool.py`，说明 CCR 精确召回与 Journal 定位读取。
9. Agent 自动工具循环：链接 `agent/agent_chat.py` 与 `transcript/tool_definitions.py`，说明工具定义、模型 tool call 执行和结果回填。

### 历史 Session 切换

该表使用六个定位项：

1. HTTP 激活入口：链接 `service/app.py`，说明 `/v1/memories/activate` 的鉴权、请求和响应边界。
2. Agent 写入前激活：链接 `agent/agent_chat.py`，说明每轮写入前如何先激活目标 `session_id`。
3. 热激活与冷恢复编排：链接 `service/session_activation.py`，说明 Redis 存在时快速返回、Redis 过期时读取 Journal 并恢复。
4. 最新摘要 checkpoint：链接 `storage/compaction_checkpoint.py` 与 `storage/journal_store.py`，说明 L3/L4 revision、覆盖 sequence 和不可变 Journal 记录。
5. 最近 N 轮完整对话：链接 `storage/recent_originals.py`，说明按完整用户轮次保留最近原文。
6. Headroom 冷重建：链接 `jobs/redis_compression_queue.py`、`jobs/compression_worker.py` 和 `jobs/redis_rebuild_completion.py`，说明恢复后如何排队重建 generation/CCR 并处理并发版本变化。

## SemanticaAdapter

### 决策规则、审批和政策例外治理

该表使用七个定位项：

1. HTTP 治理入口：链接 `http/app.py`，说明 Agent 注册、audit、evaluate、decision、approval 和 exception 路由。
2. Agent 调用客户端：链接 `http/client.py` 与 `http/wire.py`，说明稳定 Python 客户端、领域对象与 JSON wire 转换。
3. 治理生命周期：链接 `services/governance.py`，说明从画像注册到决策固化的失败关闭状态流转。
4. 领域模型与状态：链接 `domain/models.py` 与 `domain/errors.py`，说明画像、证据、规则结果、决策、审批、例外和错误语义。
5. 确定性规则执行：链接 `adapters/semantica/backend.py` 与 `adapters/semantica/mapping.py`，说明第三方 Semantica 调用和对象映射。
6. 画像版本存储：链接 `adapters/memory/profiles.py`，说明 Agent Profile 保存和版本读取。
7. 审批与例外授权：链接 `adapters/memory/approvals.py` 与 `ports/approvals.py`，说明批准者角色检查及可替换授权边界。

### 证据链、决策追踪与审计包导出

该表使用六个定位项：

1. 证据与 provenance 写入：链接 `adapters/semantica/backend.py`，说明画像快照、证据、决策、审批和例外如何写入后端。
2. Trace 查询：链接 `services/governance.py` 与 `http/app.py`，说明 decision ID 如何映射到可查询审计链。
3. JSON/RDF 导出：链接 `adapters/semantica/backend.py`，说明单个决策如何导出两种审计格式。
4. 审计包发布：链接 `services/integrity.py`，说明临时目录生成、manifest、哈希和原子发布。
5. 离线完整性校验：链接 `services/integrity.py`，说明文件摘要、图摘要和可选可信链头校验。
6. ZIP 下载接口：链接 `http/app.py`，说明如何打包审计产物并返回 `X-Content-SHA256`。

## DREAM

### 会话外的异步记忆蒸馏

该表使用八个定位项：

1. 完成会话导入：链接 `api.py` 与 `core/events.py`，说明 HTTP payload 如何转换成完成任务事件。
2. 多租户事件账本：链接 `core/ledger.py` 与 `core/scope.py`，说明幂等事件和 tenant/agent/user 数据隔离。
3. Background Review 触发：链接 `application/scheduler.py`，说明空闲、事件数、token 和最大等待条件。
4. 后台服务编排：链接 `application/service.py`，说明 pending 批次、Review 执行、Curator 和恢复。
5. LLM 结构化提取：链接 `extraction/llm_backend.py`、`extraction/prompts.py` 和 `extraction/provider_adapter.py`，说明提示、Provider 格式和结构化结果解析。
6. Curator 调度：链接 `curators/schedule.py`、`curators/semantic.py` 和 `curators/registry.py`，说明立即整理、每日兜底和长周期语义整理。
7. Publication 闭环：链接 `application/closed_loop.py` 与 `memory/publication.py`，说明候选、审核、激活和版本状态。
8. 快照、报告与失败回滚：链接 `memory/storage/snapshots.py`、`memory/storage/reports.py` 与 `memory/storage/rollback.py`，说明事务前后状态、运行报告和恢复。

### 用户画像、AI 决策卡和 Skill Candidates 沉淀

该表使用七个定位项：

1. 提取结果分类：链接 `extraction/classifier.py` 与 `extraction/models.py`，说明 Review Action 和 Artifact Kind。
2. 候选治理策略：链接 `governance/policy.py` 与 `governance/candidates.py`，说明证据强化、风险判断和候选隔离。
3. 记忆路由：链接 `governance/router.py`，说明不同知识类型进入哪个 Manager。
4. 用户画像：链接 `memory/managers/persona.py` 与 `governance/persona_merge.py`，说明画像条目合并、来源和冲突处理。
5. AI 决策卡：链接 `memory/managers/decision_cards.py` 与 `curators/ai.py`，说明决策经验保存和 `DECISION_RULES.md` 整理。
6. Skill Candidates：链接 `memory/managers/skill_candidates.py`，说明可复用流程候选的保存、证据合并与状态边界。
7. Agent 投影写回：链接 `memory/writeback.py` 与 `curators/user.py`，说明 `USER_PERSONA.md` 和面向 Agent 的投影生成。

### Memory Retrieval Skill

该表使用七个定位项：

1. Agent 公共调用入口：链接 `retrieval/skill.py` 与 `retrieval/retrieval.skill`，说明 Python API 和 Skill 使用契约。
2. 请求、响应和记忆类型：链接 `retrieval/models.py`，说明 query、limit、返回 memories/context 和类型边界。
3. 检索配置与领域识别：链接 `retrieval/config.py`，说明默认/最大条数、上下文预算和 domain 推断。
4. Active Memory 加载：链接 `retrieval/loader.py`，说明当前实际读取 Persona、Decision Rules 和 Decision Cards；明确 Skill Candidates 尚未接入 loader。
5. 作用域与类型过滤：链接 `retrieval/filters.py`，说明 tenant、agent、user、domain 和 kind 限制。
6. 相关性排序：链接 `retrieval/ranker.py` 与 `retrieval/retriever.py`，说明 query token 匹配、置信度和排序流程。
7. 去重与预算化上下文：链接 `retrieval/context_builder.py`，说明冲突选择、相似内容去重、token budget 和 Markdown context 输出。

`retrieval/selector.py` 属于另一套完整仓库投影选择器，不作为 `MemoryRetrievalSkill.retrieve()` 主链路的入口；README 可在补充说明中链接，但不得把它描述成该 Skill 当前实际调用的组件。

## 验证标准

- README 中七个核心功能后各有且仅有一张功能导航表。
- 所有导航表都只有三列，不出现“验证测试”列。
- 每个源码入口都是可点击的仓库相对链接，目标文件真实存在。
- 表格文字能够区分入口、编排、存储、后台任务、召回和输出职责。
- 不把测试文件、未实现功能或旁路组件写成主流程入口。
- DREAM Retrieval 表明确当前 loader 不读取 Skill Candidates，也不把 `selector.py` 写成 `MemoryRetrievalSkill` 主链路。
- 原有接口、依赖、部署、快速入门、数据安全和交付边界章节继续保留。
