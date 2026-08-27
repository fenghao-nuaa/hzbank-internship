# 2026-08-06 短期记忆项目 12 项任务实施复盘

## 1. 文档目的

本文详细记录 2026 年 8 月 6 日对 `short-term-memory` 项目完成的 12 项任务，包括：

- 每项任务要解决的问题；
- 主要设计和实现内容；
- 新增或修改的核心文件；
- TDD、独立审核与回归测试；
- 审核中发现的问题及修复；
- 本次明确跳过的真实服务测试；
- 当前仍存在的不足、风险和建议。

本文以代码、测试报告和 Git 提交记录为事实来源。没有运行的真实 Redis、Headroom、DeepSeek 和 100 并发测试，不会写成“已经通过”。

## 2. 最终结果概览

### 2.1 最终架构

```text
业务调用方
  ├─ POST /v1/memories/write ──> memory-api ──> Journal 原文 + Redis 在线状态
  ├─ POST /v1/memories/read  <── memory-api <── 压缩上下文 + 最近原文
  └─ OpenAI 官方 SDK ─────────> Headroom Proxy ──> DeepSeek 官方 API

compression-worker
  └─ 只读取 Redis/Journal 精确原文 ──> Headroom /v1/compress
                                      └─ Headroom 自己管理 CCR、marker 和原文召回
```

项目最终形成了三条清晰边界：

1. memory-api 只负责记忆写入与读取，不负责最终模型回答。
2. compression-worker 只把精确原文提交给 Headroom，不会把旧压缩结果再次压缩。
3. DeepSeek 使用单独的官方 OpenAI-compatible SDK 调用，通过 read 接口返回的 Headroom Proxy 信息访问模型；DeepSeek key 不进入两个记忆接口。

### 2.2 最终本地验证

| 验证项 | 最终结果 |
|---|---|
| 全量 pytest | `325 passed, 20 skipped` |
| Ruff 静态规则 | 通过 |
| `git diff --check` | 通过 |
| `compose.memory.yml config` | 通过，使用测试占位环境变量 |
| `compose.redis.yml config` | 通过 |
| DeepSeek 示例 `--help` | 通过，不需要 API key |
| 负载脚本 `--help` | 通过 |
| Python sdist/wheel | `uv build` 成功 |
| 真实 Redis | 未运行 |
| 真实 Headroom 压缩与 CCR | 未运行 |
| 真实 DeepSeek 三轮对话 | 未运行 |
| 实机 100 并发 SLO | 未运行 |

### 2.3 主要提交

| 范围 | Git 提交 |
|---|---|
| 设计文档 | `3c00a97` |
| 12 项实施计划 | `a0aa70f` |
| Task 1 | `291e1a0`、`72068d4`、`629b7a2`、`1077d17` |
| Task 2 | `4cb38c6`、`a6d2b7f`、`c256d0e` |
| Task 3 | `db674b5`、`2bcac61` |
| Task 4 | `9c0e49b`、`abe97d3` |
| Task 5 | `f8c403b`、`9b7bd6b`、`e2df48d` |
| Task 6 | `ca0aa24`、`638aed5`、`bc8a8b1`、`6548b2a` |
| Task 7 | `7ec8655`、`0433097` |
| Task 8 初始实现 | `38aa3b6` |
| Task 8 修复及 Task 9–12 收口 | `07400a1` |

---

## 3. Task 1：依赖、配置和共享领域模型

### 3.1 任务目标

先建立后续 HTTP 服务、Redis 存储、压缩 generation、DeepSeek 示例共同使用的类型系统和配置边界，尤其解决：

- 哪些配置可以暴露给调用方和老师；
- 哪些配置是密钥，绝不能从 read 接口返回；
- 四类内容如何统一表示；
- Headroom 返回的扩展字段如何保持“不透明但不可被意外修改”。

### 3.2 主要实现

- 在 `pyproject.toml` 增加三个 optional dependency group：
  - `api`：FastAPI、Uvicorn、Prometheus；
  - `deepseek`：官方 OpenAI Python SDK；
  - `dev`：pytest、Ruff、build。
- 增加两个 CLI 入口：
  - `short-term-memory-api`；
  - `short-term-memory-worker`。
- 扩充 `.env.example` 与 `config.py`，增加：
  - API host、port、worker、并发、Redis pool、body size、timeout、batch size；
  - Journal 30 天 retention；
  - Headroom CCR TTL、refresh window、generation 数量、worker 数量、queue capacity；
  - DeepSeek 官方 URL 和默认模型 `deepseek-v4-flash`。
- 明确不把 `DEEPSEEK_API_KEY` 放进 `ShortTermMemorySettings`，避免 memory-api 获得模型密钥。
- 新增不可变领域模型：
  - `MemoryEvent`；
  - `MemoryContentType`；
  - `EventReservation`；
  - `CompressionGeneration`；
  - `MemorySummaryEnvelope`。
- 新增 write/read Pydantic schema，包括请求、响应、timing、memory state、Headroom Proxy context 和安全的 `EffectiveMemoryConfig`。
- 四类内容统一支持：`conversation`、`code`、`document`、`skill`。

### 3.3 主要文件

- `.env.example`
- `pyproject.toml`
- `uv.lock`
- `src/short_term_memory/config.py`
- `src/short_term_memory/models.py`
- `src/short_term_memory/ports.py`
- `src/short_term_memory/service/schemas.py`
- `tests/factories.py`
- `tests/test_config.py`
- `tests/test_models.py`
- `tests/service/test_schemas.py`

### 3.4 审核发现和修复

Task 1 经历了三轮独立修复：

1. 配置校验不够严格：
   - 修复 `DEEPSEEK_API_URL` 空值或非 HTTP URL 未拒绝的问题；
   - 修复 API port 超过 65535 仍可接受的问题。
2. 模型不可变性不完整：
   - 冻结 `metadata`、generation messages、语义摘要列表和附件引用；
   - 修复调用方修改输入 dict 后会间接修改已创建事件的问题；
   - 保证 `model_copy(deep=True)`、JSON dump 和 round-trip 仍正常。
3. Headroom 扩展字段顶层容器仍可变：
   - 将 Pydantic `model_extra` 冻结；
   - 仍保留 `extra="allow"`，不丢弃 Headroom 的未来扩展字段；
   - 不解释、不重写这些字段语义。

### 3.5 测试记录

- 初始 RED：缺少模型与 service schema，测试收集失败。
- 第一轮 GREEN：`17 passed`；全量 `117 passed, 9 skipped`。
- 最终审核后：Task 1 聚焦模型测试通过，全量达到 `128 passed, 9 skipped`。
- Ruff 与 diff check 均通过。

---

## 4. Task 2：带 sequence 的幂等 Journal 与 30 天保留

### 4.1 任务目标

为每个 session 保存精确原文，支持崩溃恢复、事件幂等、冲突检测和可配置保留期。Journal 是本项目的恢复和审计来源，但不替代 Headroom CCR。

### 4.2 主要实现

- `JournalStore.append_event()`：按 session 追加 `MemoryEvent`。
- 使用 `event_id + SHA-256` 做幂等判断：
  - 同 event ID、相同 digest：返回 duplicate，不重复写；
  - 同 event ID、不同 digest：抛出 `JournalConflictError`。
- 每条记录写入后执行 `flush()` 和 `os.fsync()`。
- 增加：
  - `find_event()`；
  - `read_original_range()`；
  - 后续恢复使用的 recent-original APIs。
- 保持旧 JSONL message 格式向后兼容。
- 允许恢复“最后一条未写完的 JSON 片段”，但不忽略中间损坏、空白行或其他持久化破坏。
- 新增 `JournalRetentionJob`：
  - 默认 30 天；
  - 只扫描 `root/*/journals/*.jsonl`；
  - 只删除明确验证过日期、session 名称和内容时间的文件；
  - 不递归删除目录；
  - 单文件读取失败时记录 failure 并继续处理其他文件。

### 4.3 主要文件

- `src/short_term_memory/storage/journal_store.py`
- `src/short_term_memory/storage/journal_retention.py`
- `tests/storage/test_journal_store.py`
- `tests/storage/test_journal_retention.py`

### 4.4 审核发现和修复

- 修复空白或只有空格的 Journal 行被静默跳过的问题；现在除“最后一个不完整 JSON 片段”外，其余破坏都会暴露。
- 修复带时区 offset 的 `created_at` 被转换为 UTC 字符串后写回，导致无法 byte-preserving round-trip 的问题；现在原始字符串保留，只用解析值决定文件日期。
- 修复 retention 遇到非法 UTF-8 时中断整个任务的问题；现在保留问题文件并报告失败。
- 修复 `2026-07-01-.jsonl` 这种空 session 文件名可能被 retention 删除的问题；现在 session 部分也经过 `safe_component` 校验。

### 4.5 测试记录

- RED：`JournalConflictError` 与 retention module 不存在，测试收集失败。
- 初始 GREEN：`11 passed`；全量 `135 passed, 9 skipped`。
- 最终审核后：全量 `138 passed, 9 skipped`。
- 验证了中文、换行、时区、冲突、末尾损坏、非法编码和安全删除范围。

---

## 5. Task 3：异步 Redis 原子预留、提交、读取与 CAS

### 5.1 任务目标

建立面向多请求并发的 Redis 在线状态层，保证 sequence 单调、事件只提交一次、旧 worker 不能覆盖新 envelope。

### 5.2 主要实现

- 新增 `AsyncRedisMemoryStore`。
- 通过 Lua 完成原子操作：
  - `reserve_event()`：分配 sequence，保存 pending reservation；
  - `commit_event()`：验证 digest 和 sequence 后只追加一次原文；
  - `compare_and_set_envelope()`：按 version CAS 更新 summary/generation envelope；
  - `acquire_compression_lease()` / `release_compression_lease()`：token 所有权锁。
- 主要 Redis key：
  - session sequence；
  - originals messages list；
  - summary envelope；
  - 每个 event 的 reservation hash；
  - compression session lock。
- 默认 TTL 43,200 秒，并在写入/提交时刷新需要保留的 key。
- 增加 `AsyncFakeRedis`，让 Lua 状态机可以在无真实 Redis 时确定性测试。
- 增加 opt-in 真实 Redis 并发测试：100 个不同 ID 和 100 个相同 ID。

### 5.3 主要文件

- `src/short_term_memory/storage/async_redis_memory_store.py`
- `tests/storage/test_async_redis_memory_store.py`
- `tests/storage/fake_redis.py`
- `tests/integration/test_async_redis_memory_store.py`

### 5.4 审核发现和修复

- 初版 commit Lua 在 duplicate 路径前没有再次核对 digest/sequence，错误事件可能被当作安全 duplicate。
- 修复后：
  - digest 不一致抛 `EventConflictError`；
  - sequence 不一致抛明确的 `ValueError`；
  - 任何不一致都不能写入 originals list。
- 为 fake Redis 增加显式 `expire_now()`，测试 lease 到期后的重新获取。
- 加强 TTL 断言，验证只给正确的 sequence、messages、summary、event key 设置 43,200 秒。

### 5.5 测试记录与跳过

- 初始 RED：模块不存在。
- 初始 GREEN：`8 passed`。
- 最终 focused：`11 passed, 1 skipped`；全量 `149 passed, 10 skipped`。
- 真实 Redis 测试尝试连接 `127.0.0.1:6379`，但环境没有 Redis 服务，得到 `ConnectionRefusedError`；因此真实 Lua/Redis 证明没有在当天环境完成。

---

## 6. Task 4：只压缩原文的 generation 规划与读取组装

### 6.1 任务目标

直接解决“第二、第三轮会不会把压缩结果再次压缩”的问题。

### 6.2 主要实现

- 新增 `CompressionCandidate`、`GenerationPlanner`、`GenerationAssembler`。
- 增量压缩只读取：
  - sequence 大于 `compressed_through_sequence` 的 Redis 原文；
  - 从 high-water mark 后的连续 sequence 前缀。
- 冷重建只读取 Journal 的完整 `1..through_sequence` 原文范围。
- 明确不读取旧 generation messages、semantic summary 或 Headroom marker 作为压缩输入。
- 读取时组装：
  1. 五类语义摘要；
  2. TTL 内有效的 Headroom opaque generations；
  3. 最近 N 轮精确原文。
- 修改旧 `RedisSessionContext.compression_snapshot()`，让它只返回 Redis 原文，不再把 summary 中的 Headroom 内容放进压缩 snapshot。

### 6.3 三代原文范围验证

测试固定验证：

```text
generation 1: sequence 1..100
generation 2: sequence 101..180
generation 3: sequence 181..240
```

第二、第三代输入中不能出现第一、第二代的 marker。

### 6.4 主要文件

- `src/short_term_memory/compression/generations.py`
- `src/short_term_memory/storage/redis_session_context.py`
- `tests/compression/test_generations.py`
- `tests/storage/test_redis_session_context.py`

### 6.5 审核发现和修复

- 初版可能在 expected sequence 尚未提交时跳过 gap 并推进 high-water mark。
- 修复为只接受从期望 sequence 开始的连续前缀。
- rebuild 必须拥有 `1..N` 的完整范围，缺失任一 sequence 抛 `OriginalSequenceGapError`。
- 同 sequence 的完全相同事件折叠；不同事件冲突抛 `OriginalSequenceConflictError`。
- 修复 opaque Headroom message 中 `content: null` 或 null extra field 被 dump 时丢弃的问题。
- 加强 TTL 边界：恰好到期即失效；支持等价时区 offset；拒绝 naive datetime。

### 6.6 测试记录

- RED：generation module 不存在。
- 初始 GREEN：targeted `27 passed`，全量 `154 passed, 10 skipped`。
- 审核修复后：generation focused `13 passed`，全量 `163 passed, 10 skipped`。

---

## 7. Task 5：异步 Headroom Adapter 与持久压缩队列

### 7.1 任务目标

把压缩从在线请求中移出，建立可恢复、可重试、不会因 worker 崩溃丢任务的后台处理链路。

### 7.2 主要实现

- 新增 `AsyncHeadroomClient`：
  - 使用一个共享、连接数受限的 `httpx.AsyncClient`；
  - 调用官方 `POST /v1/compress`；
  - 验证公共响应字段；
  - 不解析 CCR；
  - 不在 adapter 内重试，由持久队列统一处理。
- 新增 `RedisCompressionQueue` 与冻结的 `CompressionJob` JSON schema。
- 新增 `CompressionWorker`：
  - 领取 Redis queue lease；
  - 领取 per-session compression lease；
  - 调用 GenerationPlanner；
  - 只将 `candidate.originals` 转成 Headroom messages；
  - Headroom 成功后生成新的 opaque generation；
  - 用 expected version CAS 写 envelope；
  - stale worker 不覆盖新数据；
  - 失败进入重试或 dead letter。
- standalone worker 使用 `EmptySummaryModel`，只返回五个空语义列表，不调用 DeepSeek。

### 7.3 队列状态结构

最终队列使用：

- `ready` list；
- `ready-members` 去重 set；
- `inflight` ZSET；
- 每个 job 的 token lease；
- delayed `retry` ZSET；
- overflow `pending` session set；
- per-session pending-job pointer；
- durable job payload；
- `dead` 和 `corrupt` ZSET。

### 7.4 审核发现和修复

第一轮独立审核发现初版队列有严重崩溃恢复问题：

- `LPOP` 后 worker 崩溃，job ID 会永久离开 ready；
- payload GET 与 lease 存在竞争；
- enqueue 可能覆盖已有 payload；
- capacity overflow 只记 session、不保留可提升的 job；
- worker 忽略 ACK/retry 所有权失败。

修复为完整 inflight 状态机：过期 lease 可回收、丢失 payload 进入 corrupt、pending 可提升、ACK 和 retry 都检查 token ownership。

第二轮审核又修复：

- pending coalescing 必须单调，不能让较弱任务覆盖更高 version/coverage；
- enqueue Lua 不再硬编码 job prefix，也不传未使用 key；
- retry deadline 必须基于真正失败/延迟发生时的当前时间；
- generation `created_at` 和 CCR expiry 必须以 Headroom 完成时间计算，而不是任务开始时间。

### 7.5 主要文件

- `src/short_term_memory/compression/async_headroom_client.py`
- `src/short_term_memory/jobs/redis_compression_queue.py`
- `src/short_term_memory/jobs/compression_worker.py`
- `tests/compression/test_async_headroom_client.py`
- `tests/jobs/test_redis_compression_queue.py`
- `tests/jobs/test_compression_worker.py`
- `tests/integration/test_redis_compression_queue.py`

### 7.6 测试记录与跳过

- 初始 RED：三个新模块均不存在。
- 初始 focused GREEN：`6 passed`；全量 `169 passed, 10 skipped`。
- 队列状态机审核修复后：`15 passed, 1 skipped`；全量 `178 passed, 11 skipped`。
- 第二轮修复 focused：`17 passed, 1 skipped`。
- opt-in 真实 Redis queue test 未运行；当时本机没有可用 Redis/redis-cli。

---

## 8. Task 6：memory write/read 核心用例与恢复

### 8.1 任务目标

实现两个业务接口背后的核心业务逻辑，同时保证在线写入快、原文不丢、Redis 过期可恢复、expired generation 不会被继续暴露。

### 8.2 write 逻辑

每个事件严格按以下顺序执行：

```text
Redis reserve sequence
  -> Journal append + fsync
  -> Redis commit original
  -> compression policy
  -> durable queue enqueue
```

具体实现包括：

- 对正文计算 SHA-256；
- event ID 幂等与冲突检查；
- batch 保持输入顺序；
- Redis commit 失败时抛 `RetryableWriteError`；
- 重试读取 Journal 中 canonical event，保持首次 timestamp 和 server fields；
- duplicate-only 请求不重复运行压缩策略或 enqueue；
- queue 的 ready、pending、idempotent、coalesced 都视为“持久压缩意图已经保存”。

### 8.3 read 与恢复逻辑

- warm read 并发读取 Redis envelope 和 recent originals。
- Redis originals 缺失时，在 worker thread 从 Journal 读取有限 recent turns。
- Journal 恢复通过一个 Lua `restore_originals()` 原子完成：
  - 写入前验证现存 reservation 的 digest/sequence；
  - Redis 已有更新 counter、pending reservation 或 message list 时拒绝覆盖；
  - 成功后恢复原始 JSON、reservation 状态、TTL 和最大 sequence。
- 缺失 envelope、进入 refresh window 或 generation 已过期时投递 rebuild job。
- fresh-but-near-expiry：后台刷新，不阻塞 read。
- 已过期：排队后等待受总超时限制的 rebuild completion；只有拿到 version、coverage、有效期都满足的新 envelope 才返回。
- Redis 和 Journal 都不可用时返回 `MemoryReadUnavailableError`，不构造虚假成功结果。
- read 返回 HMAC 去标识化的 Headroom Proxy URL 和 scope headers，但不调用 DeepSeek。

### 8.4 审核中发现并修复的问题

Task 6 是审核轮数最多的任务之一，重点修复如下。

#### 原子恢复与并发写竞争

- 初版恢复可能覆盖一个已经开始在线 reserve 的 session counter。
- 修复后，只要 sequence key、pending reservation 或 originals list 存在，Journal restore 就安全拒绝。
- 验证在线 seq=1 不会被 Journal 91..100 恢复覆盖；下一次仍为 seq=2。

#### Queue merge 丢失 rebuild 意图

- 初版更大的普通任务可能覆盖 rebuild job。
- 修复后 merge 采用：
  - `expected_version=max`；
  - `coverage=max`；
  - `rebuild=OR`。

#### recent turns 语义和排序

- 修复物理 append 顺序不等于 sequence 顺序的问题。
- 新增 `select_recent_turns()`：去重、冲突检测、sequence 排序、保留 system prefix、按完整 user turn 截取。
- code/document/skill 类型的 user 原文也能开启一个 turn，不再只认 conversation。

#### cold rebuild completion

- 初版没有跨 worker 完成边界，expired generation 可能只能 timeout。
- 先定义 `RebuildCompletionWaiter` protocol 和 in-process adapter；Task 8 再实现跨进程 Redis completion。
- waiter 能处理 unrelated job、coalesced target、retry、deferred、lost、stale 和并发 worker 已完成等情况。
- 对 waiter 返回 envelope 再做 version、coverage、fresh generation 验证。
- 冷重建等待耗时计入 `ReadTiming.recovery`。

#### 错误分类

- 只把明确的 Redis 基础设施错误作为可恢复/重试错误。
- `ValueError` 等数据破坏或编程错误不再被误判成 Redis miss。

#### 生命周期与有界并发

- in-process waiter 共享 worker concurrency semaphore。
- per-session locks 使用引用计数，完成、超时、取消后删除，避免长期内存增长。
- 100 个 session 操作后验证 lock table 为空。

### 8.5 主要文件

- `src/short_term_memory/service/memory_service.py`
- `src/short_term_memory/storage/async_redis_memory_store.py`
- `src/short_term_memory/storage/recent_originals.py`
- `src/short_term_memory/storage/journal_store.py`
- `src/short_term_memory/jobs/redis_compression_queue.py`
- `src/short_term_memory/jobs/compression_worker.py`
- `src/short_term_memory/ports.py`
- 对应 service/storage/jobs tests 与 fake Redis。

### 8.6 测试记录

- 最初 RED：`memory_service` module 不存在。
- 初始 focused GREEN：`5 passed`；全量 `187 passed, 11 skipped`。
- 多轮恢复/并发审核后：全量依次达到 `208`、`219`、`222`、最终 `234 passed, 12 skipped`。
- 最终 Task 6 focused：`76 passed`。

### 8.7 已知性能风险

- Redis recent read 仍可能使用完整 list 范围后再选择 recent turns。
- Journal recent read 虽改用反向固定 buffer，仍需扫描有效记录完成 sequence 排序和 turn correctness。
- 正确性已有测试，生产规模 p95/p99 必须通过 Task 11 实测决定是否增加索引。

---

## 9. Task 7：FastAPI、认证、指标与错误映射

### 9.1 任务目标

提供调用方真正可使用的两个 HTTP 业务接口，并保证认证、限流、body 限制、错误响应和指标不会泄露内容。

### 9.2 主要实现

- 业务接口严格只有：
  - `POST /v1/memories/write`；
  - `POST /v1/memories/read`。
- 运维接口：
  - `/health`；
  - `/ready` 在 Task 8 完成；
  - `/metrics`。
- Bearer token 使用 `secrets.compare_digest`。
- production 不允许空 token；development 可显式使用空 token。
- ASGI middleware 处理：
  - request ID；
  - 最大 body；
  - 即时并发 capacity；
  - `429 Retry-After`；
  - phase/request metrics。
- 稳定错误映射：401、409、413、422、429、503、500。
- 错误正文只包含稳定 error code 和 request ID，不返回 raw exception。
- Prometheus label 使用有限 route/method/status/stage，不使用 user/session/event ID，不记录正文。

### 9.3 关键审核问题：认证顺序

初版顺序是：

```text
占用 capacity -> 读取完整 body -> FastAPI dependency 做认证
```

独立审核将其判定为 Critical：未认证客户端可以发送一个永不结束的慢 body，占住容量；错误认证还可能先得到 413/422 而不是 401。

修复后顺序改为：

```text
读取并校验 Authorization header
  -> 认证成功后占 capacity
  -> 认证成功后才读取和解析 body
```

新增测试证明：

- slow unauthenticated stream 的 `receive()` 调用次数为 0；
- 它不能阻塞下一条合法请求；
- missing/wrong/duplicate/non-ASCII Authorization 都先返回相同 401；
- malformed JSON、超大 content-length、streaming oversize 都不能先于认证；
- 401 仍写入安全 metrics 和 request ID，不泄露 token/content。

### 9.4 主要文件

- `src/short_term_memory/service/auth.py`
- `src/short_term_memory/service/metrics.py`
- `src/short_term_memory/service/app.py`
- `tests/service/test_auth.py`
- `tests/service/test_metrics.py`
- `tests/service/test_app.py`

### 9.5 测试记录

- RED：三个 production module 不存在。
- 初始 focused GREEN：`29 passed`；全量 `263 passed, 12 skipped`。
- 认证顺序回归 RED：`9 failed`。
- 修复后 focused：`39 passed`；全量 `273 passed, 12 skipped`。

---

## 10. Task 8：Runtime、CLI、跨进程完成通知与部署

### 10.1 任务目标

把前面完成的 store、queue、worker、service 和 FastAPI 组装成真实可启动的独立 HTTP 服务，并满足 100 并发部署基础。

### 10.2 主要实现

- 新增 `ServiceRuntime.start()/close()`：
  - 每个进程一个共享 Redis pool；
  - 每个进程一个共享 `httpx.AsyncClient`；
  - 组装 Journal、store、planner、assembler、queue、worker、MemoryService、policy 和 scope factory；
  - partial startup 失败也关闭已经创建的资源；
  - close 某个资源失败时仍尝试关闭其他资源。
- 新增 `RedisRebuildCompletion`：
  - 使用 HMAC 去标识化 session key；
  - Redis marker 只作为通知优化；
  - envelope 始终是权威结果；
  - 支持 completion-before-wait、多个 waiter、coalesced job 和丢失通知。
- CLI：
  - `short-term-memory-api` 使用 Uvicorn app factory；
  - 默认 4 workers；
  - Uvicorn `limit_concurrency` 至少 100；
  - `short-term-memory-worker` 默认 8 个 loop。
- readiness：并发检查 Redis ping 和 Headroom `/health`，只返回组件布尔值。
- Compose 四服务：Redis、Headroom、memory-api、compression-worker。
- Headroom 作为独立 HTTP Proxy，配置 concurrency 200、DeepSeek 官方 upstream URL，不在 memory service 中安装或 import Headroom。

### 10.3 审核发现的 6 个 Important 问题

Task 8 初始实现后进行了独立审查，发现并全部修复：

1. **SIGTERM grace 实际无效**：CLI 立即 cancel worker，任务 lease 可能滞留 300 秒。
   - 修复：停止领取新任务，宽限期内等待当前任务；超时才 cancel；强制取消时立即归还 queue lease，并 shield 释放 session lease。
2. **Redis completion marker GET 失败会中断 envelope polling**。
   - 修复：marker 只是优化，读取失败继续 bounded envelope polling。
3. **completion timeout 不是总超时**。
   - 修复：一个外层 `asyncio.timeout` 包住 store read、marker read 和 sleep；外部 cancellation 继续传播。
4. **Redis Compose 没有持久化**。
   - 修复：启用 AOF `appendonly yes`、`appendfsync everysec` 和 named volume。
5. **4 个 API 进程共享 Journal，但只有进程内 RLock**。
   - 修复：每 session 的 RLock 外再加 POSIX `fcntl.flock`；lock filename 使用 session SHA-256；进程崩溃由 OS 自动释放。
6. **readiness Redis ping 可能永久挂起**。
   - 修复：Redis socket connect/operation timeout 加 readiness 总 timeout。

同时把真实 Redis HTTP integration 调整为 production Bearer auth，避免空开发认证掩盖生产问题。

### 10.4 主要文件

- `src/short_term_memory/service/runtime.py`
- `src/short_term_memory/cli.py`
- `src/short_term_memory/jobs/redis_rebuild_completion.py`
- `src/short_term_memory/jobs/compression_worker.py`
- `src/short_term_memory/jobs/redis_compression_queue.py`
- `src/short_term_memory/storage/journal_store.py`
- `compose.memory.yml`
- `compose.redis.yml`
- lifecycle、CLI、Compose 和 opt-in Redis integration tests。

### 10.5 测试记录

- 初始 Task 8 全量：`294 passed, 15 skipped`。
- 审核修复 focused：`113 passed, 3 skipped`。
- 审核修复全量：`318 passed, 20 skipped`。
- 两份 Compose 静态配置均通过。
- 真实 Redis/Headroom 没有启动。

---

## 11. Task 9：独立 DeepSeek 官方 SDK 示例

### 11.1 任务目标

证明 DeepSeek 不是“存记忆接口”或“读记忆接口”，而是调用方自己的第三条模型调用链路。

### 11.2 主要实现

新增 `examples/deepseek_chat.py`，完整执行：

1. write 用户原文；
2. read 当前 memory；
3. 从 read 响应取得 `headroom.proxy_url` 和 `scope_headers`；
4. 创建官方 `OpenAI(api_key=..., base_url=proxy_url, default_headers=scope_headers)`；
5. 调用 `chat.completions.create(model="deepseek-v4-flash", messages=...)`；
6. write assistant 回答。

安全边界：

- user 和 assistant 的 event ID 都使用 UUID；
- `DEEPSEEK_API_KEY` 只在 `main()` 从环境读取；
- key 不发送到 memory-api；
- key 不打印；
- memory service module 不 import OpenAI SDK；
- `--help` 不需要 key，也不会触发 OpenAI import。

### 11.3 主要文件

- `examples/deepseek_chat.py`
- `tests/examples/test_deepseek_chat.py`
- `tests/examples/__init__.py`

### 11.4 测试记录

- RED：`examples.deepseek_chat` 不存在。
- focused GREEN：`4 passed`。
- `--help` 通过。
- 当时全量测试受到并行 Task 10 临时未完成 import 影响；排除该临时文件后结果为 `313 passed, 19 skipped`。
- 最终统一全量测试已在 Task 12 达到 `325 passed, 20 skipped`。
- 没有进行真实 DeepSeek 调用。

---

## 12. Task 10：对话、代码、文档、Skill 案例与 Headroom/DeepSeek 验收

### 12.1 任务目标

提供用户要求的四类真实内容案例，并建立“默认跳过、显式启用就必须真实连接”的验收门禁。

### 12.2 四类 fixtures

| 类型 | 文件 | 大小 | 唯一 anchor |
|---|---|---:|---|
| 对话 | `conversation.txt` | 约 45 KB | `CONVERSATION_ORIGINAL_ANCHOR_7391` |
| 代码 | `code.py` | 约 34 KB | `CODE_ORIGINAL_ANCHOR_7391` |
| 文档 | `document.md` | 约 41 KB | `DOCUMENT_ORIGINAL_ANCHOR_7391` |
| Skill | `SKILL.md` | 约 41 KB | `SKILL_ORIGINAL_ANCHOR_7391` |

代码 fixture 能被 Python AST 解析；Skill fixture 包含合法 frontmatter、规则、步骤和示例。四个 anchor 各自只出现一次。

### 12.3 真实 Headroom 验收设计

`test_memory_headroom_cases.py` 对四类内容分别：

- 调用真实 `/v1/compress`；
- 检查 `tokens_after < tokens_before`；
- 检查报告压缩比；
- 只在测试代码中提取 CCR hash；
- 使用相同匿名 scope 调用官方 retrieval endpoint；
- 要求召回内容与原 fixture UTF-8 bytes 和 SHA-256 完全一致。

生产代码没有新增 hash 解析或自制 retrieval。

### 12.4 Fake provider continuation

扩展 deterministic fake OpenAI provider：

- 第一次请求要求出现 marker 并返回 `headroom_retrieve` tool call；
- 第二次请求必须包含配置的精确原文；
- 只有满足条件后才返回确认回答；
- snapshot 只记录 request count、reference 和 exact-match 状态，不泄露正文。

### 12.5 真实 DeepSeek 三轮 E2E 设计

- 双门禁：`SHORT_TERM_MEMORY_RUN_DEEPSEEK_E2E=1` 且 `DEEPSEEK_API_KEY` 非空。
- 三代计划范围：1–100、101–180、181–240。
- 每轮等压缩完成后通过 Headroom Proxy 调 DeepSeek。
- 要求回答包含对应 anchor，并要求 Headroom retrieval count 增长。
- API key 不进入 memory-api，失败信息不打印私密内容。

### 12.6 测试记录与跳过

- focused：`22 passed, 6 skipped`。
- 并行 Task 11 未完成期间，排除 `tests/load` 后全量为 `318 passed, 20 skipped`。
- 真实 Headroom 四类用例默认产生 4 个明确 skip。
- 显式打开 Headroom flag 且指向不可用 endpoint 时得到真实 `ConnectError`，证明不会“启用后仍假装 skip”。
- 真实 Headroom 和真实 DeepSeek 均未运行，没有使用用户 key，也没有产生费用。

---

## 13. Task 11：100 并发 SLO 负载门禁

### 13.1 任务目标

建立可重复运行的性能测试工具，不用单元测试结果冒充真实 SLO。

### 13.2 负载脚本能力

新增 `scripts/load_test_memory_api.py`，支持：

- `write`；
- `read`；
- `mixed`；
- `same-session`；
- `queue-saturated`。

实现细节：

- 默认 concurrency 100、requests 1000；
- 一个共享 `httpx.AsyncClient`，连接上限不小于 concurrency；
- 使用 `asyncio.Event` 同步释放 worker；
- 使用 `perf_counter_ns()` 计时；
- warm read 在正式计时前写入 session；
- same-session 先写唯一 event，再进行独立幂等 replay 阶段；
- queue-saturated 必须显式确认测试环境，并只读 Redis backlog 数量；
- JSON 报告只保存状态码、耗时、server timing 和错误类别，不保存 response body 或 memory content。

### 13.3 SLO 规则

| 路径 | p95 | p99 |
|---|---:|---:|
| write | ≤ 150 ms | ≤ 300 ms |
| warm read | ≤ 100 ms | ≤ 200 ms |

规则：

- 使用 inclusive nearest-rank percentile；
- 输入 1..100 时 p50=50、p95=95、p99=99；
- 任一请求错误或非 2xx 都失败；
- 任一 percentile 超限都非零退出；
- mixed 分别按 write/read 阈值判断。

### 13.4 文件和测试

- `scripts/load_test_memory_api.py`
- `tests/load/test_load_statistics.py`
- `tests/load/__init__.py`
- `docs/performance.md`

负载统计共有 7 个单元测试，已包含在最终 `325 passed` 中。脚本与文档均完成，但没有启动真实部署运行 100 并发，因此当前状态是“测试工具可用，SLO 未测量”。

---

## 14. Task 12：README、接口配置对齐、交付文档和最终验证

### 14.1 任务目标

把最终架构、两个接口、存储时间、Headroom 边界、DeepSeek 边界、并发目标、测试方式和已知不足统一写清楚，方便和老师、调用方及部署方对齐。

### 14.2 文档更新

- 重写 `README.md`：
  - 明确项目是独立 HTTP 记忆服务；
  - 明确只有两个业务接口；
  - 明确 Headroom 全权负责压缩和 CCR；
  - 明确 DeepSeek 是独立模型调用；
  - 提供本地和 Compose 启动方式；
  - 提供存储位置和默认时间表；
  - 提供安全/非安全配置概览。
- 重写 `docs/short-term-memory.md`：
  - 记录写前 Journal、Redis、queue、恢复、跨进程、故障和安全边界。
- 更新 `docs/third_party/headroom.md`：
  - `uv tool` 只作为本地安装/隔离方式；
  - 生产使用独立 HTTP/Compose；
  - 项目不假设 CCR backend 永远是内存 LRU 或 SQLite；
  - 不访问 `~/.headroom/ccr_store.db`。
- 新增 `docs/memory-api-alignment.md`：
  - 两个接口完整 JSON 示例；
  - write/read/DeepSeek 调用顺序；
  - safe/secret 配置责任矩阵；
  - 原文、generation、CCR 的位置和 TTL；
  - no-recompression 解释。
- 新增 `docs/performance.md`：
  - SLO、运行命令、环境记录表；
  - 明确写为 `NOT MEASURED`。
- 新增 `docs/known-limitations-and-skipped-tests.md`：
  - 集中记录全部未实测、格式差异、部署缺口和性能风险。

### 14.3 最终验证过程

- 安装 `api + deepseek + dev` extras。
- 全量 pytest：`325 passed, 20 skipped in 4.71s`。
- Ruff rule check：通过。
- `git diff --check`：通过。
- DeepSeek 示例和 load script `--help`：通过。
- 两份 Compose 静态配置：通过。
- `python -m build` 隔离构建最初因沙箱无法联网下载 setuptools 失败；这不是源码错误。
- 改用允许访问 uv 本地 cache 的 `uv build` 后，sdist 与 wheel 均成功：
  - `dist/short_term_memory-0.1.0.tar.gz`；
  - `dist/short_term_memory-0.1.0-py3-none-any.whl`。
- 最终工作区在提交后干净，收口提交为 `07400a1`。

### 14.4 本任务没有做的事情

- 没有因为文档任务去改 Headroom 内部存储。
- 没有把 DeepSeek key 写入 Compose 或配置响应。
- 没有把外部 SKIP 写成 PASS。
- 没有为了通过格式门禁而机械重排全部历史代码和大型 fixture。

---

## 15. 审核与测试方法汇总

### 15.1 TDD

各任务基本采用：

1. 先写失败测试或确认缺失模块导致 RED；
2. 实现最小功能达到 GREEN；
3. 跑 focused tests；
4. 跑全量 suite；
5. Ruff 和 diff check；
6. 独立审核；
7. 为审核发现补 regression test，再修复。

这次审核不是只看“功能能跑”，重点检查了：

- 不可变对象是否真的深度不可变；
- Redis Lua 与 fake 的语义是否一致；
- sequence gap、冲突和 stale CAS；
- worker 崩溃和 queue lease 恢复；
- Redis restore 与在线写的竞争；
- coalesced rebuild 不能丢失；
- expired generation 不能暴露；
- slow unauthenticated body 不能占容量；
- 多 Uvicorn process 共享 Journal 的锁；
- SIGTERM 时任务是否真正归还；
- timeout 是否覆盖整个操作而不是某个 sleep。

### 15.2 关键审核价值

如果只停留在每项任务第一次 GREEN，下列问题仍会存在：

- Headroom 扩展字段仍可被调用方修改；
- Journal retention 可能删除非法空 session 文件；
- Redis duplicate commit 可能不核对 digest；
- generation 遇到 gap 仍可能推进；
- worker crash 后任务可能永久丢失；
- Journal restore 可能覆盖在线 reserve；
- rebuild intent 可能被普通任务合并掉；
- unauth slow body 可能占满 API 容量；
- SIGTERM 后 Redis lease 可能滞留数分钟；
- 4 个 API process 可能并发写坏同一 Journal。

这些问题都通过审核后的新增 RED 测试和修复关闭。

---

## 16. 本次明确跳过的测试

### 16.1 真实 Redis

默认需要显式环境变量，例如：

- `SHORT_TERM_MEMORY_RUN_REDIS_INTEGRATION=1`；
- `RUN_REDIS_QUEUE_INTEGRATION=1`。

当天环境没有运行中的真实 Redis，部分尝试得到 `ConnectionRefusedError`，因此没有完成真实 Redis Lua、HTTP、queue 和 completion 的端到端验证。

### 16.2 真实 Headroom

需要：

- 运行中的 Headroom HTTP service；
- `SHORT_TERM_MEMORY_RUN_HEADROOM_AUTO_ROUTING=1` 或对应 Proxy CCR 开关。

真实四类压缩、官方 retrieval 和透明 CCR continuation 没有执行。只完成了 fake provider deterministic continuation，以及“显式开启但服务不可用就失败”的门禁证明。

### 16.3 真实 DeepSeek

需要同时设置：

- `SHORT_TERM_MEMORY_RUN_DEEPSEEK_E2E=1`；
- `DEEPSEEK_API_KEY`。

当天没有使用用户 key、没有请求 DeepSeek、没有产生费用。因此真实 `deepseek-v4-flash` 模型名、三轮回答质量、CCR tool continuation 和 retrieval count 均未在外部环境验证。

### 16.4 真实 100 并发

负载工具已完成，但 Redis、Headroom、memory-api 和 worker 没有组成真实部署运行。因此以下 SLO 当前都是未测量：

- write p95/p99；
- warm read p95/p99；
- cold Journal recovery；
- Headroom 20K-token compression；
- CCR retrieval；
- DeepSeek TTFT。

最终 `20 skipped` 主要来自这些显式外部服务门禁。SKIP 表示“未请求运行”，不是 PASS。

---

## 17. 当前不足和遗留风险

### 17.1 性能尚未实测

这是目前最大的未关闭项。配置能够承载至少 100 个入口并发，不等于生产 p95/p99 已达标。

- `read_recent_originals()` 仍可能扫描 Redis 完整 list。
- Journal recent read 仍需扫描记录后排序和构造完整 turns。
- same-session 写入会争用同一个 Journal lock。
- Headroom 当前 Compose 是单 worker、concurrency 200，实际 CPU/内存、压缩器和 upstream 行为未知。

建议先运行 Task 11 的五个场景，再决定是否增加 Redis 有序索引、Journal side index、更多 Headroom replica 或 session-affinity routing。

### 17.2 默认语义摘要为空

standalone worker 默认使用 `EmptySummaryModel`，五类字段保持空数组：

- `current_goal`；
- `preferences`；
- `confirmed_facts`；
- `pending_items`；
- `attachment_references`。

这不影响 Headroom 压缩和 CCR，但如果业务需要自动语义摘要，仍需注入真实 SummaryModel adapter，并补充服务配置与测试。

### 17.3 Journal retention 未接入周期调度

30 天 retention job 已实现并测试，但当前 runtime/Compose 没有单独的定时 scheduler 服务。部署方需要用 cron、Kubernetes CronJob 或 Daily Job 显式调用，否则 Journal 不会仅因为配置了 30 天就自动清理。

### 17.4 Docker 镜像流水线缺失

`compose.memory.yml` 要求部署方提供：

- `MEMORY_SERVICE_IMAGE`；
- `HEADROOM_IMAGE`。

仓库当前没有 Dockerfile、镜像 build、SBOM、签名或 registry 发布流水线。因此 Compose 是部署拓扑，不是“一条命令从源码自动构建生产镜像”。

### 17.5 Headroom 真实 backend 和 CCR 行为未验证

项目正确地不访问 Headroom backend，也不声称 Headroom 必定使用项目侧 SQLite。但仍需在实际选定的 Headroom 镜像/版本中确认：

- CCR backend 实际是内存 LRU、SQLite 还是其他实现；
- TTL 是否确实与 43,200 秒一致；
- 多 replica 时 scope/marker 能否命中同一 backend；
- DeepSeek 是否会按当前 Headroom 版本自动进行 `headroom_retrieve` 和续跑。

### 17.6 Redis AOF 仍有约一秒窗口

Compose 使用 `appendfsync everysec`。这是性能与持久性的折中；宿主机突然断电时，最近约一秒 Redis 变更仍可能丢失。精确原文可从 Journal 恢复，但 queue/envelope 在线状态可能需要重建。

### 17.7 `flock` 的平台限制

跨进程 Journal 锁依赖 POSIX `fcntl.flock`：

- Linux/macOS 可用；
- Windows 明确不支持；
- 部分网络文件系统可能没有可靠 flock 语义。

如部署到不支持 flock 的共享存储，应改为单写入者、数据库或分布式锁设计。

### 17.8 格式门禁未完全通过

Ruff 静态规则通过，但全仓库 `ruff format --check` 报告 52 个历史文件会被重排。本次没有为了格式统一制造大量无关 diff，也没有格式化作为测试数据的 `code.py` fixture。

后续可单独做一次“format-only”提交，先排除需要保持原样的 fixtures，再建立 CI format gate。

### 17.9 示例和真实验收的发布边界

DeepSeek 示例和负载脚本是 repository artifacts；wheel 主要包含 `short_term_memory` package 和 CLI modules。若要求把示例、fixtures 或 load runner 一并随 wheel 发布，需要进一步配置 setuptools package data 或改为单独的运维工具包。

### 17.10 密钥管理仍由部署方负责

代码已避免泄露，但没有提供 Vault/KMS 集成。生产仍需部署方管理：

- `MEMORY_API_AUTH_TOKEN`；
- `SHORT_TERM_MEMORY_SCOPE_SECRET`；
- `DEEPSEEK_API_KEY`；
- 含密码的 Redis URL。

---

## 18. 建议的后续顺序

1. 构建并固定 `MEMORY_SERVICE_IMAGE` 与审核后的 `HEADROOM_IMAGE`。
2. 在隔离测试环境启动 Redis、Headroom、memory-api、compression-worker。
3. 运行真实 Redis 全部 opt-in integration tests。
4. 运行四类真实 Headroom 压缩和 byte-identical CCR retrieval。
5. 使用用户自己的 key 运行一次真实 DeepSeek 三轮 E2E，并保存脱敏证据。
6. 运行 Task 11 五个 100 并发场景，记录机器、拓扑、worker 和 JSON 报告。
7. 根据数据优化 Redis/Journal read path 和 Headroom scaling。
8. 接入真实 SummaryModel 与 Journal retention scheduler。
9. 增加 Docker build、镜像扫描、CI 外部服务测试和独立 format-only 清理。

## 19. 结论

当天完成的不只是两个 API，而是从领域模型、Journal、Redis 原子状态、原文 generation、Headroom worker、恢复语义、HTTP 安全、跨进程部署、DeepSeek 独立调用、四类案例到性能门禁和交付文档的一整套短期记忆链路。

最重要的设计结论是：

- 精确原文由 Journal 和 Redis 在线副本保障；
- Headroom 全权负责压缩与 CCR；
- memory service 不读取或修改 Headroom backend；
- 每次压缩输入只来自精确原文，不会递归压缩旧压缩结果；
- DeepSeek 是独立官方模型调用，不是 memory write/read 的内部实现；
- 外部服务和真实 SLO 尚未实测，现阶段不能把 100 并发、真实 CCR 或 DeepSeek E2E 写成已经通过。
