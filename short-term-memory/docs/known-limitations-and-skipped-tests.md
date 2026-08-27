# 已知限制与未执行的外部验收

> 本文档是交付清单，不把 `SKIP` 或未测量项写成已通过。最终本地验证为 `325 passed, 20 skipped`。

## 当前已知限制

- Redis warm read 目前会读取会话的完整 message list，Journal cold recovery 会扫描会话日志后再按 sequence/轮次选择。正确性已覆盖，但大会话下的 p95/p99 仍需负载门禁确认；如不达标，需增加 Redis 有序索引和 Journal 侧边索引。
- Headroom CCR 的内存/SQLite/其他 backend 由 Headroom 部署决定；本项目不读写 `~/.headroom/ccr_store.db`，也不自建 CCR hash 索引。
- Redis AOF `everysec` 在性能和持久性之间取折；宿主机或 Redis 进程突然失效时，最近约 1 秒的 Redis 更改仍可能丢失。Journal 仍是原文恢复源。
- 跨进程 Journal 锁依赖部署文件系统正确支持 POSIX `flock`；不支持该语义的网络文件系统需改用单写入者或数据库。
- 独立 worker 当前默认使用 `EmptySummaryModel`，因此五类语义摘要字段为空；Headroom 压缩/CCR 不受影响。如业务需要自动填充 `current_goal` 等字段，仍需在 composition root 注入并部署真实 SummaryModel adapter。
- `compose.memory.yml` 要求部署方提供 `MEMORY_SERVICE_IMAGE` 和经审核的 `HEADROOM_IMAGE`；仓库当前没有 Dockerfile、镜像构建或镜像签名流水线。
- Ruff 静态检查通过，但全仓库 `ruff format --check` 仍报告 52 个历史文件会被重排；本次为避免无关的大面积格式 diff 没有机械格式化旧文件和测试 fixture。

## 默认不执行的真实服务测试

- 真实 Redis：需 `SHORT_TERM_MEMORY_RUN_REDIS_INTEGRATION=1` 和可用 `REDIS_URL`。
- 真实 Headroom 压缩/CCR：需 `SHORT_TERM_MEMORY_RUN_HEADROOM_AUTO_ROUTING=1` 和运行中的 `HEADROOM_SERVICE_URL`。
- 真实 DeepSeek：需 `SHORT_TERM_MEMORY_RUN_DEEPSEEK_E2E=1` 与用户自有 `DEEPSEEK_API_KEY`，会产生外部 API 费用。
- 100 并发实服务 SLO：需启动 Redis、Headroom、memory-api 和 compression-worker 后运行负载脚本。单元测试通过不代表实机 p95/p99 已达标。

## 验收原则

- 未设置显式开关时，上述测试必须显示为 `SKIPPED`。
- 设置开关后如服务不可用，测试必须失败，不得再转为 `SKIPPED`。
- 日志、Prometheus 指标、负载报告不记录消息原文、CCR 召回原文、API key 或认证 token。

## 本次实际执行

- 全量 pytest：`325 passed, 20 skipped`。
- Ruff rules：通过；全仓库格式门禁未通过，原因见上方已知限制。
- Python sdist/wheel：`uv build` 成功。
- DeepSeek 示例 `--help`：成功且不需要 key。
- Compose 静态配置：两份配置均通过；没有启动容器。
- 真实 Redis、Headroom、DeepSeek、100 并发：未执行。
