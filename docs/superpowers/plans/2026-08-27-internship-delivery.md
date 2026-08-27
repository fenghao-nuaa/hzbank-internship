# 实习成果仓库交付 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将短期记忆和 Agent 决策审计两个项目整理成指导老师可以直接阅读、按功能定位、安装和测试的 GitHub 成果仓库。

**Architecture:** 新仓库采用单一 Git 历史和精简源码快照。`short-term-memory` 从已提交 Git 文件导出，`SemanticaAdapter` 从明确的源码/文档目录复制；根目录使用说明承担跨项目导航，各子项目 README 承担完整技术说明。

**Tech Stack:** Git、Markdown、Python 3.11+、uv、pytest、FastAPI、Redis、Headroom、Semantica

## Global Constraints

- 本次只纳入 `short-term-memory` 和 `SemanticaAdapter`，不纳入 `DREAM`。
- 不复制嵌套 `.git`、`.venv`、`.idea`、`.env`、缓存、构建产物、数据库或运行输出。
- `short-term-memory` 只复制当前 Git `HEAD` 已跟踪文件。
- `SemanticaAdapter` 保留 `legacy/auditgraph` 迁移对照源码，但它不进入 wheel。
- 保留不含密钥的 `.env.example` 和配置模板。
- 根目录 `使用说明.md` 必须提供功能到源码和测试的定位表。
- 推送前必须检查敏感信息、大文件、测试结果和最终 Git 文件清单。

---

### Task 1: 导出 short-term-memory 可复现源码快照

**Files:**
- Create: `short-term-memory/**`
- Source: `/Users/fenghao/PycharmProjects/compression/short-term-memory` Git `HEAD`

**Interfaces:**
- Consumes: 源仓库当前 `HEAD` 的已跟踪文件。
- Produces: 不含源仓库未跟踪文件和嵌套 Git 元数据的 `short-term-memory/`。

- [ ] **Step 1: 记录源提交并生成归档**

Run:

```bash
git -C /Users/fenghao/PycharmProjects/compression/short-term-memory rev-parse HEAD
git -C /Users/fenghao/PycharmProjects/compression/short-term-memory archive --format=tar --output=/private/tmp/hzbank-short-term-memory.tar HEAD
```

Expected: 第一条命令输出 40 位提交哈希，第二条命令生成归档且退出码为 0。

- [ ] **Step 2: 解压到成果仓库**

Run:

```bash
mkdir -p short-term-memory
tar -xf /private/tmp/hzbank-short-term-memory.tar -C short-term-memory
```

Expected: `short-term-memory/README.md`、`src/short_term_memory/` 和 `tests/` 存在，不存在 `short-term-memory/.git`。

- [ ] **Step 3: 验证未跟踪个人文件没有进入快照**

Run:

```bash
find short-term-memory -name '.superpowers' -o -name 'diag_env.py' -o -name '实习周报*'
```

Expected: 无输出。

- [ ] **Step 4: 提交短期记忆快照**

Run:

```bash
git add short-term-memory
git commit -m "feat: add short-term memory project"
```

Expected: 提交只包含 `short-term-memory/` 下的项目文件。

### Task 2: 导出 SemanticaAdapter 精简源码快照

**Files:**
- Create: `SemanticaAdapter/README.md`
- Create: `SemanticaAdapter/THIRD_PARTY_NOTICES.md`
- Create: `SemanticaAdapter/.gitignore`
- Create: `SemanticaAdapter/pyproject.toml`
- Create: `SemanticaAdapter/uv.lock`
- Create: `SemanticaAdapter/src/**`
- Create: `SemanticaAdapter/tests/**`
- Create: `SemanticaAdapter/examples/**`
- Create: `SemanticaAdapter/docs/**`
- Create: `SemanticaAdapter/legacy/**`

**Interfaces:**
- Consumes: `/Users/fenghao/PycharmProjects/semantica/semantica-adapter` 当前源码。
- Produces: 可安装、可测试且包含迁移对照代码的 `SemanticaAdapter/`。

- [ ] **Step 1: 复制明确允许的文件和目录**

Run:

```bash
mkdir -p SemanticaAdapter
rsync -a --exclude='__pycache__/' --exclude='*.pyc' /Users/fenghao/PycharmProjects/semantica/semantica-adapter/README.md /Users/fenghao/PycharmProjects/semantica/semantica-adapter/THIRD_PARTY_NOTICES.md /Users/fenghao/PycharmProjects/semantica/semantica-adapter/.gitignore /Users/fenghao/PycharmProjects/semantica/semantica-adapter/pyproject.toml /Users/fenghao/PycharmProjects/semantica/semantica-adapter/uv.lock /Users/fenghao/PycharmProjects/semantica/semantica-adapter/src /Users/fenghao/PycharmProjects/semantica/semantica-adapter/tests /Users/fenghao/PycharmProjects/semantica/semantica-adapter/examples /Users/fenghao/PycharmProjects/semantica/semantica-adapter/docs /Users/fenghao/PycharmProjects/semantica/semantica-adapter/legacy SemanticaAdapter/
```

Expected: 复制成功，`SemanticaAdapter/src/semantica_adapter/` 和 `SemanticaAdapter/legacy/auditgraph/` 均存在。

- [ ] **Step 2: 验证生成数据没有进入快照**

Run:

```bash
find SemanticaAdapter -name '.venv' -o -name '.idea' -o -name '__pycache__' -o -name '*.pyc' -o -name '*.ses' -o -name 'dist' -o -name '*-output' -o -name '*-state'
```

Expected: 无输出。

- [ ] **Step 3: 提交 Agent 决策审计快照**

Run:

```bash
git add SemanticaAdapter
git commit -m "feat: add SemanticaAdapter project"
```

Expected: 提交只包含明确复制的项目源码、文档、示例和测试。

### Task 3: 编写面向指导老师的功能定位文档

**Files:**
- Create: `.gitignore`
- Create: `使用说明.md`

**Interfaces:**
- Consumes: 两个子项目最终目录和 README 中的真实入口。
- Produces: 从成果功能到源码、测试、运行命令的仓库级导航。

- [ ] **Step 1: 创建仓库级忽略规则**

使用 `apply_patch` 创建 `.gitignore`，至少包含：

```gitignore
.DS_Store
.idea/
.venv/
__pycache__/
*.py[cod]
.pytest_cache/
.ruff_cache/
.env
.env.*
!.env.example
dist/
build/
*.egg-info/
*.ses
*-output/
*-state/
```

Expected: 新仓库后续运行测试时不会把本地环境和产物加入 Git。

- [ ] **Step 2: 创建 `使用说明.md`**

使用 `apply_patch` 写入以下章节，并使用仓库内相对链接：

```markdown
# 杭银实习项目使用说明

## 成果概览
## 运行环境
## 项目一：短期记忆
### 功能定位表
### 最短启动与测试步骤
## 项目二：Agent 决策审计
### 功能定位表
### 最短启动与测试步骤
## 两个项目的关系
## 数据安全与外部依赖
## 后续成果
```

短期记忆定位表必须覆盖：HTTP 接口、四层压缩、Headroom generation/CCR、L3/L4 连续性摘要、历史 Session 激活与冷恢复、compaction checkpoint、Journal Grep/Read、Redis/Journal 存储和对应测试。

SemanticaAdapter 定位表必须覆盖：HTTP 服务与客户端、治理服务、Semantica 后端适配、版本化 Agent 画像、审批与政策例外、审计完整性校验、金额核对示例和对应测试。

Expected: 指导老师不阅读全部源码，也能从表格直接找到每项功能的实现和测试。

- [ ] **Step 3: 验证文档引用路径**

Run:

```bash
python -c "from pathlib import Path; import re; p=Path('使用说明.md'); missing=[x for x in re.findall(r'\[[^]]+\]\(([^)#]+)', p.read_text()) if not x.startswith(('http://','https://')) and not (p.parent/x).exists()]; assert not missing, missing"
```

Expected: 退出码为 0，无缺失路径。

- [ ] **Step 4: 提交使用说明**

Run:

```bash
git add .gitignore 使用说明.md
git commit -m "docs: add internship project guide"
```

Expected: 提交只包含仓库级忽略规则和使用说明。

### Task 4: 验证两个项目与仓库内容

**Files:**
- Verify: `short-term-memory/**`
- Verify: `SemanticaAdapter/**`
- Verify: `使用说明.md`

**Interfaces:**
- Consumes: 两个源码快照和使用说明。
- Produces: 测试、敏感信息和仓库质量证据。

- [ ] **Step 1: 运行 short-term-memory 测试**

Run:

```bash
/Users/fenghao/PycharmProjects/compression/short-term-memory/.venv/bin/python -m pytest -q
```

Expected: 测试全部通过；若存在依赖外部 Redis/Headroom 的测试，应按项目已有标记跳过而不是静默删除。

- [ ] **Step 2: 运行 SemanticaAdapter 测试**

Run:

```bash
/Users/fenghao/PycharmProjects/semantica/semantica-adapter/.venv/bin/python -m pytest -q
```

Expected: 测试全部通过。

- [ ] **Step 3: 检查敏感文件和明显凭据**

Run:

```bash
find . -path './.git' -prune -o -name '.env' -o -name '.env.*' ! -name '.env.example' -o -name '*.pem' -o -name '*.key' -print
rg -n --hidden --glob '!.git/**' --glob '!uv.lock' '(-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})' .
```

Expected: 两条检查均无有效凭据输出；文档中的环境变量名称和占位示例不视为凭据。

- [ ] **Step 4: 检查大文件和排除目录**

Run:

```bash
find . -path './.git' -prune -o -type f -size +10M -print
find . -path './.git' -prune -o -type d \( -name '.venv' -o -name '.idea' -o -name '__pycache__' -o -name '.pytest_cache' -o -name 'dist' \) -print
```

Expected: 无输出。

- [ ] **Step 5: 检查最终提交状态**

Run:

```bash
git status --short
git log --oneline --decorate --max-count=10
git ls-files
```

Expected: 工作区干净；提交历史包含设计、两个项目和使用说明；文件清单不含排除项。

### Task 5: 推送并确认 GitHub 交付状态

**Files:**
- Publish: Git 分支 `main`

**Interfaces:**
- Consumes: 已验证且工作区干净的本地 `main`。
- Produces: 指导老师可以访问和自取的 GitHub 成果仓库。

- [ ] **Step 1: 推送主分支**

Run:

```bash
git push -u origin main
```

Expected: `main` 成功推送到 `https://github.com/fenghao-nuaa/hzbank-internship`。

- [ ] **Step 2: 查询并设置公开可见性**

Run:

```bash
gh repo view fenghao-nuaa/hzbank-internship --json visibility,url
gh repo edit fenghao-nuaa/hzbank-internship --visibility public --accept-visibility-change-consequences
gh repo view fenghao-nuaa/hzbank-internship --json visibility,url
```

Expected: 最后一次查询返回 `"visibility":"PUBLIC"` 和正确仓库 URL；若第一次已经是 `PUBLIC`，中间修改命令可以省略。

- [ ] **Step 3: 核对远端主分支**

Run:

```bash
git ls-remote --heads origin main
git status --short --branch
```

Expected: 远端 `main` 哈希等于本地 `HEAD`，本地分支跟踪 `origin/main` 且工作区干净。
