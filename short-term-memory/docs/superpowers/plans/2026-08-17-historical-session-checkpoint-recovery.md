# Historical Session Checkpoint Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Agent 在写入历史 `session_id` 的新问题前自动恢复 Journal 中的 L3/L4 checkpoint、最近 N 轮和 sequence，并复用现有 Headroom cold rebuild/CCR 与 Journal Grep/Read 召回链路。

**Architecture:** Journal 新增幂等、不可变的 `compaction_checkpoint` 记录，L3/L4 Redis CAS 成功后立即写穿。新的 `SessionActivator` 在 `AgentChatClient.turn()` 写入前恢复 Redis 投影，并投递现有 `rebuild=True` Headroom 任务；第一轮使用 L3/L4 + 最近 N 轮，后续轮次可使用新 generation/CCR。精确原文始终由绑定历史 session 的 Grep/Read 保底。

**Tech Stack:** Python 3.11+、Pydantic v2、FastAPI、asyncio/AnyIO、Redis Lua CAS/lease、JSONL Journal、pytest/pytest-asyncio、Ruff、uv/build。

## Global Constraints

- 严格保持 `activate → write(user) → prepare → model/tools → write(assistant)` 顺序。
- 不把 Journal 全量原文注入 LLM；只恢复 checkpoint 投影和最近 N 个完整轮次。
- checkpoint 只持久化 L3/L4 状态、coverage 和 generation ID，不持久化 Headroom generation messages、CCR 原文或 hash 索引。
- Headroom cold rebuild 只接收 Journal originals，不接收 checkpoint L3/L4 或旧 generation。
- 不恢复、续期或伪造已过期 CCR marker。
- 无 checkpoint 的旧 session 恢复最近 N 轮并后台 rebuild，不同步等待 Headroom 或 continuity model。
- Journal 不可读时 activation 失败，不 reserve 新问题。
- 保留工作树中用户现有 `preview_history` 修改；不覆盖、删除或把它混入本功能的原子提交。
- 每个生产代码变更先观察对应测试因缺少行为而失败，再写最小实现。

---

## File Structure

### New files

- `src/short_term_memory/storage/compaction_checkpoint.py`: checkpoint Pydantic model、确定性 ID、envelope 恢复转换。
- `src/short_term_memory/service/session_activation.py`: 历史 session 冷激活的单一业务边界。
- `tests/storage/test_compaction_checkpoint.py`: checkpoint 建模与转换单测。
- `tests/service/test_session_activation.py`: Redis 命中/未命中、checkpoint、sequence、rebuild 和并发单测。
- `tests/integration/test_historical_session_recovery.py`: activate 到 Agent 召回的组合验收测试。

### Modified files

- `src/short_term_memory/storage/journal_store.py`: checkpoint 幂等追加、最新 checkpoint/最大 sequence 读取、record union 解析。
- `src/short_term_memory/storage/async_redis_memory_store.py`: session projection 原子恢复、sequence 读取、activation lease。
- `src/short_term_memory/ports.py`: 增加 activation 所需存储协议。
- `src/short_term_memory/compression/session_memory_compact.py`: 只有 L4 checkpoint 时的无模型 recovery revision 物化函数。
- `src/short_term_memory/service/context_coordinator.py`: L3/L4 active revision CAS 成功后 checkpoint 写穿。
- `src/short_term_memory/jobs/session_memory_worker.py`: L4 extraction CAS 成功后 checkpoint 写穿。
- `src/short_term_memory/jobs/compression_worker.py`: activation rebuild 因 envelope version 前进而 stale 时重新入队。
- `src/short_term_memory/service/schemas.py`: activation request/response contract。
- `src/short_term_memory/service/app.py`: authenticated activation endpoint。
- `src/short_term_memory/service/runtime.py`: checkpoint writer 和 activator 组装。
- `src/short_term_memory/agent/agent_chat.py`: 新问题写入前调用 activation。
- `tests/storage/test_journal_store.py`、`tests/storage/test_async_redis_memory_store.py`、`tests/storage/fake_redis.py`: 新持久化与 Lua 语义。
- `tests/service/test_context_coordinator.py`、`tests/jobs/test_session_memory_worker.py`、`tests/jobs/test_compression_worker.py`: checkpoint 写穿和 rebuild rebase。
- `tests/service/test_app.py`、`tests/agent/test_agent_chat.py`、`tests/service/test_runtime_lifecycle.py`: HTTP、Agent 顺序和 runtime wiring。
- `docs/记忆服务-业务接口文档.md`: 补充内部 activation API 和恢复语义。

---

### Task 1: Compaction checkpoint model and immutable Journal records

**Files:**
- Create: `src/short_term_memory/storage/compaction_checkpoint.py`
- Create: `tests/storage/test_compaction_checkpoint.py`
- Modify: `src/short_term_memory/storage/journal_store.py`
- Modify: `tests/storage/test_journal_store.py`
- Modify: `tests/transcript/test_journal_transcript.py`

**Interfaces:**
- Consumes: `MemorySummaryEnvelope`, `SessionMemoryRevision`, `ContextRevision`, `AutoCompactTrackingState` from `short_term_memory.models`.
- Produces: `CompactionCheckpoint`, `checkpoint_from_envelope(user_id, session_id, envelope)`, `checkpoint_to_envelope(checkpoint)`, `JournalStore.append_compaction_checkpoint(...)`, `JournalStore.read_latest_compaction_checkpoint(...)`, `JournalStore.latest_original_sequence(...)`.

- [ ] **Step 1: Write failing checkpoint model tests**

```python
def test_checkpoint_id_is_deterministic_and_does_not_store_headroom_messages():
    first = checkpoint_from_envelope("u", "s", envelope_with_l3_l4_and_generation())
    second = checkpoint_from_envelope("u", "s", envelope_with_l3_l4_and_generation())
    assert first.checkpoint_id == second.checkpoint_id
    assert first.generation_versions == (1,)
    assert "compression_generations" not in first.model_dump()


def test_checkpoint_restores_only_l3_l4_projection():
    checkpoint = checkpoint_from_envelope("u", "s", envelope_with_l3_l4_and_generation())
    restored = checkpoint_to_envelope(checkpoint)
    assert restored.active_revision == checkpoint.active_revision
    assert restored.session_memory == checkpoint.session_memory
    assert restored.compression_generations == ()
    assert restored.compressed_through_sequence == checkpoint.compressed_through_sequence
```

- [ ] **Step 2: Run the model tests and verify RED**

Run: `uv run python -m pytest tests/storage/test_compaction_checkpoint.py -q`

Expected: collection fails with `ModuleNotFoundError: short_term_memory.storage.compaction_checkpoint`.

- [ ] **Step 3: Implement the immutable checkpoint model and converters**

```python
class CompactionCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["compaction_checkpoint"] = "compaction_checkpoint"
    schema_version: Literal[1] = 1
    checkpoint_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    envelope_version: int = Field(ge=1)
    compressed_through_sequence: int = Field(ge=0)
    generation_versions: tuple[int, ...] = ()
    session_memory: SessionMemoryRevision | None = None
    active_revision: ContextRevision | None = None
    auto_compact_tracking: AutoCompactTrackingState
    created_at: str = Field(min_length=1)


def checkpoint_from_envelope(user_id: str, session_id: str,
                             envelope: MemorySummaryEnvelope) -> CompactionCheckpoint:
    state = {
        "user_id": user_id,
        "session_id": session_id,
        "envelope_version": envelope.version,
        "compressed_through_sequence": envelope.compressed_through_sequence,
        "generation_versions": tuple(g.generation for g in envelope.compression_generations),
        "session_memory": envelope.session_memory,
        "active_revision": envelope.active_revision,
        "auto_compact_tracking": envelope.auto_compact_tracking,
        "created_at": envelope.updated_at,
    }
    canonical_state = {
        **state,
        "session_memory": (
            envelope.session_memory.model_dump(mode="json")
            if envelope.session_memory else None
        ),
        "active_revision": (
            envelope.active_revision.model_dump(mode="json")
            if envelope.active_revision else None
        ),
        "auto_compact_tracking": envelope.auto_compact_tracking.model_dump(mode="json"),
    }
    canonical = json.dumps(canonical_state, sort_keys=True, separators=(",", ":"))
    return CompactionCheckpoint(
        checkpoint_id=f"sha256:{sha256(canonical.encode()).hexdigest()}", **state
    )


def checkpoint_to_envelope(checkpoint: CompactionCheckpoint) -> MemorySummaryEnvelope:
    return MemorySummaryEnvelope(
        version=checkpoint.envelope_version,
        compressed_through_sequence=checkpoint.compressed_through_sequence,
        compression_generations=(),
        session_memory=checkpoint.session_memory,
        active_revision=checkpoint.active_revision,
        auto_compact_tracking=checkpoint.auto_compact_tracking,
        updated_at=checkpoint.created_at,
    )
```

- [ ] **Step 4: Run checkpoint model tests and verify GREEN**

Run: `uv run python -m pytest tests/storage/test_compaction_checkpoint.py -q`

Expected: all tests in the file pass.

- [ ] **Step 5: Write failing Journal tests for append, idempotency, cross-day latest selection, sequence maximum and transcript exclusion**

```python
def test_append_checkpoint_is_idempotent_and_latest_crosses_days(tmp_path):
    store = JournalStore(VFSAdapter(tmp_path))
    old = checkpoint_from_envelope("u", "s", checkpoint_envelope(version=2, updated_at="2026-08-16T23:00:00+00:00"))
    new = checkpoint_from_envelope("u", "s", checkpoint_envelope(version=3, updated_at="2026-08-17T01:00:00+00:00"))
    assert store.append_compaction_checkpoint("u", "s", old).appended
    assert not store.append_compaction_checkpoint("u", "s", old).appended
    assert store.append_compaction_checkpoint("u", "s", new).appended
    assert store.read_latest_compaction_checkpoint("u", "s") == new


def test_latest_original_sequence_ignores_checkpoint_and_transcript_excludes_it(tmp_path):
    store = JournalStore(VFSAdapter(tmp_path))
    store.append_event("u", "s", memory_event(sequence=41, event_id="e41"))
    store.append_compaction_checkpoint("u", "s", checkpoint(version=7, coverage=41))
    assert store.latest_original_sequence("u", "s") == 41
    assert [line.sequence for line in JournalTranscript(store).lines("u", "s")] == [41]
```

- [ ] **Step 6: Run Journal tests and verify RED**

Run: `uv run python -m pytest tests/storage/test_journal_store.py tests/transcript/test_journal_transcript.py -q`

Expected: failures report missing `append_compaction_checkpoint`, `read_latest_compaction_checkpoint`, and `latest_original_sequence`.

- [ ] **Step 7: Extend the Journal record union and add idempotent APIs**

```python
JournalRecord = JournalMessageEvent | JournalFileEvent | CompactionCheckpoint

def append_compaction_checkpoint(self, user_id, session_id, checkpoint):
    with self._session_lock(user_id, session_id):
        for record, path in self._read_session_entries_unlocked(user_id, session_id):
            if isinstance(record, CompactionCheckpoint) and record.checkpoint_id == checkpoint.checkpoint_id:
                return JournalAppendResult(appended=False, path=path)
        at = _parse_timestamp(checkpoint.created_at)
        path = self._append_unlocked(user_id, session_id, at, checkpoint)
        return JournalAppendResult(appended=True, path=path)

def read_latest_compaction_checkpoint(self, user_id, session_id):
    checkpoints = tuple(
        record for record, _ in self._read_session_entries_unlocked(user_id, session_id)
        if isinstance(record, CompactionCheckpoint)
    )
    return max(checkpoints, key=lambda item: (item.envelope_version, item.created_at), default=None)

def latest_original_sequence(self, user_id, session_id):
    return max(
        (event.sequence for event in self.read_original_range(user_id, session_id, 1, 2**63 - 1)),
        default=0,
    )
```

Update `_read_session_entries_unlocked()` with an explicit `compaction_checkpoint` branch. Keep `read_original_range()`, `read_recent_originals()` and `JournalTranscript.lines()` message-only.

- [ ] **Step 8: Run Journal and transcript suites and verify GREEN**

Run: `uv run python -m pytest tests/storage/test_compaction_checkpoint.py tests/storage/test_journal_store.py tests/transcript -q`

Expected: all selected tests pass.

- [ ] **Step 9: Commit Task 1**

```bash
git add src/short_term_memory/storage/compaction_checkpoint.py src/short_term_memory/storage/journal_store.py tests/storage/test_compaction_checkpoint.py tests/storage/test_journal_store.py tests/transcript/test_journal_transcript.py
git commit -m "feat: persist immutable compaction checkpoints"
```

---

### Task 2: Write-through checkpoints after accepted L3 and L4 state

**Files:**
- Modify: `src/short_term_memory/service/context_coordinator.py`
- Modify: `src/short_term_memory/jobs/session_memory_worker.py`
- Modify: `src/short_term_memory/service/runtime.py`
- Modify: `tests/service/test_context_coordinator.py`
- Modify: `tests/jobs/test_session_memory_worker.py`
- Modify: `tests/service/test_runtime_lifecycle.py`

**Interfaces:**
- Consumes: `JournalStore.append_compaction_checkpoint()` and `checkpoint_from_envelope()` from Task 1.
- Produces: required `checkpoint_journal` dependency on `ContextCoordinator` and `SessionMemoryWorker`; accepted L3/L4 state is synchronously fsynced to Journal.

- [ ] **Step 1: Add failing L3 write-through tests**

```python
@pytest.mark.asyncio
async def test_prepare_checkpoints_new_active_revision_only_after_successful_cas():
    journal = RecordingCheckpointJournal()
    store = Store(envelope())
    prepared = await coordinator(store, tokens=180_000, compact=result(), checkpoint_journal=journal).prepare(
        user_id="u", session_id="s", model_profile=PROFILE
    )
    assert prepared.was_compacted
    assert len(journal.checkpoints) == 1
    assert journal.checkpoints[0].active_revision == store.envelope.active_revision


@pytest.mark.asyncio
async def test_prepare_does_not_checkpoint_tracking_only_or_failed_cas():
    journal = RecordingCheckpointJournal()
    store = Store(envelope())
    store.cas_conflict = True
    await coordinator(store, tokens=180_000, compact=result(), checkpoint_journal=journal).prepare(
        user_id="u", session_id="s", model_profile=PROFILE
    )
    assert journal.checkpoints == []
```

- [ ] **Step 2: Run L3 tests and verify RED**

Run: `uv run python -m pytest tests/service/test_context_coordinator.py -q`

Expected: construction fails because `checkpoint_journal` is not accepted or no checkpoint is appended.

- [ ] **Step 3: Persist the accepted envelope after L3/L4 context CAS**

After `compare_and_set_envelope(...)` returns true and only when `compaction_result is not None`, run the blocking Journal fsync in a worker thread:

```python
checkpoint = checkpoint_from_envelope(user_id, session_id, next_envelope)
await anyio.to_thread.run_sync(
    self.checkpoint_journal.append_compaction_checkpoint,
    user_id,
    session_id,
    checkpoint,
)
```

Do not catch Journal errors. A failed fsync must fail prepare so the caller can retry; deterministic `checkpoint_id` makes the retry safe.

- [ ] **Step 4: Run L3 tests and verify GREEN**

Run: `uv run python -m pytest tests/service/test_context_coordinator.py -q`

Expected: all context coordinator tests pass.

- [ ] **Step 5: Add failing L4 worker checkpoint tests**

```python
@pytest.mark.asyncio
async def test_session_memory_worker_checkpoints_only_after_cas_success():
    journal = RecordingJournal(events=complete_round())
    worker = session_memory_worker(journal=journal)
    result = await worker.run_once()
    assert result.state == "acked"
    assert len(journal.checkpoints) == 1
    assert journal.checkpoints[0].session_memory == worker.store.envelope.session_memory


@pytest.mark.asyncio
async def test_session_memory_worker_does_not_checkpoint_lost_cas():
    worker = session_memory_worker(cas_succeeds=False)
    await worker.run_once()
    assert worker.journals.checkpoints == []
```

- [ ] **Step 6: Run L4 tests and verify RED**

Run: `uv run python -m pytest tests/jobs/test_session_memory_worker.py -q`

Expected: checkpoint assertions fail with an empty list.

- [ ] **Step 7: Append the L4 checkpoint after successful Session Memory CAS**

```python
if not written:
    return await self._ack(lease, "stale")
checkpoint = checkpoint_from_envelope(job.user_id, job.session_id, next_envelope)
await anyio.to_thread.run_sync(
    self.journals.append_compaction_checkpoint,
    job.user_id,
    job.session_id,
    checkpoint,
)
return await self._ack(lease, "acked")
```

- [ ] **Step 8: Wire the same `JournalStore` into both producers and run tests**

Modify `ServiceRuntime.create()` so `ContextCoordinator(..., checkpoint_journal=journals)` and the existing `SessionMemoryWorker(..., journals=journals)` use the same service-owned store.

Run: `uv run python -m pytest tests/service/test_context_coordinator.py tests/jobs/test_session_memory_worker.py tests/service/test_runtime_lifecycle.py -q`

Expected: all selected tests pass.

- [ ] **Step 9: Commit Task 2**

```bash
git add src/short_term_memory/service/context_coordinator.py src/short_term_memory/jobs/session_memory_worker.py src/short_term_memory/service/runtime.py tests/service/test_context_coordinator.py tests/jobs/test_session_memory_worker.py tests/service/test_runtime_lifecycle.py
git commit -m "feat: write through L3 and L4 checkpoints"
```

---

### Task 3: Atomic Redis activation projection and session lease

**Files:**
- Modify: `src/short_term_memory/ports.py`
- Modify: `src/short_term_memory/storage/async_redis_memory_store.py`
- Modify: `tests/storage/fake_redis.py`
- Modify: `tests/storage/test_async_redis_memory_store.py`

**Interfaces:**
- Consumes: `MemoryEvent`, `MemorySummaryEnvelope`.
- Produces: `read_latest_sequence(...) -> int`, `restore_session_projection(..., latest_sequence: int, originals: tuple[MemoryEvent, ...], envelope: MemorySummaryEnvelope | None) -> bool`, `acquire_session_activation_lease(...) -> bool`, `release_session_activation_lease(...) -> bool`.

- [ ] **Step 1: Write failing atomic projection tests**

```python
@pytest.mark.asyncio
async def test_restore_session_projection_sets_history_max_not_tail_max(memory_store):
    tail = tuple(memory_event(sequence=i, event_id=f"e{i}") for i in range(91, 101))
    restored = await memory_store.restore_session_projection(
        "u", "s", latest_sequence=180, originals=tail, envelope=envelope(version=7)
    )
    assert restored
    assert await memory_store.read_latest_sequence("u", "s") == 180
    assert (await memory_store.reserve_event("u", "s", "new", "a" * 64)).sequence == 181
    assert await memory_store.read_envelope("u", "s") == envelope(version=7)


@pytest.mark.asyncio
async def test_restore_session_projection_refuses_live_or_pending_state(memory_store):
    await memory_store.reserve_event("u", "s", "live", "a" * 64)
    assert not await memory_store.restore_session_projection(
        "u", "s", latest_sequence=100, originals=(memory_event(sequence=100),), envelope=None
    )
```

- [ ] **Step 2: Run store tests and verify RED**

Run: `uv run python -m pytest tests/storage/test_async_redis_memory_store.py -q`

Expected: missing method failures for `restore_session_projection` and `read_latest_sequence`.

- [ ] **Step 3: Add one Lua transaction for sequence, bounded originals and optional envelope**

```lua
-- dream:restore-session-projection-v1
if redis.call('EXISTS', KEYS[1]) == 1
  or redis.call('LLEN', KEYS[2]) > 0
  or redis.call('EXISTS', KEYS[3]) == 1
  or redis.call('SCARD', KEYS[4]) > 0 then
  return {'not_restored'}
end
local originals = cjson.decode(ARGV[1])
for _, event in ipairs(originals) do
  local event_key = ARGV[2] .. event.event_id
  redis.call('HSET', event_key, 'digest', event.sha256, 'status', 'committed',
    'sequence', tostring(event.sequence))
  redis.call('EXPIRE', event_key, ARGV[3])
  redis.call('RPUSH', KEYS[2], cjson.encode(event))
end
redis.call('SET', KEYS[1], ARGV[4], 'EX', ARGV[3])
redis.call('EXPIRE', KEYS[2], ARGV[3])
if ARGV[5] ~= '' then
  redis.call('SET', KEYS[3], ARGV[5], 'EX', ARGV[3])
end
return {'restored'}
```

The four keys are sequence, messages, summary and pending reservations. Validate that `latest_sequence >= max(original.sequence)` before calling Lua. Serialize `envelope` as an empty string when absent.

- [ ] **Step 4: Add activation lease methods and key**

```python
async def acquire_session_activation_lease(self, user_id, session_id, token):
    return bool(await self.client.set(
        self._keys(user_id, session_id).activation_lock,
        token, nx=True, px=60_000,
    ))

async def release_session_activation_lease(self, user_id, session_id, token):
    result = await self.client.eval(
        RELEASE_LEASE_SCRIPT, 1,
        self._keys(user_id, session_id).activation_lock, token,
    )
    return self._result(result)[0] == "1"
```

Add the exact signatures to `AsyncMemoryStore` and extend `_Keys` with `activation_lock`.

- [ ] **Step 5: Extend `AsyncFakeRedis` with exact Lua semantics and run tests**

Run: `uv run python -m pytest tests/storage/test_async_redis_memory_store.py tests/storage/test_recent_originals.py -q`

Expected: all selected tests pass, including next reservation `181`.

- [ ] **Step 6: Commit Task 3**

```bash
git add src/short_term_memory/ports.py src/short_term_memory/storage/async_redis_memory_store.py tests/storage/fake_redis.py tests/storage/test_async_redis_memory_store.py
git commit -m "feat: atomically restore historical session projections"
```

---

### Task 4: Session activator with checkpoint, recent turns and Headroom rebuild

**Files:**
- Create: `src/short_term_memory/service/session_activation.py`
- Create: `tests/service/test_session_activation.py`
- Modify: `src/short_term_memory/compression/session_memory_compact.py`
- Modify: `tests/compression/test_session_memory_compact.py`

**Interfaces:**
- Consumes: Task 1 Journal APIs, Task 3 Redis APIs, existing `CompressionJob` queue, `checkpoint_to_envelope()`.
- Produces: `SessionActivationResult`, `SessionActivator.activate(user_id, session_id, history_turns)`, `materialize_session_memory_recovery_revision(...)`.

- [ ] **Step 1: Write failing local L4 materialization tests**

```python
def test_materialize_l4_recovery_revision_needs_no_model_and_keeps_recent_tail():
    revision = materialize_session_memory_recovery_revision(
        session_memory=session_memory(covered_through_sequence=80),
        recent_originals=(event(79, "old"), event(80, "answer"), event(81, "new tail")),
        now=NOW,
    )
    assert revision.boundary.strategy == "session_memory"
    assert revision.boundary.covered_through_sequence == 80
    assert "session memory" in str(revision.summary_message.content)
    assert [m.content for m in revision.messages_to_keep] == ["new tail"]
```

- [ ] **Step 2: Run the L4 test and verify RED**

Run: `uv run python -m pytest tests/compression/test_session_memory_compact.py::test_materialize_l4_recovery_revision_needs_no_model_and_keeps_recent_tail -q`

Expected: import failure for `materialize_session_memory_recovery_revision`.

- [ ] **Step 3: Implement deterministic recovery revision assembly**

Reuse the existing L4 boundary/summary formatting helpers. Do not call `ContinuityCompactionModel`:

```python
def materialize_session_memory_recovery_revision(*, session_memory, recent_originals, now):
    keep = tuple(
        annotate_active_message(
            SessionCompressionMessage(
                role=event.role.value,
                content=event.content,
                stm_timestamp=event.created_at,
            ),
            from_sequence=event.sequence,
            through_sequence=event.sequence,
            group_id=f"event:{event.sequence}",
        )
        for event in recent_originals
        if event.sequence > session_memory.covered_through_sequence
    )
    boundary = CompactBoundary(
        boundary_id=f"recovery:{session_memory.version}:{session_memory.covered_through_sequence}",
        trigger="reactive",
        strategy="session_memory",
        covered_through_sequence=session_memory.covered_through_sequence,
        pre_compact_tokens=session_memory.token_count,
        true_post_compact_tokens=session_memory.token_count,
        created_at=now.isoformat(),
    )
    return ContextRevision(
        version=1,
        boundary=boundary,
        summary_message=get_compact_user_summary_message(
            session_memory.content,
            suppress_follow_up_questions=True,
            recent_messages_preserved=bool(keep),
        ),
        messages_to_keep=keep,
        updated_at=now.isoformat(),
    )
```

Use the existing sequence annotation helper for `_event_message`; do not duplicate provider-message cleanup rules.

- [ ] **Step 4: Run the L4 suite and verify GREEN**

Run: `uv run python -m pytest tests/compression/test_session_memory_compact.py -q`

Expected: all selected tests pass.

- [ ] **Step 5: Write failing activation tests**

```python
@pytest.mark.asyncio
async def test_cold_activation_restores_checkpoint_tail_sequence_and_queues_rebuild(runtime_parts):
    parts = runtime_parts.with_journal_history(max_sequence=180, recent_from=171)
    parts.journals.append_compaction_checkpoint("u", "s", checkpoint(coverage=170, envelope_version=7))
    result = await parts.activator.activate("u", "s", history_turns=5)
    assert result.recovered
    assert result.latest_sequence == 180
    assert result.checkpoint_id is not None
    assert await parts.store.read_latest_sequence("u", "s") == 180
    restored = await parts.store.read_envelope("u", "s")
    assert restored.active_revision is not None
    assert len(parts.queue.jobs) == 1
    assert parts.queue.jobs[0].rebuild is True
    assert parts.queue.jobs[0].requested_through_sequence == 180


@pytest.mark.asyncio
async def test_warm_activation_is_idempotent_and_does_not_overwrite_redis(runtime_parts):
    parts = runtime_parts.with_live_redis(version=9)
    result = await parts.activator.activate("u", "s", history_turns=5)
    assert not result.recovered
    assert (await parts.store.read_envelope("u", "s")).version == 9
    assert parts.queue.jobs == []


@pytest.mark.asyncio
async def test_warm_activation_heals_missing_checkpoint_for_live_l3_l4(runtime_parts):
    parts = runtime_parts.with_live_redis(version=9, active_revision=True)
    assert parts.journals.read_latest_compaction_checkpoint("u", "s") is None
    result = await parts.activator.activate("u", "s", history_turns=5)
    assert not result.recovered
    healed = parts.journals.read_latest_compaction_checkpoint("u", "s")
    assert healed is not None
    assert healed.envelope_version == 9


@pytest.mark.asyncio
async def test_legacy_session_restores_recent_turns_without_checkpoint(runtime_parts):
    parts = runtime_parts.with_journal_history(max_sequence=40, recent_from=31)
    result = await parts.activator.activate("u", "s", history_turns=5)
    assert result.recovered
    assert await parts.store.read_latest_sequence("u", "s") == 40
    assert await parts.store.read_envelope("u", "s") is None
    assert len(await parts.store.read_recent_originals("u", "s", 5)) <= 10
```

- [ ] **Step 6: Run activation tests and verify RED**

Run: `uv run python -m pytest tests/service/test_session_activation.py -q`

Expected: collection fails because `SessionActivator` does not exist.

- [ ] **Step 7: Implement `SessionActivator`**

```python
@dataclass(frozen=True)
class SessionActivationResult:
    recovered: bool
    latest_sequence: int
    checkpoint_id: str | None
    rebuild_queued: bool


class SessionActivator:
    async def activate(self, user_id: str, session_id: str,
                       history_turns: int | None = None) -> SessionActivationResult:
        turns = history_turns or self.history_turns
        if await self.store.read_latest_sequence(user_id, session_id) > 0:
            return await self._warm_result(user_id, session_id)
        token = uuid.uuid4().hex
        if not await self.store.acquire_session_activation_lease(user_id, session_id, token):
            return await self._wait_for_projection(user_id, session_id)
        try:
            if await self.store.read_latest_sequence(user_id, session_id) > 0:
                return await self._warm_result(user_id, session_id)
            checkpoint, latest, recent = await self._read_journal(user_id, session_id, turns)
            restored_envelope = self._restore_envelope(checkpoint, recent)
            restored = await self.store.restore_session_projection(
                user_id, session_id, latest_sequence=latest,
                originals=recent, envelope=restored_envelope,
            )
            if not restored:
                return await self._warm_result(user_id, session_id)
            queued = await self._queue_rebuild(user_id, session_id, latest, restored_envelope)
            return SessionActivationResult(True, latest,
                checkpoint.checkpoint_id if checkpoint else None, queued)
        finally:
            await self.store.release_session_activation_lease(user_id, session_id, token)
```

Use `anyio.to_thread.run_sync` for Journal reads and checkpoint fsync. `_warm_result()` reads the live envelope and idempotently appends `checkpoint_from_envelope(...)` when L3 `active_revision` or L4 `session_memory` exists but the latest Journal checkpoint has another deterministic ID; this heals the CAS-success/Journal-failure window from Task 2. Validate `checkpoint.compressed_through_sequence <= latest`; ignore a corrupt checkpoint but retain recent-N recovery. When `latest == 0`, return a non-recovered empty result and do not enqueue.

`_wait_for_projection()` is bounded to the configured activation timeout and polls `read_latest_sequence()` every 100 ms. It returns the warm result as soon as sequence appears; on timeout it raises `SessionActivationUnavailableError("historical session activation timed out")`. It never performs a second concurrent Journal restore.

- [ ] **Step 8: Add lease contention, Journal failure and sequence corruption tests**

```python
@pytest.mark.asyncio
async def test_journal_failure_aborts_activation_without_redis_projection(parts):
    parts.journals.read_error = OSError("journal unavailable")
    with pytest.raises(OSError, match="journal unavailable"):
        await parts.activator.activate("u", "s")
    assert await parts.store.read_latest_sequence("u", "s") == 0


@pytest.mark.asyncio
async def test_checkpoint_coverage_beyond_journal_is_ignored(parts):
    parts.journals.seed_checkpoint(checkpoint(coverage=200))
    parts.journals.seed_originals(through=180)
    result = await parts.activator.activate("u", "s")
    assert result.recovered
    assert result.checkpoint_id is None
    assert await parts.store.read_envelope("u", "s") is None
```

- [ ] **Step 9: Run activation and adjacent recovery suites and verify GREEN**

Run: `uv run python -m pytest tests/service/test_session_activation.py tests/service/test_memory_service.py tests/storage/test_async_redis_memory_store.py -q`

Expected: all selected tests pass.

- [ ] **Step 10: Commit Task 4**

```bash
git add src/short_term_memory/service/session_activation.py src/short_term_memory/compression/session_memory_compact.py tests/service/test_session_activation.py tests/compression/test_session_memory_compact.py
git commit -m "feat: activate historical sessions before writes"
```

---

### Task 5: Preserve Headroom rebuild across envelope-version races

**Files:**
- Modify: `src/short_term_memory/jobs/redis_compression_queue.py`
- Modify: `src/short_term_memory/jobs/compression_worker.py`
- Modify: `tests/jobs/test_redis_compression_queue.py`
- Modify: `tests/jobs/test_compression_worker.py`

**Interfaces:**
- Consumes: activation `CompressionJob(rebuild=True)` from Task 4.
- Produces: `CompressionJob.rebased(expected_version: int) -> CompressionJob`; stale rebuilds re-enter the queue with the same coverage and a deterministic version-specific job ID.

- [ ] **Step 1: Write a failing job rebase unit test**

```python
def test_rebased_rebuild_preserves_scope_and_coverage_but_changes_identity():
    old = CompressionJob(job_id="old", user_id="u", session_id="s",
        expected_version=2, requested_through_sequence=180, rebuild=True)
    new = old.rebased(expected_version=3)
    assert new.expected_version == 3
    assert new.requested_through_sequence == 180
    assert new.rebuild is True
    assert new.job_id != old.job_id
```

- [ ] **Step 2: Run queue tests and verify RED**

Run: `uv run python -m pytest tests/jobs/test_redis_compression_queue.py -q`

Expected: `AttributeError: 'CompressionJob' object has no attribute 'rebased'`.

- [ ] **Step 3: Implement deterministic rebuild rebasing**

```python
def rebased(self, *, expected_version: int) -> "CompressionJob":
    if not self.rebuild:
        raise ValueError("only rebuild jobs can be rebased")
    identity = (
        f"{self.user_id}\n{self.session_id}\n{expected_version}\n"
        f"{self.requested_through_sequence}\nTrue\n{self.evict_oldest_generation}"
    )
    return self.model_copy(update={
        "job_id": f"memory-{uuid5(NAMESPACE_URL, identity).hex}",
        "expected_version": expected_version,
    })
```

- [ ] **Step 4: Add a failing worker race test**

```python
@pytest.mark.asyncio
async def test_stale_activation_rebuild_requeues_against_new_envelope_version(worker):
    lease = lease_for(rebuild_job(expected_version=1, through=180))
    worker.store.envelope = envelope(version=2)
    result = await worker._execute(lease, NOW)
    assert result.state == "stale"
    assert worker.queue.enqueued[-1].expected_version == 2
    assert worker.queue.enqueued[-1].requested_through_sequence == 180
```

- [ ] **Step 5: Run worker test and verify RED**

Run: `uv run python -m pytest tests/jobs/test_compression_worker.py::test_stale_activation_rebuild_requeues_against_new_envelope_version -q`

Expected: no replacement job is recorded.

- [ ] **Step 6: Requeue only stale rebuild jobs that still need coverage**

Before acknowledging the version mismatch, requeue when no fresh generation spans `1..requested_through_sequence`:

```python
if current_version != job.expected_version:
    if job.rebuild and not self._has_fresh_rebuild_coverage(envelope, job, now):
        await self.queue.enqueue(job.rebased(expected_version=current_version))
    return await self._ack(lease, "stale")
```

Do not rebase incremental compression or eviction jobs. Do not requeue if a fresh rebuild already covers the requested range.

- [ ] **Step 7: Run queue and worker suites and verify GREEN**

Run: `uv run python -m pytest tests/jobs/test_redis_compression_queue.py tests/jobs/test_compression_worker.py -q`

Expected: all selected tests pass.

- [ ] **Step 8: Commit Task 5**

```bash
git add src/short_term_memory/jobs/redis_compression_queue.py src/short_term_memory/jobs/compression_worker.py tests/jobs/test_redis_compression_queue.py tests/jobs/test_compression_worker.py
git commit -m "fix: preserve cold rebuild across envelope races"
```

---

### Task 6: Activation HTTP API, runtime wiring and pre-write Agent ordering

**Files:**
- Modify: `src/short_term_memory/service/schemas.py`
- Modify: `src/short_term_memory/service/app.py`
- Modify: `src/short_term_memory/service/runtime.py`
- Modify: `src/short_term_memory/agent/agent_chat.py`
- Modify: `tests/service/test_app.py`
- Modify: `tests/service/test_runtime_lifecycle.py`
- Modify: `tests/agent/test_agent_chat.py`

**Interfaces:**
- Consumes: `SessionActivator.activate()` from Task 4.
- Produces: `POST /v1/memories/activate`, `MemoryActivateRequest`, `MemoryActivateResponse`, Agent call ordering.

- [ ] **Step 1: Write failing API contract tests**

```python
def test_activate_endpoint_is_authenticated_and_returns_recovery_state(client, runtime):
    response = client.post("/v1/memories/activate", headers=auth(), json={
        "user_id": "u", "session_id": "historical", "history_turns": 5,
    })
    assert response.status_code == 200
    assert response.json() == {
        "request_id": response.headers["x-request-id"],
        "recovered": True,
        "latest_sequence": 180,
        "checkpoint_id": "sha256:checkpoint",
        "rebuild_queued": True,
    }
```

- [ ] **Step 2: Run app tests and verify RED**

Run: `uv run python -m pytest tests/service/test_app.py -q`

Expected: endpoint returns 404.

- [ ] **Step 3: Add frozen request/response schemas and endpoint**

```python
class MemoryActivateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    history_turns: int | None = Field(default=None, ge=1)


class MemoryActivateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: str = Field(min_length=1)
    recovered: bool
    latest_sequence: int = Field(ge=0)
    checkpoint_id: str | None = None
    rebuild_queued: bool
```

Endpoint:

```python
@app.post("/v1/memories/activate", response_model=MemoryActivateResponse,
          responses=_ERROR_RESPONSES, dependencies=[Depends(authenticate)])
async def activate_memory(request: Request, body: MemoryActivateRequest):
    result = await app.state.session_activator.activate(
        body.user_id, body.session_id, body.history_turns
    )
    return MemoryActivateResponse(request_id=request.state.request_id, **asdict(result))
```

- [ ] **Step 4: Wire `SessionActivator` into runtime and app state**

Create it from the existing `store`, `journals`, `queue`, settings and the same deterministic compression-job factory used by `MemoryService`. Expose `runtime.session_activator`; `install()` assigns `app.state.session_activator`.

- [ ] **Step 5: Run API/runtime tests and verify GREEN**

Run: `uv run python -m pytest tests/service/test_app.py tests/service/test_runtime_lifecycle.py -q`

Expected: all selected tests pass.

- [ ] **Step 6: Write a failing Agent ordering test**

```python
@pytest.mark.asyncio
async def test_turn_activates_historical_session_before_user_write():
    transport = RecordingTransport()
    client = AgentChatClient(memory_api_url="http://memory", model_call=final_model,
                             http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)))
    await client.turn("u", "historical", "continue")
    assert transport.paths[:3] == [
        "/v1/memories/activate",
        "/v1/memories/write",
        "/v1/memories/prepare",
    ]
```

- [ ] **Step 7: Run Agent test and verify RED**

Run: `uv run python -m pytest tests/agent/test_agent_chat.py::test_turn_activates_historical_session_before_user_write -q`

Expected: first path is `/v1/memories/write`, not `/v1/memories/activate`.

- [ ] **Step 8: Call activation before constructing/reserving the user event**

At the top of `turn()`:

```python
await self._post("/v1/memories/activate", {
    "user_id": user_id,
    "session_id": session_id,
    "history_turns": history_turns,
})
```

Keep the existing preview method untouched. If activation raises, do not issue `/write`.

- [ ] **Step 9: Run Agent and service suites and verify GREEN**

Run: `uv run python -m pytest tests/agent/test_agent_chat.py tests/service/test_app.py tests/service/test_runtime_lifecycle.py -q`

Expected: all selected tests pass.

- [ ] **Step 10: Commit Task 6 without staging unrelated preview changes**

Because `schemas.py`, `agent_chat.py` and the Agent tests already contain user changes, inspect `git diff` and stage only the activation hunks with `git add -p`; do not stage `history`/`preview_history` hunks.

```bash
git add src/short_term_memory/service/app.py src/short_term_memory/service/runtime.py
git add -p src/short_term_memory/service/schemas.py src/short_term_memory/agent/agent_chat.py tests/agent/test_agent_chat.py
git add tests/service/test_app.py tests/service/test_runtime_lifecycle.py
git diff --cached --check
git commit -m "feat: activate sessions before agent writes"
```

---

### Task 7: Historical recovery integration and exact recall acceptance

**Files:**
- Create: `tests/integration/test_historical_session_recovery.py`
- Modify: `tests/service/test_memory_service.py`
- Modify: `tests/agent/test_agent_chat.py`

**Interfaces:**
- Consumes: activation API, checkpoint restore, `prepare`, Headroom rebuild queue, `headroom_retrieve`, Journal Grep/Read.
- Produces: regression coverage for the originally reproduced empty-history and duplicate-sequence failures.

- [ ] **Step 1: Add the failing cold-history regression test before enabling the full path**

```python
@pytest.mark.asyncio
async def test_expired_redis_history_recovers_context_before_first_new_question(stack):
    await stack.seed_journal_session("u", "old", through=180)
    await stack.seed_checkpoint("u", "old", coverage=170, summary="RECOVERED CONTINUITY")
    await stack.expire_redis_session("u", "old")
    answer = await stack.agent.turn("u", "old", "what did we decide?")
    first_model_messages = stack.model.calls[0]["messages"]
    assert any("RECOVERED CONTINUITY" in str(m["content"]) for m in first_model_messages)
    assert any("what did we decide?" in str(m["content"]) for m in first_model_messages)
    assert await stack.store.read_latest_sequence("u", "old") == 182
    assert len({event.sequence for event in stack.journals.read_original_range("u", "old", 1, 2**63 - 1)}) == 182
```

- [ ] **Step 2: Run the integration test and verify RED**

Run: `uv run python -m pytest tests/integration/test_historical_session_recovery.py::test_expired_redis_history_recovers_context_before_first_new_question -q`

Expected: fail until the runtime fixture uses activation and checkpoint recovery end to end.

- [ ] **Step 3: Complete only the missing fixture/wiring required by the test**

Use `AsyncFakeRedis`, real `JournalStore`, real `AsyncRedisMemoryStore`, real `SessionActivator`, existing context coordinator and a deterministic fake model. Do not bypass HTTP ordering by calling service methods directly in this acceptance test.

- [ ] **Step 4: Add Headroom rebuild and CCR/Grep fallback acceptance**

```python
@pytest.mark.asyncio
async def test_history_activation_rebuilds_headroom_then_can_recall_or_grep(stack):
    await stack.seed_expired_historical_session()
    await stack.activate("u", "old")
    job = await stack.queue.claim()
    assert job.rebuild and job.requested_through_sequence == stack.journal_max_sequence
    await stack.worker.execute(job)
    envelope = await stack.store.read_envelope("u", "old")
    assert envelope.compression_generations
    assert extract_marker_hashes(envelope.compression_generations[0].messages)
    assert await stack.agent_recall_marker("u", "old") == "EXACT CCR ORIGINAL"
    stack.ccr.fail_all = True
    assert await stack.agent_grep_then_read("u", "old", "TTL") == "TTL is 43200"
```

- [ ] **Step 5: Run integration and recall suites and verify GREEN**

Run: `uv run python -m pytest tests/integration/test_historical_session_recovery.py tests/compression/test_ccr_recall.py tests/transcript tests/agent/test_agent_chat.py -q`

Expected: all selected tests pass.

- [ ] **Step 6: Replace the insufficient history-mode unit assertion**

In the user-owned `test_read_history_mode_returns_compressed_view_not_journal_originals`, preserve the preview behavior but add a separate production activation assertion. Do not make `/read?history=true` responsible for cold activation; `/activate` owns that behavior.

```python
@pytest.mark.asyncio
async def test_activation_not_history_preview_owns_cold_restore(service_stack):
    result = await service_stack.activator.activate("u", "s", history_turns=10)
    assert result.recovered
    assert await service_stack.store.read_latest_sequence("u", "s") == 7
```

- [ ] **Step 7: Run all touched suites and verify GREEN**

Run: `uv run python -m pytest tests/storage tests/compression tests/jobs tests/service tests/agent tests/transcript tests/integration/test_historical_session_recovery.py -q`

Expected: zero failures; environment-gated integration tests may remain explicitly skipped.

- [ ] **Step 8: Commit Task 7 with patch staging for user-owned files**

```bash
git add tests/integration/test_historical_session_recovery.py
git add -p tests/service/test_memory_service.py tests/agent/test_agent_chat.py
git diff --cached --check
git commit -m "test: verify historical session recovery and recall"
```

---

### Task 8: API documentation and final verification

**Files:**
- Modify: `docs/记忆服务-业务接口文档.md`
- Verify: all files changed in Tasks 1–7

**Interfaces:**
- Consumes: completed implementation.
- Produces: operator-facing activation contract and verified release state.

- [ ] **Step 1: Document the exact activation request and response**

Add:

```markdown
### POST /v1/memories/activate

在向历史 session 写入新消息前调用。Redis 过期时，服务从 Journal 恢复最新 L3/L4 checkpoint、最近 N 个完整轮次和历史最大 sequence，然后后台投递 Headroom cold rebuild。

Request:
{"user_id":"u-1","session_id":"s-old","history_turns":10}

Response:
{"request_id":"...","recovered":true,"latest_sequence":180,
 "checkpoint_id":"sha256:...","rebuild_queued":true}
```

State explicitly that checkpoint/CCR data are not returned to end users and that exact recall uses session-scoped Grep/Read.

- [ ] **Step 2: Run formatting and static checks**

Run: `uv run ruff format --check src tests`

Expected: exit 0.

Run: `uv run ruff check src tests`

Expected: exit 0 with no diagnostics.

- [ ] **Step 3: Run the complete test suite**

Run: `uv run python -m pytest -q`

Expected: zero failures; only explicitly environment-gated tests are skipped.

- [ ] **Step 4: Build the package**

Run: `uv build`

Expected: exit 0 and both sdist and wheel are created under `dist/`.

- [ ] **Step 5: Audit the final diff and user-owned worktree changes**

Run:

```bash
git diff --check
git status --short
git diff -- src/short_term_memory/agent/agent_chat.py src/short_term_memory/service/memory_service.py src/short_term_memory/service/schemas.py tests/agent/test_agent_chat.py tests/service/test_memory_service.py examples/chat_loop.py
```

Expected: no whitespace errors. Existing preview-history changes remain present unless their individual activation hunks were intentionally committed with patch staging; unrelated diagnostic scripts and reports remain untouched.

- [ ] **Step 6: Commit documentation only**

```bash
git add docs/记忆服务-业务接口文档.md
git commit -m "docs: describe historical session activation"
```

- [ ] **Step 7: Record final evidence**

Report the exact test pass/skip counts, Ruff results, build artifacts, commits created, and any pre-existing uncommitted files that were deliberately preserved.
