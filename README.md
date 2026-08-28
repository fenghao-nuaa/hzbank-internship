# 杭银实习项目成果

本仓库汇总实习期间完成的三个 Agent 基础能力项目，面向指导老师、项目维护者和接入这些服务的 Agent 开发者。本文先解释每个项目解决什么问题、核心功能如何工作，再给出实际接口、依赖、部署方法和最短运行步骤；实现细节可继续进入各子项目 README 阅读。

## 成果概览

| 项目 | 解决的问题 | 核心能力 | 详细文档 |
|---|---|---|---|
| short-term-memory | 长会话上下文持续增长、一次压缩不够，以及 Redis 过期后历史会话难以恢复 | 递进式上下文压缩、压缩原文召回、历史 Session 热激活与冷恢复 | [项目 README](short-term-memory/README.md) |
| SemanticaAdapter | Agent 的业务规则、证据、决策、审批和例外缺少统一治理边界 | 确定性规则治理、审批与政策例外、决策追踪和可验证审计包 | [项目 README](SemanticaAdapter/README.md) |
| DREAM | 画像更新、经验总结等重模型任务放在会话内会增加响应延迟 | 会话外记忆蒸馏、用户画像与 AI 决策经验沉淀、相关记忆检索 Skill | [项目 README](DREAM/README.md) |

## 仓库结构

```text
hzbank-internship/
├── README.md                 # 本使用说明，也是 GitHub 仓库首页
├── short-term-memory/       # 短期上下文压缩、召回与历史 Session 恢复
├── SemanticaAdapter/        # Agent 决策治理与审计
├── DREAM/                   # 会话外记忆蒸馏与 Memory Retrieval Skill
└── docs/                    # 本成果仓库的设计规格和实施记录
```

## 通用环境要求

- Git；
- [uv](https://docs.astral.sh/uv/)；
- Python 3.11 或更高版本。short-term-memory 和 DREAM 要求低于 Python 3.14；
- 生产密钥通过环境变量或密钥管理系统注入，不提交真实 `.env`。

克隆仓库：

```bash
git clone https://github.com/fenghao-nuaa/hzbank-internship.git
cd hzbank-internship
```

三个项目彼此独立安装和运行，请根据需要进入对应目录。

## 项目一：short-term-memory

short-term-memory 是一个独立 HTTP 服务，负责维护 Agent 当前 Session 的有界短期上下文。完整原始消息持久化在 Journal；Redis 保存可过期、可重建的在线上下文投影；Headroom 负责细粒度压缩和 CCR 原文缓存。服务只管理和准备上下文，不替 Agent 生成最终回答。

### 核心功能一：上下文压缩与原文召回优化

早期实现只能把较旧原文压缩成若干 Headroom generation。Session 继续增长后，generation 本身仍会逐渐占满模型窗口。当前实现把压缩处理组织为可重复执行的递进链路：

1. L1 清理满足时间条件的陈旧工具结果，同时保留最近消息；
2. L2 根据模型上下文窗口、保留输出预算和当前 token 数判断是否需要继续压缩；
3. L4 优先维护结构化 Session Memory，用于保存任务状态、文件、流程、错误修正和关键结果；
4. L4 不存在、不可用或仍不足时，L3 对“上一版摘要 + 后续新增上下文”生成新的连续性摘要。

L3/L4 不是把新摘要继续堆到旧上下文末尾，而是通过 compact boundary 和 context revision 替换已经覆盖的历史。因此同一个 Session 可以经历多次“摘要 + 新消息 → 新摘要”，不会再次退化成只能压缩一次。

Headroom 与 L3/L4 不冲突。Headroom 继续负责较早原文的细粒度 generation 压缩；L3/L4 在更高水位对已有摘要、未覆盖 generation 和新增消息做进一步归纳。活动上下文最终由“最新连续性摘要 + 未覆盖压缩段 + 最近完整消息”组成。

压缩不会删除最终原文来源。当模型需要摘要无法提供的代码、参数、错误信息或历史工具输出时，Agent 的工具循环会自动选择两条路径：

- 摘要或 generation 中存在 Headroom marker 时，调用 `headroom_retrieve(hash)`，服务通过 CCR 取回对应原文；
- 没有可用 marker、CCR 已失效或需要从大范围历史中定位内容时，先对当前 Session Journal 执行 Grep，再按 sequence 范围 Read 原文。

用户不需要手工触发召回。Agent 把工具结果作为 `role="tool"` 放回本轮 working messages，再调用同一个模型继续回答。

关键实现：

- [上下文准备与压缩协调](short-term-memory/src/short_term_memory/service/context_coordinator.py)
- [活动上下文和摘要覆盖组装](short-term-memory/src/short_term_memory/compression/context_query.py)
- [Headroom CCR 召回](short-term-memory/src/short_term_memory/compression/ccr_recall.py)
- [Agent 自动工具循环](short-term-memory/src/short_term_memory/agent/agent_chat.py)

### 核心功能二：历史 Session 切换

当用户从当前会话切回历史 `session_id` 时，Agent 应在写入新问题前调用激活接口。这样服务可以先确定目标 Session 的最新 sequence 和有效上下文版本，避免新消息写到过期投影或错误位置。

如果目标 Session 仍在 Redis 中，激活走幂等热路径并继续使用当前在线投影。如果 Redis 已经过期，服务会从该 Session 的日期化 Journal 中读取最新不可变 `compaction_checkpoint`，恢复最新 L3/L4 revision、摘要覆盖范围、最大 sequence 和最近 N 轮完整对话，得到一份不会被完整历史占满的有界上下文。

冷恢复后，Agent 立即获得“最新摘要 + 最近原文”的基础上下文，可以继续提问；Headroom generation 和 CCR 索引由后台重建。需要历史细节时，仍可使用 CCR 或 Journal Grep/Read，因此恢复压缩上下文不会切断原文来源。

关键实现：

- [Session 热激活和冷恢复](short-term-memory/src/short_term_memory/service/session_activation.py)
- [L3/L4 checkpoint 持久化](short-term-memory/src/short_term_memory/storage/compaction_checkpoint.py)
- [最近 N 轮原文恢复](short-term-memory/src/short_term_memory/storage/recent_originals.py)

### HTTP 接口

生产环境中的业务接口需要 `Authorization: Bearer <MEMORY_API_AUTH_TOKEN>`。

| 方法 | 路径 | 作用 |
|---|---|---|
| `POST` | `/v1/memories/activate` | 在写入前激活当前或历史 Session；Redis 过期时执行冷恢复 |
| `POST` | `/v1/memories/write` | 幂等写入原始事件，并投递需要的后台压缩任务 |
| `POST` | `/v1/memories/read` | 读取当前在线上下文或有界历史视图 |
| `POST` | `/v1/memories/prepare` | 按模型预算运行上下文处理并返回本轮 messages、tools 和 boundary |
| `POST` | `/v1/memories/recall` | 按 Headroom marker hash 从 CCR 召回原文 |
| `POST` | `/v1/memories/transcript/grep` | 在当前 Session Journal 中按关键词定位 sequence |
| `POST` | `/v1/memories/transcript/read` | 按 sequence 范围有界读取 Journal 原文 |
| `GET` | `/health` | 进程存活检查 |
| `GET` | `/ready` | Redis 和 Headroom 就绪检查 |
| `GET` | `/metrics` | Prometheus 指标 |

完整请求与响应示例见 [short-term-memory 核心接口](short-term-memory/README.md#核心接口)。

### 依赖与配置

项目要求 Python 3.11-3.13。基础依赖包括 HTTPX、Pydantic 和 `redis==6.4.0`；`api` extra 安装 FastAPI、Uvicorn 和 Prometheus Client，`deepseek` extra 安装 OpenAI-compatible SDK，`dev` extra 安装测试、检查和构建工具。准确版本范围见 [pyproject.toml](short-term-memory/pyproject.toml)。

运行时还需要：

- Redis：保存带 TTL 的 Session 在线投影、压缩队列和索引；
- Headroom：独立部署，生成压缩段并维护 CCR 原文缓存；
- Journal 数据目录：持久保存完整消息和 compaction checkpoint；
- Continuity Compaction Model：使用与 Agent 兼容的模型 Provider，作为独立 compact 请求生成 L3/L4 摘要。

常用配置位于 [`.env.example`](short-term-memory/.env.example)，包括 `REDIS_URL`、`HEADROOM_SERVICE_URL`、`SHORT_TERM_MEMORY_HOME`、`MEMORY_API_AUTH_TOKEN`、`SHORT_TERM_MEMORY_SCOPE_SECRET` 和压缩阈值。

### 部署

本地开发可单独启动 Redis、Headroom、HTTP API 和 compression worker。API 处理 Agent 请求，worker 异步处理 generation 和 Session Memory 更新；二者必须使用相同的 Redis、Journal 目录、Headroom 服务和 scope secret。

容器部署可参考 [compose.memory.yml](short-term-memory/compose.memory.yml)。该文件包含 Redis、Headroom、`memory-api` 和 `compression-worker` 四个服务，并分别持久化 Redis 数据与 Journal。部署前需要提供 `MEMORY_SERVICE_IMAGE`、`HEADROOM_IMAGE`、API token 和 scope secret。

### 快速入门

```bash
cd short-term-memory
uv sync --extra api --extra deepseek --extra dev
docker compose -f compose.redis.yml up -d
cp .env.example .env
```

启动已独立安装的 Headroom 后，运行 API 与 worker：

```bash
uv run short-term-memory-api
uv run short-term-memory-worker
```

检查服务：

```bash
curl http://127.0.0.1:8080/health
curl http://127.0.0.1:8080/ready
```

Agent 推荐通过 `AgentChatClient` 接入，它会完成 Session 激活、消息写入、上下文准备、模型调用、工具召回和最终回答写回。完整代码见 [Agent 接入示例](short-term-memory/README.md#6-接入-agent)。

默认验证命令：

```bash
uv run python -m pytest -q
uv run python -m ruff check src tests examples scripts
uv build
```

## 项目二：SemanticaAdapter

SemanticaAdapter 是公司 Agent 与 Semantica 之间的稳定治理边界。Agent 只依赖本项目定义的领域模型和 HTTP 接口，不直接传递第三方库内部对象；服务端负责把业务规则、证据、决策、审批和例外组织为可复算、可追踪的审计过程。

### 核心功能一：决策规则、审批和政策例外治理

Agent 首先注册版本化画像，声明自身用途、输入输出和适用规则。每次业务处理创建独立 audit，提交本次输入和证据；治理服务调用 Semantica 后端完成本体校验和确定性规则计算，再把模型或业务 Agent 的建议结果固化为 decision。

治理流程采用失败关闭：缺少证据、规则不通过、需要人工审批但尚未授权时，决策不能直接进入最终状态。授权人员可以提交 Approval Record；确需偏离常规规则时，则必须记录 Policy Exception、理由和责任主体。这样“模型给出建议”和“业务允许执行”被明确分离。

Semantica 通过端口和映射层隔离。以后替换后端或升级第三方版本时，Agent 侧 HTTP 协议和领域对象可以保持稳定。

关键实现：

- [治理生命周期与失败关闭](SemanticaAdapter/src/semantica_adapter/services/governance.py)
- [决策、审批和例外领域模型](SemanticaAdapter/src/semantica_adapter/domain/models.py)
- [Semantica 后端适配](SemanticaAdapter/src/semantica_adapter/adapters/semantica/backend.py)

### 核心功能二：证据链、决策追踪与审计包导出

一次决策不会只保存最终结论。系统同时记录 Agent 画像版本、输入证据、规则执行结果、决策理由、置信度、审批和政策例外。调用 trace 接口可以按照 decision ID 读取完整审计链，供开发排查、人工复核或监管检查。

需要移交材料时，服务导出 ZIP 审计包，其中包含 JSON/RDF 等审计产物和 SHA-256 完整性信息。离线验证可以发现包内文件被修改；生产环境若要抵抗攻击者同时重写文件和清单，还需要把审计链头写入外部 WORM、签名服务或监管存证系统。

关键实现：

- [审计链与完整性验证](SemanticaAdapter/src/semantica_adapter/services/integrity.py)
- [HTTP trace 与审计包接口](SemanticaAdapter/src/semantica_adapter/http/app.py)

### HTTP 接口

除 `/health` 外，所有接口都需要 `X-API-Key`。

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET` | `/health` | 服务及治理后端健康检查 |
| `POST` | `/v1/agents` | 注册版本化 Agent 画像 |
| `POST` | `/v1/audits` | 创建 audit 并记录输入证据 |
| `POST` | `/v1/audits/{audit_id}/evaluate` | 执行本体校验和确定性规则 |
| `POST` | `/v1/audits/{audit_id}/decisions` | 固化决策结论和依据 |
| `POST` | `/v1/approvals` | 提交人工审批记录 |
| `POST` | `/v1/exceptions` | 记录政策例外 |
| `GET` | `/v1/decisions/{decision_id}/trace` | 查询完整决策审计链 |
| `POST` | `/v1/decisions/{decision_id}/audit-package` | 下载带完整性信息的 ZIP 审计包 |

字段定义和调用示例见 [SemanticaAdapter HTTP 接口](SemanticaAdapter/README.md#http-接口)。Agent 也可以使用项目提供的 `SemanticaHttpClient`，避免自行拼装 wire payload。

### 依赖与配置

项目要求 Python 3.11+。基础包只依赖 HTTPX，便于 Agent 侧安装轻量客户端；`server` extra 安装 FastAPI、Uvicorn 和锁定版本 `semantica==0.6.6`。依赖定义见 [pyproject.toml](SemanticaAdapter/pyproject.toml)。

服务端主要配置：

- `SEMANTICA_ADAPTER_API_KEY`：业务接口密钥，必填；
- `SEMANTICA_ADAPTER_AUTHORIZED_ACTORS`：允许审批的 `[actor_id, role]` JSON 数组；
- `SEMANTICA_ADAPTER_PROVENANCE_PATH`：provenance SQLite 文件路径；
- `SEMANTICA_ADAPTER_HOST`、`SEMANTICA_ADAPTER_PORT`：监听地址和端口，默认 `127.0.0.1:8001`。

### 部署

当前项目提供独立 Uvicorn 服务命令，没有内置生产反向代理。单机开发可以直接运行；生产环境应将服务部署在 HTTPS/mTLS、企业身份认证、限流和访问审计之后，并把 API Key 交给密钥管理系统。provenance 数据库应挂载持久卷，审计包和可信链头应进入受控存储。

### 快速入门

```bash
cd SemanticaAdapter
uv sync --extra server

export SEMANTICA_ADAPTER_API_KEY='development-only-key'
export SEMANTICA_ADAPTER_AUTHORIZED_ACTORS='[["risk-manager","reviewer"]]'
export SEMANTICA_ADAPTER_PROVENANCE_PATH='runtime-state/provenance.db'

uv run semantica-adapter-server
```

检查服务：

```bash
curl http://127.0.0.1:8001/health
```

运行测试和金额核对示例：

```bash
uv run python -m pytest -q
uv run python -m compileall -q src tests examples
uv run python examples/amount_reconciliation.py
```

完整 Agent 客户端示例见 [Agent 通过 Python 客户端接入](SemanticaAdapter/README.md#agent-通过-python-客户端接入)。

## 项目三：DREAM

DREAM 把耗时的记忆总结和长期经验整理移动到会话之外。完成的会话先进入按 `tenant_id / agent_id / user_id` 隔离的事件账本，后台任务再提取有长期价值的信息、更新 Active Memory，并为下一次用户提问提供相关记忆。

### 核心功能一：会话外的异步记忆蒸馏

Agent 完成任务后，只需把完整会话提交给 DREAM，当前会话不等待重模型总结。Background Review 根据用户空闲时间、累计事件数、累计 token 数或最大等待时间决定何时形成批次，在后台调用确定性 backend 或 OpenAI-compatible Provider 提取长期知识。

每个 `event_id` 都是幂等键，成功处理前保留在 pending ledger 中。用户画像或决策内容发生变化后，确定性 Curator 可立即整理；每天默认凌晨 3 点还会执行一次幂等兜底，Semantic Curator 则可以较长周期运行更重的归纳任务。

一次 Dream 会记录处理前快照、候选 Publication、处理报告和处理后快照。低风险结果可以自动激活，高风险结果进入审核流程；候选生成或写回失败时恢复原 Active Version，并保留 pending 事件供修复后重试。

关键实现：

- [Background Review 调度条件](DREAM/src/dream/application/scheduler.py)
- [Dream 和 Curator 编排](DREAM/src/dream/application/service.py)
- [Publication、激活与失败回滚](DREAM/src/dream/application/closed_loop.py)

### 核心功能二：用户画像、AI 决策卡和 Skill Candidates 沉淀

Background Review 提取出的内容不会全部写进同一个摘要。治理层会根据记忆类型、证据和风险把候选内容路由到不同存储：

- 用户画像保存较稳定的偏好、约束和交互习惯，并形成 `USER_PERSONA.md` 投影；
- AI 决策卡保存已验证的判断条件、行动建议、暂停条件和来源证据，经 Curator 整理后形成 `DECISION_RULES.md`；
- Skill Candidates 保存可能复用的工作流程和操作经验，等待后续验证或提升为正式 Skill。

这些内容按版本写入 Active Memory，而不是直接覆盖唯一文件。Agent 可以固定一个 snapshot 处理当前任务，避免后台 Dream 更新导致同一任务中途看到不同版本。

关键实现：

- [用户画像管理](DREAM/src/dream/memory/managers/persona.py)
- [AI 决策卡管理](DREAM/src/dream/memory/managers/decision_cards.py)
- [Skill Candidates 管理](DREAM/src/dream/memory/managers/skill_candidates.py)
- [候选记忆治理路由](DREAM/src/dream/governance/router.py)

当前版本虽然定义了 Todo 数据类型和 `TODOS.md` 快照边界，但尚未形成独立 Todo Manager 的完整写入、治理和检索闭环，因此不把“明日待办”列为已完成能力。

### 核心功能三：Memory Retrieval Skill

用户提出新问题时，外部 Agent 可以调用 `dream.retrieval.MemoryRetrievalSkill`。Skill 使用当前 query 和可选 task context 推断任务领域，只读取相同 tenant、agent、user 作用域下的 Active Memory，从中排序并筛选与问题相关的用户画像、Decision Rules 和 Decision Cards。

检索结果还会经过数量上限、重复内容过滤和 token budget 控制，最终返回结构化 memories 与可直接注入模型请求的 Markdown context。这样 Agent 不必把整个长期记忆目录加入提示词，只加载本轮问题需要的部分。

该能力目前是 Python Runtime 接口，不是 HTTP API。`POST /v1/tasks/start` 用于创建冻结的任务快照，不能替代按 query 进行相关性筛选的 Memory Retrieval Skill。当前检索数据模型已经预留 `SKILL_CANDIDATE` 类型，但 `MemoryLoader` 尚未把 Skill Candidates 加入实际检索源，因此不能宣称当前 Skill 已经检索这类候选。

关键实现：

- [Memory Retrieval Skill 公共入口](DREAM/src/dream/retrieval/skill.py)
- [Active Memory 加载范围](DREAM/src/dream/retrieval/loader.py)
- [相关记忆选择](DREAM/src/dream/retrieval/selector.py)
- [预算化上下文构建](DREAM/src/dream/retrieval/context_builder.py)

最小调用方式：

```python
from pathlib import Path

from dream.retrieval import MemoryRetrievalSkill

skill = MemoryRetrievalSkill(
    home=Path("~/.dream").expanduser(),
    tenant_id="enterprise-a",
    agent_id="service-agent",
)

result = skill.retrieve(
    user_id="user-001",
    query="这次高风险任务应该先检查什么？",
    task_context={"channel": "customer-service"},
    limit=5,
)

# 将 result.context 注入本轮 Agent/LLM 上下文。
```

### HTTP 与 Python 接口

会话、任务和后台处理：

| 方法 | 路径 | 作用 |
|---|---|---|
| `POST` | `/v1/dream/conversations` | 提交一段已完成会话，进入 pending ledger |
| `POST` | `/v1/tasks/start` | 创建下一任务使用的冻结 Active Memory 快照 |
| `POST` | `/v1/dream/run-pending` | 立即处理所有达到条件的 pending 作用域 |
| `POST` | `/v1/dream/run-curators` | 强制运行指定作用域的确定性 Curator |
| `POST` | `/v1/dream/run-due-curators` | 执行已经到期的每日 Curator |
| `GET` | `/v1/dream/reports/{run_id}` | 读取 Dream 运行报告 |
| `POST` | `/v1/dream/rollback/{snapshot_id}` | 按快照恢复 Active Memory |

验证和 Publication：

| 方法 | 路径 | 作用 |
|---|---|---|
| `POST` | `/v1/validation/import` | 通过 NDJSON 导入完整任务 |
| `POST` | `/v1/validation/dream` | 对指定作用域执行一次闭环 Dream |
| `GET` | `/v1/validation/publications/status` | 查询 latest 和 active 版本 |
| `POST` | `/v1/validation/publications/{version}/approve` | 批准高风险候选 |
| `POST` | `/v1/validation/publications/{version}/confirm-writeback` | 确认投影文件已经写回 |
| `POST` | `/v1/validation/publications/{version}/activate` | 激活已经就绪的版本 |
| `POST` | `/v1/validation/publications/{version}/reject` | 拒绝候选并恢复原状态 |
| `POST` | `/v1/validation/publications/{version}/rollback` | 恢复历史 Active Version |

Python Runtime 对外入口为 `dream.retrieval.MemoryRetrievalSkill.retrieve(user_id, query, task_context=None, limit=5)`，返回 `MemoryRetrievalResponse`。完整请求示例见 [DREAM Memory Formation](DREAM/README.md#完成一次-memory-formation)。

### 依赖与配置

项目要求 Python 3.11-3.13，运行依赖包括 FastAPI、HTTPX、`openai==2.24.0`、Pydantic 和 Uvicorn；`dev` extra 安装 pytest、pytest-asyncio 和 Ruff。准确版本见 [pyproject.toml](DREAM/pyproject.toml)。

默认确定性 backend 不需要模型密钥；真实 Background Review 需要 OpenAI-compatible Provider。主要配置位于 [`.env.example`](DREAM/.env.example)：

- `DREAM_HOME`：多租户事件账本、快照和 Active Memory 的根目录；
- `DREAM_REVIEW_BACKEND`、`DREAM_REVIEW_MODEL`、`DREAM_REVIEW_BASE_URL`、`DREAM_LLM_API_KEY`：记忆提取 Provider；
- `DREAM_REVIEW_IDLE_HOURS`、批次事件/token 和最大等待配置：Background Review 触发条件；
- `DREAM_CURATOR_DAILY_HOUR`：每日确定性 Curator 的本地时区小时，默认 3；
- `DREAM_CURATOR_CONSOLIDATE`：是否启用长周期 Semantic Curator。

### 部署

DREAM 当前以单个 FastAPI 进程部署。应用 lifespan 会启动后台 worker，周期检查 pending Dream、到期 Curator 和可选的实习会话同步，因此生产部署必须为 `DREAM_HOME` 挂载持久卷，并避免多个未协调进程同时写入同一作用域。

服务默认监听 `127.0.0.1:8765`。当前 API 没有内置企业身份认证，生产部署应放在经过认证、授权、TLS 和限流的网关之后。模型密钥只通过部署环境注入。

### 快速入门

```bash
cd DREAM
uv sync --extra dev
cp .env.example .env
uv run uvicorn dream.api:app --host 127.0.0.1 --port 8765
```

打开 Swagger：

```text
http://127.0.0.1:8765/docs
```

默认测试不需要真实模型：

```bash
PYTHONDONTWRITEBYTECODE=1 uv run python -m pytest -q -p no:cacheprovider
uv run ruff check src tests
uv build
```

## 数据安全

- 仓库不包含真实 `.env`、API Key、用户会话、Journal、Redis 数据、SQLite 运行库或审计输出；
- `.env.example` 和本文中的密钥均为字段名或开发占位值，生产环境必须由密钥管理系统注入；
- 默认单元测试不连接生产服务，真实 Redis、Headroom、Semantica 和模型 Provider 测试必须使用隔离环境；
- Session、Journal、DREAM Active Memory 和审计材料都可能包含敏感信息，生产部署需要独立的数据权限、备份、保留期限和访问审计。

## 当前交付边界

- short-term-memory 的真实 Redis、Headroom 和模型链路需要部署对应外部服务后执行 opt-in 验证；
- SemanticaAdapter 的生产接入还需要企业身份认证、HTTPS/mTLS、限流以及可信外部审计链头；
- DREAM 的 Memory Retrieval Skill 当前通过 Python Runtime 接入，不提供 HTTP 检索端点；Semantic Curator 默认关闭，Skill 仍处于候选阶段，Todo 尚未完成独立 Manager 闭环。
