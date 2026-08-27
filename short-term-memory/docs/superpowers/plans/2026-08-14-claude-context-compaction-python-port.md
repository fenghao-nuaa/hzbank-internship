# Claude Code Context Compaction Python Port Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在保留 Headroom original-only generation、CCR、Redis 最近上下文和 Journal 原文机制的前提下，将当前 Claude Code 源码中的 L2 Auto Compact、L4 Session Memory、L3 Traditional Compact、Journal Grep/Read 自动召回及可移植的 L1 Micro Compact 逐语义翻译为 Python，使活动上下文可以被反复压缩而不是只能淘汰最旧 generation。

**Architecture:** Redis envelope v2 保存 Headroom generations、L4 Session Memory 和唯一活动 `ContextRevision`；请求前 `ContextCoordinator` 按 Claude `query.ts → autoCompact.ts → sessionMemoryCompact.ts/compact.ts` 的顺序组装、压缩并以 CAS 替换活动上下文。Journal 继续保存逐字原文，并通过绑定当前 session 的 HTTP Grep/Read 工具等价替代 Claude 本地 transcript；Headroom 仍只接收原文，不拥有也不改写活动上下文。

**Tech Stack:** Python 3.11–3.13、Pydantic 2.13、FastAPI 0.141、httpx 0.28、redis-py 6.4、pytest 9、pytest-asyncio 1.3；算法依据为本机 `../Claude code源码/claude-code-complete/claude-code-complete/src` TypeScript 源码快照。

## Global Constraints

- 唯一设计规格为 `docs/superpowers/specs/2026-08-14-claude-code-context-compaction-python-port-design.md`；实现前后均按其第 2、18、19 节逐项核对。
- 默认行为必须是 Claude TypeScript 的逐语义 Python 翻译；每个新模块顶部写 `Claude source:` 注释，列出源文件和被翻译函数名。
- 允许的项目适配只有：Journal 代替 transcript、`sequence` 代替 message UUID、Redis/CAS 代替进程内状态、本地工具改为认证 HTTP 工具、可注入 provider 代替 Claude 内部 forked agent。
- 任何 Claude 源码没有的分支必须在代码注释中以 `Project adaptation:` 开头，说明为什么分布式 HTTP 架构必需；不得用该标签改写 Claude 已有算法。
- Headroom `/v1/compress` 输入永远只包含 Journal/Redis originals；不得包含旧 generation、L3 summary 或 L4 Session Memory。
- compact 成功必须替换为 `boundary + summary + messages_to_keep + attachments + hook_results`，不得把新摘要追加在旧活动上下文之后。
- L2 调度固定为 L4 优先、L3 兜底；L4 结果在真实 post-compact token 复核后仍超阈值时必须返回 `None`。
- L3 compact 是独立、无工具、单轮文本请求；连续失败 3 次打开自动 compact 断路器。
- Journal 逐字原文不可被 compact、micro-compact 或 generation eviction 修改。
- 活动消息用 `stm_sequence_from`、`stm_sequence_through`、`stm_group_id` 保存 Claude message UUID/group 的 Python 等价定位元数据；`to_provider_messages()` 在任何 LLM/Headroom 请求前剥离所有 `stm_` 字段。
- 当前工作树已有未提交的 history-preview 改动；执行涉及 `agent_chat.py`、`memory_service.py`、`schemas.py` 及相应测试前先重新读取 working tree，保留这些改动，不得 checkout/reset 覆盖。
- 每个任务严格执行 Red → Green → Refactor；任务内指定的单测通过后再运行该目录测试，提交中只包含该任务列出的文件。
- 所有命令从仓库根目录 `/Users/fenghao/PycharmProjects/compression/short-term-memory` 执行。
- L1 以当前源码真实状态为准：移植 time-based tool-result clearing；Anthropic cache-editing API 依赖的 cached microcompact 不在本项目伪造，旧 legacy microcompact 已被 Claude 源码删除。

---

## File map and source-parity ledger

| Python 文件 | Claude 源文件与函数 | 职责 |
|---|---|---|
| `src/short_term_memory/models.py` | `compact.ts: CompactionResult/RecompactionInfo`、`autoCompact.ts: AutoCompactTrackingState`、Session Memory types | 持久化 v2 类型和请求内结果类型 |
| `src/short_term_memory/compression/context_messages.py` | Claude message UUID/message-id metadata | 用 sequence/group 标注活动投影并在 provider 前剥离内部字段 |
| `src/short_term_memory/compression/message_rounds.py` | `sessionMemoryCompact.ts: adjustIndexToPreserveAPIInvariants/calculateMessagesToKeepIndex`、`compact.ts: groupMessagesByApiRound` | 保留完整 API round、tool_use/tool_result 和尾部 |
| `src/short_term_memory/compression/compact_prompt.py` | `compact/prompt.ts` | L3 九段式 prompt、partial prompt、summary 格式化和 continuation message |
| `src/short_term_memory/compression/traditional_compact.py` | `compact/compact.ts` | L3、PTL 头部裁剪、CompactionResult、post-compact 构造 |
| `src/short_term_memory/compression/auto_compact.py` | `compact/autoCompact.ts` | Claude 阈值、递归保护、L4→L3、三次失败断路器 |
| `src/short_term_memory/compression/session_memory_prompt.py` | `SessionMemory/prompts.ts` | L4 十章节模板和更新 prompt |
| `src/short_term_memory/compression/session_memory_state.py` | `SessionMemory/sessionMemoryUtils.ts` | 初始化/增长/工具调用阈值、15 秒等待、60 秒 stale |
| `src/short_term_memory/compression/session_memory.py` | `SessionMemory/sessionMemory.ts` | 后台 extraction/update 和 coverage CAS |
| `src/short_term_memory/compression/session_memory_compact.py` | `compact/sessionMemoryCompact.ts` | L4 快速 compact 和真实 token 复核 |
| `src/short_term_memory/compression/context_query.py` | `query.ts` | 从最后 boundary 组装请求并用 compact 结果替换 |
| `src/short_term_memory/compression/micro_compact.py` | `compact/microCompact.ts` | L1 时间触发和旧工具结果投影清理 |
| `src/short_term_memory/transcript/journal_transcript.py` | Claude transcript 文件读取语义 | 将跨日 Journal 渲染为稳定逻辑 JSONL |
| `src/short_term_memory/transcript/grep_tool.py` | `tools/GrepTool/GrepTool.ts` | regex、三输出模式、上下文、offset/head_limit |
| `src/short_term_memory/transcript/read_tool.py` | `tools/FileReadTool/FileReadTool.ts` | 1-based offset/limit、编号、响应预算 |
| `src/short_term_memory/service/context_coordinator.py` | `query.ts` 请求前 compact 部分 | 服务端 prepare、session lease、CAS、硬上限降级 |
| `src/short_term_memory/agent/agent_chat.py` | `query.ts` tool-use loop | Agent 自动 Grep→Read→继续同一模型回合 |

## Stable interfaces used by all tasks

以下名字在后续任务中不得自行改名：

```python
class TokenEstimator(Protocol):
    def estimate(self, messages: tuple[dict[str, Any], ...]) -> int: ...

def to_provider_messages(
    messages: tuple[SessionCompressionMessage, ...],
) -> tuple[dict[str, Any], ...]: ...

class ContinuityCompactionModel(Protocol):
    async def update_session_memory(
        self, *, current_memory: str, messages: tuple[dict[str, Any], ...],
        prompt: str, model: str, query_source: Literal["session_memory"] = "session_memory",
    ) -> str: ...

    async def compact(
        self, *, messages: tuple[dict[str, Any], ...], prompt: str,
        model: str, max_output_tokens: int,
        query_source: Literal["compact"] = "compact",
    ) -> "CompactionModelResponse": ...

class ContextCoordinator:
    async def prepare(
        self, *, user_id: str, session_id: str,
        model_profile: "ModelProfile", query_source: str = "main",
    ) -> "PreparedContext": ...
```

---

### Task 1: Introduce envelope v2 and lazy v1 migration

**Claude source basis:** `services/compact/compact.ts` (`CompactionResult`, `RecompactionInfo`), `services/compact/autoCompact.ts` tracking fields, `services/SessionMemory/sessionMemory.ts` coverage state. `schema_version` and v1 migration are a necessary project adaptation because Claude does not store this state in the project's Redis envelope.

**Files:**
- Modify: `src/short_term_memory/models.py:135-320`
- Modify: `src/short_term_memory/storage/async_redis_memory_store.py:245-278`
- Modify: `tests/factories.py:20-36`
- Modify: `tests/test_models.py`
- Modify: `tests/storage/test_async_redis_memory_store.py`

**Interfaces:**
- Consumes: existing `CompressionGeneration`, `SessionCompressionMessage`, Redis CAS on `version`.
- Produces: `CompactBoundary`, `SessionMemoryRevision`, `ContextRevision`, `AutoCompactTrackingState`, `MemorySummaryEnvelope` schema v2, `migrate_v1_envelope(raw: dict[str, Any])`.

- [ ] **Step 1: Replace legacy envelope tests with v2 model tests**

Add tests that construct all four new immutable models, round-trip JSON, allow an emergency L3 boundary to exceed Headroom's independent `compressed_through_sequence`, reject negative/ill-ordered fields, and verify `AutoCompactTrackingState.record_failure()` caps only through caller policy while `reset_success()` creates `compacted=True`, `turn_counter=0`, a non-empty `turn_id`, and zero failures.

```python
def test_v2_envelope_round_trips_boundary_memory_revision_and_tracking() -> None:
    boundary = CompactBoundary(
        boundary_id="b1", trigger="auto", strategy="traditional",
        covered_through_sequence=8, pre_compact_tokens=100_000,
        true_post_compact_tokens=20_000,
        created_at="2026-08-14T00:00:00+00:00",
    )
    revision = ContextRevision(
        version=1, boundary=boundary,
        summary_message=SessionCompressionMessage(role="user", content="summary"),
        messages_to_keep=(), covered_generation_ids=(1, 2),
        updated_at="2026-08-14T00:00:00+00:00",
    )
    envelope = envelope(version=4, through=8).model_copy(
        update={"active_revision": revision}
    )
    assert MemorySummaryEnvelope.model_validate_json(
        envelope.model_dump_json()
    ) == envelope
    assert envelope.schema_version == 2

def test_v1_envelope_is_lazily_migrated_without_injecting_five_categories() -> None:
    migrated = migrate_v1_envelope({
        "version": 3, "compressed_through_sequence": 7,
        "compression_generations": [], "current_goal": ["legacy"],
        "preferences": ["brief"], "confirmed_facts": ["x"],
        "pending_items": ["y"], "attachment_references": [],
        "updated_at": "2026-08-14T00:00:00+00:00",
    })
    assert migrated.schema_version == 2
    assert migrated.active_revision is None
    assert "current_goal" not in migrated.model_dump()
```

- [ ] **Step 2: Run the model tests and confirm red**

Run: `uv run pytest tests/test_models.py tests/storage/test_async_redis_memory_store.py -q`

Expected: FAIL during import because the new v2 types and `migrate_v1_envelope` do not exist.

- [ ] **Step 3: Implement the v2 types and migration boundary**

Use frozen Pydantic models with the exact fields in design §5. Add these methods without adding new semantic-summary fields:

```python
class AutoCompactTrackingState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    compacted: bool = False
    turn_counter: int = Field(default=0, ge=0)
    turn_id: str = ""
    consecutive_failures: int = Field(default=0, ge=0)

    def record_failure(self) -> "AutoCompactTrackingState":
        return self.model_copy(update={
            "compacted": False,
            "consecutive_failures": self.consecutive_failures + 1,
        })

    def reset_success(self, turn_id: str) -> "AutoCompactTrackingState":
        return AutoCompactTrackingState(
            compacted=True, turn_counter=0, turn_id=turn_id,
            consecutive_failures=0,
        )

def migrate_v1_envelope(raw: dict[str, Any]) -> MemorySummaryEnvelope:
    if raw.get("schema_version") == 2:
        return MemorySummaryEnvelope.model_validate(raw)
    allowed = {
        "version", "compressed_through_sequence",
        "compression_generations", "updated_at",
    }
    return MemorySummaryEnvelope.model_validate({
        "schema_version": 2,
        **{key: value for key, value in raw.items() if key in allowed},
        "session_memory": None,
        "active_revision": None,
        "auto_compact_tracking": {},
    })
```

Change `AsyncRedisMemoryStore.read_envelope()` to `json.loads()` then call the migration function. Preserve the existing Lua CAS version semantics unchanged.

- [ ] **Step 4: Run focused and storage regression tests**

Run: `uv run pytest tests/test_models.py tests/storage/test_async_redis_memory_store.py -q`

Expected: PASS; CAS conflict tests continue to pass and serialized writes contain only `schema_version: 2` fields.

- [ ] **Step 5: Commit the schema slice**

```bash
git add src/short_term_memory/models.py src/short_term_memory/storage/async_redis_memory_store.py tests/factories.py tests/test_models.py tests/storage/test_async_redis_memory_store.py
git commit -m "feat: add compaction envelope v2"
```

---

### Task 2: Port compact-boundary slicing and API-round preservation

**Claude source basis:** `utils/messages.ts:getMessagesAfterCompactBoundary`, `services/compact/sessionMemoryCompact.ts:hasTextBlocks`, `adjustIndexToPreserveAPIInvariants`, `calculateMessagesToKeepIndex`, and `services/compact/compact.ts:groupMessagesByApiRound`.

**Files:**
- Create: `src/short_term_memory/compression/context_messages.py`
- Create: `src/short_term_memory/compression/message_rounds.py`
- Create: `tests/compression/test_context_messages.py`
- Create: `tests/compression/test_message_rounds.py`

**Interfaces:**
- Consumes: `SessionCompressionMessage`, `TokenEstimator`.
- Produces: `annotate_active_message()`, `to_provider_messages()`, `get_messages_after_compact_boundary()`, `group_messages_by_api_round()`, `adjust_index_to_preserve_api_invariants()`, `calculate_messages_to_keep_index()`.

- [ ] **Step 1: Write table-driven parity tests**

Cover: annotation returns a defensive copy; `to_provider_messages()` strips only `stm_` keys and preserves opaque Headroom fields; no boundary returns all; last boundary wins; a kept `tool_result` pulls in its matching assistant `tool_use`; assistant fragments sharing `message_id` are kept together; adjacent entries sharing `stm_group_id` are never split; tail expands backward until both 10,000 tokens and five text messages; expansion stops at 40,000 tokens and never crosses the last boundary; recent N user turns can move the index farther backward but never past the 40,000 hard cap.

```python
@pytest.mark.parametrize(
    ("start", "expected"), [(3, 1), (1, 1), (0, 0)]
)
def test_adjust_index_preserves_tool_pairs(start: int, expected: int) -> None:
    messages = (
        msg("assistant", blocks=[tool_use("call-1")], message_id="a1"),
        msg("assistant", blocks=[text("working")], message_id="a1"),
        msg("user", blocks=[tool_result("call-1")]),
        msg("assistant", "done"),
    )
    assert adjust_index_to_preserve_api_invariants(messages, start) == expected
```

- [ ] **Step 2: Run the new tests and confirm red**

Run: `uv run pytest tests/compression/test_context_messages.py tests/compression/test_message_rounds.py -q`

Expected: FAIL because `compression.message_rounds` does not exist.

- [ ] **Step 3: Translate the Claude loops directly**

Represent block-bearing messages through `SessionCompressionMessage.model_extra`; do not flatten tool blocks to text. `context_messages.py` writes `stm_sequence_from`, `stm_sequence_through`, `stm_group_id` on a copied projection and strips them from provider payloads. Use exact defaults:

```python
DEFAULT_MIN_TOKENS = 10_000
DEFAULT_MIN_TEXT_BLOCK_MESSAGES = 5
DEFAULT_MAX_TOKENS = 40_000

def calculate_messages_to_keep_index(
    messages: tuple[SessionCompressionMessage, ...],
    last_summarized_index: int,
    estimator: TokenEstimator,
    *, recent_user_turns: int,
    min_tokens: int = DEFAULT_MIN_TOKENS,
    min_text_messages: int = DEFAULT_MIN_TEXT_BLOCK_MESSAGES,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> int:
    start = last_summarized_index + 1 if last_summarized_index >= 0 else len(messages)
    total = sum(_message_tokens(message, estimator) for message in messages[start:])
    text_count = sum(_has_text_blocks(message) for message in messages[start:])
    floor = _last_boundary_index(messages) + 1
    while start > floor and total < max_tokens:
        if total >= min_tokens and text_count >= min_text_messages:
            break
        candidate = messages[start - 1]
        total += _message_tokens(candidate, estimator)
        text_count += int(_has_text_blocks(candidate))
        start -= 1
    recent_start = _start_of_recent_user_turns(messages, recent_user_turns, floor)
    start = min(start, recent_start)
    while start < len(messages) and _range_tokens(messages[start:], estimator) > max_tokens:
        start = _next_complete_round_start(messages, start + 1)
    return adjust_index_to_preserve_api_invariants(messages, start)
```

Implement the referenced private helpers in the same module: each calls the injected estimator, detects text/tool blocks without flattening them, finds the last compact boundary, locates the Nth recent user turn, and advances only to the next complete API-round start. `group_messages_by_api_round()` starts a new round at each user message and keeps following assistant/tool events in that round, matching Claude PTL truncation behavior. When adjacent entries share `stm_group_id`, expand the selected index to include the whole group; this is the sequence/group equivalent of Claude's same-message-id preservation.

- [ ] **Step 4: Run parity and existing generation tests**

Run: `uv run pytest tests/compression/test_context_messages.py tests/compression/test_message_rounds.py tests/compression/test_generations.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the message-boundary slice**

```bash
git add src/short_term_memory/compression/context_messages.py src/short_term_memory/compression/message_rounds.py tests/compression/test_context_messages.py tests/compression/test_message_rounds.py
git commit -m "feat: port Claude compact message boundaries"
```

---

### Task 3: Render Journal as a stable virtual transcript

**Claude source basis:** Claude summaries point to a complete transcript; this task is the approved Journal-for-transcript adaptation in design §10. No summarization or search heuristic is introduced here.

**Files:**
- Create: `src/short_term_memory/transcript/__init__.py`
- Create: `src/short_term_memory/transcript/journal_transcript.py`
- Modify: `src/short_term_memory/storage/journal_store.py:94-240`
- Create: `tests/transcript/test_journal_transcript.py`
- Modify: `tests/storage/test_journal_store.py`

**Interfaces:**
- Consumes: `JournalStore.read_session()`/a new ordered message-record reader.
- Produces: `JOURNAL_TRANSCRIPT_URI`, `TranscriptLine`, `JournalTranscript.lines()`, `JournalTranscript.render()`.

- [ ] **Step 1: Write transcript stability tests**

Create events on two UTC dates out of physical-file enumeration order and assert sequence sorting, one JSON object per logical line, JSON-escaped embedded newlines, exact 1-based sequence numbers, and exclusion of file events without a message sequence.

```python
def test_virtual_transcript_is_sorted_by_sequence_across_daily_files(tmp_path) -> None:
    store = journal_store(tmp_path)
    append(store, sequence=2, content="second\nline", at="2026-08-14T00:00:00+00:00")
    append(store, sequence=1, content="first", at="2026-08-13T23:59:00+00:00")
    transcript = JournalTranscript(store).render("u", "s")
    assert transcript.splitlines() == [
        '1\t{"sequence":1,"role":"user","content":"first"}',
        '2\t{"sequence":2,"role":"user","content":"second\\nline"}',
    ]
```

- [ ] **Step 2: Run transcript tests and confirm red**

Run: `uv run pytest tests/transcript/test_journal_transcript.py -q`

Expected: FAIL because the transcript package does not exist.

- [ ] **Step 3: Implement ordered record access and JSONL rendering**

```python
JOURNAL_TRANSCRIPT_URI = "journal://current-session"

@dataclass(frozen=True)
class TranscriptLine:
    sequence: int
    text: str

class JournalTranscript:
    def __init__(self, journals: JournalStore) -> None:
        self.journals = journals

    def lines(self, user_id: str, session_id: str) -> tuple[TranscriptLine, ...]:
        events = self.journals.read_original_range(user_id, session_id, 1, 2**63 - 1)
        return tuple(
            TranscriptLine(
                sequence=event.sequence,
                text=json.dumps(
                    {"sequence": event.sequence, "role": event.role.value,
                     "content": event.content},
                    ensure_ascii=False, separators=(",", ":"),
                ),
            )
            for event in sorted(events, key=lambda item: item.sequence)
        )

    def render(self, user_id: str, session_id: str) -> str:
        return "\n".join(f"{line.sequence}\t{line.text}" for line in self.lines(user_id, session_id))
```

Keep all real filesystem paths inside `JournalStore`; no response object may contain them.

- [ ] **Step 4: Run transcript and journal regression tests**

Run: `uv run pytest tests/transcript/test_journal_transcript.py tests/storage/test_journal_store.py tests/storage/test_journal_retention.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the virtual transcript**

```bash
git add src/short_term_memory/transcript src/short_term_memory/storage/journal_store.py tests/transcript tests/storage/test_journal_store.py
git commit -m "feat: expose Journal as virtual transcript"
```

---

### Task 4: Port Claude Grep semantics to the virtual transcript

**Claude source basis:** `tools/GrepTool/GrepTool.ts` request fields and output modes: regex, `content/files_with_matches/count`, `-A/-B/-C`, default head limit 250, zero as unlimited, offset pagination and multiline mode.

**Files:**
- Create: `src/short_term_memory/transcript/grep_tool.py`
- Create: `tests/transcript/test_grep_tool.py`

**Interfaces:**
- Consumes: `JournalTranscript.lines()`, `JOURNAL_TRANSCRIPT_URI`.
- Produces: `TranscriptGrepRequest`, `TranscriptGrepMatch`, `TranscriptGrepResult`, `grep_transcript()`.

- [ ] **Step 1: Write one test per Claude Grep behavior**

Tests must separately cover: invalid regex; case-insensitive default; case-sensitive override; content mode with before/after; `context` overriding both; count mode; files-with-matches returning only the logical URI; offset applied before head limit; `head_limit=0`; overlapping context lines de-duplicated; multiline matching; response character budget returning `was_truncated=True` without cutting a JSON line.

```python
def test_grep_applies_offset_then_head_limit() -> None:
    result = grep_transcript(
        lines(1, "TTL one", 2, "TTL two", 3, "TTL three"),
        TranscriptGrepRequest(
            path=JOURNAL_TRANSCRIPT_URI, pattern="TTL",
            output_mode="content", offset=1, head_limit=1,
        ),
        max_response_chars=10_000,
    )
    assert [match.sequence for match in result.matches] == [2]
    assert result.applied_offset == 1
    assert result.was_truncated is True
```

- [ ] **Step 2: Run Grep tests and confirm red**

Run: `uv run pytest tests/transcript/test_grep_tool.py -q`

Expected: FAIL because `grep_tool.py` does not exist.

- [ ] **Step 3: Implement the regex line engine**

Compile with `re.IGNORECASE` by default and `re.MULTILINE | re.DOTALL` only when `multiline=True`. For content mode, match first, expand indices by effective context, merge overlaps, then paginate matches and apply the response budget. Return structured data plus Claude-like printable content; never invoke shell `rg` on a user-supplied path.

```python
class TranscriptPatternError(ValueError):
    pass

def grep_transcript(
    transcript_lines: tuple[TranscriptLine, ...],
    request: TranscriptGrepRequest,
    *, max_response_chars: int,
) -> TranscriptGrepResult:
    try:
        flags = (re.IGNORECASE if request.case_insensitive else 0)
        if request.multiline:
            flags |= re.MULTILINE | re.DOTALL
        pattern = re.compile(request.pattern, flags)
    except re.error as error:
        raise TranscriptPatternError(str(error)) from error
    rendered = "\n".join(f"{line.sequence}\t{line.text}" for line in transcript_lines)
    matched_indices = (
        _multiline_match_indices(pattern, rendered, transcript_lines)
        if request.multiline
        else [
            index for index, line in enumerate(transcript_lines)
            if pattern.search(line.text)
        ]
    )
    if request.output_mode == "files_with_matches":
        return TranscriptGrepResult.for_file(
            JOURNAL_TRANSCRIPT_URI if matched_indices else None
        )
    if request.output_mode == "count":
        return TranscriptGrepResult.for_count(len(matched_indices))
    before = request.context if request.context is not None else request.context_before
    after = request.context if request.context is not None else request.context_after
    expanded = _merge_context_indices(
        matched_indices, before, after, len(transcript_lines)
    )
    page, more = _page_indices(expanded, request.offset, request.head_limit)
    return _bounded_content_result(
        transcript_lines, page, applied_offset=request.offset,
        already_truncated=more, max_response_chars=max_response_chars,
    )
```

- [ ] **Step 4: Run all transcript tests**

Run: `uv run pytest tests/transcript -q`

Expected: PASS.

- [ ] **Step 5: Commit the Grep port**

```bash
git add src/short_term_memory/transcript/grep_tool.py tests/transcript/test_grep_tool.py
git commit -m "feat: port Claude Grep for Journal transcript"
```

---

### Task 5: Port Claude Read semantics to the virtual transcript

**Claude source basis:** `tools/FileReadTool/FileReadTool.ts`: 1-based offset, default/maximum 2,000 lines, numbered output, explicit out-of-range and oversized-result diagnostics.

**Files:**
- Create: `src/short_term_memory/transcript/read_tool.py`
- Create: `tests/transcript/test_read_tool.py`

**Interfaces:**
- Consumes: `JournalTranscript.lines()`, `JOURNAL_TRANSCRIPT_URI`.
- Produces: `TranscriptReadRequest`, `TranscriptReadResult`, `read_transcript()`, `TranscriptOffsetError`, `TranscriptResultTooLargeError`.

- [ ] **Step 1: Write Read contract tests**

Cover exact offset inclusion, omitted limit using 2,000, validation above 2,000, last partial page, offset after final sequence, missing transcript, numbered text, and size budget instructing the caller to Grep or narrow the range.

```python
def test_read_uses_sequence_as_one_based_offset() -> None:
    result = read_transcript(
        lines(7, "seven", 8, "eight", 9, "nine"),
        TranscriptReadRequest(file_path=JOURNAL_TRANSCRIPT_URI, offset=8, limit=2),
        max_response_chars=10_000,
    )
    assert result.content.splitlines() == ["8\teight", "9\tnine"]
    assert result.sequence_from == 8
    assert result.sequence_through == 9
```

- [ ] **Step 2: Run Read tests and confirm red**

Run: `uv run pytest tests/transcript/test_read_tool.py -q`

Expected: FAIL because `read_tool.py` does not exist.

- [ ] **Step 3: Implement bounded sequence reads**

```python
DEFAULT_READ_LIMIT = 2_000

def read_transcript(
    transcript_lines: tuple[TranscriptLine, ...],
    request: TranscriptReadRequest,
    *, max_response_chars: int,
) -> TranscriptReadResult:
    selected = tuple(
        line for line in transcript_lines if line.sequence >= request.offset
    )[: request.limit or DEFAULT_READ_LIMIT]
    if not selected:
        raise TranscriptOffsetError(f"offset {request.offset} is out of range")
    content = "\n".join(f"{line.sequence}\t{line.text}" for line in selected)
    if len(content) > max_response_chars:
        raise TranscriptResultTooLargeError(
            "result too large; use Grep or reduce offset/limit"
        )
    return TranscriptReadResult(
        content=content, sequence_from=selected[0].sequence,
        sequence_through=selected[-1].sequence,
    )
```

- [ ] **Step 4: Run transcript regression tests**

Run: `uv run pytest tests/transcript -q`

Expected: PASS.

- [ ] **Step 5: Commit the Read port**

```bash
git add src/short_term_memory/transcript/read_tool.py tests/transcript/test_read_tool.py
git commit -m "feat: port Claude Read for Journal transcript"
```

---

### Task 6: Expose authenticated transcript Grep/Read HTTP endpoints

**Claude source basis:** Claude executes local Grep/Read bound to its transcript path. HTTP transport and session binding are the required project adaptation because Agent and Journal are in different containers.

**Files:**
- Create: `src/short_term_memory/transcript/tool_definitions.py`
- Modify: `src/short_term_memory/service/schemas.py`
- Modify: `src/short_term_memory/service/memory_service.py:56-100,417-470`
- Modify: `src/short_term_memory/service/app.py:35-45,266-470`
- Modify: `tests/service/test_schemas.py`
- Modify: `tests/service/test_memory_service.py`
- Modify: `tests/service/test_app.py`

**Interfaces:**
- Consumes: transcript request/result types from Tasks 4–5.
- Produces: `TRANSCRIPT_TOOL_DEFINITIONS`, `MemoryTranscriptGrepRequest/Response`, `MemoryTranscriptReadRequest/Response`, `MemoryService.grep_transcript()`, `MemoryService.read_transcript()`, two authenticated and session-scoped routes.

- [ ] **Step 1: Add schema, service, and route tests**

Assert exact routes `POST /v1/memories/transcript/grep` and `/read`, bearer auth, request-id propagation, current user/session passed by SDK rather than model tool arguments, `X-Memory-Session-Scope` HMAC matching the body user/session, scope mismatch→403, invalid regex→422 `invalid_pattern`, missing transcript/offset→404, large result→413, Journal `OSError`→503, and no physical path in any error.

```python
async def test_grep_route_binds_requested_session_and_returns_matches(client) -> None:
    response = await client.post(
        "/v1/memories/transcript/grep",
        headers={**AUTH, "X-Memory-Session-Scope": scope_for("u", "s")},
        json={"user_id": "u", "session_id": "s",
              "path": "journal://current-session", "pattern": "TTL",
              "output_mode": "content"},
    )
    assert response.status_code == 200
    assert response.json()["matches"][0]["sequence"] == 87
```

- [ ] **Step 2: Run service tests and confirm red**

Run: `uv run pytest tests/service/test_schemas.py tests/service/test_memory_service.py tests/service/test_app.py -q`

Expected: FAIL because the new schemas, service methods and routes are absent.

- [ ] **Step 3: Add the HTTP adapter without broadening file access**

Add both paths to `_BUSINESS_PATHS`. Each service method first compares `X-Memory-Session-Scope` with `OptimizationScopeFactory.for_session(user_id, session_id).session_scope` using `secrets.compare_digest`, then calls `JournalTranscript.lines(user_id, session_id)` in `anyio.to_thread.run_sync` and the pure Grep/Read function. The model-facing tool parameters contain only path, pattern and search/read controls; `AgentChatClient` adds `user_id/session_id` and the signed session scope from the current turn. This is the concrete enforcement of “模型不能改 URI 或 session 去读别人的 Journal”.

```python
@app.post("/v1/memories/transcript/grep", response_model=MemoryTranscriptGrepResponse)
async def grep_memory_transcript(request: Request, body: MemoryTranscriptGrepRequest):
    return await request.app.state.memory_service.grep_transcript(
        body, request.state.request_id,
        session_scope=request.headers.get("x-memory-session-scope", ""),
    )

@app.post("/v1/memories/transcript/read", response_model=MemoryTranscriptReadResponse)
async def read_memory_transcript(request: Request, body: MemoryTranscriptReadRequest):
    return await request.app.state.memory_service.read_transcript(
        body, request.state.request_id,
        session_scope=request.headers.get("x-memory-session-scope", ""),
    )
```

- [ ] **Step 4: Run full service tests**

Run: `uv run pytest tests/service -q`

Expected: PASS, including existing history-preview tests from the dirty working tree.

- [ ] **Step 5: Commit the HTTP transcript adapter**

```bash
git add src/short_term_memory/transcript/tool_definitions.py src/short_term_memory/service/schemas.py src/short_term_memory/service/memory_service.py src/short_term_memory/service/app.py tests/service/test_schemas.py tests/service/test_memory_service.py tests/service/test_app.py
git commit -m "feat: add authenticated Journal Grep and Read APIs"
```

---

### Task 7: Replace heuristic recall with Claude-style autonomous Grep→Read tool loop

**Claude source basis:** `query.ts` around `runTools(...)`: append assistant tool-use blocks, execute tools, append tool results, then call the same model again. Tool names and schemas follow `GrepTool.ts` and `FileReadTool.ts`. The existing keyword-driven proactive recall and custom guidance are not part of Claude's transcript recall path.

**Files:**
- Modify: `src/short_term_memory/agent/agent_chat.py:1-310`
- Delete: `src/short_term_memory/compression/recall_policy.py`
- Modify: `tests/agent/test_agent_chat.py`
- Modify: `examples/chat_loop.py`

**Interfaces:**
- Consumes: Task 6 HTTP endpoints and existing `headroom_retrieve` CCR endpoint.
- Produces: `HEADROOM_RETRIEVE_TOOL_DEFINITION`, `MEMORY_TOOL_DEFINITIONS`, `_grep_transcript()`, `_read_transcript()`, a model loop that passes the merged definitions on every request.

- [ ] **Step 1: Write an exact two-tool-round Agent test**

The fake model first returns `Grep`, then `Read`, then a final TTL answer. Assert all three model calls receive the same registered tool definitions, the Grep result is included in call 2, the Read result is included in call 3, tool-call IDs are preserved, and neither `should_recall()` nor a proactive system injection appears.

```python
async def test_agent_autonomously_greps_then_reads_before_answering() -> None:
    model = ScriptedModel([
        tool_call("g1", "Grep", {"path": "journal://current-session",
                                  "pattern": "TTL", "output_mode": "content"}),
        tool_call("r1", "Read", {"file_path": "journal://current-session",
                                  "offset": 84, "limit": 8}),
        {"content": "之前确定的 TTL 是 43200 秒。", "tool_calls": []},
    ])
    transport = RecordingTranscriptTransport()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        client = AgentChatClient(
            memory_api_url="http://memory", model_call=model, http_client=http,
        )
        answer = await client.turn("u", "s", "之前 TTL 是多少？")
    assert answer == "之前确定的 TTL 是 43200 秒。"
    assert transport.paths[-2:] == [
        "/v1/memories/transcript/grep", "/v1/memories/transcript/read",
    ]
    assert all(call["tools"] == MEMORY_TOOL_DEFINITIONS for call in model.calls)
```

- [ ] **Step 2: Run Agent tests and confirm red**

Run: `uv run pytest tests/agent/test_agent_chat.py -q`

Expected: FAIL because only `headroom_retrieve` is dispatched and `tools` is not passed to `model_call`.

- [ ] **Step 3: Translate the query tool loop**

Import the two shared transcript definitions and append a local `headroom_retrieve` function definition with required string `hash`; bind `MEMORY_TOOL_DEFINITIONS = (*TRANSCRIPT_TOOL_DEFINITIONS, HEADROOM_RETRIEVE_TOOL_DEFINITION)`. Do not expose `user_id/session_id` in any model schema. Bind those identifiers inside the dispatcher:

```python
async def _execute_tool(
    self, *, name: str, arguments: dict[str, Any],
    user_id: str, session_id: str, session_scope: str,
) -> str:
    if name == "headroom_retrieve":
        return await self._recall(user_id, session_id, str(arguments.get("hash", ""))) or "not found"
    if name == "Grep":
        body = await self._post(
            "/v1/memories/transcript/grep",
            {"user_id": user_id, "session_id": session_id, **arguments},
            headers={"X-Memory-Session-Scope": session_scope},
        )
        return str(body["content"])
    if name == "Read":
        body = await self._post(
            "/v1/memories/transcript/read",
            {"user_id": user_id, "session_id": session_id, **arguments},
            headers={"X-Memory-Session-Scope": session_scope},
        )
        return str(body["content"])
    return f"unknown tool {name}"
```

Extend `_post(path, payload, *, headers=None)` so tool dispatch can add only `X-Memory-Session-Scope` while preserving the client's bearer header. Keep `max_tool_rounds` as the safety bound. Read `session_scope` from the memory/prepare response's `headroom.scope_headers["x-headroom-session-id"]`; it is application metadata, never a model argument. Delete proactive `should_recall()` and `retrieve_guidance()` calls; preserve history-preview working-tree behavior and the explicit CCR tool.

- [ ] **Step 4: Run Agent and example regression tests**

Run: `uv run pytest tests/agent/test_agent_chat.py tests/examples/test_chat_loop_triggers.py -q`

Expected: PASS; tool calls continue until a final text answer and history preview remains unchanged.

- [ ] **Step 5: Commit the autonomous recall loop**

```bash
git add src/short_term_memory/agent/agent_chat.py src/short_term_memory/compression/recall_policy.py tests/agent/test_agent_chat.py examples/chat_loop.py
git commit -m "feat: add autonomous Journal Grep Read recall"
```

---

### Task 8: Port L4 Session Memory prompt and extraction predicates

**Claude source basis:** `services/SessionMemory/prompts.ts` ten-section template and limits; `sessionMemory.ts:shouldExtractMemory`; `sessionMemoryUtils.ts` defaults and in-progress wait/stale values.

**Files:**
- Create: `src/short_term_memory/compression/session_memory_prompt.py`
- Create: `src/short_term_memory/compression/session_memory_state.py`
- Create: `tests/compression/test_session_memory_prompt.py`
- Create: `tests/compression/test_session_memory_state.py`

**Interfaces:**
- Consumes: active `SessionCompressionMessage` tuples and token/tool-call counts.
- Produces: `EMPTY_SESSION_MEMORY`, `build_session_memory_update_prompt()`, `SessionMemoryConfig`, `should_extract_memory()`, `is_extraction_stale()`.

- [ ] **Step 1: Write prompt snapshot and predicate truth-table tests**

Assert the exact ten headings in order, 2,000-token per-section instruction, 12,000-token total instruction, preservation priority for Current State and Errors & Corrections, current memory inclusion, and no accidental L3 nine-section headings. Predicate cases must prove token growth is always required and implement `(growth and tool threshold) or (growth and last assistant has no tools)`.

```python
@pytest.mark.parametrize(
    ("growth", "tool_calls", "last_has_tools", "expected"),
    [(False, 3, True, False), (True, 3, True, True),
     (True, 0, False, True), (True, 0, True, False)],
)
def test_should_extract_memory_matches_claude_truth_table(
    growth: bool, tool_calls: int, last_has_tools: bool, expected: bool
) -> None:
    assert should_extract_memory_from_counts(
        token_growth_reached=growth,
        tool_calls_since_update=tool_calls,
        last_assistant_turn_has_tool_calls=last_has_tools,
    ) is expected
```

- [ ] **Step 2: Run L4 prompt/state tests and confirm red**

Run: `uv run pytest tests/compression/test_session_memory_prompt.py tests/compression/test_session_memory_state.py -q`

Expected: FAIL because both modules are absent.

- [ ] **Step 3: Translate constants and predicates**

```python
@dataclass(frozen=True)
class SessionMemoryConfig:
    minimum_message_tokens_to_init: int = 10_000
    minimum_tokens_between_update: int = 5_000
    tool_calls_between_updates: int = 3
    wait_for_extraction_seconds: float = 15.0
    stale_extraction_seconds: float = 60.0
    max_section_tokens: int = 2_000
    max_total_tokens: int = 12_000

def should_extract_memory_from_counts(
    *, token_growth_reached: bool, tool_calls_since_update: int,
    last_assistant_turn_has_tool_calls: bool,
    config: SessionMemoryConfig = SessionMemoryConfig(),
) -> bool:
    return (
        token_growth_reached
        and tool_calls_since_update >= config.tool_calls_between_updates
    ) or (token_growth_reached and not last_assistant_turn_has_tool_calls)
```

Translate the full prompt text rather than paraphrasing it. Keep headings and structural instructions in constants so snapshot drift is reviewable.

- [ ] **Step 4: Run focused L4 tests**

Run: `uv run pytest tests/compression/test_session_memory_prompt.py tests/compression/test_session_memory_state.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the L4 pure logic**

```bash
git add src/short_term_memory/compression/session_memory_prompt.py src/short_term_memory/compression/session_memory_state.py tests/compression/test_session_memory_prompt.py tests/compression/test_session_memory_state.py
git commit -m "feat: port Claude Session Memory prompts and thresholds"
```

---

### Task 9: Add distributed L4 extraction state and worker

**Claude source basis:** `SessionMemory/sessionMemory.ts` starts an isolated forked update after sampling, includes current memory plus conversation, serializes updates, and advances coverage only on success. Redis job/lease state is a project adaptation for multiprocess deployment.

**Files:**
- Create: `src/short_term_memory/compression/continuity_model.py`
- Create: `src/short_term_memory/compression/session_memory.py`
- Create: `src/short_term_memory/jobs/session_memory_queue.py`
- Create: `src/short_term_memory/jobs/session_memory_worker.py`
- Modify: `src/short_term_memory/storage/async_redis_memory_store.py`
- Modify: `src/short_term_memory/ports.py`
- Create: `tests/jobs/test_session_memory_queue.py`
- Create: `tests/jobs/test_session_memory_worker.py`
- Create: `tests/compression/test_session_memory.py`
- Modify: `tests/storage/fake_redis.py`
- Modify: `tests/storage/test_async_redis_memory_store.py`

**Interfaces:**
- Consumes: Task 8 prompt/predicate, `ContinuityCompactionModel.update_session_memory()`, Journal originals, envelope CAS.
- Produces: `extract_session_memory_revision()`, `SessionMemoryJob`, `RedisSessionMemoryQueue`, `SessionMemoryWorker`, extraction-state read/write/clear methods.

- [ ] **Step 1: Write queue, lease and worker tests**

Test idempotent enqueue per `(user, session, expected_version, through_sequence)`, single session lease, 60-second stale lease recovery, model input containing current memory plus full active context, `query_source="session_memory"` metadata, successful ten-heading validation, failed output not advancing coverage, CAS conflict returning stale, and a newer Headroom envelope version causing safe retry rather than overwrite.

```python
async def test_worker_updates_memory_and_coverage_only_after_valid_model_output() -> None:
    model = FakeContinuityModel(session_memory=VALID_TEN_SECTION_MEMORY)
    result = await worker(model=model).run_once()
    saved = await store.read_envelope("u", "s")
    assert result.state == "acked"
    assert saved.session_memory.content == VALID_TEN_SECTION_MEMORY
    assert saved.session_memory.covered_through_sequence == 12
    assert model.update_calls[0]["query_source"] == "session_memory"
```

- [ ] **Step 2: Run worker tests and confirm red**

Run: `uv run pytest tests/compression/test_session_memory.py tests/jobs/test_session_memory_queue.py tests/jobs/test_session_memory_worker.py tests/storage/test_async_redis_memory_store.py -q`

Expected: FAIL because queue, worker and extraction-state storage methods do not exist.

- [ ] **Step 3: Implement the distributed translation**

Define the provider protocol exactly once:

```python
class ContinuityCompactionModel(Protocol):
    async def update_session_memory(
        self, *, current_memory: str,
        messages: tuple[dict[str, Any], ...], prompt: str, model: str,
        query_source: Literal["session_memory"] = "session_memory",
    ) -> str: ...

    async def compact(
        self, *, messages: tuple[dict[str, Any], ...], prompt: str,
        model: str, max_output_tokens: int,
        query_source: Literal["compact"] = "compact",
    ) -> CompactionModelResponse: ...
```

`compression/session_memory.py` directly translates the model-input construction, update prompt call, ten-heading validation and revision creation; it has no Redis/queue code. Store extraction state under `dream:session:{user}:{session}:session-memory-extraction` with `SET NX PX`; the JSON value contains token, expected envelope version and ISO `started_at`. This is the Redis equivalent of Claude's in-process extraction promise. Worker calls `extract_session_memory_revision()`, sets `SessionMemoryRevision.version = previous + 1`, advances only to the last safe complete round sequence, increments envelope version, CAS-writes, then clears the lease with compare-token Lua.

- [ ] **Step 4: Run jobs and storage regressions**

Run: `uv run pytest tests/compression/test_session_memory.py tests/jobs tests/storage/test_async_redis_memory_store.py -q`

Expected: PASS; existing Headroom queue/lease tests remain green.

- [ ] **Step 5: Commit the L4 worker slice**

```bash
git add src/short_term_memory/compression/continuity_model.py src/short_term_memory/compression/session_memory.py src/short_term_memory/jobs/session_memory_queue.py src/short_term_memory/jobs/session_memory_worker.py src/short_term_memory/storage/async_redis_memory_store.py src/short_term_memory/ports.py tests/compression/test_session_memory.py tests/jobs/test_session_memory_queue.py tests/jobs/test_session_memory_worker.py tests/storage/fake_redis.py tests/storage/test_async_redis_memory_store.py
git commit -m "feat: add distributed Session Memory extraction"
```

---

### Task 10: Port L4 Session Memory compact fast path

**Claude source basis:** `services/compact/sessionMemoryCompact.ts:calculateMessagesToKeepIndex`, `trySessionMemoryCompaction`; wait up to 15 seconds, reject stale/empty/unlocatable memory, preserve tail/API invariants, build result, then true post-token check.

**Files:**
- Create: `src/short_term_memory/compression/session_memory_compact.py`
- Create: `tests/compression/test_session_memory_compact.py`

**Interfaces:**
- Consumes: Tasks 1, 2, 8, 9; `build_compact_user_summary_message()` from Task 11 is imported only after Task 11 and initially injected as a formatter callable to keep this task independently testable.
- Produces: `try_session_memory_compaction() -> CompactionResult | None`.

- [ ] **Step 1: Write every L4 fallback and success test**

Cover no memory, empty template, in-progress extraction completing within 15 seconds, extraction older than 60 seconds, timeout, coverage not found, coverage after latest message, recent-tail expansion, tool-pair preservation, 40,000 cap, existing recent-N-turn union, success under threshold, and result over L2 threshold returning `None`.

```python
async def test_l4_result_over_threshold_falls_back_to_l3() -> None:
    result = await try_session_memory_compaction(
        messages=messages_after_coverage(), session_memory=revision(through=20),
        context=context(estimator=ConstantEstimator(120_000), threshold=100_000),
    )
    assert result is None

async def test_l4_keeps_matching_tool_use_when_tail_starts_at_tool_result() -> None:
    result = await try_session_memory_compaction(
        messages=tail_starting_at_tool_result(),
        session_memory=revision(through=20),
        context=context(estimator=BlockEstimator(), threshold=100_000),
    )
    assert tool_ids(result.messages_to_keep) == {"call-9"}
```

- [ ] **Step 2: Run L4 compact tests and confirm red**

Run: `uv run pytest tests/compression/test_session_memory_compact.py -q`

Expected: FAIL because `session_memory_compact.py` is absent.

- [ ] **Step 3: Translate the fast-path control flow**

Use these exact defaults and return `None` for every Claude fallback rather than throwing:

```python
SM_MIN_TOKENS = 10_000
SM_MIN_TEXT_MESSAGES = 5
SM_MAX_TOKENS = 40_000
SM_WAIT_SECONDS = 15.0
SM_STALE_SECONDS = 60.0

async def try_session_memory_compaction(
    *, messages: tuple[SessionCompressionMessage, ...],
    session_memory: SessionMemoryRevision | None,
    context: SessionMemoryCompactContext,
) -> CompactionResult | None:
    memory = await context.wait_for_current_extraction(
        session_memory, timeout_seconds=SM_WAIT_SECONDS,
        stale_seconds=SM_STALE_SECONDS,
    )
    if memory is None or memory.content == EMPTY_SESSION_MEMORY:
        return None
    coverage_index = find_coverage_index(messages, memory.covered_through_sequence)
    if coverage_index is None:
        return None
    start = calculate_messages_to_keep_index(
        messages, coverage_index, context.token_estimator,
        recent_user_turns=context.history_turns,
    )
    result = context.build_result(memory, messages[start:])
    post = context.token_estimator.estimate(
        to_provider_messages(build_post_compact_messages(result))
    )
    return None if post >= context.auto_compact_threshold else replace_true_post(result, post)
```

- [ ] **Step 4: Run L4 and round-preservation tests**

Run: `uv run pytest tests/compression/test_session_memory_compact.py tests/compression/test_message_rounds.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the L4 compact path**

```bash
git add src/short_term_memory/compression/session_memory_compact.py tests/compression/test_session_memory_compact.py
git commit -m "feat: port Claude Session Memory compaction"
```

---

### Task 11: Translate the L3 compact prompts and continuation message verbatim

**Claude source basis:** `services/compact/prompt.ts`: `NO_TOOLS_PREAMBLE`, detailed analysis base, full nine-section prompt, both partial variants, no-tools trailer, `formatCompactSummary`, and `getCompactUserSummaryMessage`.

**Files:**
- Create: `src/short_term_memory/compression/compact_prompt.py`
- Create: `tests/compression/test_compact_prompt.py`
- Modify: `src/short_term_memory/compression/session_memory_compact.py`
- Modify: `tests/compression/test_session_memory_compact.py`

**Interfaces:**
- Consumes: raw compact model text and `journal://current-session`.
- Produces: `get_compact_prompt()`, `get_partial_compact_prompt()`, `format_compact_summary()`, `get_compact_user_summary_message()`.

- [ ] **Step 1: Write source-parity snapshots**

Assert full prompt contains all nine headings in source order, partial `from` and `up_to` differ exactly as Claude does, no-tools preamble/trailer wrap every prompt, custom instructions are inserted once, `<analysis>` is removed, only `<summary>` content survives, extra blank lines collapse, and continuation text mentions Journal Grep/Read, recent raw messages and direct continuation without recap.

```python
def test_format_compact_summary_discards_analysis_and_keeps_summary() -> None:
    raw = "<analysis>private chain</analysis>\n<summary>\nA\n\n\nB\n</summary>"
    assert format_compact_summary(raw) == "A\n\nB"

def test_continuation_message_points_to_virtual_transcript() -> None:
    message = get_compact_user_summary_message("structured summary")
    assert "journal://current-session" in str(message.content)
    assert "Grep" in str(message.content) and "Read" in str(message.content)
    assert "不要复述摘要" in str(message.content)
```

- [ ] **Step 2: Run prompt tests and confirm red**

Run: `uv run pytest tests/compression/test_compact_prompt.py tests/compression/test_session_memory_compact.py -q`

Expected: FAIL because prompt functions do not exist.

- [ ] **Step 3: Translate prompt constants without creative rewriting**

Copy each semantic paragraph from the inspected TypeScript source into Python triple-quoted constants, retaining XML tags and section numbering. Implement formatting with explicit tag extraction:

```python
SUMMARY_PATTERN = re.compile(r"<summary>\s*(.*?)\s*</summary>", re.DOTALL)

def format_compact_summary(raw: str) -> str:
    match = SUMMARY_PATTERN.search(raw)
    text = match.group(1) if match else re.sub(
        r"<analysis>.*?</analysis>", "", raw, flags=re.DOTALL
    )
    return re.sub(r"\n{3,}", "\n\n", text.strip())
```

Replace Task 10's injected formatter with this concrete continuation-message function.

- [ ] **Step 4: Run prompt and L4 regressions**

Run: `uv run pytest tests/compression/test_compact_prompt.py tests/compression/test_session_memory_compact.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the compact prompts**

```bash
git add src/short_term_memory/compression/compact_prompt.py src/short_term_memory/compression/session_memory_compact.py tests/compression/test_compact_prompt.py tests/compression/test_session_memory_compact.py
git commit -m "feat: translate Claude compact prompts"
```

---

### Task 12: Port L3 Traditional Compact and prompt-too-long retry

**Claude source basis:** `services/compact/compact.ts:compactConversation`, `partialCompactConversation`, `truncateHeadForPTLRetry`, `buildPostCompactMessages`; exact PTL constants are `MAX_PTL_RETRIES=3`, marker `[earlier conversation truncated for compaction retry]`, token-gap head dropping with 20% group fallback. Streaming no-response retry is provider transport behavior and remains inside the injected provider adapter; L3 orchestration handles PTL only.

**Files:**
- Create: `src/short_term_memory/compression/traditional_compact.py`
- Create: `tests/compression/test_traditional_compact.py`
- Modify: `src/short_term_memory/compression/continuity_model.py`

**Interfaces:**
- Consumes: Tasks 1, 2, 11 and `ContinuityCompactionModel.compact()`.
- Produces: `CompactionModelResponse`, `CompactionResult`, `TraditionalCompactContext`, `truncate_head_for_ptl_retry()`, `compact_conversation()`, `partial_compact_conversation()`, `build_post_compact_messages()`.

- [ ] **Step 1: Write L3 success, replacement and PTL tests**

Cover empty input error; model called with `query_source="compact"`, no tools and one turn; old summary included in next compact input; output order boundary→summary→keep→attachments→hooks; actual post token recorded; partial `from` summarizes pivot-to-tail and keeps prefix; partial `up_to` summarizes prefix, keeps suffix and strips stale boundary/compact-summary messages from that suffix; both reject an empty summarize side; PTL parsed gap drops enough oldest complete API rounds; unparseable gap drops `floor(20%)` but at least one; assistant-first remainder gets synthetic marker; prior marker is removed before retry; one group cannot be truncated; exactly three PTL retries then diagnostic failure.

```python
def test_truncate_head_for_ptl_retry_drops_complete_rounds() -> None:
    truncated = truncate_head_for_ptl_retry(
        three_api_rounds(), token_gap=1_500, estimator=RoundEstimator(1_000)
    )
    assert truncated[0].role == "user"
    assert truncated[0].content == PTL_RETRY_MARKER
    assert no_orphan_tool_results(truncated)

async def test_second_compact_summarizes_previous_summary_not_full_journal() -> None:
    await compact_conversation(
        messages=(summary_message("AB"), generation_message("CD"), raw("tail")),
        context=context(),
    )
    assert model.calls[0]["messages"][0]["content"] == "AB"
    assert all("journal original A" not in str(m) for m in model.calls[0]["messages"])
```

- [ ] **Step 2: Run L3 tests and confirm red**

Run: `uv run pytest tests/compression/test_traditional_compact.py -q`

Expected: FAIL because `traditional_compact.py` does not exist.

- [ ] **Step 3: Translate compactConversation and PTL loops**

Use exact source constants and make replacement construction a pure function:

```python
MAX_PTL_RETRIES = 3
PTL_RETRY_MARKER = "[earlier conversation truncated for compaction retry]"
COMPACT_MAX_OUTPUT_TOKENS = 20_000

def build_post_compact_messages(result: CompactionResult) -> tuple[SessionCompressionMessage, ...]:
    return (
        result.boundary_marker, *result.summary_messages,
        *result.messages_to_keep, *result.attachments, *result.hook_results,
    )

async def compact_conversation(
    messages: tuple[SessionCompressionMessage, ...],
    context: TraditionalCompactContext,
    *, is_auto_compact: bool = False,
    recompaction_info: RecompactionInfo | None = None,
) -> CompactionResult:
    messages_to_summarize = messages
    for ptl_attempt in range(MAX_PTL_RETRIES + 1):
        response = await context.model.compact(
            messages=to_provider_messages(messages_to_summarize),
            prompt=get_compact_prompt(context.custom_instructions),
            model=context.model_name,
            max_output_tokens=COMPACT_MAX_OUTPUT_TOKENS,
            query_source="compact",
        )
        if not response.prompt_too_long:
            return make_compaction_result(response, messages, context)
        truncated = truncate_head_for_ptl_retry(
            messages_to_summarize, response.token_gap, context.token_estimator
        ) if ptl_attempt < MAX_PTL_RETRIES else None
        if truncated is None:
            raise ContextCompactionPromptTooLong(ERROR_MESSAGE_PROMPT_TOO_LONG)
        messages_to_summarize = truncated
    raise AssertionError("bounded PTL loop exhausted without return")
```

`make_compaction_result()` must call `format_compact_summary()`, create a boundary whose coverage lands on the last summarized complete sequence, call the continuation formatter, preserve the selected tail, build final messages, and calculate true post tokens from that final tuple.

`partial_compact_conversation(all_messages, pivot_index, context, direction)` must translate Claude's slice/filter rules exactly: `up_to` summarizes `[:pivot]`, keeps `[pivot:]` without progress/boundary/old compact-summary entries; `from` summarizes `[pivot:]` and keeps `[:pivot]` without progress entries. It reuses the same PTL loop and selects the Task 11 partial prompt for the direction.

- [ ] **Step 4: Run all compaction pure tests**

Run: `uv run pytest tests/compression/test_traditional_compact.py tests/compression/test_compact_prompt.py tests/compression/test_message_rounds.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the L3 port**

```bash
git add src/short_term_memory/compression/traditional_compact.py src/short_term_memory/compression/continuity_model.py tests/compression/test_traditional_compact.py
git commit -m "feat: port Claude traditional compaction"
```

---

### Task 13: Port L2 Auto Compact thresholds, ordering and circuit breaker

**Claude source basis:** `services/compact/autoCompact.ts`: `MAX_OUTPUT_TOKENS_FOR_SUMMARY=20_000`, `AUTOCOMPACT_BUFFER_TOKENS=13_000`, manual buffer 3,000, maximum consecutive failures 3, effective window calculation, compact/session-memory recursion guards, L4 before L3 and tracking reset/failure behavior.

**Files:**
- Create: `src/short_term_memory/compression/auto_compact.py`
- Create: `tests/compression/test_auto_compact.py`

**Interfaces:**
- Consumes: Tasks 1, 10, 12.
- Produces: `ModelProfile`, `AutoCompactContext`, `AutoCompactResult`, `effective_context_window()`, `auto_compact_threshold()`, `auto_compact_if_needed()`.

- [ ] **Step 1: Write threshold and dispatcher truth-table tests**

Test output reserve is capped at 20,000; threshold is effective window minus 13,000; manual buffer is 3,000; below threshold makes no compact calls; `query_source` compact/session_memory recurses into neither layer; three failures skip; L4 success skips L3 and resets tracking; L4 `None` invokes L3; L3 error increments exactly once; L4 over-threshold result is treated as `None`; manual compact bypasses only the automatic failure breaker.

```python
def test_auto_threshold_matches_claude_constants() -> None:
    profile = ModelProfile(context_window_tokens=200_000, max_output_tokens=32_000)
    assert effective_context_window(profile) == 180_000
    assert auto_compact_threshold(profile) == 167_000

async def test_auto_compact_tries_l4_before_l3() -> None:
    calls: list[str] = []
    result = await auto_compact_if_needed(
        messages=over_threshold_messages(), context=context(calls),
        tracking=AutoCompactTrackingState(),
    )
    assert calls == ["l4"]
    assert result.tracking.consecutive_failures == 0
```

- [ ] **Step 2: Run L2 tests and confirm red**

Run: `uv run pytest tests/compression/test_auto_compact.py -q`

Expected: FAIL because `auto_compact.py` is absent.

- [ ] **Step 3: Translate the dispatcher directly**

```python
MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000
AUTOCOMPACT_BUFFER_TOKENS = 13_000
MANUAL_COMPACT_BUFFER_TOKENS = 3_000
MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3

def effective_context_window(profile: ModelProfile) -> int:
    return profile.context_window_tokens - min(
        profile.max_output_tokens, MAX_OUTPUT_TOKENS_FOR_SUMMARY
    )

def auto_compact_threshold(profile: ModelProfile) -> int:
    return effective_context_window(profile) - AUTOCOMPACT_BUFFER_TOKENS

async def auto_compact_if_needed(
    messages: tuple[SessionCompressionMessage, ...],
    context: AutoCompactContext,
    tracking: AutoCompactTrackingState,
) -> AutoCompactResult:
    if context.query_source in {"session_memory", "compact"}:
        return AutoCompactResult.not_compacted(tracking)
    if tracking.consecutive_failures >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES:
        return AutoCompactResult.not_compacted(tracking)
    if context.token_estimator.estimate(to_provider_messages(messages)) < auto_compact_threshold(context.model_profile):
        return AutoCompactResult.not_compacted(tracking)
    l4 = await context.try_session_memory(messages)
    if l4 is not None:
        return AutoCompactResult.compacted_result(
            l4, tracking.reset_success(uuid4().hex)
        )
    try:
        l3 = await context.compact_conversation(messages, tracking)
    except Exception:
        return AutoCompactResult.not_compacted(tracking.record_failure())
    return AutoCompactResult.compacted_result(
        l3, tracking.reset_success(uuid4().hex)
    )
```

- [ ] **Step 4: Run L2/L3/L4 test group**

Run: `uv run pytest tests/compression/test_auto_compact.py tests/compression/test_session_memory_compact.py tests/compression/test_traditional_compact.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the L2 dispatcher**

```bash
git add src/short_term_memory/compression/auto_compact.py tests/compression/test_auto_compact.py
git commit -m "feat: port Claude auto compaction dispatcher"
```

---

### Task 14: Assemble one active context after the last boundary

**Claude source basis:** `query.ts` initializes with `getMessagesAfterCompactBoundary(messages)` and on compact success replaces with `buildPostCompactMessages(compactionResult)`. Generation visibility is the approved Headroom adaptation in design §9.3.

**Files:**
- Create: `src/short_term_memory/compression/context_query.py`
- Modify: `src/short_term_memory/compression/generations.py:100-190`
- Create: `tests/compression/test_context_query.py`
- Modify: `tests/compression/test_generations.py`

**Interfaces:**
- Consumes: v2 envelope, recent originals, generation expiry, Tasks 2 and 12.
- Produces: `generation_is_visible()`, `load_active_messages()`, `apply_compaction_result()`; imports projection helpers from Task 2.

- [ ] **Step 1: Write recursive assembly and late-generation tests**

Assert no revision produces fresh generations plus recent originals; with revision, output begins boundary then one summary; fully covered generation is hidden but still present in envelope; partially newer generation remains; expired generation remains hidden; recent originals at/before coverage are not duplicated; newer recent tail remains; every projection entry has a stable sequence range and atomic group; one Headroom generation's opaque messages share a group; provider projection strips all `stm_` keys; applying a second compact removes summary AB and leaves only summary ABCD; a late generation whose `through_sequence <= boundary.covered_through_sequence` never reappears.

```python
def test_second_compact_replaces_first_summary_instead_of_appending() -> None:
    first = load_active_messages(envelope_with_summary("AB"), recent_cd(), NOW)
    second = apply_compaction_result(first, compact_result("ABCD"))
    contents = [str(message.content) for message in second]
    assert any("ABCD" in content for content in contents)
    assert not any(content == "AB" for content in contents)

def test_late_covered_generation_stays_out_of_active_prompt() -> None:
    env = envelope_with_boundary(through=20, generations=[generation(2, 11, 20)])
    assert generation(2, 11, 20) not in visible_generations(env, NOW)
    assert len(env.compression_generations) == 1
```

- [ ] **Step 2: Run assembly tests and confirm red**

Run: `uv run pytest tests/compression/test_context_query.py tests/compression/test_generations.py -q`

Expected: FAIL because current assembler injects the five-category semantic summary and ignores active revision coverage.

- [ ] **Step 3: Implement boundary-aware assembly**

```python
def generation_is_visible(
    generation: CompressionGeneration,
    boundary: CompactBoundary | None,
) -> bool:
    return boundary is None or (
        generation.through_sequence > boundary.covered_through_sequence
    )

def load_active_messages(
    envelope: MemorySummaryEnvelope | None,
    recent_originals: tuple[MemoryEvent, ...],
    now: datetime,
) -> tuple[SessionCompressionMessage, ...]:
    result: list[SessionCompressionMessage] = []
    boundary = None
    if envelope is not None and envelope.active_revision is not None:
        revision = envelope.active_revision
        boundary = revision.boundary
        result.extend((boundary_message(boundary), revision.summary_message))
    if envelope is not None:
        for generation in envelope.compression_generations:
            if generation_is_fresh(generation, now) and generation_is_visible(generation, boundary):
                result.extend(
                    annotate_active_message(
                        message, from_sequence=generation.from_sequence,
                        through_sequence=generation.through_sequence,
                        group_id=f"generation:{generation.generation}",
                    )
                    for message in generation.messages
                )
    if envelope is not None and envelope.active_revision is not None:
        result.extend(envelope.active_revision.messages_to_keep)
    covered = boundary.covered_through_sequence if boundary else 0
    result.extend(
        annotate_active_message(
            event_message(event), from_sequence=event.sequence,
            through_sequence=event.sequence, group_id=f"event:{event.sequence}",
        )
        for event in recent_originals if event.sequence > covered
    )
    return deduplicate_by_sequence_and_message_identity(tuple(result))
```

`annotate_active_message()` returns a defensive copy and never mutates `CompressionGeneration.messages`. `to_provider_messages()` calls `model_dump(mode="json")` and removes only keys beginning with `stm_`; use it in L3, L4 extraction and the main Agent provider call. Delete `GenerationAssembler._semantic_summary()`. Preserve every opaque Headroom field and CCR marker extraction.

- [ ] **Step 4: Run all generation/context tests**

Run: `uv run pytest tests/compression/test_context_query.py tests/compression/test_generations.py tests/jobs/test_compression_worker.py -q`

Expected: PASS.

- [ ] **Step 5: Commit active-context assembly**

```bash
git add src/short_term_memory/compression/context_query.py src/short_term_memory/compression/generations.py tests/compression/test_context_query.py tests/compression/test_generations.py
git commit -m "feat: assemble boundary-aware active context"
```

---

### Task 15: Add ContextCoordinator and `/v1/memories/prepare`

**Claude source basis:** `query.ts` lines around initial boundary slicing, pre-request auto compact, post-compact replacement and subsequent model request. HTTP endpoint, Redis lease and CAS are project adaptations required by the standalone service.

**Files:**
- Create: `src/short_term_memory/service/context_coordinator.py`
- Modify: `src/short_term_memory/service/schemas.py`
- Modify: `src/short_term_memory/service/app.py`
- Modify: `src/short_term_memory/storage/async_redis_memory_store.py`
- Create: `tests/service/test_context_coordinator.py`
- Modify: `tests/service/test_schemas.py`
- Modify: `tests/service/test_app.py`
- Modify: `tests/storage/test_async_redis_memory_store.py`

**Interfaces:**
- Consumes: Tasks 10, 12–14, store recent originals/envelope/CAS.
- Produces: `MemoryPrepareRequest`, `MemoryPrepareResponse`, `PreparedContext`, `ContextCoordinator.prepare()`, `/v1/memories/prepare`, context-compaction lease methods.

- [ ] **Step 1: Write coordinator state-machine tests**

Test load→L2→no-compact response; L4 success persisted by CAS; L4 `None` then L3 success; CAS conflict reloads once without reusing stale result; second conflict returns current safe context; concurrent prepare gets the existing context rather than issuing a second L3 call; compact failure below hard model limit returns uncompressed context with failure tracking persisted; failure above effective window returns `context_compaction_unavailable`; response always includes Grep/Read tools and Headroom proxy data.

```python
async def test_prepare_persists_replacement_and_returns_only_post_compact_messages() -> None:
    prepared = await coordinator.prepare(
        user_id="u", session_id="s",
        model_profile=ModelProfile(
            context_window_tokens=128_000, max_output_tokens=8_192,
        ),
    )
    assert prepared.was_compacted is True
    assert prepared.messages[0].model_extra["compact_boundary"] is not None
    assert [m.content for m in prepared.messages].count("new summary") == 1
    assert "old generation" not in [m.content for m in prepared.messages]
```

- [ ] **Step 2: Run coordinator/API tests and confirm red**

Run: `uv run pytest tests/service/test_context_coordinator.py tests/service/test_schemas.py tests/service/test_app.py tests/storage/test_async_redis_memory_store.py -q`

Expected: FAIL because coordinator, schemas, route and lease methods are absent.

- [ ] **Step 3: Implement prepare with bounded CAS handling**

Request and response fields are fixed:

```python
class MemoryPrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    model_profile: ModelProfile
    query_source: Literal["main", "compact", "session_memory"] = "main"
    history_turns: int | None = Field(default=None, ge=1)

class MemoryPrepareResponse(BaseModel):
    request_id: str
    messages: list[SessionCompressionMessage]
    tools: list[dict[str, Any]]
    headroom: HeadroomProxyContext
    compacted: bool
    boundary: CompactBoundary | None
```

Coordinator algorithm: read state; call `load_active_messages`; acquire per-session context lease; call L2; if no compact, persist changed tracking only; if compact, make `ContextRevision(version=previous+1)` and CAS envelope; on one CAS miss reload and return safely without a second model call; always release compare-token lease. If final token estimate exceeds `effective_context_window()` and no compact result exists, raise `ContextCompactionUnavailableError` mapped to HTTP 503 code `context_compaction_unavailable`.

- [ ] **Step 4: Run service and storage suites**

Run: `uv run pytest tests/service tests/storage/test_async_redis_memory_store.py -q`

Expected: PASS.

- [ ] **Step 5: Commit request preparation**

```bash
git add src/short_term_memory/service/context_coordinator.py src/short_term_memory/service/schemas.py src/short_term_memory/service/app.py src/short_term_memory/storage/async_redis_memory_store.py tests/service/test_context_coordinator.py tests/service/test_schemas.py tests/service/test_app.py tests/storage/test_async_redis_memory_store.py
git commit -m "feat: add Claude-style context prepare endpoint"
```

---

### Task 16: Wire provider injection, L4 scheduling and Agent prepare calls

**Claude source basis:** `sessionMemory.ts` schedules extraction after an assistant sampling result; `query.ts` performs compact before the next model request. Runtime injection replaces Claude internal provider wiring but preserves independent `query_source` requests.

**Files:**
- Modify: `src/short_term_memory/service/runtime.py`
- Modify: `src/short_term_memory/service/memory_service.py`
- Modify: `src/short_term_memory/agent/agent_chat.py`
- Modify: `src/short_term_memory/config.py`
- Modify: `tests/service/test_runtime_lifecycle.py`
- Modify: `tests/service/test_memory_service.py`
- Modify: `tests/agent/test_agent_chat.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Consumes: `ContinuityCompactionModel`, `ContextCoordinator`, L4 queue/worker, prepare endpoint.
- Produces: runtime-owned coordinator/session-memory worker, `COMPACTION_PREPARE_TIMEOUT_SECONDS`, Agent calls prepare before main provider.

- [ ] **Step 1: Write composition and scheduling tests**

Assert `ServiceRuntime.start()` requires an injected continuity model when compaction is enabled; the same instance is passed to L4 worker and L3 context; prepare timeout defaults to 300 seconds and is independent from 10-second read timeout; assistant event writes evaluate L4 predicate and enqueue only after successful journal/Redis commit; Agent `turn()` calls `/prepare` rather than `/read`; main model gets prepare messages/tools/headroom; compact model is never called by every ordinary turn.

```python
async def test_agent_uses_prepare_output_for_main_model_call() -> None:
    await client.turn("u", "s", "continue")
    assert memory_api.paths[:2] == ["/v1/memories/write", "/v1/memories/prepare"]
    assert model.calls[0]["messages"] == memory_api.prepare_response["messages"]
    assert model.calls[0]["tools"] == [
        *memory_api.prepare_response["tools"], HEADROOM_RETRIEVE_TOOL_DEFINITION,
    ]
```

- [ ] **Step 2: Run composition tests and confirm red**

Run: `uv run pytest tests/service/test_runtime_lifecycle.py tests/service/test_memory_service.py tests/agent/test_agent_chat.py tests/test_config.py -q`

Expected: FAIL because runtime has no coordinator/L4 worker and Agent still calls read.

- [ ] **Step 3: Wire the composition root**

Replace `summary_model` and `EmptySummaryModel` runtime arguments with `continuity_model: ContinuityCompactionModel`. Add config:

```python
@dataclass(frozen=True)
class ContinuityCompactionSettings:
    enabled: bool = True
    model: str = ""
    prepare_timeout_seconds: float = 300.0

# environment keys
# CONTINUITY_COMPACTION_ENABLED=true|false
# CONTINUITY_COMPACTION_MODEL defaults to DEEPSEEK_MODEL
# COMPACTION_PREPARE_TIMEOUT_SECONDS=300
```

After an assistant event commit, compute active token growth/tool calls and enqueue `SessionMemoryJob` only if Task 8 predicate is true. Agent posts `model_profile` supplied at construction; default it from explicit constructor values, not from the memory service's Headroom ratio.

- [ ] **Step 4: Run runtime, Agent and service suites**

Run: `uv run pytest tests/service tests/agent tests/test_config.py -q`

Expected: PASS.

- [ ] **Step 5: Commit runtime integration**

```bash
git add src/short_term_memory/service/runtime.py src/short_term_memory/service/memory_service.py src/short_term_memory/agent/agent_chat.py src/short_term_memory/config.py tests/service/test_runtime_lifecycle.py tests/service/test_memory_service.py tests/agent/test_agent_chat.py tests/test_config.py
git commit -m "feat: wire continuity compaction into request flow"
```

---

### Task 17: Preserve Headroom generations while removing fake recompression semantics

**Claude source basis:** Claude compact replaces its activity chain; it does not call Headroom. This task preserves the project's original-only Headroom design and renames its storage-pressure deletion so it cannot masquerade as L3/L4 recompaction.

**Files:**
- Modify: `src/short_term_memory/jobs/redis_compression_queue.py`
- Modify: `src/short_term_memory/jobs/compression_worker.py`
- Modify: `src/short_term_memory/service/memory_service.py`
- Modify: `tests/jobs/test_redis_compression_queue.py`
- Modify: `tests/jobs/test_compression_worker.py`
- Modify: `tests/service/test_memory_service.py`

**Interfaces:**
- Consumes: v2 envelope and boundary-aware assembly.
- Produces: `CompressionJob.evict_oldest_generation`, `_execute_evict_oldest_generation()`, generation writes that preserve newer active revision/tracking.

- [ ] **Step 1: Write Headroom invariants and race tests**

Assert normal Headroom request contains only original event texts; no L3/L4 content enters it; successful generation creation no longer invokes `SummaryModel`; v2 session memory/revision/tracking survive generation CAS update; generation completed after a covering boundary is stored for CCR but invisible in active context; eviction drops only the oldest generation and retains CCR summary hashes; job JSON lazily accepts legacy `recompress=true` as `evict_oldest_generation=true` during one TTL compatibility window.

```python
async def test_generation_worker_preserves_context_revision_and_never_summarizes_summary() -> None:
    await worker.run_once()
    assert headroom.calls[0]["messages"] == [
        {"role": "user", "content": "journal original"}
    ]
    saved = await store.read_envelope("u", "s")
    assert saved.active_revision == existing.active_revision
    assert saved.session_memory == existing.session_memory
```

- [ ] **Step 2: Run worker/queue/service tests and confirm red**

Run: `uv run pytest tests/jobs/test_compression_worker.py tests/jobs/test_redis_compression_queue.py tests/service/test_memory_service.py -q`

Expected: FAIL because worker still depends on five-category `SummaryModel` and uses `recompress` naming.

- [ ] **Step 3: Remove five-category runtime and rename eviction**

Delete `EmptySummaryModel`, worker `summary_model` parameter/import and `anyio.to_thread` summary call. `_next_envelope()` copies all current v2 fields and appends/rebuilds only `compression_generations` plus compressed coverage/version. Rename the job field and method; migration validator maps the legacy field only while parsing old queued jobs. Keep marker summary catalog and recursive CCR retrieval unchanged.

```python
next_envelope = current.model_copy(update={
    "version": candidate.expected_version + 1,
    "compressed_through_sequence": candidate.through_sequence,
    "compression_generations": generations,
    "updated_at": now.isoformat(),
}) if current else MemorySummaryEnvelope(
    version=1, compressed_through_sequence=candidate.through_sequence,
    compression_generations=generations, updated_at=now.isoformat(),
)
```

- [ ] **Step 4: Run compression, service and CCR suites**

Run: `uv run pytest tests/jobs tests/compression/test_generations.py tests/compression/test_ccr_recall.py tests/service/test_memory_service.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the Headroom lifecycle cleanup**

```bash
git add src/short_term_memory/jobs/redis_compression_queue.py src/short_term_memory/jobs/compression_worker.py src/short_term_memory/service/memory_service.py tests/jobs/test_redis_compression_queue.py tests/jobs/test_compression_worker.py tests/service/test_memory_service.py
git commit -m "refactor: separate generation eviction from compaction"
```

---

### Task 18: Port the currently available L1 time-based Micro Compact

**Claude source basis:** `services/compact/microCompact.ts:evaluateTimeBasedTrigger`, `maybeTimeBasedMicrocompact`, `calculateToolResultTokens`, `estimateMessageTokens`, `TIME_BASED_MC_CLEARED_MESSAGE`. The cached-microcompact branch is intentionally excluded because it emits Anthropic cache-editing API blocks and leaves local messages unchanged; the legacy path is explicitly removed in current source.

**Files:**
- Create: `src/short_term_memory/compression/micro_compact.py`
- Modify: `src/short_term_memory/service/context_coordinator.py`
- Modify: `src/short_term_memory/config.py`
- Create: `tests/compression/test_micro_compact.py`
- Modify: `tests/service/test_context_coordinator.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Consumes: active context projection and current timestamp; never Journal storage.
- Produces: `TimeBasedMicroCompactConfig`, `evaluate_time_based_trigger()`, `microcompact_messages()`.

- [ ] **Step 1: Write direct L1 parity tests**

Cover Claude defaults `enabled=False`, `gap_threshold_minutes=60`, `keep_recent=5`; missing/non-main query source; no assistant; invalid timestamp; gap below threshold does not fire while equal/above fires; only compactable tools; keep floor of at least one even when config says zero; only older tool results replaced; already-cleared blocks unchanged; zero saved tokens returns original tuple; image/document count as 2,000; text/thinking/tool_use estimates and 4/3 padding; original input objects and Journal unchanged.

```python
def test_time_based_microcompact_clears_old_results_and_keeps_latest() -> None:
    result = microcompact_messages(
        tool_conversation(ids=("a", "b", "c"), last_assistant_at=OLD),
        query_source="main", now=NOW,
        config=TimeBasedMicroCompactConfig(
            enabled=True, gap_threshold_minutes=30, keep_recent=1,
        ),
    )
    assert tool_result_content(result.messages, "a") == TIME_BASED_MC_CLEARED_MESSAGE
    assert tool_result_content(result.messages, "b") == TIME_BASED_MC_CLEARED_MESSAGE
    assert tool_result_content(result.messages, "c") == "latest full result"
```

- [ ] **Step 2: Run L1 tests and confirm red**

Run: `uv run pytest tests/compression/test_micro_compact.py tests/service/test_context_coordinator.py tests/test_config.py -q`

Expected: FAIL because L1 module and coordinator projection hook do not exist.

- [ ] **Step 3: Translate time-based microcompact**

```python
TIME_BASED_MC_CLEARED_MESSAGE = "[Old tool result content cleared]"
IMAGE_MAX_TOKEN_SIZE = 2_000
COMPACTABLE_TOOLS = frozenset({
    "Read", "Bash", "Grep", "Glob", "WebSearch", "WebFetch", "Edit", "Write",
})

@dataclass(frozen=True)
class TimeBasedMicroCompactConfig:
    enabled: bool = False
    gap_threshold_minutes: float = 60.0
    keep_recent: int = 5

def evaluate_time_based_trigger(
    messages: tuple[SessionCompressionMessage, ...],
    query_source: str | None, *, now: datetime,
    config: TimeBasedMicroCompactConfig,
) -> TimeBasedTrigger | None:
    if not config.enabled or not query_source or not query_source.startswith("main"):
        return None
    last_assistant = next((m for m in reversed(messages) if m.role == "assistant"), None)
    if last_assistant is None or last_assistant.timestamp is None:
        return None
    gap = (now - parse_aware(last_assistant.timestamp)).total_seconds() / 60
    return TimeBasedTrigger(gap, config) if gap >= config.gap_threshold_minutes else None
```

`microcompact_messages()` performs copy-on-write replacement only in the request projection returned by prepare. Do not save cleared content to Redis or Journal. Run L1 before L2 token estimation, matching Claude `query` dependency ordering.

Map the remote-config values to explicit service environment settings `TIME_BASED_MICROCOMPACT_ENABLED=false`, `TIME_BASED_MICROCOMPACT_GAP_MINUTES=60`, and `TIME_BASED_MICROCOMPACT_KEEP_RECENT=5`; validation accepts zero only for `keep_recent` because the runtime deliberately floors it to one exactly like Claude.

- [ ] **Step 4: Run L1/coordinator/Journal regressions**

Run: `uv run pytest tests/compression/test_micro_compact.py tests/service/test_context_coordinator.py tests/storage/test_journal_store.py tests/test_config.py -q`

Expected: PASS and Journal text remains byte-for-byte unchanged.

- [ ] **Step 5: Commit the portable L1 path**

```bash
git add src/short_term_memory/compression/micro_compact.py src/short_term_memory/service/context_coordinator.py src/short_term_memory/config.py tests/compression/test_micro_compact.py tests/service/test_context_coordinator.py tests/test_config.py
git commit -m "feat: port Claude time based micro compaction"
```

---

### Task 19: Remove legacy five-category runtime, add full recursive-compaction acceptance, and document parity

**Claude source basis:** final integration of `query.ts`, `autoCompact.ts`, Session Memory, Traditional Compact, Grep/Read and portable Micro Compact. Deletions remove only the superseded project summary layer, not Headroom/CCR.

**Files:**
- Delete: `src/short_term_memory/compression/summary.py`
- Delete: `tests/compression/test_summary.py`
- Modify: `src/short_term_memory/models.py`
- Modify: `src/short_term_memory/ports.py`
- Modify: `tests/test_public_package.py`
- Create: `tests/integration/test_recursive_context_compaction.py`
- Create: `tests/integration/test_journal_grep_read_recall.py`
- Modify: `README.md`
- Modify: `.env.example`
- Modify: `docs/superpowers/specs/2026-08-14-claude-code-context-compaction-python-port-design.md`

**Interfaces:**
- Consumes: all previous tasks.
- Produces: a single supported v2 runtime path, end-to-end proof for AB→ABCD→ABCDE, source-parity documentation and migration instructions.

- [ ] **Step 1: Write the recursive compact and automatic recall acceptance tests**

The recursive fixture must execute: originals A/B leave recent zone→Headroom generations A/B→L3 summary AB; C/D first remain raw→later generation CD→second L3 receives exactly summary AB + generation CD + recent tail→summary ABCD; E still raw during worker lag→emergency L3 receives summary ABCD + eligible E raw + recent tail→summary ABCDE; late generation E is stored but hidden; all final prompt estimates are below L2 threshold; Journal A–E are unchanged; querying exact A detail makes Agent call Grep then Read and answer from original.

```python
async def test_context_can_compact_repeatedly_with_generations_and_raw_emergency_tail(stack) -> None:
    await stack.write_and_headroom("A", "B")
    first = await stack.prepare_over_threshold()
    assert first.summary == "AB"

    await stack.write_and_headroom("C", "D")
    second = await stack.prepare_over_threshold()
    assert stack.compact_model.calls[-1].semantic_inputs == ("AB", "generation CD")
    assert second.summary == "ABCD"

    await stack.write_without_waiting_for_headroom("E")
    third = await stack.prepare_over_threshold()
    assert stack.compact_model.calls[-1].semantic_inputs == ("ABCD", "raw E")
    assert third.summary == "ABCDE"
    assert stack.journal_contents() == ORIGINAL_A_TO_E
```

- [ ] **Step 2: Run integration tests and confirm red or legacy conflicts**

Run: `uv run pytest tests/integration/test_recursive_context_compaction.py tests/integration/test_journal_grep_read_recall.py -q`

Expected: before cleanup, FAIL on remaining legacy imports/fields or missing integration wiring; after all earlier tasks, only explicitly identified legacy conflicts should remain.

- [ ] **Step 3: Delete legacy summary contracts and finish documentation**

Remove `SessionSummaryPayload`, `SessionAttachmentReference`, `SessionSummaryDocument`, `SummaryModel`, `SessionSummaryGenerator` and their exports only after `rg` shows no production caller. Keep unrelated public `PreparedTurn`/`CompletionResult` API stable. README must document:

```text
L1 request-only stale tool-result clearing
→ L2 Claude threshold and L4→L3 dispatch
→ L4 ten-section background Session Memory
→ L3 nine-section recursive continuity summary
→ Headroom original-only generations + CCR
→ Journal Grep/Read exact-detail recall
```

Add a “Claude source parity” table listing every Python module, TypeScript source/function, direct translation notes and each necessary project adaptation. Change the design status from `待用户审阅` to `实施计划已批准，待执行` only if the user has approved execution by then; otherwise use `实施计划已完成，待执行确认`.

- [ ] **Step 4: Run import, unit, integration, lint and package checks**

Run in order:

```bash
uv run pytest tests/compression tests/transcript tests/jobs tests/service tests/agent -q
uv run pytest tests/integration/test_recursive_context_compaction.py tests/integration/test_journal_grep_read_recall.py -q
uv run pytest -q
uv run ruff check src tests
uv build
```

Expected: every command exits 0; complete pytest has no failures; ruff reports `All checks passed!`; wheel and sdist are created under `dist/`.

- [ ] **Step 5: Verify forbidden architecture regressions by search**

Run:

```bash
rg -n "SessionSummaryPayload|EmptySummaryModel|current_goal|confirmed_facts|_execute_recompress|should_recall|retrieve_guidance" src tests
rg -n "headroom\.compress|/v1/compress" src/short_term_memory
rg -n "journal://current-session|Grep|Read" src/short_term_memory/compression src/short_term_memory/agent
```

Expected: first command has no runtime legacy hits (migration fixtures/comments may be explicitly reviewed); second command shows only original-only Headroom worker/client call sites; third command shows continuation prompt, tool schemas and Agent dispatcher.

- [ ] **Step 6: Commit the final acceptance slice**

```bash
git add -A src/short_term_memory/compression/summary.py tests/compression/test_summary.py src/short_term_memory/models.py src/short_term_memory/ports.py tests/test_public_package.py tests/integration/test_recursive_context_compaction.py tests/integration/test_journal_grep_read_recall.py README.md .env.example docs/superpowers/specs/2026-08-14-claude-code-context-compaction-python-port-design.md
git commit -m "feat: complete Claude context compaction Python port"
```

---

## Spec coverage matrix

| 设计规格章节 | 实施任务 | 验收证据 |
|---|---|---|
| §2 源码一致边界 | 全部任务的 `Claude source basis`；Task 19 | parity 表无无法追溯分支，项目适配均有标签 |
| §5 数据模型/v2 | Task 1、14、17 | v1 懒迁移、独立 Headroom/activity coverage、CAS race tests |
| §6 L2 | Task 13、15 | 常量、阈值、递归保护、L4→L3、三次失败断路器 |
| §7 L4 | Task 8–10、16 | 十章节、组合触发、串行 extraction、15/60 秒、保尾和真实 token 复核 |
| §8 L3 | Task 11–13 | 九章节/full/partial prompt、单轮无工具请求、PTL 三次重试、替换语义 |
| §9 Headroom 融合 | Task 14、17、19 | original-only、covered generation 隐藏、late generation、eviction/CCR 不变 |
| §10–14 Journal/Grep/Read | Task 3–7 | 跨日稳定 JSONL、正则/分页/范围、HTTP session scope、Agent 自动续跑 |
| §15–17 prepare/一致性/失败 | Task 15–16 | lease、CAS、一次 stale reload、硬上限 503、独立 prepare timeout |
| §18 测试策略 | 每任务 Red/Green；Task 19 | 单元、integration、full suite、ruff、build |
| §19 阶段与 L1 | Task 18、下方 checkpoints | 当前源码 time-based clearing；cached API 分支明确排除 |
| §20 禁止方案 | Global Constraints、Task 19 搜索 | 无 generation→Headroom、无五类摘要、无向量召回、无用户手动召回 |

## Execution checkpoints

1. **Checkpoint A — exact-detail recall works independently:** Tasks 1–7. Demo an Agent autonomously calling Grep then Read against Journal before any L3/L4 rollout.
2. **Checkpoint B — recursive activity compaction works:** Tasks 8–15. Demo L4 fast path, L3 fallback, second same-chain compact and boundary replacement with fake provider.
3. **Checkpoint C — production integration preserves Headroom:** Tasks 16–17. Run all existing Headroom/CCR/Redis tests plus prepare endpoint tests.
4. **Checkpoint D — four-layer parity and acceptance:** Tasks 18–19. Verify L1 is request-only, complete AB→ABCD→ABCDE acceptance, full suite, lint and package build.

At each checkpoint, compare the implementation diff against the source-parity ledger above. If a behavior cannot be traced to a listed Claude function or an explicitly marked project adaptation, stop and remove it before continuing.
