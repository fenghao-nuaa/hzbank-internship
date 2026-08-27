# Memory API 对齐说明

本文用于和调用方、部署方对齐边界。业务调用只有两个 HTTP 接口；DeepSeek 是第三条、独立的官方模型调用链路。

## 调用链路

```text
调用方 ──POST /v1/memories/write──> memory-api ──> Journal + Redis
调用方 <──POST /v1/memories/read─── memory-api <── 压缩上下文 + 最近原文
调用方 ──OpenAI SDK───────────────> Headroom Proxy ──> DeepSeek 官方 API
                                     │
compression-worker ──原文────────────>│ Headroom 压缩与 CCR
```

memory-api 不调用 DeepSeek，也不读取、写入或解析 Headroom 的 CCR 存储。`read` 返回 Headroom Proxy URL 和匿名 scope headers，聊天调用方用自己的 `DEEPSEEK_API_KEY` 调用官方 OpenAI-compatible SDK。

## 1. 存记忆

`POST /v1/memories/write`

```http
Authorization: Bearer <MEMORY_API_AUTH_TOKEN>
Content-Type: application/json

{
  "user_id": "u-001",
  "session_id": "s-001",
  "session_seconds": 30,
  "events": [
    {
      "event_id": "evt-001",
      "role": "user",
      "content_type": "conversation",
      "content": "继续上一次讨论",
      "metadata": {}
    }
  ]
}
```

`event_id` 是调用方生成的幂等键。相同 ID、相同正文可安全重试；相同 ID、不同正文返回 `409`。服务端先把原文写入 Journal，再提交 Redis，并在达到任一压缩阈值时异步排队，因此接口不等待 Headroom。

## 2. 读记忆

`POST /v1/memories/read`

```http
Authorization: Bearer <MEMORY_API_AUTH_TOKEN>
Content-Type: application/json

{
  "user_id": "u-001",
  "session_id": "s-001",
  "history_turns": 10,
  "include_effective_config": true
}
```

响应中的关键字段：

- `messages`：语义摘要、Headroom 压缩消息和最近原文组成的模型上下文；Headroom 消息保持不透明，不由 memory-api 解析 marker。
- `memory`：压缩覆盖序号、最新序号、读取来源和压缩段数。
- `headroom.proxy_url`、`headroom.scope_headers`：给独立聊天 SDK 使用的 Headroom Proxy 参数。
- `effective_config`：仅在请求时返回白名单内的非敏感有效配置。
- `timing_ms`：总耗时及 Redis、恢复、组装分段耗时。

## 配置责任矩阵

| 配置 | 谁提供 | 是否可由 read 返回 | 说明 |
|---|---|---:|---|
| `REDIS_HISTORY_TURNS` | 记忆服务部署方 | 是 | 默认读取最近轮数 |
| `REDIS_SESSION_TTL_SECONDS` | 记忆服务部署方 | 是 | Redis 在线状态 TTL |
| `HEADROOM_CCR_TTL_SECONDS` | Headroom 与记忆服务共同对齐 | 是 | 压缩 generation 可使用时间，默认 12 小时 |
| `JOURNAL_RETENTION_DAYS` | 记忆服务部署方 | 是 | 原文 Journal 保留期，默认 30 天 |
| `HEADROOM_TRIGGER_RATIO` | 业务与记忆服务共同对齐 | 是 | 范围 0.60–0.70，默认 0.65 |
| `HEADROOM_MAX_MESSAGES` / `HEADROOM_MAX_SESSION_SECONDS` | 业务与记忆服务共同对齐 | 否 | 另外两个 OR 触发阈值 |
| `MEMORY_API_CONCURRENCY_LIMIT` | 部署方 | 否 | 每个 API 进程的入口容量，默认 100 |
| `MEMORY_API_WORKERS` | 部署方 | 否 | 默认 4 个 HTTP 进程 |
| `HEADROOM_COMPRESSION_WORKERS` | 部署方 | 否 | 后台压缩并发，默认 8 |
| `HEADROOM_SERVICE_URL` | 部署方 | 否 | memory-api 内部地址；响应只给作用域化 Proxy URL |
| `DEEPSEEK_MODEL` | 聊天调用方 | 否 | 默认 `deepseek-v4-flash` |
| `MEMORY_API_AUTH_TOKEN` | 密钥管理系统 | 永不返回 | 只发给 memory-api |
| `SHORT_TERM_MEMORY_SCOPE_SECRET` | 密钥管理系统 | 永不返回 | 只用于生成 HMAC scope |
| `DEEPSEEK_API_KEY` | 聊天调用方/密钥管理系统 | 永不返回 | 只发往 Headroom Proxy/DeepSeek，不发给两个记忆接口 |

## 原文、压缩内容与重复压缩

- 精确原文：先写按 session 的 Journal JSONL，并在 Redis 保存在线副本；Journal 默认保留 30 天，Redis 默认 12 小时。Journal 是本项目的恢复与审计来源，不是 CCR 的替代实现。
- 压缩内容：Headroom 返回的消息作为不透明 generation envelope 保存在 Redis，generation 的 `ccr_expires_at` 默认 12 小时。
- CCR 原文和 marker：完全由 Headroom 官方服务及其部署配置管理。本项目不假定其一定是内存 LRU 或 SQLite，也不访问 `~/.headroom/ccr_store.db`。
- 防止二次压缩：worker 只按单调 sequence 从 Redis 原文或 Journal 原文构造 Headroom 输入；既有压缩 generation、summary 和 marker 永不进入下一次压缩输入。需要重建时也从 Journal 的 1..N 原文重建。

因此，多轮对话可能生成多个压缩 generation，但不会把“压缩结果再次压缩”。原文能否由 CCR 在过期前透明召回，仍由实际 Headroom 版本、上游模型工具调用能力和 CCR 配置决定。

## 部署与验收

生产使用 `compose.memory.yml` 启动 Redis、Headroom、memory-api 和 compression-worker。`uv tool` 适合本地单进程试验，不作为 100 并发生产部署方式。服务提供 `/health`、`/ready`、`/metrics`，业务路径仍只有上述两个。

接口 SLO 目标：write p95 ≤ 150 ms、p99 ≤ 300 ms；read p95 ≤ 100 ms、p99 ≤ 200 ms，100 并发且错误数必须为 0。测试方法和是否实测见 `docs/performance.md`；未实际运行的外部测试不会写成通过。
