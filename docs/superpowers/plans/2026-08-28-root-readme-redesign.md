# 根 README 重构实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将根目录 `使用说明.md` 重构为 GitHub 首页展示的 `README.md`，按当前源码准确说明三个项目的核心功能、接口、依赖、部署和快速入门。

**Architecture:** 根 README 作为成果导航和独立项目使用手册，不重复子项目 README 的全部细节。每个项目先解释少量核心功能，再集中列出真实接口、依赖、部署和最短验证路径；关键机制提供源码链接，Memory Retrieval Skill 只归入 DREAM。

**Tech Stack:** Markdown、Git、Python 3.11-3.13、FastAPI、Redis、Headroom、Semantica 0.6.6、OpenAI-compatible Provider、uv。

## Global Constraints

- 删除根目录 `使用说明.md`，根目录只保留 `README.md` 作为总说明。
- 不设置“项目之间的关系”或“三个项目如何协同”章节。
- short-term-memory 只按“上下文压缩与原文召回优化”“历史 Session 切换”两个核心模块组织。
- SemanticaAdapter 只按“决策规则、审批和政策例外治理”“证据链、决策追踪与审计包导出”两个核心模块组织。
- DREAM 按“会话外异步记忆蒸馏”“用户画像、AI 决策卡和 Skill Candidates 沉淀”“Memory Retrieval Skill”三个核心模块组织。
- DREAM Memory Retrieval Skill 是 Python Runtime 接口，不得写成 HTTP API。
- 所有接口、依赖、端口、命令和能力边界必须来自当前仓库源码或对应 `pyproject.toml`。
- 关键机制可以链接源码，但不恢复逐文件、逐测试的大型定位表。

---

### Task 1: 建立根 README 骨架并完成文件替换

**Files:**
- Create: `README.md`
- Delete: `使用说明.md`

**Interfaces:**
- Consumes: 已确认的设计规格 `docs/superpowers/specs/2026-08-28-root-readme-redesign.md`
- Produces: GitHub 仓库首页唯一总说明 `README.md`

- [ ] **Step 1: 读取现有使用说明并建立内容映射**

运行：

```bash
sed -n '1,260p' 使用说明.md
```

预期：识别可保留的仓库简介、成果概览、环境要求、安全边界和三个项目启动命令；删除过细的功能定位表与协同章节。

- [ ] **Step 2: 使用 `apply_patch` 将文件替换为根 README**

README 一级结构固定为：

```markdown
# 杭银实习项目成果

## 成果概览
## 仓库结构
## 通用环境要求
## 项目一：short-term-memory
## 项目二：SemanticaAdapter
## 项目三：DREAM
## 数据安全
## 当前交付边界
```

预期：根目录存在 `README.md`，不存在 `使用说明.md`；没有项目协同章节。

- [ ] **Step 3: 验证文件替换和章节约束**

运行：

```bash
test -f README.md
test ! -e 使用说明.md
rg -n '^#{1,3} ' README.md
! rg -n '^#{1,3} (项目之间的关系|三个项目如何协同)$' README.md
```

预期：四条命令均成功。

### Task 2: 重写 short-term-memory 使用说明

**Files:**
- Modify: `README.md`
- Reference: `short-term-memory/README.md`
- Reference: `short-term-memory/pyproject.toml`
- Reference: `short-term-memory/src/short_term_memory/service/app.py`

**Interfaces:**
- Consumes: 当前 HTTP 路由、CLI 入口、依赖 extras 和子项目 README
- Produces: 两个核心功能模块、接口表、依赖、部署和快速入门说明

- [ ] **Step 1: 编写“上下文压缩与原文召回优化”**

内容必须覆盖：Redis 在线投影、Journal 原文、Headroom generation/CCR、L1-L4 递进压缩、摘要替换覆盖历史、`headroom_retrieve` 与 Journal Grep/Read 自动原文召回。关键链接指向 `context_coordinator.py`、`context_query.py`、`ccr_recall.py` 和 `agent_chat.py`。

- [ ] **Step 2: 编写“历史 Session 切换”**

内容必须覆盖：新写入前调用 activate、Redis 热激活、Redis 过期后的 Journal 冷恢复、最新 compaction checkpoint、最近 N 轮、sequence 连续性和 Headroom generation/CCR 后台重建。关键链接指向 `session_activation.py`、`compaction_checkpoint.py` 和 `recent_originals.py`。

- [ ] **Step 3: 编写接口、依赖、部署和快速入门**

接口表必须包含：

```text
POST /v1/memories/activate
POST /v1/memories/write
POST /v1/memories/read
POST /v1/memories/prepare
POST /v1/memories/recall
POST /v1/memories/transcript/grep
POST /v1/memories/transcript/read
GET  /health
GET  /ready
GET  /metrics
```

依赖和命令必须包含 Python 3.11-3.13、Redis、独立 Headroom 服务、`api/deepseek/dev` extras、Redis compose、API 和 worker 双进程启动、健康检查以及测试命令。

- [ ] **Step 4: 与源码交叉验证**

运行：

```bash
rg -n '@app\.(get|post)' short-term-memory/src/short_term_memory/service/app.py
rg -n 'requires-python|dependencies|optional-dependencies|project.scripts' short-term-memory/pyproject.toml
```

预期：README 中每个路由和命令都能在源码或配置中找到依据。

### Task 3: 重写 SemanticaAdapter 使用说明

**Files:**
- Modify: `README.md`
- Reference: `SemanticaAdapter/README.md`
- Reference: `SemanticaAdapter/pyproject.toml`
- Reference: `SemanticaAdapter/src/semantica_adapter/http/app.py`

**Interfaces:**
- Consumes: 当前治理服务、HTTP 路由、Semantica 后端适配与完整性实现
- Produces: 两个核心功能模块、接口表、依赖、部署和快速入门说明

- [ ] **Step 1: 编写“决策规则、审批和政策例外治理”**

解释 Agent 如何注册版本化画像、创建审计、执行确定性规则、固化决策，并通过人工审批或政策例外完成失败关闭治理。关键链接指向 `services/governance.py`、`domain/models.py` 和 `adapters/semantica/backend.py`。

- [ ] **Step 2: 编写“证据链、决策追踪与审计包导出”**

解释证据、规则结果、决策理由、审批和例外如何形成 trace；说明 ZIP 审计包、SHA-256、离线校验和外部可信链头。关键链接指向 `services/integrity.py` 和 `http/app.py`。

- [ ] **Step 3: 编写接口、依赖、部署和快速入门**

接口表包含 `/health`、Agent 注册、审计创建、evaluate、decision、approval、exception、trace 和 audit-package。依赖说明 Python 3.11+、基础 HTTPX、server extra 中的 FastAPI/Uvicorn/Semantica 0.6.6；部署说明 `X-API-Key`、授权角色、provenance SQLite 路径和生产 HTTPS/mTLS 边界。

- [ ] **Step 4: 与源码交叉验证**

运行：

```bash
rg -n '@app\.(get|post)' SemanticaAdapter/src/semantica_adapter/http/app.py
rg -n 'requires-python|dependencies|optional-dependencies|project.scripts' SemanticaAdapter/pyproject.toml
```

预期：README 不包含源码中不存在的路由或依赖。

### Task 4: 重写 DREAM 使用说明

**Files:**
- Modify: `README.md`
- Reference: `DREAM/README.md`
- Reference: `DREAM/pyproject.toml`
- Reference: `DREAM/src/dream/api.py`
- Reference: `DREAM/src/dream/retrieval/skill.py`

**Interfaces:**
- Consumes: Background Review、Memory Manager、Curator/Publication 与 Memory Retrieval Skill
- Produces: 三个核心功能模块、HTTP/Python 接口、依赖、部署和快速入门说明

- [ ] **Step 1: 编写“会话外的异步记忆蒸馏”**

解释完成会话进入事件账本，Background Review 根据空闲时间、事件数、token 或最大等待条件后台执行；说明幂等、pending、Curator、Publication 和失败回滚如何保证后台链路可靠。关键链接指向 `application/scheduler.py`、`application/service.py` 和 `application/closed_loop.py`。

- [ ] **Step 2: 编写“用户画像、AI 决策卡和 Skill Candidates 沉淀”**

解释提取结果如何经过治理路由进入 Persona、Decision Cards 和 Skill Candidates，并形成 Active Memory。关键链接指向三个 memory manager 和治理路由；明确 Todo 尚未完成独立闭环。

- [ ] **Step 3: 编写“Memory Retrieval Skill”**

解释 Agent 在用户提问时调用 `dream.retrieval.MemoryRetrievalSkill`，根据问题选择相关 Active Memory，按预算构造上下文并注入当前模型请求。明确它是 Python Runtime 接口，不是 HTTP API，并链接 `DREAM/src/dream/retrieval/skill.py`、`context_builder.py` 和 `selector.py`。

- [ ] **Step 4: 编写接口、依赖、部署和快速入门**

HTTP 接口按会话/任务、Dream/Curator、Publication/回滚分组；Python 接口单独列出 Memory Retrieval Skill。依赖说明 Python 3.11-3.13、FastAPI、HTTPX、OpenAI 2.24.0、Pydantic 和 Uvicorn；部署说明 `.env`、Provider、数据目录、后台 lifespan worker、8765 端口和 Swagger。

- [ ] **Step 5: 与源码交叉验证**

运行：

```bash
rg -n '@application\.(get|post)' DREAM/src/dream/api.py
rg -n 'class MemoryRetrievalSkill' DREAM/src/dream/retrieval/skill.py
rg -n 'requires-python|dependencies|optional-dependencies' DREAM/pyproject.toml
```

预期：README 将 Retrieval Skill 标记为 Python 接口，所有 HTTP 路由来自 FastAPI 源码。

### Task 5: 文档质量验证、提交与上传

**Files:**
- Verify: `README.md`
- Verify: three project source trees and READMEs

**Interfaces:**
- Consumes: 完整根 README
- Produces: 可在 GitHub 首页直接阅读且链接有效的公开使用说明

- [ ] **Step 1: 验证 Markdown 相对链接**

解析 `README.md` 中所有非 HTTP、非锚点链接，以 README 所在目录为基准检查文件是否存在。

预期：不存在失效的项目 README、源码或文档链接。

- [ ] **Step 2: 验证内容边界**

运行：

```bash
test -f README.md
test ! -e 使用说明.md
! rg -n '^#{1,3} (项目之间的关系|三个项目如何协同)$' README.md
rg -n 'MemoryRetrievalSkill|Memory Retrieval Skill' README.md
git diff --check
git status --short
```

预期：文件替换正确、禁用章节不存在、Retrieval Skill 位于 DREAM 章节、Markdown diff 无新增格式错误。

- [ ] **Step 3: 检查敏感信息和提交范围**

运行：

```bash
git diff --name-status
git grep -nE 'AKIA[0-9A-Z]{16}|-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----|sk-[A-Za-z0-9_-]{20,}' -- README.md
```

预期：只修改根文档和计划记录，不包含真实密钥或运行数据。

- [ ] **Step 4: 提交文档变更**

```bash
git add README.md 使用说明.md
git commit -m "docs: restructure internship repository guide"
```

预期：生成只包含根 README 替换的提交。

- [ ] **Step 5: 推送并核验 GitHub 首页**

```bash
git push origin main
git ls-remote origin refs/heads/main
```

随后通过 GitHub 公共 API 读取根目录 `README.md`。

预期：远端 `main` 与本地 HEAD 一致，仓库公开，根 README 可读取并在仓库首页展示。
