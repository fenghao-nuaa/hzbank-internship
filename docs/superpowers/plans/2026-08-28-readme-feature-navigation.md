# README 功能导航表实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在根 `README.md` 的七个核心功能后增加细粒度、可点击的源码导航表，使指导老师能从想查看的功能直接定位实现文件。

**Architecture:** 保留现有项目说明、接口、依赖、部署和快速入门，在每个核心功能的解释文字后插入三列表格，并替换原有“关键实现”列表。表格按处理阶段定位主链路，不加入测试列，也不把未接入组件写成当前实现。

**Tech Stack:** Markdown、Git、Node.js 文件链接验证。

## Global Constraints

- 七个核心功能各增加一张且仅一张“功能导航表”。
- 每张表固定为“想定位的部分、这部分负责什么、源码入口”三列。
- 不出现“验证测试”列，不链接测试文件。
- 每个源码入口必须使用可点击的仓库相对 Markdown 链接。
- 导航粒度以入口、编排、处理、存储和输出阶段为单位，不为辅助函数单独建行。
- 保留现有接口、依赖、部署、快速入门、安全和交付边界章节。
- DREAM Retrieval Skill 当前只检索 Persona、Decision Rules 和 Decision Cards；Skill Candidates 尚未接入 loader。
- `retrieval/selector.py` 不属于 `MemoryRetrievalSkill.retrieve()` 当前主链路。

---

### Task 1: 补充 short-term-memory 两张功能导航表

**Files:**
- Modify: `README.md`
- Reference: `short-term-memory/src/short_term_memory/`

**Interfaces:**
- Consumes: short-term-memory 两个核心功能的现有文字说明
- Produces: 9 行压缩/召回导航和 6 行历史 Session 导航

- [ ] **Step 1: 替换“上下文压缩与原文召回优化”的关键实现列表**

使用 `apply_patch` 增加九行三列表格，依次定位：上下文准备入口、活动上下文组装、L1、L2、L4、L3、Headroom generation、CCR/Journal 两条召回路径、Agent 自动工具循环。

每个源码单元格必须链接设计规格指定的文件，并用链接文字说明职责，例如：

```markdown
| L3 连续性摘要 | 基于上一版摘要和 boundary 后新增上下文生成新 revision | [L3 执行逻辑](short-term-memory/src/short_term_memory/compression/traditional_compact.py)、[摘要提示](short-term-memory/src/short_term_memory/compression/compact_prompt.py) |
```

- [ ] **Step 2: 替换“历史 Session 切换”的关键实现列表**

增加六行三列表格，依次定位：HTTP 激活入口、Agent 写前激活、热/冷恢复编排、checkpoint、最近 N 轮、Headroom 冷重建。

- [ ] **Step 3: 核对所有 short-term-memory 链接**

运行：

```bash
rg -n '^\| .* \| .* \| .*short-term-memory/src/' README.md
```

预期：该项目两张表共 15 个数据行，每个源码入口可点击。

### Task 2: 补充 SemanticaAdapter 两张功能导航表

**Files:**
- Modify: `README.md`
- Reference: `SemanticaAdapter/src/semantica_adapter/`

**Interfaces:**
- Consumes: SemanticaAdapter 两个核心功能的现有说明
- Produces: 7 行治理导航和 6 行审计导航

- [ ] **Step 1: 替换“决策规则、审批和政策例外治理”的关键实现列表**

增加七行三列表格，依次定位：HTTP 治理入口、Agent 客户端、治理生命周期、领域模型、确定性规则执行、画像版本存储、审批/例外授权。

- [ ] **Step 2: 替换“证据链、决策追踪与审计包导出”的关键实现列表**

增加六行三列表格，依次定位：provenance 写入、Trace 查询、JSON/RDF 导出、审计包发布、离线完整性校验、ZIP 下载接口。

- [ ] **Step 3: 核对所有 SemanticaAdapter 链接**

运行：

```bash
rg -n '^\| .* \| .* \| .*SemanticaAdapter/src/' README.md
```

预期：该项目两张表共 13 个数据行，第三方适配、治理服务和 HTTP 边界可以分别定位。

### Task 3: 补充 DREAM 三张功能导航表

**Files:**
- Modify: `README.md`
- Reference: `DREAM/src/dream/`

**Interfaces:**
- Consumes: DREAM 三个核心功能的现有说明
- Produces: 8 行异步蒸馏导航、7 行记忆沉淀导航和 7 行 Retrieval Skill 导航

- [ ] **Step 1: 替换“会话外的异步记忆蒸馏”的关键实现列表**

增加八行三列表格，依次定位：会话导入、事件账本与作用域、触发策略、服务编排、LLM 提取、Curator 调度、Publication、快照/报告/回滚。

- [ ] **Step 2: 替换“用户画像、AI 决策卡和 Skill Candidates 沉淀”的关键实现列表**

增加七行三列表格，依次定位：提取结果分类、候选治理策略、记忆路由、用户画像、AI 决策卡、Skill Candidates、Agent 投影写回。

- [ ] **Step 3: 替换“Memory Retrieval Skill”的关键实现列表**

增加七行三列表格，依次定位：公共调用入口、模型、配置/领域识别、Active Memory loader、过滤、排序/检索、上下文构建。

表格必须明确：

```markdown
| Active Memory 加载 | 当前实际读取 Persona、Decision Rules 和 Decision Cards；尚不加载 Skill Candidates | [Active Memory Loader](DREAM/src/dream/retrieval/loader.py) |
```

不得把 `DREAM/src/dream/retrieval/selector.py` 列为该 Skill 的主流程入口。

- [ ] **Step 4: 核对所有 DREAM 链接**

运行：

```bash
rg -n '^\| .* \| .* \| .*DREAM/src/' README.md
```

预期：三张表共 22 个数据行，蒸馏、沉淀和检索主链路相互独立且可定位。

### Task 4: 验证、提交并上传

**Files:**
- Verify: `README.md`

**Interfaces:**
- Consumes: 七张完整功能导航表
- Produces: GitHub 首页可直接使用的功能定位文档

- [ ] **Step 1: 验证表格数量、列结构和禁用内容**

运行一个只读 Node.js 检查脚本，按七个核心功能切分 README，确认每段都包含 `功能导航`、固定三列表头和至少一个源码链接；全文不得出现 `| 验证测试 |`。

预期：`navigation_tables=7`，没有缺失表格或测试列。

- [ ] **Step 2: 验证全部 Markdown 相对链接**

解析 README 中所有非 HTTP、非锚点链接，以仓库根目录为基准检查目标文件。

预期：所有源码、子项目 README 和配置链接均存在。

- [ ] **Step 3: 验证功能定位覆盖**

检查设计规格列出的 50 个定位数据行全部存在：short-term-memory 15 行、SemanticaAdapter 13 行、DREAM 22 行。

预期：读者可以分别定位 L1-L4、双通道召回、历史冷恢复、治理状态、审批授权、审计包、后台蒸馏、记忆沉淀和 Retrieval Skill 主链路。

- [ ] **Step 4: 检查格式、敏感信息和提交范围**

```bash
git diff --check
git status --short
git diff --stat
```

同时扫描 README 中常见密钥格式。预期只修改根 README 和本次设计/计划记录，不包含密钥或运行产物。

- [ ] **Step 5: 提交 README 变更**

```bash
git add -- README.md
git commit -m "docs: add source navigation for project features"
```

- [ ] **Step 6: 合并、推送并核验 GitHub**

在独立 worktree 验证后快进合并到 `main`，再次运行链接与表格检查，推送 `origin main`。使用 GitHub 集成读取远端 README 和提交，确认首页内容已经包含七张功能导航表。
