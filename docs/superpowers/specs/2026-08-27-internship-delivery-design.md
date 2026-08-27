# 实习成果仓库整理设计

## 目标

将两个已完成项目整理到 `hzbank-internship`，让指导老师能够从根目录使用文档了解成果、按功能定位源码，并依据各项目 README 完成安装、启动和测试。

本次纳入：

- `short-term-memory`：短期记忆、递进式上下文压缩、历史 Session 切换和原文召回。
- `SemanticaAdapter`：Agent 决策审计、统一适配接口、HTTP 服务和审计产物。

`DREAM` 暂不纳入，待项目展示名称和交付内容调整完成后另行加入。

## 仓库结构

```text
hzbank-internship/
├── 使用说明.md
├── .gitignore
├── short-term-memory/
│   ├── README.md
│   └── 项目源码、测试和必要配置
└── SemanticaAdapter/
    ├── README.md
    └── 项目源码、测试和必要配置
```

## 源码来源与复制规则

### short-term-memory

源目录：`/Users/fenghao/PycharmProjects/compression/short-term-memory`

只复制该仓库当前 Git 已跟踪文件，以确保上传的是可复现的项目版本。源目录内未跟踪的实习报告、诊断脚本、个人工作流目录和临时方案文档不进入成果仓库。

### SemanticaAdapter

源目录：`/Users/fenghao/PycharmProjects/semantica/semantica-adapter`

复制 README、第三方声明、项目配置、锁文件、`src`、`tests`、`examples`、`docs` 和 `legacy`。`legacy/auditgraph` 作为迁移对照源码保留，但不进入 Python wheel。

排除 `.venv`、`.idea`、pytest/字节码缓存、构建目录、`.ses` 数据库、运行状态和所有示例审计输出。

## 使用说明设计

根目录 `使用说明.md` 面向指导老师，提供：

1. 两个成果的目标与功能边界；
2. 功能到源码目录、关键文件和测试文件的定位表；
3. 各项目的最短安装、启动和测试命令；
4. 外部依赖、环境变量和敏感信息说明；
5. 两个项目之间的关系，以及当前未纳入 DREAM 的说明；
6. 指向各子项目 README 的链接，避免重复完整技术细节。

## 安全与仓库质量

- 不复制任何嵌套 `.git`、虚拟环境、IDE 配置、缓存或生成产物。
- 不上传 `.env`、API Key、令牌、密码或本地服务凭据。
- 保留 `.env.example` 等不含密钥的配置模板。
- 推送前检查大文件、敏感字段、Git 状态和最终目录结构。
- 新仓库采用单一 Git 历史的一次源码快照，不使用 submodule 或 subtree。

## 验证

- 校验两个项目 README 和根目录功能索引中的路径真实存在。
- 对 `short-term-memory` 运行其现有测试集。
- 对 `SemanticaAdapter` 运行其现有测试集及构建检查。
- 列出最终提交文件和体积，确认没有虚拟环境、运行输出或本地密钥。
- 推送后确认远端分支和仓库可见性；若仓库仍为私有，则改为公开。
