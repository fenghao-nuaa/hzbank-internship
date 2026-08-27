# Headroom Memory HTTP and DeepSeek Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two production-oriented HTTP memory APIs, sequence-aware original-only Headroom compression, 30-day journal retention, an independent DeepSeek-through-Headroom example, and deterministic/real/load acceptance tests.

**Architecture:** Keep the existing synchronous SDK public API compatible while adding a focused asynchronous service layer. New HTTP writes use a Redis sequence/idempotency reservation, durable journals, and an atomic Redis commit; a separate worker consumes persistent compression jobs and stores opaque Headroom generations using compare-and-swap. Reads assemble fresh Headroom generations plus recent originals, while DeepSeek remains a separate official-SDK call whose base URL is the Headroom Proxy.

**Tech Stack:** Python 3.11-3.13, Pydantic 2, FastAPI, Uvicorn, redis-py `redis.asyncio`, httpx, Prometheus client, pytest/pytest-asyncio, optional OpenAI SDK for the DeepSeek example, Docker Redis, Headroom 0.33.0.

## Global Constraints

- Expose exactly two business endpoints: `POST /v1/memories/write` and `POST /v1/memories/read`.
- Operational `/health`, `/ready`, `/metrics`, and `/openapi.json` endpoints must not read or write business memory.
- Memory endpoints must never call DeepSeek or receive/store `DEEPSEEK_API_KEY`.
- The DeepSeek example defaults to `deepseek-v4-flash` and uses the official SDK with Headroom Proxy as `base_url`.
- `HEADROOM_COMPRESSION_MODEL` defaults to `deepseek-v4-flash` so background compression and the chat caller use the same model family.
- Headroom exclusively owns compression algorithms, CCR cache/backend, hashes, `headroom_retrieve`, original retrieval, and model continuation.
- Never import Headroom or read/write its SQLite/memory backend from this package.
- Never submit a Headroom-returned message to `/v1/compress`; compression inputs contain original Redis/journal events only.
- Redis session and Headroom CCR TTL default to exactly `43200` seconds.
- Journal retention defaults to exactly `30` days.
- Default API concurrency limit is `100`, Redis pool size is `200`, API worker recommendation is `4`, and compression worker concurrency is `8`.
- Release SLOs: 100-concurrent writes p95 <= 150 ms and p99 <= 300 ms; warm reads p95 <= 100 ms and p99 <= 200 ms; journal recovery p95 <= 1 s; CCR retrieval p95 <= 100 ms; 20K-token compression p95 <= 5 s; DeepSeek TTFT p95 <= 3 s measured separately.
- Preserve existing synchronous SDK imports and tests unless an explicitly replaced behavior is covered by a migration test.
- Real Headroom/Redis/DeepSeek tests are opt-in. Missing services or credentials must report `skipped`, never pass.

---

## File Structure

### Shared domain and configuration

- `src/short_term_memory/config.py`: extend environment-backed API, queue, retention, and DeepSeek-public configuration.
- `src/short_term_memory/models.py`: add original event, reservation, compression generation, and service result models while retaining legacy models.
- `src/short_term_memory/ports.py`: add async storage, journal, compression, queue, and clock protocols.

### Storage and compression

- `src/short_term_memory/storage/journal_store.py`: append/read idempotent sequence-bearing events and preserve legacy journal records.
- `src/short_term_memory/storage/journal_retention.py`: delete expired journal files safely.
- `src/short_term_memory/storage/async_redis_memory_store.py`: Redis keys and Lua-backed reserve/commit/CAS operations.
- `src/short_term_memory/compression/generations.py`: original-only candidate selection and read-time generation assembly.
- `src/short_term_memory/compression/async_headroom_client.py`: shared bounded async `/v1/compress` adapter.
- `src/short_term_memory/jobs/redis_compression_queue.py`: persistent job enqueue/lease/ack/retry.
- `src/short_term_memory/jobs/compression_worker.py`: independently runnable bounded worker.

### HTTP service

- `src/short_term_memory/service/schemas.py`: public request/response schemas.
- `src/short_term_memory/service/memory_service.py`: write/read use cases and timing.
- `src/short_term_memory/service/auth.py`: bearer authentication without secret leakage.
- `src/short_term_memory/service/metrics.py`: content-free Prometheus metrics.
- `src/short_term_memory/service/runtime.py`: async dependency lifecycle and wiring.
- `src/short_term_memory/service/app.py`: FastAPI routes, middleware, and error mapping.
- `src/short_term_memory/cli.py`: API and compression-worker commands.

### Examples, fixtures, and tests

- `examples/deepseek_chat.py`: official-SDK DeepSeek call through returned Headroom Proxy details.
- `tests/fixtures/memory_cases/`: conversation, code, document, and skill originals with anchors.
- `tests/factories.py`: complete reusable constructors for events, envelopes, requests, and HTTP payloads.
- `tests/service/`, `tests/storage/`, `tests/compression/`, `tests/jobs/`: deterministic TDD coverage.
- `tests/integration/test_memory_http_redis.py`: opt-in real Redis API flow.
- `tests/integration/test_memory_headroom_cases.py`: opt-in real Headroom compression/retrieval.
- `tests/integration/test_memory_deepseek_e2e.py`: opt-in real DeepSeek three-turn flow.
- `scripts/load_test_memory_api.py`: 100-concurrency SLO gate.

---

### Task 1: Dependencies, Settings, and Shared Domain Models

**Files:**
- Modify: `pyproject.toml`
- Modify: `.env.example`
- Modify: `src/short_term_memory/config.py`
- Modify: `src/short_term_memory/models.py`
- Modify: `src/short_term_memory/ports.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_models.py`
- Create: `tests/service/__init__.py`
- Create: `tests/service/test_schemas.py`
- Create: `tests/factories.py`
- Create: `src/short_term_memory/service/__init__.py`
- Create: `src/short_term_memory/service/schemas.py`

**Interfaces:**
- Produces: `MemoryEvent`, `MemoryContentType`, `EventReservation`, `CompressionGeneration`, `MemorySummaryEnvelope`.
- Produces: `MemoryWriteRequest`, `MemoryWriteResponse`, `MemoryReadRequest`, `MemoryReadResponse`.
- Produces settings: `ApiSettings`, `JournalSettings`, `CompressionQueueSettings`, `DeepSeekPublicSettings` nested in `ShortTermMemorySettings`.
- Consumes: existing `SessionSummaryPayload`, `HeadroomServiceSettings`, and Redis settings.

- [ ] **Step 1: Write failing settings and schema tests**

Add tests with exact defaults and secret boundaries:

```python
def test_http_memory_defaults_are_teacher_visible(monkeypatch):
    settings = load_settings()
    assert settings.api.concurrency_limit == 100
    assert settings.api.redis_pool_size == 200
    assert settings.api.max_body_bytes == 10 * 1024 * 1024
    assert settings.journal.retention_days == 30
    assert settings.compression_queue.worker_concurrency == 8
    assert settings.headroom_service.ccr_ttl_seconds == 43_200
    assert settings.headroom_service.compression_model == "deepseek-v4-flash"
    assert settings.deepseek_public.model == "deepseek-v4-flash"
    assert settings.deepseek_public.api_url == "https://api.deepseek.com"


def test_write_schema_accepts_all_four_content_types():
    request = MemoryWriteRequest.model_validate(
        {
            "user_id": "u1",
            "session_id": "s1",
            "events": [
                {
                    "event_id": f"e-{kind}",
                    "role": "user",
                    "content_type": kind,
                    "content": f"original-{kind}",
                    "metadata": {},
                }
                for kind in ("conversation", "code", "document", "skill")
            ],
        }
    )
    assert [event.content_type.value for event in request.events] == [
        "conversation", "code", "document", "skill"
    ]


def test_effective_config_never_contains_secrets():
    assert set(EffectiveMemoryConfig.model_fields) == {
        "history_turns", "redis_ttl_seconds", "ccr_ttl_seconds",
        "journal_retention_days", "trigger_ratio", "policy_version",
    }
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_config.py tests/test_models.py tests/service/test_schemas.py
```

Expected: FAIL because the API settings and memory service schemas do not exist.

- [ ] **Step 3: Add optional dependency groups and minimal models**

Add these dependency groups without adding Headroom to this package:

```toml
[project.optional-dependencies]
api = [
  "fastapi>=0.141,<1",
  "prometheus-client>=0.23,<1",
  "uvicorn[standard]>=0.52,<1",
]
deepseek = [
  "openai>=2.51,<3",
]
dev = [
  "build>=1,<2",
  "pytest>=9,<10",
  "pytest-asyncio>=1.3,<2",
  "ruff==0.15.10",
]

[project.scripts]
short-term-memory-api = "short_term_memory.cli:api_main"
short-term-memory-worker = "short_term_memory.cli:worker_main"
```

Add immutable domain types equivalent to:

```python
class MemoryContentType(str, Enum):
    CONVERSATION = "conversation"
    CODE = "code"
    DOCUMENT = "document"
    SKILL = "skill"


class MemoryEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sequence: int = Field(ge=1)
    event_id: str = Field(min_length=1, max_length=200)
    role: JournalRole
    content_type: MemoryContentType
    content: str = Field(min_length=1)
    metadata: dict[str, str] = Field(default_factory=dict)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: str


class CompressionGeneration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    generation: int = Field(ge=1)
    from_sequence: int = Field(ge=1)
    through_sequence: int = Field(ge=1)
    messages: list[SessionCompressionMessage]
    tokens_before: int = Field(ge=0)
    tokens_after: int = Field(ge=0)
    created_at: str
    ccr_expires_at: str

    @model_validator(mode="after")
    def ordered_range(self):
        if self.through_sequence < self.from_sequence:
            raise ValueError("through_sequence must be >= from_sequence")
        return self
```

`MemorySummaryEnvelope` includes the existing five semantic fields, `version`, `compressed_through_sequence`, and `compression_generations`. API event input omits server-generated sequence, digest, and timestamp; response models match the approved JSON contract.

Create `tests/factories.py` with reusable constructors used by later tasks:

```python
from datetime import datetime, timezone
from hashlib import sha256

from short_term_memory.models import MemoryEvent, MemorySummaryEnvelope
from short_term_memory.service.schemas import MemoryReadRequest, MemoryWriteRequest


def memory_event(*, sequence=1, event_id="event-1", content="original", created_at=None):
    timestamp = created_at or datetime(2026, 8, 6, tzinfo=timezone.utc)
    return MemoryEvent(
        sequence=sequence,
        event_id=event_id,
        role="user",
        content_type="conversation",
        content=content,
        metadata={},
        sha256=sha256(content.encode("utf-8")).hexdigest(),
        created_at=timestamp.isoformat(),
    )


def envelope(*, version=1, through=0, generations=None):
    return MemorySummaryEnvelope(
        version=version,
        compressed_through_sequence=through,
        compression_generations=list(generations or []),
        current_goal=[], preferences=[], confirmed_facts=[], pending_items=[],
        attachment_references=[], updated_at="2026-08-06T00:00:00+00:00",
    )


def write_request(event_id="event-1", content="original"):
    return MemoryWriteRequest.model_validate({
        "user_id": "u", "session_id": "s", "session_seconds": 0,
        "events": [{"event_id": event_id, "role": "user",
                    "content_type": "conversation", "content": content, "metadata": {}}],
    })


def read_request():
    return MemoryReadRequest(user_id="u", session_id="s", history_turns=10,
                             include_effective_config=True)


def write_payload(content="original"):
    return write_request(content=content).model_dump(mode="json")


def read_payload():
    return read_request().model_dump(mode="json")


def scope_headers(label="s"):
    return {"x-headroom-user-id": f"u-{label}",
            "x-headroom-session-id": f"s-{label}",
            "x-headroom-project-id": f"p-{label}"}
```

- [ ] **Step 4: Load every new environment variable with validation**

Implement dataclasses and parsing for:

```text
MEMORY_API_HOST=127.0.0.1
MEMORY_API_PORT=8080
MEMORY_API_WORKERS=4
MEMORY_API_CONCURRENCY_LIMIT=100
MEMORY_API_REDIS_POOL_SIZE=200
MEMORY_API_MAX_BODY_BYTES=10485760
MEMORY_API_REQUEST_TIMEOUT_SECONDS=10
MEMORY_WRITE_MAX_BATCH_EVENTS=100
MEMORY_API_AUTH_TOKEN=
JOURNAL_RETENTION_DAYS=30
HEADROOM_CCR_REFRESH_SECONDS=3600
HEADROOM_MAX_COMPRESSION_SEGMENTS=8
HEADROOM_COMPRESSION_WORKERS=8
HEADROOM_QUEUE_CAPACITY=10000
HEADROOM_COMPRESSION_MODEL=deepseek-v4-flash
DEEPSEEK_API_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
```

Keep `DEEPSEEK_API_KEY` out of `ShortTermMemorySettings`. Redact credentials from `REDIS_URL` when forming effective configuration.

- [ ] **Step 5: Run targeted and legacy tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_config.py tests/test_models.py tests/service/test_schemas.py
PYTHONPATH=src .venv/bin/python -m pytest -q
```

Expected: all tests PASS; the full suite may only skip existing opt-in integration tests.

- [ ] **Step 6: Commit Task 1**

```bash
git add pyproject.toml .env.example src/short_term_memory/config.py \
  src/short_term_memory/models.py src/short_term_memory/ports.py \
  src/short_term_memory/service tests/test_config.py tests/test_models.py \
  tests/service tests/factories.py
git commit -m "feat: define HTTP memory contracts"
```

---

### Task 2: Sequence-Bearing Idempotent Journals and 30-Day Retention

**Files:**
- Modify: `src/short_term_memory/storage/journal_store.py`
- Create: `src/short_term_memory/storage/journal_retention.py`
- Modify: `tests/storage/test_journal_store.py`
- Create: `tests/storage/test_journal_retention.py`

**Interfaces:**
- Consumes: `MemoryEvent`, `MemoryContentType`.
- Produces: `JournalStore.append_event(user_id, session_id, event) -> JournalAppendResult`.
- Produces: `JournalStore.find_event(user_id, session_id, event_id) -> MemoryEvent | None`.
- Produces: `JournalStore.read_original_range(user_id, session_id, from_sequence, through_sequence) -> tuple[MemoryEvent, ...]`.
- Produces: `JournalRetentionJob.run(now) -> JournalRetentionResult`.

- [ ] **Step 1: Write failing journal idempotency and retention tests**

```python
def test_append_event_is_byte_preserving_and_idempotent(tmp_path):
    store = JournalStore(VFSAdapter(tmp_path))
    event = memory_event(sequence=7, event_id="same", content="a\n中文\n")
    first = store.append_event("u", "s", event)
    second = store.append_event("u", "s", event)
    assert first.appended is True
    assert second.appended is False
    assert store.find_event("u", "s", "same") == event
    assert store.read_original_range("u", "s", 7, 7)[0].content == "a\n中文\n"


def test_same_event_id_with_different_digest_is_conflict(tmp_path):
    store = JournalStore(VFSAdapter(tmp_path))
    store.append_event("u", "s", memory_event(event_id="same", content="one"))
    with pytest.raises(JournalConflictError):
        store.append_event("u", "s", memory_event(event_id="same", content="two"))


def test_retention_removes_only_files_older_than_thirty_days(tmp_path):
    store = JournalStore(VFSAdapter(tmp_path))
    store.append_event("u", "old", memory_event(
        event_id="old", created_at=datetime(2026, 7, 1, tzinfo=UTC)))
    store.append_event("u", "fresh", memory_event(
        event_id="fresh", created_at=datetime(2026, 7, 20, tzinfo=UTC)))
    result = JournalRetentionJob(store.vfs, retention_days=30).run(
        datetime(2026, 8, 6, tzinfo=UTC)
    )
    assert [path.name for path in result.removed] == ["2026-07-01-old.jsonl"]
    assert store.read_session("u", "fresh")
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/storage/test_journal_store.py tests/storage/test_journal_retention.py
```

Expected: FAIL because `append_event`, range reads, conflict errors, and retention job are missing.

- [ ] **Step 3: Implement backward-compatible event records**

Extend journal message records with optional fields so old JSONL remains readable:

```python
class JournalMessageEvent(JournalEvent):
    type: Literal["message"] = "message"
    role: JournalRole
    content: str
    event_id: str | None = None
    sequence: int | None = Field(default=None, ge=1)
    content_type: MemoryContentType = MemoryContentType.CONVERSATION
    metadata: dict[str, str] = Field(default_factory=dict)
    sha256: str | None = None
```

`append_event` must hold a per-session lock, scan the target session's current journal files for the event ID, compare digest, and append one compact JSON line with `flush()` and `os.fsync()`. Legacy `append_message` continues to work and does not require callers to provide an API event ID.

Implement exact sequence range selection:

```python
def read_original_range(self, user_id, session_id, from_sequence, through_sequence):
    events = (
        self._memory_event(record)
        for record in self.read_session(user_id, session_id)
        if isinstance(record, JournalMessageEvent) and record.sequence is not None
    )
    return tuple(
        event for event in events
        if from_sequence <= event.sequence <= through_sequence
    )
```

- [ ] **Step 4: Implement retention without broad deletion**

`JournalRetentionJob` must inspect only `root/*/journals/*.jsonl`, parse the date prefix, and unlink an explicit file only when its latest valid event timestamp is older than `now - retention_days`. It must skip malformed filenames, report failures, and never recursively remove a directory.

- [ ] **Step 5: Run journal and full tests**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/storage/test_journal_store.py tests/storage/test_journal_retention.py
PYTHONPATH=src .venv/bin/python -m pytest -q
```

Expected: PASS with legacy journal fixtures still readable.

- [ ] **Step 6: Commit Task 2**

```bash
git add src/short_term_memory/storage/journal_store.py \
  src/short_term_memory/storage/journal_retention.py \
  tests/storage/test_journal_store.py tests/storage/test_journal_retention.py
git commit -m "feat: add durable idempotent memory journals"
```

---

### Task 3: Async Redis Reservation, Commit, Read, and Compare-and-Swap

**Files:**
- Create: `src/short_term_memory/storage/async_redis_memory_store.py`
- Create: `tests/storage/test_async_redis_memory_store.py`
- Modify: `tests/storage/fake_redis.py`
- Create: `tests/integration/test_async_redis_memory_store.py`

**Interfaces:**
- Produces: `AsyncRedisMemoryStore.reserve_event(user_id, session_id, event_id, digest) -> EventReservation`.
- Produces: `AsyncRedisMemoryStore.commit_event(user_id, session_id, event) -> Literal["committed", "duplicate"]`.
- Produces: `read_recent_originals`, `read_originals_after`, `read_envelope`, `compare_and_set_envelope`.
- Produces: `acquire_compression_lease` and `release_compression_lease`.
- Consumes: `MemoryEvent`, `MemorySummaryEnvelope`.

- [ ] **Step 1: Write failing repository behavior tests**

Use an async in-test Redis double for domain behavior and a real-Redis opt-in test for Lua execution:

```python
@pytest.mark.asyncio
async def test_reserve_retry_and_conflict(memory_store):
    first = await memory_store.reserve_event("u", "s", "e", "a" * 64)
    retry = await memory_store.reserve_event("u", "s", "e", "a" * 64)
    assert first.sequence == retry.sequence == 1
    assert first.state == "reserved"
    assert retry.state == "pending"
    with pytest.raises(EventConflictError):
        await memory_store.reserve_event("u", "s", "e", "b" * 64)


@pytest.mark.asyncio
async def test_commit_makes_event_visible_once(memory_store):
    reservation = await memory_store.reserve_event("u", "s", "e", "a" * 64)
    event = memory_event(sequence=reservation.sequence, event_id="e")
    assert await memory_store.commit_event("u", "s", event) == "committed"
    assert await memory_store.commit_event("u", "s", event) == "duplicate"
    assert await memory_store.read_recent_originals("u", "s", 10) == (event,)


@pytest.mark.asyncio
async def test_summary_cas_rejects_stale_worker(memory_store):
    assert await memory_store.compare_and_set_envelope("u", "s", 0, envelope(1))
    assert not await memory_store.compare_and_set_envelope("u", "s", 0, envelope(2))
    assert (await memory_store.read_envelope("u", "s")).version == 1
```

- [ ] **Step 2: Run the tests and verify RED**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/storage/test_async_redis_memory_store.py
```

Expected: FAIL because the async Redis store is missing.

- [ ] **Step 3: Implement explicit Redis keys and Lua scripts**

Use these keys after validating components with `safe_component`:

```text
dream:session:{user}:{session}:sequence
dream:session:{user}:{session}:messages
dream:session:{user}:{session}:summary
dream:session:{user}:{session}:event:{event_id}
dream:session:{user}:{session}:compression-lock
```

The reserve script must have these semantics:

```lua
local digest = redis.call('HGET', KEYS[2], 'digest')
if digest then
  if digest ~= ARGV[1] then return {'conflict', '0'} end
  return {redis.call('HGET', KEYS[2], 'status'), redis.call('HGET', KEYS[2], 'sequence')}
end
local sequence = redis.call('INCR', KEYS[1])
redis.call('HSET', KEYS[2], 'digest', ARGV[1], 'status', 'pending', 'sequence', sequence)
redis.call('EXPIRE', KEYS[2], ARGV[2])
redis.call('EXPIRE', KEYS[1], ARGV[3])
return {'reserved', tostring(sequence)}
```

The commit script verifies digest/sequence, returns `duplicate` for committed entries, appends the serialized event exactly once, marks the event committed, and refreshes message/summary/event/sequence TTL. The CAS script compares envelope version before `SET EX`. The lease uses `SET key token NX PX` and a token-checking release script.

- [ ] **Step 4: Add a real Redis integration proof**

Gate with `SHORT_TERM_MEMORY_RUN_REDIS_INTEGRATION=1`. Execute 100 concurrent reservations for distinct event IDs and assert sequences are unique and ordered. Execute 100 concurrent reservations for the same event ID and assert one sequence and one committed event.

- [ ] **Step 5: Run unit, optional integration, and full tests**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/storage/test_async_redis_memory_store.py
SHORT_TERM_MEMORY_RUN_REDIS_INTEGRATION=1 REDIS_URL=redis://127.0.0.1:6379/15 \
  PYTHONPATH=src .venv/bin/python -m pytest -q -s \
  tests/integration/test_async_redis_memory_store.py
PYTHONPATH=src .venv/bin/python -m pytest -q
```

Expected: unit and real Redis tests PASS when Redis is running; without the opt-in flag, the integration test is explicitly skipped.

- [ ] **Step 6: Commit Task 3**

```bash
git add src/short_term_memory/storage/async_redis_memory_store.py \
  tests/storage/test_async_redis_memory_store.py \
  tests/storage/fake_redis.py tests/integration/test_async_redis_memory_store.py
git commit -m "feat: add atomic async Redis memory store"
```

---

### Task 4: Original-Only Compression Generations

**Files:**
- Create: `src/short_term_memory/compression/generations.py`
- Create: `tests/compression/test_generations.py`
- Modify: `src/short_term_memory/storage/redis_session_context.py`
- Modify: `tests/storage/test_redis_session_context.py`

**Interfaces:**
- Produces: `CompressionCandidate` containing original events and expected envelope version.
- Produces: `GenerationPlanner.plan_incremental(user_id, session_id)` and `plan_rebuild(user_id, session_id, through_sequence)`.
- Produces: `GenerationAssembler.build_read_messages(envelope, recent_originals, now)`.
- Consumes: async memory store, journals, `CompressionGeneration`, `MemorySummaryEnvelope`.

- [ ] **Step 1: Write the three-generation no-recompression test**

```python
async def seed_originals(repository, journals, sequences):
    for sequence in sequences:
        digest = sha256(f"original-{sequence}".encode("utf-8")).hexdigest()
        reservation = await repository.reserve_event("u", "s", f"e-{sequence}", digest)
        assert reservation.sequence == sequence
        event = memory_event(sequence=reservation.sequence, event_id=f"e-{sequence}",
                             content=f"original-{sequence}")
        journals.append_event("u", "s", event)
        await repository.commit_event("u", "s", event)


def envelope_from(candidate, marker):
    generation = CompressionGeneration(
        generation=candidate.expected_version + 1,
        from_sequence=candidate.from_sequence,
        through_sequence=candidate.through_sequence,
        messages=[{"role": "system", "content": marker}],
        tokens_before=100, tokens_after=25,
        created_at="2026-08-06T00:00:00+00:00",
        ccr_expires_at="2026-08-06T12:00:00+00:00",
    )
    return envelope(version=candidate.expected_version + 1,
                    through=candidate.through_sequence,
                    generations=[generation])


@pytest.mark.asyncio
async def test_later_generations_contain_only_new_original_events(repository, journals):
    planner = GenerationPlanner(repository, journals, max_segments=8)
    await seed_originals(repository, journals, range(1, 101))
    first = await planner.plan_incremental("u", "s")
    await repository.compare_and_set_envelope("u", "s", 0, envelope_from(first, marker="HR-1"))

    await seed_originals(repository, journals, range(101, 181))
    second = await planner.plan_incremental("u", "s")
    await repository.compare_and_set_envelope("u", "s", 1, envelope_from(second, marker="HR-2"))

    await seed_originals(repository, journals, range(181, 241))
    third = await planner.plan_incremental("u", "s")

    assert [e.sequence for e in first.originals] == list(range(1, 101))
    assert [e.sequence for e in second.originals] == list(range(101, 181))
    assert [e.sequence for e in third.originals] == list(range(181, 241))
    rendered = json.dumps([e.content for e in (*second.originals, *third.originals)])
    assert "HR-1" not in rendered and "HR-2" not in rendered
```

Add a legacy regression test:

```python
def test_legacy_compression_snapshot_never_includes_summary_messages(context):
    summary = SessionSummaryDocument(
        user_id="u", session_id="s",
        coverage=SessionSummaryCoverage(processed_message_count=0),
        current_goal=[], preferences=[], confirmed_facts=[], pending_items=[],
        attachment_references=[],
        compression_context=SessionCompressionContext(
            messages=[{"role": "system", "content": "OLD_HEADROOM_MARKER"}],
            tokens_before=10, tokens_after=2),
        updated_at="2026-08-06T00:00:00+00:00",
    )
    context.set_summary("u", "s", summary.model_dump_json())
    context.append_message("u", "s", {"role": "user", "content": "new original"})
    snapshot = context.compression_snapshot("u", "s")
    assert snapshot.messages == ({"role": "user", "content": "new original"},)
```

- [ ] **Step 2: Run tests and verify RED**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/compression/test_generations.py \
  tests/storage/test_redis_session_context.py::test_legacy_compression_snapshot_never_includes_summary_messages
```

Expected: FAIL because planners do not exist and the legacy snapshot currently prepends summary messages.

- [ ] **Step 3: Implement candidate planning**

Incremental planning must use the envelope high-water mark only:

```python
async def plan_incremental(self, user_id: str, session_id: str) -> CompressionCandidate | None:
    envelope = await self.store.read_envelope(user_id, session_id)
    through = envelope.compressed_through_sequence if envelope else 0
    originals = await self.store.read_originals_after(user_id, session_id, through)
    if not originals:
        return None
    return CompressionCandidate(
        user_id=user_id,
        session_id=session_id,
        expected_version=envelope.version if envelope else 0,
        from_sequence=originals[0].sequence,
        through_sequence=originals[-1].sequence,
        originals=originals,
        rebuild=False,
    )
```

Rebuild planning loads the covered sequence range from journals and never reads generation messages. Read assembly emits semantic summary, unexpired opaque generation messages, and recent originals. It records duplicates by sequence internally but retains the approved recent-original overlap policy only for read context.

- [ ] **Step 4: Remove legacy summary content from compression input**

Change `RedisSessionContext.compression_snapshot()` to read only Redis message-list originals. Leave `build_history()` unchanged so existing read behavior remains compatible. Update its test to distinguish compression input from answer history.

- [ ] **Step 5: Run targeted and full tests**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/compression/test_generations.py tests/storage/test_redis_session_context.py
PYTHONPATH=src .venv/bin/python -m pytest -q
```

Expected: PASS; no compression test input contains stored summary/Headroom messages.

- [ ] **Step 6: Commit Task 4**

```bash
git add src/short_term_memory/compression/generations.py \
  src/short_term_memory/storage/redis_session_context.py \
  tests/compression/test_generations.py tests/storage/test_redis_session_context.py
git commit -m "fix: prevent recompressing Headroom output"
```

---

### Task 5: Async Headroom Adapter and Persistent Compression Queue

**Files:**
- Create: `src/short_term_memory/compression/async_headroom_client.py`
- Create: `src/short_term_memory/jobs/redis_compression_queue.py`
- Create: `src/short_term_memory/jobs/compression_worker.py`
- Create: `tests/compression/test_async_headroom_client.py`
- Create: `tests/jobs/test_redis_compression_queue.py`
- Create: `tests/jobs/test_compression_worker.py`

**Interfaces:**
- Produces: `AsyncHeadroomClient.compress(messages, model, correlation_id, scope_headers)`.
- Produces: `CompressionJob` JSON schema.
- Produces: `RedisCompressionQueue.enqueue`, `lease`, `ack`, `retry`.
- Produces: `CompressionWorker.run_once()` and `run_forever()`.
- Consumes: `GenerationPlanner`, memory store, scope factory, Headroom settings.
- The worker accepts the existing injectable `SummaryModel`; the standalone CLI uses an `EmptySummaryModel` that returns five empty categories and never calls DeepSeek. Companies may replace it through runtime composition without changing either memory API.

- [ ] **Step 1: Write failing adapter and worker tests**

```python
class RecordingAsyncTransport:
    def __init__(self, compressed_messages):
        self.compressed_messages = compressed_messages
        self.requests = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        payload = {"messages": self.compressed_messages, "tokens_before": 100,
                   "tokens_after": 25, "tokens_saved": 75,
                   "compression_ratio": 4.0, "transforms_applied": ["test"]}
        return httpx.Response(200, json=payload, request=request)


def compression_job(*, through_sequence=10, expected_version=0):
    return CompressionJob(job_id=f"job-{through_sequence}-{expected_version}",
                          user_id="u", session_id="s",
                          expected_version=expected_version,
                          requested_through_sequence=through_sequence,
                          attempt=0)


@pytest.mark.asyncio
async def test_async_headroom_sends_only_candidate_originals():
    transport = RecordingAsyncTransport(compressed_messages=[{"role": "system", "content": "marker"}])
    client = AsyncHeadroomClient("http://headroom:8787", timeout_seconds=5, transport=transport)
    result = await client.compress(
        ({"role": "user", "content": "ORIGINAL"},),
        model="deepseek-v4-flash",
        correlation_id="req-1",
        scope_headers=scope_headers(),
    )
    assert transport.requests[0].url.path == "/v1/compress"
    assert json.loads(transport.requests[0].content)["messages"] == [
        {"role": "user", "content": "ORIGINAL"}
    ]
    assert result.messages[0]["content"] == "marker"


@pytest.mark.asyncio
async def test_worker_stores_generation_only_after_headroom_success(worker, store):
    await worker.queue.enqueue(compression_job(through_sequence=10, expected_version=0))
    result = await worker.run_once()
    envelope = await store.read_envelope("u", "s")
    assert result.state == "acked"
    assert envelope.compressed_through_sequence == 10
    assert envelope.compression_generations[0].messages[0].content == "marker"


@pytest.mark.asyncio
async def test_stale_worker_is_acked_without_overwrite(worker, store):
    await store.compare_and_set_envelope("u", "s", 0, envelope(version=1))
    await worker.queue.enqueue(compression_job(expected_version=0))
    result = await worker.run_once()
    assert result.state == "stale"
    assert (await store.read_envelope("u", "s")).version == 1
```

- [ ] **Step 2: Run tests and verify RED**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/compression/test_async_headroom_client.py \
  tests/jobs/test_redis_compression_queue.py \
  tests/jobs/test_compression_worker.py
```

Expected: FAIL because the async adapter, queue, and worker do not exist.

- [ ] **Step 3: Implement the bounded shared HTTP client**

Construct one `httpx.AsyncClient` per service/worker process:

```python
limits = httpx.Limits(max_connections=200, max_keepalive_connections=100)
self.http = http_client or httpx.AsyncClient(limits=limits, timeout=timeout_seconds)
```

Validate the same public response fields as the synchronous client. Return typed failures without logging content. Never retry inside the adapter; the persistent queue owns retry policy.

- [ ] **Step 4: Implement queue leasing and retry**

Use Redis structures:

```text
dream:compression:ready       LIST of job IDs
dream:compression:job:{id}    STRING JSON payload
dream:compression:lease:{id}  STRING worker token with TTL
dream:compression:retry       ZSET score=next_attempt_unix_ms
dream:compression:pending     SET session keys that overflowed soft capacity
```

`enqueue` stores the job before pushing its ID. At the soft capacity, it records the session in `pending` instead of dropping it. `lease` atomically moves a due job into a lease; `ack` deletes payload/lease; `retry` increments attempt and uses capped exponential backoff. After max attempts, retain the payload in a dead-letter sorted set and expose a metric.

- [ ] **Step 5: Implement original-only worker execution**

The worker obtains a session compression lease, asks `GenerationPlanner` for a fresh candidate, converts only `candidate.originals` to Headroom messages, calls Headroom, builds the next `CompressionGeneration`, and CAS-writes the envelope. It must release the session lease in `finally`. A Headroom failure retries without advancing high-water mark or trimming messages.

Run an injected synchronous `SummaryModel.summarize()` with `anyio.to_thread.run_sync`; its output may update only the five semantic lists. `EmptySummaryModel` returns `SessionSummaryPayload` with five empty lists and is the standalone CLI default, so neither API process nor worker imports/calls DeepSeek.

- [ ] **Step 6: Run targeted and full tests**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/compression/test_async_headroom_client.py \
  tests/jobs/test_redis_compression_queue.py \
  tests/jobs/test_compression_worker.py
PYTHONPATH=src .venv/bin/python -m pytest -q
```

Expected: PASS with deterministic timeout, retry, stale-result, and success cases.

- [ ] **Step 7: Commit Task 5**

```bash
git add src/short_term_memory/compression/async_headroom_client.py \
  src/short_term_memory/jobs/redis_compression_queue.py \
  src/short_term_memory/jobs/compression_worker.py \
  tests/compression/test_async_headroom_client.py \
  tests/jobs/test_redis_compression_queue.py tests/jobs/test_compression_worker.py
git commit -m "feat: add persistent Headroom compression worker"
```

---

### Task 6: Write and Read Memory Use Cases

**Files:**
- Create: `src/short_term_memory/service/memory_service.py`
- Create: `tests/service/test_memory_service.py`

**Interfaces:**
- Produces: `MemoryService.write(request, request_id) -> MemoryWriteResponse`.
- Produces: `MemoryService.read(request, request_id) -> MemoryReadResponse`.
- Consumes: async Redis store, JournalStore, GenerationPlanner/Assembler, compression queue, policy, scope factory, settings, clock.

- [ ] **Step 1: Write failing write-ahead and read tests**

```python
@pytest.mark.asyncio
async def test_write_reserves_journals_commits_then_queues(service, calls):
    response = await service.write(write_request("event-1", "original"), "req-1")
    assert calls == ["reserve", "journal_fsync", "redis_commit", "policy", "enqueue"]
    assert response.accepted is True
    assert response.sequence_from == response.sequence_through == 1


@pytest.mark.asyncio
async def test_retry_after_commit_failure_repairs_without_duplicate_journal(service):
    service.store.fail_next_commit = True
    with pytest.raises(RetryableWriteError):
        await service.write(write_request("event-1", "original"), "req-1")
    response = await service.write(write_request("event-1", "original"), "req-2")
    assert response.accepted is True
    assert service.journals.append_count("event-1") == 1


@pytest.mark.asyncio
async def test_read_returns_proxy_scope_without_calling_deepseek(service):
    service.store.seed_envelope(envelope_with_marker("HR-MARKER"))
    response = await service.read(read_request(), "req-2")
    assert any("HR-MARKER" in str(message.content) for message in response.messages)
    assert response.headroom.proxy_url == "http://headroom:8787/v1"
    assert set(response.headroom.scope_headers) == {
        "x-headroom-user-id", "x-headroom-session-id", "x-headroom-project-id"
    }
    assert not hasattr(service, "deepseek_client")
```

- [ ] **Step 2: Run tests and verify RED**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/service/test_memory_service.py
```

Expected: FAIL because `MemoryService` is missing.

- [ ] **Step 3: Implement write orchestration and timings**

For each event:

```python
digest = sha256(event.content.encode("utf-8")).hexdigest()
reservation = await store.reserve_event(user_id, session_id, event.event_id, digest)
memory_event = event.with_server_fields(
    sequence=reservation.sequence,
    sha256=digest,
    created_at=clock().isoformat(),
)
await anyio.to_thread.run_sync(journals.append_event, user_id, session_id, memory_event)
await store.commit_event(user_id, session_id, memory_event)
```

Batch writes preserve input order. Conflicting event IDs raise `EventConflictError`. Policy evaluation uses original Redis events only. Queue saturation reports durable pending work rather than failing the write.

- [ ] **Step 4: Implement read assembly and cold recovery**

Warm read loads the envelope and recent originals concurrently, assembles messages, and returns proxy URL plus HMAC scope. Missing Redis state loads bounded recent journal events in a worker thread and repopulates Redis. A generation inside the refresh window queues a rebuild. An already expired generation performs a cold rebuild through the queue/worker service boundary before exposure, records `source="journal_rebuild"`, and uses the compression SLO rather than the warm-read SLO.

`include_effective_config=false` returns `effective_config=null`. Effective Redis URLs are credential-redacted.

- [ ] **Step 5: Run targeted and full tests**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/service/test_memory_service.py
PYTHONPATH=src .venv/bin/python -m pytest -q
```

Expected: PASS with no DeepSeek dependency imported by memory service modules.

- [ ] **Step 6: Commit Task 6**

```bash
git add src/short_term_memory/service/memory_service.py tests/service/test_memory_service.py
git commit -m "feat: implement memory write and read services"
```

---

### Task 7: FastAPI Routes, Authentication, Metrics, and Error Mapping

**Files:**
- Create: `src/short_term_memory/service/auth.py`
- Create: `src/short_term_memory/service/metrics.py`
- Create: `src/short_term_memory/service/app.py`
- Create: `tests/service/test_app.py`
- Create: `tests/service/test_auth.py`
- Create: `tests/service/test_metrics.py`

**Interfaces:**
- Produces: `create_app(runtime_factory) -> FastAPI`.
- Produces: bearer-token dependency using `MEMORY_API_AUTH_TOKEN`.
- Produces: content-free request/phase metrics.
- Consumes: `MemoryService`, public schemas.

- [ ] **Step 1: Write failing HTTP contract tests**

```python
def test_only_two_business_routes_exist(client):
    schema = client.get("/openapi.json").json()
    business = sorted(path for path in schema["paths"] if path.startswith("/v1/memories/"))
    assert business == ["/v1/memories/read", "/v1/memories/write"]


def test_write_and_read_contract(client, auth_header):
    written = client.post("/v1/memories/write", headers=auth_header, json=write_payload())
    assert written.status_code == 200
    assert written.json()["accepted"] is True
    read = client.post("/v1/memories/read", headers=auth_header, json=read_payload())
    assert read.status_code == 200
    assert read.json()["headroom"]["proxy_url"].endswith("/v1")


def test_conflict_and_retryable_errors_are_stable(client, service, auth_header):
    service.next_error = EventConflictError("private content must not appear")
    conflict = client.post("/v1/memories/write", headers=auth_header, json=write_payload())
    assert conflict.status_code == 409
    assert conflict.json() == {"error": "event_id_conflict", "request_id": conflict.json()["request_id"]}
    assert "private content" not in conflict.text


def test_metrics_never_include_memory_content(client, auth_header):
    client.post("/v1/memories/write", headers=auth_header, json=write_payload(content="SECRET_ANCHOR"))
    metrics = client.get("/metrics").text
    assert "SECRET_ANCHOR" not in metrics
```

- [ ] **Step 2: Run tests and verify RED**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/service/test_app.py tests/service/test_auth.py tests/service/test_metrics.py
```

Expected: FAIL because the app, auth, and metrics modules do not exist.

- [ ] **Step 3: Implement auth and request middleware**

Bearer auth uses `secrets.compare_digest` and returns the same `401` for missing/incorrect credentials. Development may allow an empty configured token only when `SHORT_TERM_MEMORY_ENV=development`; production rejects startup without a token.

Middleware must:

```python
request_id = request.headers.get("x-request-id") or uuid4().hex
with metrics.in_flight.track():
    started = perf_counter()
    response = await call_next(request)
    response.headers["x-request-id"] = request_id
    metrics.observe_http(request.url.path, response.status_code, perf_counter() - started)
    return response
```

Apply body-size and concurrency limits before parsing large request bodies. A full semaphore returns `429` with `Retry-After`, not an unbounded wait.

- [ ] **Step 4: Implement routes and sanitized error mapping**

Routes call only `MemoryService.write/read`. Map validation to `422`, event conflict to `409`, overload to `429`, retryable infrastructure errors to `503`, and internal errors to `500`. Error bodies contain stable codes and request IDs, never raw exception messages.

Prometheus labels are bounded: route template, method, status class, and stage. Never use user/session/event IDs as labels.

- [ ] **Step 5: Run HTTP and full tests**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/service/test_app.py tests/service/test_auth.py tests/service/test_metrics.py
PYTHONPATH=src .venv/bin/python -m pytest -q
```

Expected: PASS; OpenAPI lists exactly two memory business paths.

- [ ] **Step 6: Commit Task 7**

```bash
git add src/short_term_memory/service/auth.py \
  src/short_term_memory/service/metrics.py src/short_term_memory/service/app.py \
  tests/service/test_app.py tests/service/test_auth.py tests/service/test_metrics.py
git commit -m "feat: expose authenticated memory HTTP APIs"
```

---

### Task 8: Runtime Lifecycle, CLI, and Deployment Services

**Files:**
- Create: `src/short_term_memory/service/runtime.py`
- Create: `src/short_term_memory/cli.py`
- Create: `tests/service/test_runtime_lifecycle.py`
- Create: `tests/test_cli.py`
- Create: `tests/integration/test_memory_http_redis.py`
- Create: `compose.memory.yml`
- Modify: `compose.redis.yml`

**Interfaces:**
- Produces: async `ServiceRuntime.start/close`, readiness checks, API app factory.
- Produces console commands `short-term-memory-api` and `short-term-memory-worker`.
- Consumes settings, Redis pool, shared Headroom client, journals, queue, and service.

- [ ] **Step 1: Write failing lifecycle and CLI tests**

```python
class RuntimeFakes:
    def __init__(self):
        self.redis = FakeClosableRedis()
        self.headroom_http = FakeClosableAsyncClient()


class FakeClosableRedis:
    def __init__(self):
        self.closed = False

    async def ping(self):
        return True

    async def aclose(self):
        self.closed = True


class FakeClosableAsyncClient:
    def __init__(self):
        self.is_closed = False

    async def aclose(self):
        self.is_closed = True


def record_uvicorn_run(monkeypatch):
    call = SimpleNamespace(kwargs=None)
    monkeypatch.setattr("short_term_memory.cli.uvicorn.run",
                        lambda *args, **kwargs: setattr(call, "kwargs", kwargs))
    return call


@pytest.mark.asyncio
async def test_runtime_reuses_one_redis_pool_and_headroom_client(settings):
    fakes = RuntimeFakes()
    runtime = await ServiceRuntime.start(settings, redis=fakes.redis,
                                         headroom_http=fakes.headroom_http)
    assert runtime.memory_service.store.client is runtime.redis
    assert runtime.worker.headroom.http is runtime.headroom_http
    await runtime.close()
    assert runtime.redis.closed and runtime.headroom_http.is_closed


def test_cli_uses_configured_workers(monkeypatch):
    monkeypatch.setenv("MEMORY_API_WORKERS", "4")
    called = record_uvicorn_run(monkeypatch)
    api_main([])
    assert called.kwargs["workers"] == 4
    assert called.kwargs["limit_concurrency"] >= 100
```

- [ ] **Step 2: Run tests and verify RED**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/service/test_runtime_lifecycle.py tests/test_cli.py
```

Expected: FAIL because runtime and CLI entrypoints are missing.

- [ ] **Step 3: Implement lifecycle and readiness**

FastAPI lifespan starts Redis, constructs one `httpx.AsyncClient`, builds `MemoryService`, and closes both deterministically. `/health` reports process liveness. `/ready` requires Redis ping and Headroom `/health`; it returns `503` with component booleans when either is unavailable and does not expose URLs containing credentials.

The worker command creates `CompressionWorker` with `HEADROOM_COMPRESSION_WORKERS=8` concurrent loops and handles SIGTERM by finishing/returning leases within the configured shutdown grace period.

- [ ] **Step 4: Add deployment composition**

`compose.memory.yml` contains explicit services:

```yaml
services:
  redis:
    image: redis:7.2.15-bookworm
  headroom:
    image: ${HEADROOM_IMAGE}
    command: ["headroom", "proxy", "--host", "0.0.0.0", "--port", "8787", "--workers", "1", "--limit-concurrency", "200", "--openai-api-url", "https://api.deepseek.com"]
    environment:
      HEADROOM_CCR_TTL_SECONDS: "43200"
  memory-api:
    command: ["short-term-memory-api"]
    depends_on: [redis, headroom]
  compression-worker:
    command: ["short-term-memory-worker"]
    depends_on: [redis, headroom]
```

Do not bake DeepSeek credentials into Compose. The chat caller supplies its key in the SDK request. Document that production scales single-worker Headroom replicas behind session-affinity routing.

- [ ] **Step 5: Run lifecycle, CLI, and full tests**

Before running, add `tests/integration/test_memory_http_redis.py`, gated by `SHORT_TERM_MEMORY_RUN_REDIS_INTEGRATION=1`. It must create the FastAPI app with real Redis plus a fake Headroom adapter, write one event over HTTP, read it over HTTP, and assert the original is present and `source="redis"`.

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/service/test_runtime_lifecycle.py tests/test_cli.py
PYTHONPATH=src .venv/bin/python -m pytest -q
```

Expected: PASS; resource close assertions prove no pool/client leak.

- [ ] **Step 6: Commit Task 8**

```bash
git add src/short_term_memory/service/runtime.py src/short_term_memory/cli.py \
  tests/service/test_runtime_lifecycle.py tests/test_cli.py \
  compose.memory.yml compose.redis.yml
git commit -m "feat: add memory API and worker runtimes"
```

---

### Task 9: Independent DeepSeek Official-SDK Example

**Files:**
- Create: `examples/deepseek_chat.py`
- Create: `tests/examples/test_deepseek_chat.py`
- Create: `tests/examples/__init__.py`

**Interfaces:**
- Produces: `run_turn(memory_api_url, user_id, session_id, prompt, deepseek_api_key, model)`.
- Consumes only HTTP memory endpoints plus the official OpenAI SDK.
- Must not be imported by memory service modules.

- [ ] **Step 1: Write a failing dependency-boundary example test**

```python
def test_deepseek_example_calls_write_read_proxy_write(fake_memory_api, fake_openai):
    answer = run_turn(
        memory_api_url=fake_memory_api.url,
        user_id="u",
        session_id="s",
        prompt="What is the anchor?",
        deepseek_api_key="test-key",
        model="deepseek-v4-flash",
        openai_factory=fake_openai.factory,
    )
    assert fake_memory_api.paths == [
        "/v1/memories/write", "/v1/memories/read", "/v1/memories/write"
    ]
    assert fake_openai.kwargs["base_url"] == "http://headroom:8787/v1"
    assert fake_openai.requests[0]["model"] == "deepseek-v4-flash"
    assert answer == "assistant answer"


def test_memory_package_does_not_import_openai():
    code = "import sys; import short_term_memory.service.app; assert 'openai' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True, env={**os.environ, "PYTHONPATH": "src"})
```

- [ ] **Step 2: Run tests and verify RED**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/examples/test_deepseek_chat.py
```

Expected: FAIL because the example does not exist.

- [ ] **Step 3: Implement the explicit three-call example**

The example must:

1. write the user event with a UUID event ID;
2. read memory;
3. create `OpenAI(api_key=deepseek_api_key, base_url=proxy_url, default_headers=headroom_scope_headers)`;
4. call `chat.completions.create(model=model, messages=memory_messages)`;
5. write the assistant response with a new UUID event ID;
6. print answer and content-free timings.

It reads `DEEPSEEK_API_KEY` only in `main()`. It must not send that key to the memory API or print it.

- [ ] **Step 4: Run tests and example help**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/examples/test_deepseek_chat.py
PYTHONPATH=src .venv/bin/python examples/deepseek_chat.py --help
```

Expected: tests PASS and help documents API URL, user/session, prompt, and model without requiring a key.

- [ ] **Step 5: Commit Task 9**

```bash
git add examples/deepseek_chat.py tests/examples
git commit -m "feat: add DeepSeek through Headroom example"
```

---

### Task 10: Four Content Fixtures and Real Headroom/DeepSeek Acceptance

**Files:**
- Create: `tests/fixtures/memory_cases/conversation.txt`
- Create: `tests/fixtures/memory_cases/code.py`
- Create: `tests/fixtures/memory_cases/document.md`
- Create: `tests/fixtures/memory_cases/SKILL.md`
- Create: `tests/integration/test_memory_headroom_cases.py`
- Create: `tests/integration/test_memory_deepseek_e2e.py`
- Modify: `tests/integration/fake_openai_provider.py`

**Interfaces:**
- Consumes running Headroom via `HEADROOM_SERVICE_URL`.
- Consumes real DeepSeek only when `SHORT_TERM_MEMORY_RUN_DEEPSEEK_E2E=1` and `DEEPSEEK_API_KEY` exist.
- Produces exact anchor/hash/CCR continuation evidence.

- [ ] **Step 1: Add exact original fixtures**

Each fixture must be large enough to route/compress and contain exactly one anchor:

```text
CONVERSATION_ORIGINAL_ANCHOR_7391
CODE_ORIGINAL_ANCHOR_7391
DOCUMENT_ORIGINAL_ANCHOR_7391
SKILL_ORIGINAL_ANCHOR_7391
```

The skill fixture is a valid `SKILL.md` with frontmatter, rules, steps, and examples. Record fixture SHA-256 at test runtime rather than hard-coding a digest that becomes stale after edits.

- [ ] **Step 2: Write the opt-in real Headroom test**

```python
@pytest.mark.parametrize("kind,filename,anchor", CASES)
def test_headroom_compresses_and_retrieves_byte_identical_original(kind, filename, anchor):
    original = (Path("tests/fixtures/memory_cases") / filename).read_text(encoding="utf-8")
    compressed = httpx.post(
        f"{os.environ['HEADROOM_SERVICE_URL'].rstrip('/')}/v1/compress",
        json={"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": original}]},
        headers=scope_headers(kind),
        timeout=300,
    ).raise_for_status().json()
    rendered = json.dumps(compressed["messages"], ensure_ascii=False)
    marker_hash = re.search(r"hash=([0-9a-fA-F]{12,24})", rendered).group(1)
    retrieved = httpx.get(
        f"{os.environ['HEADROOM_SERVICE_URL'].rstrip('/')}/v1/retrieve/{marker_hash}",
        headers=scope_headers(kind),
        timeout=10,
    ).raise_for_status().json()["original_content"]
    assert compressed["tokens_after"] < compressed["tokens_before"]
    assert anchor in retrieved
    assert sha256(retrieved.encode()).digest() == sha256(original.encode()).digest()
```

Hash extraction exists only in the test to invoke the official retrieval acceptance endpoint; production code must not import it.

- [ ] **Step 3: Verify RED or explicit skip**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q -s \
  tests/integration/test_memory_headroom_cases.py
```

Expected without flag: four explicit skips. With `SHORT_TERM_MEMORY_RUN_HEADROOM_AUTO_ROUTING=1` and Headroom stopped: FAIL, proving the test is live rather than a fake pass.

- [ ] **Step 4: Extend the deterministic fake upstream for continuation**

The fake provider first returns a `headroom_retrieve` tool call for the marker hash and then requires the retrieved original anchor in the continuation request before returning `FAKE_PROVIDER_CONFIRMED_CCR_ORIGINAL`. Record request count and whether the exact original appeared.

- [ ] **Step 5: Add opt-in real DeepSeek three-turn E2E**

The test writes all four types through the API, waits for compression completion, asks DeepSeek about an anchor hidden outside compressed top content, writes the assistant response, and repeats for turns two and three. It asserts:

```python
assert response.model == "deepseek-v4-flash"
assert expected_anchor in assistant_answer
assert headroom_stats_after["retrievals"] > headroom_stats_before["retrievals"]
assert captured_generation_inputs == [
    list(range(1, 101)), list(range(101, 181)), list(range(181, 241))
]
```

Gate real cost with both a run flag and API key. Never print the key, prompt body, retrieved original, or full provider response.

- [ ] **Step 6: Run deterministic, opt-in, and full tests**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/integration/test_headroom_proxy_ccr_flow.py
SHORT_TERM_MEMORY_RUN_HEADROOM_AUTO_ROUTING=1 \
  HEADROOM_SERVICE_URL=http://127.0.0.1:8787 \
  PYTHONPATH=src .venv/bin/python -m pytest -q -s \
  tests/integration/test_memory_headroom_cases.py
SHORT_TERM_MEMORY_RUN_DEEPSEEK_E2E=1 \
  DEEPSEEK_API_KEY="$DEEPSEEK_API_KEY" \
  PYTHONPATH=src .venv/bin/python -m pytest -q -s \
  tests/integration/test_memory_deepseek_e2e.py
PYTHONPATH=src .venv/bin/python -m pytest -q
```

Expected: deterministic fake continuation PASS; real tests PASS only with running/configured services and otherwise explicitly skip when not requested.

- [ ] **Step 7: Commit Task 10**

```bash
git add tests/fixtures/memory_cases \
  tests/integration/test_memory_headroom_cases.py \
  tests/integration/test_memory_deepseek_e2e.py \
  tests/integration/fake_openai_provider.py
git commit -m "test: add memory compression and CCR cases"
```

---

### Task 11: 100-Concurrency SLO Load Gate

**Files:**
- Create: `scripts/load_test_memory_api.py`
- Create: `tests/load/test_load_statistics.py`
- Create: `tests/load/__init__.py`
- Create: `docs/performance.md`

**Interfaces:**
- Produces CLI scenarios `write`, `read`, `mixed`, `same-session`, `queue-saturated`.
- Produces JSON report and nonzero exit on SLO violation.
- Consumes a running memory API; does not start/delete external services.

- [ ] **Step 1: Write failing percentile and gate tests**

```python
def test_percentiles_use_inclusive_nearest_rank():
    stats = summarize_ms(list(range(1, 101)))
    assert stats.p50 == 50
    assert stats.p95 == 95
    assert stats.p99 == 99
    assert stats.maximum == 100


def test_gate_fails_when_write_p95_exceeds_150_ms():
    report = scenario_report("write", latencies=[100.0] * 94 + [151.0] * 6, errors=0)
    assert evaluate_slo(report).exit_code == 1


def test_gate_fails_on_any_request_error():
    report = scenario_report("read", latencies=[10.0] * 99, errors=1)
    assert evaluate_slo(report).exit_code == 1
```

- [ ] **Step 2: Run tests and verify RED**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/load/test_load_statistics.py
```

Expected: FAIL because the load runner does not exist.

- [ ] **Step 3: Implement bounded async scenarios**

Use one `httpx.AsyncClient` with limits >= concurrency, an `asyncio.Barrier`-style event for synchronized start, and `perf_counter_ns()` for latency. Never store response bodies in the report. For each request record status, total latency, and returned Redis/journal/queue timings.

CLI example:

```bash
python scripts/load_test_memory_api.py \
  --base-url http://127.0.0.1:8080 \
  --scenario mixed \
  --concurrency 100 \
  --requests 1000 \
  --output /private/tmp/memory-load-mixed.json
```

Warm-read setup writes sessions before starting the measurement window. Same-session uses unique event IDs plus a separate idempotent replay phase. Queue saturation requires an explicit test-only worker configuration and reports backlog before/after.

- [ ] **Step 4: Run unit tests and local load gate**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/load/test_load_statistics.py
PYTHONPATH=src .venv/bin/python scripts/load_test_memory_api.py \
  --base-url http://127.0.0.1:8080 --scenario write \
  --concurrency 100 --requests 1000 \
  --output /private/tmp/memory-load-write.json
```

Expected: unit tests PASS. The live command exits `0` only when all requests succeed and write p95/p99 meet 150/300 ms; otherwise it prints the measured failure and exits `1`.

- [ ] **Step 5: Document reproducible hardware and interpretation**

`docs/performance.md` records date, CPU, RAM, Redis/Headroom topology, fixture size, worker counts, command, result path, and each SLO. It distinguishes warm reads, journal recovery, compression, CCR retrieval, and DeepSeek TTFT; no synthetic result may be reported as a real-service result.

- [ ] **Step 6: Commit Task 11**

```bash
git add scripts/load_test_memory_api.py tests/load docs/performance.md
git commit -m "test: add 100-concurrency memory SLO gate"
```

---

### Task 12: README, Configuration Alignment, Distribution, and Final Verification

**Files:**
- Modify: `README.md`
- Modify: `docs/short-term-memory.md`
- Modify: `docs/third_party/headroom.md`
- Create: `docs/memory-api-alignment.md`
- Modify: `tests/test_distribution_contents.py`
- Modify: `tests/test_public_package.py`

**Interfaces:**
- Documents the two business APIs, storage/retention table, LRU/backend distinction, no-recompression invariant, DeepSeek separation, deployment, SLOs, and exact acceptance commands.
- Produces teacher-facing safe/secret configuration matrix.

- [ ] **Step 1: Write failing distribution/documentation assertions**

```python
def test_distribution_contains_http_service_and_deepseek_example(built_wheel):
    names = wheel_names(built_wheel)
    assert "short_term_memory/service/app.py" in names
    assert "short_term_memory/jobs/compression_worker.py" in names


def test_readme_states_headroom_owns_ccr_and_deepseek_is_separate():
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "POST /v1/memories/write" in readme
    assert "POST /v1/memories/read" in readme
    assert "deepseek-v4-flash" in readme
    assert "不会把 Headroom 返回的压缩消息再次提交压缩" in readme
    assert "Headroom 全权负责 CCR" in readme
```

- [ ] **Step 2: Run tests and verify RED**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_distribution_contents.py tests/test_public_package.py
```

Expected: FAIL because service files and the approved documentation language are absent.

- [ ] **Step 3: Update user and teacher documentation**

Add exact tables for:

- Redis originals/summary: 12-hour sliding TTL;
- journals: 30-day configurable retention;
- Headroom cache: Headroom-owned LRU behavior with pluggable backend, 12-hour configured TTL;
- two business endpoints and operational endpoints;
- write/read/DeepSeek call order;
- all safe configuration values and secret-only settings;
- 100-concurrency SLOs and measured-versus-unmeasured status;
- failure boundaries and opt-in acceptance tests.

State clearly that `uv tool` is an installation/isolation method and `headroom proxy` is already an HTTP service. Do not claim transparent real-DeepSeek CCR or a latency SLO passed unless the corresponding fresh command passed.

- [ ] **Step 4: Build and inspect distributions**

```bash
.venv/bin/python -m build
python -m zipfile -l dist/short_term_memory-0.1.0-py3-none-any.whl
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_distribution_contents.py tests/test_public_package.py
```

Expected: build exits `0`; wheel contains service, worker, models, and CLI modules; tests PASS. Examples remain repository artifacts unless deliberately included through setuptools data configuration.

- [ ] **Step 5: Run the complete verification matrix**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests examples scripts
.venv/bin/python -m build
git diff --check
```

Then, with services running:

```bash
SHORT_TERM_MEMORY_RUN_REDIS_INTEGRATION=1 \
  REDIS_URL=redis://127.0.0.1:6379/15 \
  PYTHONPATH=src .venv/bin/python -m pytest -q -s \
  tests/integration/test_async_redis_memory_store.py \
  tests/integration/test_memory_http_redis.py

SHORT_TERM_MEMORY_RUN_HEADROOM_AUTO_ROUTING=1 \
  HEADROOM_SERVICE_URL=http://127.0.0.1:8787 \
  PYTHONPATH=src .venv/bin/python -m pytest -q -s \
  tests/integration/test_memory_headroom_cases.py

PYTHONPATH=src .venv/bin/python scripts/load_test_memory_api.py \
  --base-url http://127.0.0.1:8080 --scenario mixed \
  --concurrency 100 --requests 1000 \
  --output /private/tmp/memory-load-final.json
```

Run the real DeepSeek test only with explicit user-owned credentials:

```bash
SHORT_TERM_MEMORY_RUN_DEEPSEEK_E2E=1 \
  DEEPSEEK_API_KEY="$DEEPSEEK_API_KEY" \
  PYTHONPATH=src .venv/bin/python -m pytest -q -s \
  tests/integration/test_memory_deepseek_e2e.py
```

Record actual PASS/FAIL/SKIP and measured percentiles. A skipped opt-in test is not success evidence.

- [ ] **Step 6: Review requirements line by line**

Confirm with evidence:

```text
[ ] two and only two memory business endpoints
[ ] DeepSeek is independent and defaults to deepseek-v4-flash
[ ] DeepSeek SDK routes through Headroom Proxy
[ ] Headroom owns CCR storage and retrieval
[ ] compression inputs contain originals only
[ ] Redis/CCR 12-hour and journals 30-day retention
[ ] four content fixtures and three-turn test
[ ] 100-concurrency SLO report
[ ] no secrets or message content in logs/metrics/responses
[ ] legacy SDK tests still pass
```

- [ ] **Step 7: Commit Task 12**

```bash
git add README.md docs/short-term-memory.md docs/third_party/headroom.md \
  docs/memory-api-alignment.md tests/test_distribution_contents.py \
  tests/test_public_package.py
git commit -m "docs: document memory HTTP deployment and acceptance"
```

---

## Execution Notes

- Before Task 1 execution, create or repair `.venv` with `python3.13 -m venv .venv` and install `-e ".[api,deepseek,dev]"`; dependency installation is setup, not a substitute for a failing feature test.
- Use `apply_patch` for hand-authored edits.
- For every behavior change, follow RED -> verify expected failure -> GREEN -> full regression -> commit.
- Do not run real DeepSeek tests without explicit credentials and authorization to incur API usage.
- Do not inspect or mutate `~/.headroom/ccr_store.db`; Headroom acceptance uses only public HTTP endpoints.
- Keep unrelated user changes untouched and review `git status --short` before every commit.
