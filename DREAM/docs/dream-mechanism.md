# DREAM 做梦机制

## 什么是做梦

DREAM 中的“做梦”是一次后台知识整理过程。它读取已经完成但尚未处理的会话，调用 Agnes 发现长期知识，再由本地代码完成分类、合并、治理、写回和版本激活。

做梦不是重新回答用户，也不是把整段聊天直接保存为长期上下文。它只保留具有持续价值的信息：

- 用户稳定的偏好、习惯和约束；
- Agent 可复用的决策原则和边界；
- 具有完整流程特征的 Workflow Skill 候选。

## 自适应触发

Background Review 按 `tenant_id / agent_id / user_id` 隔离待处理队列。任一条件满足即可进入处理：

- 用户达到配置的空闲时长，默认 2 小时；
- 待处理事件数量达到批量上限；
- 本地估算 Token 达到批量上限；
- 最早事件达到最大等待时间。

正式验证接口也可以显式启动当前用户的待处理批次。无论自动还是手动触发，都读取同一事件账本和 Pending 状态，不需要重新导入已经成功写入的会话。

## 单批处理流程

一次普通批次依次执行：

```text
读取 Pending Events
        ↓
创建 Before Snapshot 与 Publication Version
        ↓
Agnes Background Review
        ↓
Provider Adapter 解包、规范化和严格校验
        ↓
Knowledge Governance 与本地 Manager
        ↓
确定性 Curator
        ↓
生成 Agent/User 投影
        ↓
创建 After Snapshot、报告和版本
        ↓
自动激活或进入审核
```

### Agnes 调用限制

正常情况下，每个批次只调用 Agnes 一次。Agnes 返回后，Provider Adapter 会处理常见外部格式差异，例如：

- JSON 或 Markdown 包裹的 JSON；
- tool/function 的 `arguments`；
- `name + parameters` 包装；
- 单对象和数组形式；
- 字段命名差异和允许忽略的额外字段。

如果结果结构非法，DREAM 会把安全的校验原因反馈给模型，最多额外修复一次。第二次仍然非法时，本批次失败并进入安全恢复，不继续增加调用次数。

网络失败、鉴权失败或 Provider 超时不属于非法结构修复，不会通过反复调用掩盖。

### 语义缓存

通过严格校验的 Background Review 结果会按输入事件、当前快照、工具范围、后端和 Prompt 版本生成缓存键。本地写入阶段失败后再次处理同一语义输入时，可以复用已验证的结果，不必重复调用 Agnes。

Prompt 版本或有效输入发生变化时，缓存键随之改变，旧结果不会被错误复用。

## 两层 Curator

### 确定性 Curator

确定性 Curator 使用本地规则整理已经写入的知识：

- User Curator 根据 `USER.md` 生成或更新用户画像投影；
- AI Curator 根据 Decision Cards 生成或更新 `DECISION_RULES.md`。

相关画像或决策卡发生变化后，对应 Curator 会在该批次内立即运行。每天本地时间默认凌晨 3 点还会执行一次幂等兜底检查，用于补跑之前遗漏的本地整理；没有内容变化时不会重复产生不同结果。

### Semantic Curator

Semantic Curator 使用大模型进行更长周期的语义整理，默认关闭。启用后，它拥有独立调度条件：

- 距离上次尝试达到配置周期，默认 168 小时；
- 对应作用域达到最小空闲时间，默认 2 小时；
- 当前确实存在可整理候选。

Semantic Curator 在隔离的临时副本中运行，结果写入候选目录，不直接覆盖 Active Memory。

## 自动治理

知识提取成功后，Memory Governance 根据类型、置信度、来源数量、结构完整性和风险信号决定：

| 结果 | 含义 |
|---|---|
| `auto_activate` | 低风险且证据充分，可以自动写回并激活 |
| `observe` | 当前证据不足，保存候选等待后续会话强化 |
| `require_review` | 涉及敏感、权限或高风险内容，需要人工确认 |

如果同一候选在后续批次得到更多独立事件支持，候选存储会合并来源证据，再重新交给治理策略判断。

自动治理不会删除审核接口。审核从默认步骤变成高风险和异常情况的安全门。

## 快照、版本与回滚

每次做梦都通过本地事务衔接以下产物：

1. **Event Ledger**：保存原始完成事件；
2. **Before Snapshot**：记录处理前的 Active Memory；
3. **Publication Version**：记录来源事件和状态流转；
4. **Dream Report / Review Trace**：记录提取、适配、路由、Curator 和错误摘要；
5. **After Snapshot**：记录候选写回后的完整结果；
6. **Active Version**：标记下一任务应该读取的版本。

当任一应用步骤失败时，DREAM 会：

- 恢复 Before Snapshot；
- 将版本标记为失败并记录 `failure_reason`；
- 保持原 Active Version 不变；
- 恢复 Pending 和 Review Progress，使事件可以安全重试；
- 不留下部分写入的用户画像、决策卡或投影。

## 超时与安全失败

普通做梦事务默认具有 300 秒总截止时间。Deadline 会覆盖模型等待和本地阶段检查，而不是让每个步骤分别获得新的 300 秒。

模型请求还受独立 Provider timeout 控制。模型等待运行在隔离线程中，超过剩余总预算时，主事务停止等待并执行回滚；迟到的模型结果不能继续修改本地状态。

安全失败时，FastAPI 对外返回统一错误，不暴露 API Key、认证头或不必要的 Provider 内容。详细诊断保存在本地报告和日志中。

## 运行结果

成功批次可能得到三种结果：

- **Active**：低风险知识已经自动写回并成为下一任务可用版本；
- **Ready for Review**：候选已生成，但必须审核后才能激活；
- **Observation Stored**：候选证据不足，Active Memory 保持不变。

失败批次不会消耗 Pending Events。修复网络、模型协议或本地错误后，只需重新运行做梦接口，不应重新导入同一批会话。
