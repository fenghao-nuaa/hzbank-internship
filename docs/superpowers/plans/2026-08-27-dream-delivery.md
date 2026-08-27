# DREAM 成果交付 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将远端 `teacher/fenghao` 的梦境机制源码快照加入成果仓库并形成可按功能定位的交付文档。

**Architecture:** 使用 `git archive` 从锁定远端提交导出纯源码快照，避免混入本地后续重构和未提交内容。DREAM 保持独立 Python 项目，根目录使用说明只负责跨项目导航和运行入口。

**Tech Stack:** Git、Markdown、Python 3.11+、uv、pytest、FastAPI

## Global Constraints

- 来源必须是 `teacher/fenghao` 提交 `aa46eb17bfee39ff5bfb2b605fca0e89edb5023b`。
- 目标目录必须是 `DREAM/`。
- 不包含本地 `fenghao` 后续 95 个提交、工作区修改、未跟踪文件或嵌套 `.git`。
- 不上传 `.env`、密钥、虚拟环境、缓存、构建产物和运行数据。
- 只允许调整成果快照 README 中的仓库进入路径，不修改 DREAM 业务逻辑。
- 根目录使用说明必须标注 Todo 尚未形成完整 Manager 闭环。

---

### Task 1: 导出 DREAM 远端源码快照

**Files:**
- Create: `DREAM/**`

- [ ] 使用 `git archive --format=tar --output=/private/tmp/hzbank-dream.tar teacher/fenghao` 生成来源归档。
- [ ] 创建 `DREAM/` 并解压归档。
- [ ] 对比 `git ls-tree -r --name-only teacher/fenghao` 与 `find DREAM -type f` 的规范化文件清单。
- [ ] 检查 `DREAM/` 不包含 `.git`、`.env`、`.venv`、缓存或运行产物。
- [ ] 提交 `feat: add DREAM project`。

### Task 2: 对齐成果仓库文档

**Files:**
- Modify: `DREAM/README.md`
- Modify: `使用说明.md`

- [ ] 将 DREAM README 的获取/进入目录说明调整为从成果仓库根目录执行 `cd DREAM`。
- [ ] 在成果概览和目录结构中加入 DREAM。
- [ ] 增加 Background Review、调度、Persona、Decision Card、Skill、Curator、版本回滚的源码与测试定位表。
- [ ] 增加最短安装、启动和测试步骤。
- [ ] 将原“后续成果”改为 Todo 完整度说明，不宣称不存在的闭环能力。
- [ ] 验证所有 Markdown 相对链接存在。
- [ ] 提交 `docs: document DREAM project`。

### Task 3: 验证并发布

**Files:**
- Verify: `DREAM/**`
- Verify: `使用说明.md`

- [ ] 使用隔离环境运行 DREAM 默认测试。
- [ ] 运行 DREAM sdist/wheel 构建。
- [ ] 检查敏感凭据、禁止文件、超过 10 MB 文件和 Git 状态。
- [ ] 合并 `delivery/dream` 到 `main` 后重新运行关键验证。
- [ ] 推送 `main` 并核对远端提交哈希。
- [ ] 通过 GitHub 公共 API 确认 `DREAM/` 和根目录使用说明可匿名读取。
