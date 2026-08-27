# 历史 Session 压缩检查点与冗余恢复设计

**日期：** 2026-08-17  
**状态：** 待实施  
**范围：** short-term-memory HTTP 服务的历史 `session_id` 冷激活、L3/L4 持久化与 Journal 精确召回

## 1. 目标

用户切换回历史 `session_id` 时，即使该 session 的 Redis key 已全部过期，Agent 也必须在写入新问题之前自动恢复一个有界的上下文：

- 最新可用的 L3 `active_revision` 和 L4 `session_memory`；
- checkpoint 之后的 Journal 增量消息；
- 最近 N 个完整用户轮次；
- 新问题必须使用 Journal 历史最大 sequence 之后的新 sequence。

恢复后只把压缩摘要、保留尾部和新问题发给 LLM，不把 Journal 全量原文塞入当前上下文。需要精确细节时，Agent 仍按 Claude 的 `Grep → Read → 原文` 路径从当前选中 session 的 Journal 召回。

## 2. 非目标

- 本阶段不实现历史会话的 UI 预览或用户可见文本。
- 不把 Headroom 改造为上下文存储器。Headroom 仍只负责 generation 压缩和 CCR。
- 不把 Journal 原文删除或替换为摘要。
- 不依赖 Redis keyspace expiration 通知。
- 不在 session 切换的同步路径上等待新的 LLM 摘要请求。

## 3. 设计原则

### 3.1 在过期前写穿，不在过期后抢救

Redis key 过期时值通常已不可读，过期通知也不是可靠事件流。因此，每次成功生成并接受新 L3/L4 状态时，必须立即向 Journal 追加不可变 checkpoint。

### 3.2 Journal 是耐久恢复源

Journal 同时保存：

- 不可变原始消息；
- 不可变 compaction checkpoint。

Redis 是 checkpoint 和最近原文的在线投影，不是唯一恢复源。

### 3.3 恢复先于写入

Agent 不得在冷激活之前 reserve 新事件。否则 Redis sequence 过期后会从 1 重新开始，与 Journal 历史 sequence 冲突。

### 3.4 摘要用于连续性，Journal 用于精确性

L3/L4 摘要让模型知道过去做了什么、当前状态和可搜索线索。精确代码、错误、工具结果和原始措辞仍以 Journal Grep/Read 为准。Headroom CCR 是快速路径，不是跨 TTL 的耐久保证。

## 4. `compaction_checkpoint` 记录

Journal 新增一种记录类型，不伪装成用户或助手消息，也不出现在 `JournalTranscript.lines()` 中。

```json
{
  "type": "compaction_checkpoint",
  "schema_version": 1,
  "checkpoint_id": "sha256:<deterministic-digest>",
  "user_id": "u-1",
  "session_id": "s-1",
  "envelope_version": 12,
  "compressed_through_sequence": 180,
  "generation_versions": [4, 5],
  "session_memory": {},
  "active_revision": {},
  "auto_compact_tracking": {},
  "created_at": "2026-08-17T12:00:00+08:00"
}
```

### 4.1 字段约束

- `checkpoint_id` 由 session 身份、envelope version、L3/L4 revision version、coverage 和内容摘要确定性生成，用于幂等追加。
- `active_revision` 保存完整 `ContextRevision`，包括 boundary、summary message 和 `messages_to_keep`。
- `session_memory` 保存完整 `SessionMemoryRevision`。
- `generation_versions` 仅保存恢复和诊断所需的 generation ID；不用它们恢复旧 marker。Headroom generation messages、CCR 原文和 hash 索引都不写入 checkpoint。
- checkpoint 必须包含 `compressed_through_sequence`，用于识别 Journal 增量尾部。
- 对旧 schema 只读兼容；不原地覆盖旧 checkpoint。

### 4.2 写入时机

仅在以下状态成功变更后追加 checkpoint：

1. `ContextCoordinator` 成功 CAS 新 `active_revision`，即 L3 或 L4 实际替换当前上下文。
2. `SessionMemoryWorker` 成功 CAS 新 `session_memory`。

单纯更新 tracking、写入普通消息、Headroom generation 增加或淘汰时不生成 checkpoint。如果 L3/L4 内容没有变化，不重复追加。

checkpoint 追加使用确定性 ID 幂等。Redis CAS 成功但 Journal 追加失败时，该次 compact/prepare 不得静默报成功；返回可重试错误，并在后续读写路径对当前 Redis L3/L4 状态执行幂等补写。

## 5. 历史 Session 冷激活

### 5.1 HTTP 边界

新增内部语义的 session activation 端点：

```text
POST /v1/memories/activate
```

请求只需 `user_id`、`session_id` 和可选 `history_turns`。响应返回是否发生恢复、恢复的 checkpoint version 和最大 sequence，不返回用户可见的历史预览。

`AgentChatClient.turn()` 固定调用顺序为：

```text
activate(session)
→ write(user question)
→ prepare(context)
→ model/tool loop
→ write(assistant answer)
```

### 5.2 Redis 命中

若 Redis sequence/envelope/original tail 仍存在，activation 是幂等快速路径，不读取 Journal 全量原文，只执行必要的 checkpoint 补写检查。

### 5.3 Redis 未命中

在每 session activation lease 内执行：

1. 从 Journal 读取最新有效 checkpoint。
2. 从 Journal 确定该 session 的最大原始 sequence。
3. 读取最近 N 个完整轮次，但不读入 LLM 全量上下文。
4. 原子恢复 Redis sequence 和最近原文尾部。sequence 设为 Journal 最大 sequence，不是尾部第一条或消息数量。
5. 若 checkpoint 存在，将其 L3/L4 状态恢复为新的 Redis envelope。若只有 L4 `session_memory` 而没有 `active_revision`，使用现有 Claude L4 结果构造逻辑在本地生成 recovery `ContextRevision`，不调用模型。恢复是新的在线投影，不修改 Journal checkpoint。
6. 使用现有 `_compression_job(..., rebuild=True)` 向 Headroom 压缩队列提交截至 Journal 历史最大 sequence 的 cold rebuild。
7. 释放 activation lease，允许写入新问题。

恢复时不等待 Headroom 或 continuity model 新请求。第一次请求使用 checkpoint L3/L4 + 最近 N 轮；Headroom generation/CCR 由已存在的 `rebuild=True` 工作者链路在后台重建。

### 5.4 复用现有 Headroom cold rebuild 与 CCR

历史 session 不从 checkpoint 恢复 Headroom generation，而是直接复用项目已有逻辑：

1. `GenerationPlanner.plan_rebuild()` 从 Journal 读取 `1..requested_through_sequence` 的完整连续原文。
2. `CompressionWorker` 只把这些 Journal originals 发给 Headroom，不把 L3/L4 摘要或旧 generation 再次压缩。
3. rebuild 成功后使用一个全新 generation 替换旧 generation 列表，并生成新 `ccr_expires_at`、marker 和 Headroom CCR 内容。
4. 后续 `prepare()` 通过现有 `load_active_messages()` 只注入未过期、且没被 compact boundary 覆盖的 generation。
5. Agent 看到 marker 后可调用现有 `headroom_retrieve(hash)`；该调用继续使用历史 `user_id/session_id` 生成的 Headroom scope。
6. CCR 返回 `not found` 或尚未重建时，Agent 仍使用同一 session 的 `Grep → Read` 从 Journal 召回。

不延长 checkpoint 中记录的旧 `ccr_expires_at`，不恢复可能已失效的 marker，也不访问 Headroom 内部存储。

### 5.5 无 checkpoint 的旧 session

不得阻塞用户等待新摘要。activation 恢复 Journal 最近 N 轮和 sequence，然后允许写入新问题；Headroom/L3/L4 依现有策略在后续 prepare/工作者中重建。

## 6. 恢复后的活动上下文

`ContextCoordinator.prepare()` 继续使用现有 `load_active_messages()` 语义：

1. 有 `active_revision` 时，注入 compact boundary 和 L3/L4 continuity summary。
2. 注入 checkpoint 内的 `messages_to_keep`。
3. 仅追加 boundary coverage 之后的最近 Journal 原文。
4. 追加 activation 后写入的新问题。
5. 再按 Claude L1 → L2 → L4 → L3 现有顺序进行请求时压缩。

L4 `session_memory` 作为 Claude 原策略中的候选素材恢复，不无条件叠加到已有 `active_revision` 之前。若 checkpoint 只有 L4 memory 而没有 active revision，activation 使用已持久化的 L4 内容、coverage 和最近 N 轮在本地物化 recovery revision；该过程只做确定性组装，不调用 Headroom 或 continuity model。因此第一次 `prepare()` 也会看到 L4 continuity summary。

## 7. 精确召回保证

checkpoint 不需要存储完整原文，也不需要把摘要反向映射到唯一原文范围。摘要中的名称、文件、错误、决策和任务线索供 Agent 选择搜索词。

Agent 调用 `Grep`/`Read` 时：

- 客户端强制注入当前 `user_id` 和历史 `session_id`；
- 服务端校验 HMAC session scope；
- Grep 只搜索该 session 的 Journal logical transcript；
- Read 按 Grep 返回的 sequence 范围读取完整原始事件。

因此，Journal retention 期内的精确召回不依赖 Redis TTL 或 CCR TTL。

## 8. 并发、幂等与失败处理

- activation 使用每 `user_id/session_id` 独立 lease，防止两个 Agent 同时冷恢复。
- 恢复 sequence 和 originals 使用原子 Redis 操作；已有 pending reservation 时不覆盖。
- checkpoint append 按 `checkpoint_id` 幂等，重试不产生重复记录。
- checkpoint 解析失败时不降级为“新会话”；记录明确错误，但仍可在原始 Journal 完整时恢复最近 N 轮。
- Journal 无法读取时 activation 失败，不写入新问题，避免 sequence 污染。
- checkpoint coverage 超过 Journal 最大 sequence 时视为损坏，不使用该 checkpoint。
- Redis 恢复后若发生 CAS 竞争，重读 Redis；不覆盖更新的在线状态。

## 9. 测试要求

### 9.1 Journal checkpoint

- L3 CAS 成功后追加一条 checkpoint。
- L4 session memory CAS 成功后追加一条 checkpoint。
- CAS 失败不追加已接受 checkpoint。
- 相同 checkpoint 重试不重复写入。
- Journal transcript 排除 checkpoint 记录。
- 能够跨日找到最新有效 checkpoint。

### 9.2 冷激活

- Redis 全空时恢复 checkpoint、最近 N 轮和历史最大 sequence。
- 新问题 sequence 严格等于历史最大 sequence + 1。
- Redis 全空时向现有队列提交一个 `rebuild=True` 任务，coverage 到达历史最大 sequence。
- Headroom rebuild 只接收 Journal originals，不接收 checkpoint L3/L4 或旧 generation。
- rebuild 成功后的下一次 prepare 能看到新 generation 和 marker，并能通过 `headroom_retrieve` 召回。
- Redis 命中时 activation 不覆盖在线 envelope。
- 无 checkpoint 时恢复最近 N 轮，不读全量 Journal 到活动上下文。
- Journal 读取失败时不执行新消息 reserve。
- 并发 activation 仅有一个恢复写入者。

### 9.3 Agent 端到端顺序

- `turn()` 请求顺序必须是 activate → write → prepare。
- 冷恢复后的第一次模型请求包含 continuity summary、最近原文和新问题。
- 模型可以对恢复 session 自动 Grep 后 Read，并获取该 session 的精确 Journal 原文。
- 伪造或错误 session scope 在读取 Journal 之前被拒绝。

## 10. 验收标准

1. Redis 全过期后切回历史 session，不会返回空上下文，也不会把 Journal 全量原文注入 LLM。
2. 有 checkpoint 时，无需等待 Headroom 或 continuity model 即可恢复 L3/L4 状态和最近 N 轮；现有 Headroom cold rebuild 同时在后台恢复 generation/CCR。
3. 无 checkpoint 时，至少恢复最近 N 轮，并保证新 sequence 连续。
4. L3/L4 每次成功变更都有幂等、不可变的 Journal checkpoint。
5. Headroom rebuild 完成后，Agent 可使用新 marker 和现有 `headroom_retrieve` 召回 CCR；CCR 不可用时，Agent 仍能通过绑定当前历史 session 的 Grep/Read 召回 Journal 精确内容。
6. 新增定向测试、原有全量测试、Ruff 和构建全部通过。
