# DREAM 成果交付设计

## 目标

将 `https://github.com/ZCDu/AGFS-MEM/tree/fenghao` 对应的远端提交作为梦境机制成果，加入 `hzbank-internship/DREAM/`，与 `short-term-memory/`、`SemanticaAdapter/` 并列。

## 版本边界

- 来源仓库：`/Users/fenghao/PycharmProjects/dream/DREAM`
- 来源引用：`teacher/fenghao`
- 锁定提交：`aa46eb17bfee39ff5bfb2b605fca0e89edb5023b`
- 导出方式：`git archive teacher/fenghao`
- 不包含：本地 `fenghao` 领先的 95 个提交、当前未提交修改、未跟踪实验脚本、源仓库 `.git`。

## 交付内容

`DREAM/` 保留远端提交中的源码、测试、README、配置模板和技术文档。成果仓库内只调整 README 的进入目录说明，不改变 Python 包、业务逻辑或测试。

根目录 `使用说明.md` 增加 DREAM 成果概览、后台蒸馏流程、功能到源码/测试定位表、最短运行步骤，以及与另外两个服务的关系。

## 功能边界

DREAM 交付版本重点展示：

- 已完成会话的后台 Background Review；
- 自适应批量触发和每日 Curator 兜底；
- 用户画像及 `USER_PERSONA.md` 投影；
- Decision Cards 与 `DECISION_RULES.md`；
- Skill Candidates；
- Semantic Curator 的隔离候选流程；
- 快照、版本、自动治理、审核和失败回滚。

Todo 已有 `USER_TODO`、`todo_manage` 和 `TODOS.md` 数据边界，但没有与画像、决策卡和 Skill 同等完整的 Manager 写入链路，使用说明必须如实标注为未完整闭环。

## 验证

- 确认归档内容与 `teacher/fenghao` Git tree 一致，交付文档调整除外；
- 检查敏感文件、凭据、大文件和生成目录；
- 运行 DREAM 默认测试和构建检查；
- 验证根目录文档全部相对链接；
- 合并到 `main`，推送并通过 GitHub 公共 API 确认 `DREAM/` 可见。
