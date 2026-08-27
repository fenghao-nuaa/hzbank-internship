# Claude Code 上下文压缩机制 Python 移植设计

> 状态：实施完成，待最终确认
>
> 日期：2026-08-14
>
> 目标项目：`short-term-memory`

## 1. 目标

本方案只解决当前最优先的问题：Headroom 对原文完成一次压缩后，多个
`CompressionGeneration` 仍会持续累积；当压缩段自身再次超过阈值时，现有
`_execute_recompress()` 只能删除最旧 generation，无法像 Claude Code 一样把
“旧摘要 + 新上下文”再次压缩为新的活动上下文。

改造目标如下：

1. 按 Claude Code 源码移植 L2 Auto Compact、L4 Session Memory 和 L3
   Traditional Compact，保留其调用顺序、状态机、边界替换、失败处理和摘要
   prompt 的语义。
2. TypeScript 只转换为 Python，不重新发明另一套摘要算法。
3. 使用 Journal 替代 Claude Code 的 transcript；Agent 仍通过
   `摘要 → Grep → Read → 原文` 自主召回，用户不参与召回操作。
4. 完整保留 short-term-memory 已有的 Headroom generation、CCR marker、Redis
   session、Journal 原文、最近 N 轮和最旧 generation 淘汰策略。
5. 删除现有五类摘要在运行时的职责，避免五类摘要、L4 和 L3 成为三个互相
   覆盖的摘要事实源。

## 2. “与源码一致”的边界

### 2.1 必须保持一致

- L2 在模型请求前判断活动上下文是否需要压缩。
- L2 达阈值后先尝试 L4，L4 不可用或压缩后仍超阈值才调用 L3。
- L4 在上下文达到高水位前后台更新 Session Memory。
- L3 使用独立、无工具、单轮 compact 请求生成连续性摘要。
- compact 成功后，不追加摘要到旧上下文，而是用
  `CompactBoundary + summary + messagesToKeep + attachments + hooks`
  替换活动上下文。
- 下一次 compact 可以看到上一版 compact summary，从而支持同一链条内再次压缩。
- L4 和 L3 完成后重新计算真正的 post-compact token 数。
- Auto Compact 连续失败 3 次后打开断路器；成功后清零。
- L4 等待正在进行的 Session Memory extraction 最多 15 秒；超过 60 秒的更新
  视为陈旧，不继续等待。
- L4 保留尾部消息时不得拆开完整 user/assistant/tool 轮次。
- L3 compact 自身 prompt-too-long 时，按完整 API round 从头裁剪并有界重试。
- 摘要只告诉 Agent 完整 transcript 在哪里；需要精确细节时由 Agent 自主调用
  Grep/Read，而不是要求用户手动召回。

### 2.2 只允许的必要适配

| Claude Code | Python 项目适配 | 原因 |
|---|---|---|
| TypeScript 模块和类型 | Python 模块、Pydantic、dataclass、Protocol | 语言不同 |
| 单个 transcript JSONL | 一个 session 跨日期的 Journal JSONL | 已有事实存储不同 |
| 本地 transcript path | `journal://current-session` | Agent 与服务不在同一容器 |
| 本地 Grep/Read | HTTP-backed Grep/Read | Journal 只能由 memory service 访问 |
| message UUID | Journal `sequence` | 项目已有稳定覆盖游标 |
| session memory Markdown 文件 | Redis 中的 SessionMemoryRevision | 服务为分布式多进程部署 |
| REPL post-sampling hook | assistant turn 写入后的后台队列 | 项目没有 Claude REPL 生命周期 |
| forked compact agent | 可注入的同 provider 独立 compact 请求 | Agent provider 通过接口注入 |

除此以外，不通过“更适合 Python”为理由改变算法。

## 3. 最终架构

```text
                           ┌──────────────────────────┐
用户消息 ──写入───────────▶│ Redis recent originals   │
   │                       └──────────────────────────┘
   └──────────────────────▶┌──────────────────────────┐
                           │ Journal immutable events │◀──── Grep / Read
                           └──────────────────────────┘

旧原文 ──后台──────────────▶ Headroom /v1/compress
                              │
                              ├─ CompressionGeneration
                              └─ CCR marker / retrieve

活动上下文增长
   │
   ├─ L4 后台更新 Session Memory
   │
   └─ L2 请求前判断
        ├─ L4 可用且压缩后低于阈值 ──▶ ContextRevision(session_memory)
        └─ L4 不可用/不足 ───────────▶ L3 独立 compact 请求
                                        └─ ContextRevision(traditional)

发给 Agent 的上下文
  = CompactBoundary
  + L4/L3 summary
  + 未被 boundary 覆盖的 Headroom generations
  + 保留的最近完整轮次
  + Grep/Read 工具
```

### 3.1 三个数据平面

1. **原文平面：Journal**
   - 保存不可变原始事件。
   - 等价于 Claude transcript。
   - 是 Grep/Read 的数据源，也是重建的最终事实源。

2. **细节压缩与召回平面：Headroom generations**
   - 仍然只接收 Redis/Journal 原文，不接收旧 generation 或 L3/L4 summary。
   - 保留 CCR marker 和现有召回逻辑。
   - generation 被 L3/L4 覆盖只代表退出活动 prompt，不代表立即删除。

3. **活动上下文平面：L4/L3 Context Revision**
   - 控制当前真正发送给 Agent 的上下文。
   - 可以反复替换，而不是不断追加摘要段。
   - 精确细节缺失时通过 Journal Grep/Read 找回。

## 4. Claude 源码与 Python 模块一一对应

| Claude 源码 | Python 目标模块 | 保留的核心职责 |
|---|---|---|
| `src/query.ts` | `service/context_coordinator.py`、`compression/context_query.py` | 请求前 L1→L2、替换活动消息、Redis lease/CAS |
| `services/compact/microCompact.ts` | `compression/micro_compact.py` | 时间触发、工具白名单、keep floor、copy-on-write、token 估算 |
| `services/compact/autoCompact.ts` | `compression/auto_compact.py` | 阈值、L4→L3 调度、跟踪状态、断路器 |
| `services/compact/compact.ts` | `compression/traditional_compact.py` | L3、CompactResult、边界、PTL retry |
| `services/compact/prompt.ts` | `compression/compact_prompt.py` | L3 prompt、summary 格式化、继续会话提示 |
| `services/SessionMemory/sessionMemory.ts` | `compression/session_memory.py` | L4 后台抽取与更新 |
| `services/SessionMemory/sessionMemoryUtils.ts` | `compression/session_memory_state.py` | 更新阈值、覆盖游标、进行中状态 |
| `services/SessionMemory/prompts.ts` | `compression/session_memory_prompt.py` | L4 十章节模板、更新 prompt、长度限制 |
| `services/compact/sessionMemoryCompact.ts` | `compression/session_memory_compact.py` | 用现成 L4 memory 生成 CompactResult |
| `tools/GrepTool/GrepTool.ts` | `transcript/grep_tool.py` | 正则检索、上下文、分页、输出模式 |
| `tools/FileReadTool/FileReadTool.ts` | `transcript/read_tool.py` | offset/limit 分段读取、编号和大小限制 |
| transcript filesystem | `transcript/journal_transcript.py` | 将跨日 Journal 渲染成单一逻辑 transcript |
| Claude tool-use query loop | `agent/agent_chat.py` | Agent 自主 Grep→Read，工具结果回到同一模型循环 |

必要适配只有：`repl_main_thread` 映射为 HTTP 请求的 `main...` query source；
message UUID 映射为 Journal sequence；Session Memory 文件映射为 Redis revision；
本地 transcript 工具映射为带 session scope 的 HTTP Grep/Read。Anthropic 专属
cached microcompact cache-edit block 不移植，因为它不修改本地消息，外部 provider
也没有对应协议。

## 5. 数据模型

### 5.1 CompactBoundary

对应 Claude `createCompactBoundaryMessage()` 和 compact metadata：

```python
class CompactBoundary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    boundary_id: str
    trigger: Literal["auto", "manual", "reactive"]
    strategy: Literal["session_memory", "traditional"]
    covered_through_sequence: int = Field(ge=0)
    pre_compact_tokens: int = Field(ge=0)
    true_post_compact_tokens: int = Field(ge=0)
    created_at: str
```

`covered_through_sequence` 是 Claude `lastSummarizedMessageId` 和 compact boundary
在当前项目中的稳定等价物。

### 5.2 SessionMemoryRevision

```python
class SessionMemoryRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(ge=1)
    content: str = Field(min_length=1)
    covered_through_sequence: int = Field(ge=1)
    token_count: int = Field(ge=0)
    extraction_started_at: str | None = None
    updated_at: str
```

`content` 严格使用 Claude L4 Session Memory 十章节模板：

1. Session Title
2. Current State
3. Task specification
4. Files and Functions
5. Workflow
6. Errors & Corrections
7. Codebase and System Documentation
8. Learnings
9. Key results
10. Worklog

每个章节最大约 2,000 token，整个 Session Memory 最大约 12,000 token；超限时
更新 prompt 必须要求模型压缩旧内容，并优先保持 Current State 与
Errors & Corrections 准确。

### 5.3 ContextRevision

```python
class ContextRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(ge=1)
    boundary: CompactBoundary
    summary_message: SessionCompressionMessage
    messages_to_keep: tuple[SessionCompressionMessage, ...]
    covered_generation_ids: tuple[int, ...]
    updated_at: str
```

`summary_message` 内容使用 Claude `getCompactUserSummaryMessage()` 的等价 Python
实现。它同时包含摘要、逻辑 transcript URI、最近消息保留提示和继续工作提示。

### 5.4 AutoCompactTrackingState

```python
class AutoCompactTrackingState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    compacted: bool = False
    turn_counter: int = Field(default=0, ge=0)
    turn_id: str = ""
    consecutive_failures: int = Field(default=0, ge=0)
```

成功 compact 后：

```python
tracking = AutoCompactTrackingState(
    compacted=True,
    turn_counter=0,
    turn_id=uuid4().hex,
    consecutive_failures=0,
)
```

失败时只增加 `consecutive_failures`；达到 3 后本 session 不再自动调用 L3，避免
每轮重复消耗模型请求。

### 5.5 MemorySummaryEnvelope v2

```python
class MemorySummaryEnvelope(BaseModel):
    schema_version: Literal[2] = 2
    version: int
    compressed_through_sequence: int
    compression_generations: tuple[CompressionGeneration, ...]
    session_memory: SessionMemoryRevision | None = None
    active_revision: ContextRevision | None = None
    auto_compact_tracking: AutoCompactTrackingState
    updated_at: str
```

删除运行时使用：

- `current_goal`
- `preferences`
- `confirmed_facts`
- `pending_items`
- `attachment_references`
- `SessionSummaryPayload`
- `SessionSummaryGenerator`
- `EmptySummaryModel`

为了避免旧 Redis envelope 因 `extra="forbid"` 无法读取，反序列化边界提供一次
v1→v2 懒迁移：读取旧字段但不再将其注入 prompt；写回时只写 v2。经过一个 Redis
TTL 周期后删除 v1 parser。若部署明确清空 Redis，则可以跳过兼容窗口。

## 6. L2 Auto Compact 的 Python 移植

### 6.1 阈值公式

直接翻译 Claude `autoCompact.ts`：

```python
MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000
AUTOCOMPACT_BUFFER_TOKENS = 13_000
MANUAL_COMPACT_BUFFER_TOKENS = 3_000
MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3


def effective_context_window(profile: ModelProfile) -> int:
    reserved = min(profile.max_output_tokens, MAX_OUTPUT_TOKENS_FOR_SUMMARY)
    return profile.context_window_tokens - reserved


def auto_compact_threshold(profile: ModelProfile) -> int:
    return effective_context_window(profile) - AUTOCOMPACT_BUFFER_TOKENS
```

现有 `trigger_ratio=0.65` 不删除，但职责调整为提前生成 Headroom generation 和
触发 L4 extraction 的低水位策略。真正替换活动上下文的 L2 使用 Claude 的模型
profile 阈值公式，不能混为同一个阈值。

### 6.2 调度顺序

```python
async def auto_compact_if_needed(
    messages: tuple[Message, ...],
    context: CompactContext,
    tracking: AutoCompactTrackingState,
) -> AutoCompactResult:
    if context.query_source in {"session_memory", "compact"}:
        return AutoCompactResult.not_compacted(tracking)

    if tracking.consecutive_failures >= 3:
        return AutoCompactResult.not_compacted(tracking)

    if token_count(messages) < auto_compact_threshold(context.model_profile):
        return AutoCompactResult.not_compacted(tracking)

    sm_result = await try_session_memory_compaction(messages, context)
    if sm_result is not None:
        return AutoCompactResult.compacted(sm_result, tracking.reset_success())

    try:
        result = await compact_conversation(
            messages,
            context,
            is_auto_compact=True,
            recompaction_info=RecompactionInfo.from_tracking(tracking),
        )
        return AutoCompactResult.compacted(result, tracking.reset_success())
    except Exception:
        return AutoCompactResult.not_compacted(tracking.record_failure())
```

该顺序不得改为 L3→L4，也不得在 L4 结果仍超过阈值时误报成功。

## 7. L4 Session Memory 的 Python 移植

### 7.1 更新阈值

保持 Claude 默认值：

```python
DEFAULT_SESSION_MEMORY_CONFIG = SessionMemoryConfig(
    minimum_message_tokens_to_init=10_000,
    minimum_tokens_between_update=5_000,
    tool_calls_between_updates=3,
)
```

`should_extract_memory()` 保持源码的组合条件：

```python
should_extract = (
    token_growth_reached and tool_call_threshold_reached
) or (
    token_growth_reached and not last_assistant_turn_has_tool_calls
)
```

token 增长阈值始终是必要条件；不能因为工具调用达到 3 次就高频更新。

### 7.2 后台更新

Claude 使用 post-sampling hook 和 forked agent 编辑 Session Memory 文件。Python
移植使用 assistant event 写入后的持久队列，但保持隔离和串行语义：

```python
class SessionMemoryModel(Protocol):
    async def update(
        self,
        *,
        current_memory: str,
        messages: tuple[dict[str, Any], ...],
        prompt: str,
        model: str,
    ) -> str: ...
```

- provider 与主 Agent 相同，但请求独立。
- `query_source="session_memory"`，防止递归触发 Auto Compact。
- 同一个 session 的更新严格串行。
- 与 Claude 的 fork context 一致，模型输入包括当前活动消息上下文和当前 Session
  Memory；`covered_through_sequence` 记录本次成功更新覆盖的最后一个安全事件，
  不能用它把模型输入简化成缺少前因的孤立增量。
- 输出必须保持十章节模板标题和说明行不变。
- 成功后 CAS 写入 Redis，并推进 `covered_through_sequence`。
- 失败不推进 coverage，不影响 Headroom generation 和 Journal。

### 7.3 L4 作为 compact 快速路径

保持 `sessionMemoryCompact.ts` 的核心行为：

1. 等待进行中的 extraction，最多 15 秒；超过 60 秒的 extraction 视为陈旧。
2. Session Memory 不存在或仍是空模板时返回 `None`，让 L2 回退 L3。
3. coverage 在当前活动消息中无法定位时返回 `None`。
4. 从 coverage 后开始选择尾部消息，再向前扩展。
5. 默认至少保留 10,000 token、至少 5 条文本消息，最多保留 40,000 token。
6. 同时保留项目现有最近 N 个完整 user turns；最终保留范围取两种规则的并集，
   但仍受 40,000 token 硬上限约束。
7. 不拆开 user/assistant/tool 配对，也不拆开同一个工具调用链。
8. 生成 `CompactBoundary + Session Memory summary + messagesToKeep`。
9. 对完整 post-compact messages 重新计数；仍达到 L2 阈值时返回 `None`。

## 8. L3 Traditional Compact 的 Python 移植

### 8.1 模型接口

```python
class TraditionalCompactionModel(Protocol):
    async def compact(
        self,
        *,
        messages: tuple[dict[str, Any], ...],
        prompt: str,
        model: str,
        max_output_tokens: int,
    ) -> CompactionModelResponse: ...
```

- 使用与主 Agent 相同的 provider 和默认模型 profile。
- 请求与主 Agent 分离，`query_source="compact"`。
- compact 请求禁止工具调用，只有一次文本输出机会。
- 常规路径不会每轮调用；只有 L2 超阈值且 L4 不可用/不足时调用。

### 8.2 L3 prompt

`compact_prompt.py` 从 Claude `prompt.ts` 逐段翻译以下常量：

- `NO_TOOLS_PREAMBLE`
- `DETAILED_ANALYSIS_INSTRUCTION_BASE`
- `BASE_COMPACT_PROMPT`
- `PARTIAL_COMPACT_PROMPT`
- `PARTIAL_COMPACT_UP_TO_PROMPT`
- `NO_TOOLS_TRAILER`

L3 的基础摘要固定为源码中的 9 个章节：

1. Primary Request and Intent
2. Key Technical Concepts
3. Files and Code Sections
4. Errors and fixes
5. Problem Solving
6. All user messages
7. Pending Tasks
8. Current Work
9. Optional Next Step

`up_to` partial compact 使用源码中的 `Work Completed` 和
`Context for Continuing Work` 变体，不与 L4 十章节模板混用。

模型返回：

```xml
<analysis>...</analysis>
<summary>...</summary>
```

`format_compact_summary()` 严格对应源码：删除 `<analysis>` 草稿，只保留并格式化
`<summary>`，然后合并多余空行。

### 8.3 L3 输入必须是活动上下文

为保持 Claude 算法，L3 输入是最近 `CompactBoundary` 后的活动消息：

```text
上一版 L3/L4 summary
+ 尚未覆盖的 Headroom generations
+ 最近完整原文
```

不能每次从 Journal 重新把全部历史原文塞给 L3，否则这不再是 Claude 的递进 compact，
也可能使 compact 请求本身再次超限。Journal 只承担 transcript 召回和灾难恢复。

因此第二次 L3 可以自然实现：

```text
summary AB + generation C + recent D
                     ↓
summary ABCD
```

### 8.4 CompactResult 与替换

```python
@dataclass(frozen=True)
class CompactionResult:
    boundary_marker: CompactBoundary
    summary_messages: tuple[dict[str, Any], ...]
    messages_to_keep: tuple[dict[str, Any], ...]
    attachments: tuple[dict[str, Any], ...]
    hook_results: tuple[dict[str, Any], ...]
    pre_compact_token_count: int
    true_post_compact_token_count: int


def build_post_compact_messages(result: CompactionResult) -> tuple[dict, ...]:
    return (
        boundary_message(result.boundary_marker),
        *result.summary_messages,
        *result.messages_to_keep,
        *result.attachments,
        *result.hook_results,
    )
```

成功后必须执行语义等价的替换：

```python
messages_for_query = build_post_compact_messages(compaction_result)
```

禁止写成：

```python
messages_for_query += compaction_result.summary_messages
```

后者会继续累积旧上下文，正是当前压缩不足问题的根源。

### 8.5 L3 prompt-too-long 重试

保持 Claude 思路：

- 只在 compact 请求自身发生 prompt-too-long 时启动。
- 以完整 API round/完整 turn 为裁剪单位。
- 从最旧头部裁剪，不拆 tool_use/tool_result。
- 裁剪规模使用超出 token 的缺口或约 20% 安全量。
- 使用源码当前配置的有界尝试次数，不允许无限递归。
- 仍失败时交给 L2 记录失败并增加断路器计数。

## 9. 活动上下文组装与 Headroom 融合

### 9.1 Headroom 原有规则不变

`CompressionWorker` 正常任务继续：

```text
未压缩 Journal/Redis originals
  → Headroom /v1/compress
  → 新 CompressionGeneration
  → CCR marker catalog
```

以下不变量继续成立：

- Headroom 输入只来自原文。
- 不把旧 generation、L4 memory 或 L3 summary 再交给 `/v1/compress`。
- Headroom Proxy 只压缩并转发 Agent 当前请求，不拥有 Redis 活动上下文。
- Journal 不因 compact 被修改。

### 9.2 Headroom generation 融入 Claude 递进摘要的完整时序

Headroom generation 是 L3/L4 活动摘要的上游细节压缩资产，但新消息不会一写入就
立即变成 generation。必须保持“最近完整轮次优先”的顺序：

```text
新 user/assistant/tool 消息
  ↓
先逐字写入 Redis recent originals 和 Journal
  ↓
位于最近 N 轮/retain token 保护区
  ├─ 是：保持原文，不提交 Headroom
  └─ 否：成为 Headroom compression candidate
           ↓
        /v1/compress(originals only)
           ↓
        CompressionGeneration + CCR marker
           ↓
活动上下文达到 Claude L2 阈值
  ↓
优先 L4；L4 不可用或结果仍过长时调用 L3
  ↓
旧 L3/L4 summary + 尚未覆盖 generations + 待压缩的较旧原文
  ↓
新 L3/L4 summary + 最近完整尾部原文
```

常规路径应尽量让离开保护区的原文先变成 Headroom generation，再由 L3/L4 把
generation 折叠进连续性摘要：

```text
原文 → Headroom generation → L4/L3 activity summary
```

这形成两个职责不同的压缩级别：

| 级别 | 输入 | 输出 | 职责 |
|---|---|---|---|
| Headroom | 离开最近保护区的原文 | generation + CCR marker | 压缩细节并保持精确召回入口 |
| L4/L3 | 旧摘要 + 未覆盖 generation + 必要的较旧原文 | 新活动摘要 | 折叠整个活动上下文并允许后续再次压缩 |

#### 9.2.1 第一次递进压缩

假设 A、B 已经离开最近保护区，尾部消息仍需逐字保留：

```text
原文 A + 原文 B
       ↓ Headroom 后台压缩
generation A + generation B
       ↓ L2 达阈值，L4 优先、L3 兜底
summary AB
```

compact 前活动上下文：

```text
generation A
+ generation B
+ 最近尾部原文
```

compact 后活动上下文：

```text
CompactBoundary(covered_through_sequence=B.through_sequence)
+ summary AB
+ 最近尾部原文
```

generation A、B 退出活动 prompt，但仍按现有策略保存在 Redis；CCR marker 和 Journal
原文继续可召回。

#### 9.2.2 第二次递进压缩

继续聊天产生 C、D 时，C、D 首先保持为最近原文：

```text
summary AB + 原文 C + 原文 D + 最近尾部原文
```

当 C、D 离开最近保护区后，Headroom 生成新的未覆盖 generation：

```text
原文 C + 原文 D
       ↓ Headroom
generation CD
```

下一次 L2 达阈值时，L3/L4 看到的是最近 boundary 之后的有效活动上下文：

```text
summary AB + generation CD + 最近尾部原文
                    ↓
summary ABCD + 最近尾部原文
```

新 boundary 推进到 `generation CD.through_sequence`。旧 summary AB 参与生成新摘要，
但不会与 summary ABCD 一起继续留在活动 prompt。

#### 9.2.3 后续继续压缩

E 同样先保持为最近原文，离开保护区后成为 generation E：

```text
summary ABCD + generation E + 最近尾部原文
                     ↓
summary ABCDE + 最近尾部原文
```

该过程可继续重复：

```text
summary(1..N) + new generations + old eligible originals
                          ↓
summary(1..N+M) + recent verbatim tail
```

这就是本项目对 Claude 同链 recompaction 的等价实现。

#### 9.2.4 Headroom 尚未完成时的紧急路径

L3 不得为了等待 Headroom worker 而让 Agent 请求超过硬上下文上限。如果 L2 已达到
阈值，而部分已经离开最近保护区的原文尚未来得及生成 generation，L3 可以直接压缩
当前活动上下文中的混合输入：

```text
上一版 summary
+ 已完成的 Headroom generations
+ 尚未完成 Headroom 压缩、但已离开保护区的较旧原文
+ 最近尾部原文（只保留，不纳入被替换前缀）
```

生成：

```text
新 summary + 最近尾部原文
```

约束如下：

- 这是 Claude L3 对“当前有效上下文”执行 compact 的自然结果，不是另一套摘要算法。
- 最近 N 轮/保尾算法选中的消息仍逐字保留，不能为了压缩率全部摘要掉。
- 未完成的 Headroom job 可以继续执行；若返回时其 sequence 已被更新 boundary 覆盖，
  generation 仍可作为 CCR/存储资产写入，但不得重新进入活动 prompt。
- L3 摘要及旧 generation 永远不能作为 Headroom `/v1/compress` 的输入。
- CAS 检查必须防止迟到的 Headroom 或 L3 结果回退最新 boundary/coverage。

#### 9.2.5 L4 与 generation 的关系

L4 Session Memory 同样消费当前活动上下文中的信息，包括旧活动摘要、尚未覆盖的
Headroom generations 和必要的原文尾部。L4 在后台提前更新十章节工作记忆；L2 真正
触发时，`try_session_memory_compaction()` 使用已完成的 Session Memory 快速创建新
boundary，并保留 coverage 之后的安全尾部。

因此 L4 并不是“把每个 generation 单独再摘要一次”，而是持续维护整个 session 的
连续工作状态；generation 是它可观察的输入之一。L4 结果必须经过完整 post-compact
token 复核，不足时仍回退 L3。

### 9.3 generation 可见性

不必修改 `CompressionGeneration` 的原始消息。活动组装时依据 boundary 计算可见性：

```python
def generation_is_visible(
    generation: CompressionGeneration,
    boundary: CompactBoundary | None,
) -> bool:
    return (
        boundary is None
        or generation.through_sequence > boundary.covered_through_sequence
    )
```

完全被 boundary 覆盖的 generation：

- 不再进入当前 Agent prompt；
- 仍可暂存在 Redis；
- CCR marker 和现有召回能力继续有效；
- 达到存储预算后按现有策略淘汰最旧 generation；
- Journal 原文仍可通过 Grep/Read 读取。

`_execute_recompress()` 重命名为明确的 `_execute_evict_oldest_generation()`。
generation 淘汰是存储降级，不再冒充 Claude compact。

### 9.4 组装顺序

```text
CompactBoundary（内部状态，可投影为 system metadata）
L4/L3 summary message
未被覆盖且 CCR 未过期的 Headroom generation messages
Claude 保尾算法与现有最近 N 轮共同保留的原文
恢复工具和必要 system instructions
```

组装后用与 L2 相同的 token estimator 重新计算；不能用字符长度冒充 token。

## 10. Journal 等价 Transcript

### 10.1 逻辑 URI

compact summary 中使用：

```text
journal://current-session
```

该 URI 不暴露真实磁盘路径。Agent 工具处理器自动绑定当前认证的
`user_id/session_id`，模型不能通过修改 URI 访问其他 session。

### 10.2 虚拟 JSONL

`JournalTranscript` 将一个 session 跨日期的 Journal 文件按 sequence 排序，并渲染为
单一逻辑 JSONL。每个 sequence 对应一行，正文中的换行使用 JSON 转义：

```text
87→{"sequence":87,"role":"user","content":"最终 TTL 设置为 43200 秒"}
88→{"sequence":88,"role":"assistant","content":"已确认 Redis TTL 为 43200 秒"}
```

这样 Grep 的匹配位置和 Read 的 offset 都稳定映射到 sequence，不受跨日物理文件影响。

## 11. Python Grep 工具

### 11.1 请求模型

```python
class TranscriptGrepRequest(BaseModel):
    path: Literal["journal://current-session"]
    pattern: str = Field(min_length=1)
    output_mode: Literal["content", "files_with_matches", "count"] = (
        "files_with_matches"
    )
    context_before: int = Field(default=0, ge=0)
    context_after: int = Field(default=0, ge=0)
    context: int | None = Field(default=None, ge=0)
    head_limit: int = Field(default=250, ge=0)
    offset: int = Field(default=0, ge=0)
    case_insensitive: bool = True
    multiline: bool = False
```

保持 Claude Grep 行为：

- regex pattern；
- `context` 等价 `-C`，优先于 `context_before/context_after`；
- `content`、`files_with_matches`、`count` 三种输出；
- 默认 `head_limit=250`；
- `head_limit=0` 表示不截条数，但服务端仍应用最大响应 token 安全限制；
- `offset` 用于分页；
- 返回 `was_truncated` 和 `applied_offset`，让 Agent 知道可以继续翻页。

物理执行不启动 shell `rg`，而是在受限的当前 session 行集合上用 Python `re`
完成，避免路径注入。算法语义保持为正则行检索。

## 12. Python Read 工具

```python
class TranscriptReadRequest(BaseModel):
    file_path: Literal["journal://current-session"]
    offset: int = Field(default=1, ge=1)
    limit: int | None = Field(default=None, ge=1, le=2_000)
```

保持 Claude Read 行为：

- 从 1 开始编号；在本项目中编号就是 Journal sequence。
- 支持 `offset/limit` 精确读取。
- 默认最多 2,000 行，但还要受响应 token/字符预算约束。
- 返回类似 `cat -n` 的带编号文本。
- 超过预算时返回明确错误，要求先 Grep 或缩小 offset/limit。
- 只能读取 `journal://current-session`，不能读取任意服务端文件。

## 13. Agent 自动 Grep→Read 续跑

用户不调用 Grep/Read。工具注册给 Agent，调用由模型自主决定。

compact summary 使用 Claude `getCompactUserSummaryMessage()` 的等价提示：

```text
本会话从此前因上下文不足而压缩的对话继续。以下摘要覆盖了较早部分。

<L3 或 L4 摘要>

如果需要压缩前的具体细节，例如准确代码、错误信息、工具结果或此前生成的内容，
请使用 Grep 和 Read 读取完整 transcript：journal://current-session

最近消息仍按原文保留。请从之前停止的位置直接继续，不要复述摘要。
```

`AgentChatClient` 的现有 tool-call loop 扩展为：

```python
if name == "headroom_retrieve":
    tool_content = await self._recall_ccr(...)
elif name == "Grep":
    tool_content = await self._grep_transcript(...)
elif name == "Read":
    tool_content = await self._read_transcript(...)
else:
    tool_content = f"unknown tool {name}"
```

每次工具执行后保持 Claude query loop 的语义：

```text
assistant tool_use
+ tool_result
→ 再次调用同一个 Agent
→ 直到 Agent 返回最终文本或达到 max_tool_rounds
```

典型运行：

```text
用户：“之前确定的 TTL 是多少？”
  ↓
Agent 从摘要知道讨论过 TTL，但缺少数值
  ↓ 自动 tool_use
Grep(pattern="Redis.*TTL|TTL.*Redis", path="journal://current-session")
  ↓
返回 sequence 87 附近命中
  ↓ 自动 tool_use
Read(offset=84, limit=8, file_path="journal://current-session")
  ↓
返回 Journal 原文
  ↓
Agent 同一轮回答用户
```

## 14. HTTP 接口

由于 Agent 与 Journal 不在同一容器，新增两个认证接口：

```text
POST /v1/memories/transcript/grep
POST /v1/memories/transcript/read
```

工具输入中的 `path/file_path` 只允许逻辑 URI。HTTP body 可以包含 SDK 当前绑定的
`user_id/session_id`，但服务端必须用认证 scope 再校验，不能只相信模型参数。

错误响应应区分：

- invalid pattern；
- transcript not found；
- offset out of range；
- result too large；
- unauthorized session；
- Journal temporarily unavailable。

错误正文不泄露物理路径、其他用户 ID 或 Journal 内容。

## 15. 请求前 Context Coordinator

新增 `ContextCoordinator`，职责等价于 Claude `query.ts` 中模型调用前的部分：

```python
class ContextCoordinator:
    async def prepare(
        self,
        *,
        user_id: str,
        session_id: str,
        model_profile: ModelProfile,
        query_source: str = "main",
    ) -> PreparedContext:
        messages = await self.load_active_messages(...)
        result = await self.auto_compact_if_needed(...)
        if result.was_compacted:
            messages = build_post_compact_messages(result.compaction_result)
            await self.persist_revision(...)
        return PreparedContext(messages=messages, tools=TRANSCRIPT_TOOLS)
```

为保持现有 `MemoryService` “存储在线路径不直接依赖模型 provider”的边界：

- `/v1/memories/read` 保持兼容读取接口；
- 新增 `/v1/memories/prepare` 作为 Agent 发起 LLM 请求前的上下文准备接口；
- `ContextCoordinator` 注入 `SessionMemoryModel` 和 `TraditionalCompactionModel`；
- `AgentChatClient.turn()` 改为调用 prepare，而不是把 raw read 直接发给 Agent；
- Headroom Proxy 只接收 prepare 返回的最终消息并转发。

L3 是 Claude 式同步兜底，prepare 超时必须单独配置，不能复用当前普通 read 的 10 秒
超时。L4 命中时不产生新的同步模型调用。

## 16. 并发与一致性

- Headroom generation、L4 extraction 和 L3 compact 都使用 envelope version CAS。
- L3/L4 开始时记录 `expected_version`；模型返回后若版本变化，结果标记 stale，不覆盖
  新写入消息。
- stale 结果允许重新读取后再调度，但不能无限自动重试。
- 每个 session 同时最多一个 L4 extraction 或 L3 compact。
- L4 extraction 不阻塞 Journal/Redis 写入。
- L3 compact 期间的新消息先正常写入 Journal；CAS 失败后保留原活动上下文，由下一次
  prepare 重试，不能丢消息。
- boundary coverage 必须落在完整 turn 和 generation 边界上。

## 17. 失败与降级

```text
L4 不存在/空模板/coverage 无效/更新超时/结果仍超阈值
  → L3

L3 prompt-too-long
  → 按完整轮次裁剪头部后有界重试

L3 普通失败
  → consecutive_failures + 1
  → 本轮使用未 compact 的安全上下文（若仍低于硬限制）

连续失败 3 次
  → 自动 compact 断路器打开
  → 允许显式 manual compact 或配置重置

上下文达到硬阻塞限制且 L3 失败
  → 不向上游发送必然失败的请求
  → 返回可诊断的 context_compaction_unavailable

Headroom generation 存储超预算
  → 淘汰最旧 generation
  → Journal 原文不受影响
```

## 18. 测试策略

### 18.1 源码等价单元测试

按 Claude 函数划分测试，不只按项目文件划分：

- `get_effective_context_window_size()` 保留 summary output reservation。
- `get_auto_compact_threshold()` 等于 effective window - 13,000。
- query source 为 `session_memory/compact` 时不递归 compact。
- consecutive failures 为 3 时不再调用 L4/L3。
- L2 始终先调用 L4，L4 返回 None 才调用 L3。
- `build_post_compact_messages()` 顺序固定。
- 第二次 compact 输入包含上一版 summary，但不包含 boundary 前旧消息。
- L4 empty template、coverage 缺失、post tokens 超阈值时返回 None。
- L4 tail 至少满足 token/text message 条件且不拆 tool pair。
- `format_compact_summary()` 删除 analysis、保留 summary。
- L3 PTL retry 只按完整轮次裁剪且次数有界。

### 18.2 Headroom 融合测试

- 正常 generation 输入仍然只包含 Journal/Redis 原文。
- 新消息位于最近 N 轮/retain token 保护区时保持原文，不立即提交 Headroom。
- 消息离开保护区后才成为 Headroom candidate，并生成未覆盖 generation。
- 常规第二次 compact 的输入明确为上一版 summary + 新 generation + 最近原文尾部。
- L2 已达阈值但 Headroom 尚未完成时，L3 能压缩旧 summary、已完成 generation 和
  已离开保护区的较旧原文，同时逐字保留最近尾部。
- 迟到的 Headroom generation 若已被新 boundary 覆盖，可以保存为 CCR 资产，但不会
  再次进入活动 prompt，也不会回退 coverage。
- L3/L4 summary 永不发送到 Headroom `/v1/compress`。
- boundary 覆盖后的 generation 不再进入活动 prompt。
- generation 退出活动 prompt 后仍保留 CCR marker 和 Redis 元数据。
- 最旧 generation 淘汰不删除 Journal 原文。
- 最近 N 轮仍以原文进入 post-compact messages。

### 18.3 Grep/Read 测试

- 跨两个日期文件的同一 session 被渲染为一个有序 transcript。
- Grep 支持 regex、大小写、上下文、offset/head_limit 和截断标记。
- Read 使用 sequence offset，最多 2,000 条并受 token budget 限制。
- 多行正文保持 JSONL 单行和稳定 sequence。
- `journal://other-session`、绝对路径和 `../` 被拒绝。
- 当前 user 不能访问其他 user/session。

### 18.4 Agent 自动召回测试

使用 RecordingModel 模拟三次响应：

1. 第一次返回 Grep tool_use；
2. 看到 Grep 结果后返回 Read tool_use；
3. 看到 Journal 原文后返回最终答案。

断言用户只提交一次普通问题，AgentChatClient 自动完成两次工具调用和模型续跑。

### 18.5 关键端到端验收

构造至少三批历史：

```text
原文 A → Headroom generation A
原文 B → Headroom generation B
原文 C → Headroom generation C
```

使 A+B+C+recent 超过 L2 阈值，验收：

1. A、B、C 在最近保护区内时保持原文；离开后才依次生成 Headroom generations。
2. L4 命中时生成 boundary 并替换活动上下文，而不是删除 A。
3. L4 不可用时 L3 生成 summary ABC。
4. 新增 D 时先以最近原文存在；D 离开保护区并生成 generation D 后再次超限，L3
   输入为 summary ABC + generation D + recent，输出 summary ABCD。
5. 模拟 generation D 尚未生成但上下文已经超限，L3 仍能处理 summary ABC + D 原文，
   并保留最近尾部；迟到的 generation D 不会重新进入活动 prompt。
6. 活动 prompt token 确实降到阈值以下。
7. 用户询问 A 中的精确内容时，Agent 自动 Grep→Read Journal 并正确回答。
8. Journal A 原文逐字不变。
9. Headroom generation/CCR 原有测试继续通过。

## 19. 分阶段实施

### 阶段 1：数据模型和 v2 兼容

- 引入 boundary、session memory、context revision、tracking。
- 增加 v1→v2 懒迁移。
- 五类摘要停止生成和注入。

### 阶段 2：Journal Transcript + Grep/Read

- 实现跨日虚拟 transcript。
- 增加 Python Grep/Read 和 HTTP 接口。
- 扩展 AgentChatClient tool loop，完成自动 Grep→Read 续跑。

先完成该阶段可独立证明 transcript 召回成立，再让 L3/L4 摘要依赖它。

### 阶段 3：L4 Session Memory

- 移植十章节模板、更新阈值、串行后台 extraction。
- 移植 `try_session_memory_compaction()` 和尾部保留算法。
- post-compact token 复核。

### 阶段 4：L3 Traditional Compact

- 移植 prompt、format、full/up_to compact、PTL retry。
- 接入与 Agent 相同 provider 的独立 compact 请求。
- 保存 ContextRevision。

### 阶段 5：L2 与 prepare/query loop

- 实现 Claude 阈值和 L4→L3 调度。
- 增加连续失败断路器和 recompaction tracking。
- AgentChatClient 使用 prepare 返回的活动上下文。

### 阶段 6：Headroom generation 生命周期收口

- assembler 按 boundary 隐藏已覆盖 generation。
- `recompress` 更名为明确的 eviction。
- 验证 CCR、Journal 和旧 generation 淘汰不受影响。

### 阶段 7：L1 Micro Compact

当前首要问题由 L2/L4/L3 解决。L1 随后按 Claude `microCompact` 单独移植，用请求投影
清理陈旧工具输出；不得篡改 Journal，也不得阻塞本阶段的递进摘要上线。

## 20. 明确不采用的方案

- 不把 Headroom generation 再次提交 `/v1/compress`。
- 不让 `_execute_recompress()` 继续用删除最旧段冒充重新压缩。
- 不保留五类业务摘要作为第三套活动摘要。
- 不为每句 L3/L4 摘要生成自定义 source ref。
- 不新增向量数据库或自定义 `journal_recall` 替代 Claude Grep/Read。
- 不把 Journal 全量原文每次重新塞给 L3。
- 不让用户手动执行召回。
- 不允许 Agent 直接读取 memory service 的真实文件路径。

## 21. 最终结果

改造后的核心链路为：

```text
Headroom：原文 → 一次性细节压缩段 + CCR

Claude L4：后台维护 Session Memory
Claude L3：L4 不可用时现场生成 Traditional Compact Summary
Claude L2：请求前按阈值执行 L4→L3，并替换活动上下文

活动上下文：
CompactBoundary
+ L4/L3 summary
+ 未覆盖 Headroom generations
+ 最近完整轮次

精确历史召回：
用户正常提问
→ Agent 根据摘要自主 Grep Journal
→ Agent 自主 Read 命中范围
→ 工具结果回到同一 Agent query loop
→ Agent 给用户最终答案
```

这样可以在保留 short-term-memory 的 Headroom/CCR/Redis/Journal 设计基础上，补齐
Claude Code 最关键的能力：**压缩结果真正替换当前上下文，并且可以在后续增长后再次
压缩；需要准确历史时，Agent 能自动从完整 Journal transcript 找回原文。**
