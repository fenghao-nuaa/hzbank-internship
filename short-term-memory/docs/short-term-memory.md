# short-term-memory 设计与集成边界

## 当前架构

本项目是独立 HTTP 记忆服务，不承担最终回答，也不内嵌 Headroom 或 DeepSeek。

```text
memory-api (4 processes) ──> Redis online state + compression queue
          │                └─> per-session Journal JSONL originals
          └─ read response: messages + Headroom Proxy context

compression-worker (8 loops) ── originals only ──> Headroom /v1/compress
chat caller ── official OpenAI SDK ──> Headroom Proxy ──> DeepSeek official API
```

业务接口固定为 `POST /v1/memories/write` 和 `POST /v1/memories/read`。详细请求、响应与配置责任见 `memory-api-alignment.md`。

## 写入保证

每个事件由 `(user_id, session_id, event_id)` 标识。写入顺序是：

1. Redis 预留单调 sequence，并校验相同 event ID 的正文摘要。
2. 在 per-session 跨进程锁内追加 Journal 原文并刷盘。
3. 幂等提交 Redis 在线原文。
4. 三个 OR 条件任一满足时，只投递压缩意图，不在线等待 Headroom。

相同 ID、相同内容可重试；相同 ID、不同内容返回冲突。Redis 提交失败时 Journal 仍保留原文，调用方可以安全重试。

## 读取与恢复

正常读取并发取得 Redis generation envelope 和最近 N 轮原文。Redis 原文缺失时，从相同 session 的 Journal 恢复；需要冷重建且既有 generation 已过期时，在总超时内等待后台 worker 发布新的 envelope，否则返回服务不可用，不把残缺上下文伪装成成功。

读取上下文按以下顺序组装：

1. 五类语义摘要：`current_goal`、`preferences`、`confirmed_facts`、`pending_items`、`attachment_references`。
2. TTL 内有效的 Headroom 压缩 generation，原样保存和返回。
3. 最近 N 轮精确原文；与压缩覆盖区间允许重叠，用于保持近期细节。

## 原文与压缩内容的位置

- Journal：完整、可审计的精确原文，默认保留 30 天；用于 Redis 过期恢复和 generation 重建。
- Redis originals：在线原文副本，默认 TTL 12 小时。
- Redis envelope：语义摘要与 Headroom 返回的压缩 generation，generation 带 CCR 到期时间，默认 12 小时。
- Headroom CCR：原文恢复载荷、marker、缓存实现和 `headroom_retrieve` 全部由独立 Headroom 服务管理。

本项目既不实现 CCR LRU，也不访问 Headroom SQLite。Journal 是记忆服务的数据安全与恢复机制，不替代或干预 Headroom 的官方召回逻辑。

## 为什么不会重复压缩压缩结果

`GenerationPlanner` 的增量输入只来自 sequence 大于 `compressed_through_sequence` 的 Redis 原文；冷重建输入只来自 Journal 的完整 `1..through_sequence` 原文。worker 明确不读取既有 generation、语义摘要或 marker 作为 Headroom 输入。

因此第二、第三轮可以追加新的 generation，也可以从原文重建，但不会把上一轮压缩文本再次压缩。CCR 是否在 TTL 内透明召回成功，由实际 Headroom 与 DeepSeek 工具调用链路决定。

## 进程和故障边界

- memory-api：Uvicorn 多进程，认证在读取请求体和占用业务容量之前完成；限制请求体、批量数、并发和超时。
- compression-worker：Redis 持久队列、session lease 和 CAS envelope；SIGTERM 停止领取新任务，宽限期内完成当前任务，强制取消时立即归还 queue lease。
- Redis：Compose 启用 AOF `everysec` 和命名卷。
- Journal：同进程 `RLock` 加 POSIX `flock`，支持多个 API 进程共享卷；Windows 和缺少正确 flock 语义的文件系统不支持。
- readiness：同时检查 Redis 与 Headroom，并受总超时限制。

## 安全边界

- 业务接口使用 Bearer token；production 不允许空 token。
- Headroom scope 用服务端 HMAC secret 去标识化 user/session，原始 ID 不作为 scope header 发出。
- `DEEPSEEK_API_KEY` 只存在于聊天调用方并作为模型调用凭据，不发送到 memory write/read。
- 日志、Prometheus 和负载报告只记录状态、数量和耗时，不记录消息正文、CCR 原文或密钥。

## 保留期与清理

Redis TTL 和 CCR generation TTL 都默认 43,200 秒，Journal 默认 30 天。Redis AOF 负责进程重启后的在线状态；Journal retention job 负责到期清理文件。改变 CCR TTL 时必须同时对齐 Headroom 服务与记忆服务配置。

## 验收

单元/假服务测试默认不需要外部凭据。真实 Redis、Headroom、DeepSeek 和 100 并发负载测试均要求显式开关；开关打开而依赖不可用时必须失败，未打开时显示 `SKIPPED`。性能方法见 `performance.md`，未执行项和残余风险见 `known-limitations-and-skipped-tests.md`。
