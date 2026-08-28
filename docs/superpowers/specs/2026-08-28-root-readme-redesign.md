# 根 README 重构设计

## 目标

将仓库根目录的 `使用说明.md` 重构为 GitHub 默认展示的 `README.md`，面向指导老师、项目维护者和接入方，准确说明三个项目的功能、接口、依赖、部署和快速入门方式。

## 文件与首页行为

- 删除根目录 `使用说明.md`。
- 新建根目录 `README.md`，作为仓库首页唯一的总览和使用入口。
- 保留三个子项目现有 README，根 README 只提供足够理解和启动项目的信息，并链接到子项目详细文档。
- 不加入“项目之间的关系”或“三个项目如何协同”章节。

## 信息结构

根 README 依次包含：仓库简介、成果概览、仓库结构、通用环境要求、三个项目的独立说明、数据安全和交付边界。

每个项目采用相同的阅读顺序：

1. 项目解决的问题；
2. 核心功能及完整文字说明；
3. 少量关键源码入口；
4. 对外接口；
5. 依赖和配置；
6. 部署方式；
7. 快速入门与验证命令；
8. 子项目详细 README 链接。

不再保留逐文件、逐测试的大型功能定位表。源码链接只用于支持关键机制和快速定位实现。

## 项目功能边界

### short-term-memory

#### 1. 上下文压缩与原文召回优化

说明 Redis 在线上下文、Journal 原文、Headroom generation/CCR 与 L1-L4 递进压缩之间的职责；说明摘要替换已覆盖历史而不是持续累加，并说明 Agent 如何通过 `headroom_retrieve` 或 Journal Grep/Read 恢复准确原文。

关键源码链接集中指向上下文协调、递进压缩、CCR 召回和 Agent 工具循环，不把内部每个类拆成独立功能。

#### 2. 历史 Session 切换

说明写入新问题前的 Session 激活、Redis 热恢复、Redis 过期后的 Journal 冷恢复、`compaction_checkpoint`、最近 N 轮原文和 Headroom generation/CCR 重建。

关键源码链接集中指向 Session 激活、checkpoint 持久化和最近原文恢复。

### SemanticaAdapter

#### 1. 决策规则、审批和政策例外治理

说明 Agent 通过稳定领域模型和 HTTP API 注册画像、创建审计、执行确定性规则、固化决策，并在需要时经过人工审批或政策例外流程。说明失败关闭和后端隔离的作用，但不单独拆成更多一级功能。

#### 2. 证据链、决策追踪与审计包导出

说明证据、规则结果、决策理由、审批和例外如何形成可查询审计链；说明 ZIP 审计包、SHA-256 清单、离线完整性校验及可信外部链头的生产边界。

### DREAM

#### 1. 会话外的异步记忆蒸馏

说明完成会话进入事件账本后，由 Background Review 按空闲时间、事件数、token 或最大等待时间在后台处理，使重模型总结不阻塞当前会话。说明 pending 批次、幂等事件、定时 Curator、Publication 和失败回滚属于这条后台链路的可靠性机制。

#### 2. 用户画像、AI 决策卡和 Skill Candidates 沉淀

说明长期信息如何经过提取与治理后分别进入用户画像、Decision Cards 和可复用 Skill Candidates，并形成面向 Agent 的 Active Memory 投影。不得宣称尚未完成的 Todo Manager 闭环已经可用。

#### 3. Memory Retrieval Skill

说明用户提出新问题时，外部 Agent 调用 `dream.retrieval.MemoryRetrievalSkill`，按当前问题从 Active Memory 中选择相关的画像、决策规则、Decision Cards 和 Skill 内容，构造受预算约束的上下文并注入本轮请求。

必须明确该能力当前是 Python Runtime 接口，不是 HTTP API；`POST /v1/tasks/start` 提供冻结任务快照，但不能替代相关性检索 Skill。

## 接口与运行信息

### short-term-memory

- 列出 `/v1/memories/activate`、`write`、`read`、`prepare`、`recall`、`transcript/grep`、`transcript/read`，以及 `/health`、`/ready`、`/metrics`。
- 依赖以 `pyproject.toml` 为准：Python 3.11-3.13、Redis 6.4 客户端、Headroom 服务；API、模型和开发依赖通过 extras 安装。
- 部署说明 HTTP API 与 worker 必须同时运行，并说明 Redis、Headroom 和 Journal 数据目录配置。

### SemanticaAdapter

- 列出 Agent 注册、审计创建、规则执行、决策、审批、例外、trace 和 audit-package 接口。
- 依赖以 `pyproject.toml` 为准：Python 3.11+、HTTP 客户端；服务端 extra 固定使用 Semantica 0.6.6、FastAPI 和 Uvicorn。
- 部署说明 API Key、授权角色、provenance 数据库和生产 HTTPS/mTLS 边界。

### DREAM

- 按会话/任务、Dream/Curator、Publication/回滚三个接口组概述现有 FastAPI 路由。
- 单独列出 `MemoryRetrievalSkill` Python 接口及其非 HTTP 属性。
- 依赖以 `pyproject.toml` 为准：Python 3.11-3.13、FastAPI、HTTPX、OpenAI-compatible SDK、Pydantic 和 Uvicorn。
- 部署说明数据目录、模型 Provider、后台 worker 生命周期和 Swagger 地址。

## 验证标准

- 根目录只存在 `README.md`，不存在 `使用说明.md`。
- GitHub 能自动把根 README 显示在仓库首页。
- 每项功能均能对应当前源码，不把 DREAM 的 Memory Retrieval Skill 写入 short-term-memory。
- 所有接口路径、Python 版本、依赖、命令入口和端口均与源码或子项目 README 一致。
- 所有相对源码链接和子项目 README 链接存在。
- 文档不存在“项目之间的关系”或“三个项目如何协同”章节。
- 不提交真实密钥、运行数据、虚拟环境或构建产物。
