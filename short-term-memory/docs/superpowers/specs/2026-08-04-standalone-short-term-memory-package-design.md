# Standalone Short-Term Memory Package Design

## 1. Objective

Reorganize the existing `short-term-memory` branch into an independently
installable Python SDK whose distribution name is `short-term-memory` and whose
only import package is `short_term_memory`.

The package provides DREAM's PLAN-defined short-term-memory boundary:

- Redis Session Context isolated by `user_id + session_id`.
- Append-only journals for complete conversation events and Redis-expiry
  recovery.
- PLAN threshold evaluation for Headroom compression.
- Replaceable HTTP integration with the official Headroom service.
- DREAM-owned five-category session summary generation and Redis storage.
- `prepare_turn()` and `complete_turn()` integration points for a company Agent.

The package does not implement the company Agent, the final LLM answer, an HTTP
chat API, historical-session UI, Memory Retrieval Skill, Persona, Decision
Card, Curator, Daily Memory Job, Wiki, indexes, or other medium/long-term-memory
features.

## 2. Confirmed decisions

- Perform the migration in the existing `short-term-memory` branch and its
  existing worktree.
- Distribution name: `short-term-memory`.
- Python import package: `short_term_memory`.
- Do not retain any `dream.api.*` or other `dream.*` compatibility layer.
- Expose a Python SDK only; do not add an online chat HTTP interface.
- Keep Redis and Headroom as external services.
- Keep Headroom replaceable behind an HTTP compression/proxy boundary.
- Preserve existing Redis key names, journals layout, journals JSONL format,
  summary envelope, and Headroom HMAC scope identity.
- Do not archive removed DREAM code inside the repository. Git commits are the
  only rollback mechanism.

## 3. Target repository structure

```text
short-term-memory/
├── src/
│   └── short_term_memory/
│       ├── __init__.py
│       ├── config.py
│       ├── models.py
│       ├── ports.py
│       ├── api/
│       │   ├── __init__.py
│       │   ├── conversation_handler.py
│       │   └── runtime.py
│       ├── storage/
│       │   ├── __init__.py
│       │   ├── redis_runtime.py
│       │   ├── redis_session_context.py
│       │   ├── journal_store.py
│       │   └── vfs_adapter.py
│       ├── compression/
│       │   ├── __init__.py
│       │   ├── policy.py
│       │   ├── headroom_client.py
│       │   ├── summary.py
│       │   ├── scope.py
│       │   └── telemetry.py
│       └── jobs/
│           ├── __init__.py
│           └── session_compression_job.py
├── tests/
│   ├── api/
│   ├── storage/
│   ├── compression/
│   ├── jobs/
│   └── integration/
├── docs/
│   ├── short-term-memory.md
│   ├── short-term-memory-flow.svg
│   └── third_party/
│       ├── redis.md
│       └── headroom.md
├── README.md
├── pyproject.toml
├── .env.example
├── compose.redis.yml
└── .gitignore
```

This follows PLAN section 13's `api`, `storage`, and `jobs` boundaries while
adding a dedicated `compression` boundary for the replaceable Headroom
integration. Directories for raw/source processing, Wiki persistence, indexes,
and retrieval state are intentionally absent.

## 4. Dependency direction

```text
Company Agent
      ↓
api
      ↓
models + ports + config
      ↓
storage      compression
       \       /
          jobs
      ↓         ↓
Redis/journals  official Headroom
```

Rules:

- `models.py` contains transport-neutral session and compression data models.
- `ports.py` contains replaceable interfaces such as `TokenEstimator`,
  `SummaryModel`, `CompressionClient`, `RetryQueue`, and `BackgroundExecutor`.
- `storage` does not import Headroom-specific modules.
- `compression` does not read journals and does not directly mutate Redis.
- `jobs` composes storage ports with compression ports and owns background
  orchestration.
- `api` owns the company-Agent lifecycle and is the only layer exposed through
  the package root.
- Internal modules may depend on `models` and `ports`; `models` and `ports` do
  not depend on infrastructure modules.

## 5. Component responsibilities

### 5.1 `api`

`api/conversation_handler.py` owns the two-step Agent lifecycle:

1. `prepare_turn()` restores/reads the Redis session, writes the user message
   to Redis and journals, and returns history plus Headroom Proxy parameters.
2. `complete_turn()` writes the assistant message to Redis and journals,
   evaluates the PLAN compression policy, and queues background work when any
   threshold is met.

`api/runtime.py` builds the component from injected ports and exposes stable
methods directly on the runtime object:

```python
runtime = build_runtime(...)
prepared = runtime.prepare_turn(...)
completion = runtime.complete_turn(prepared, assistant_content=...)
```

The company Agent remains responsible for the real model request and final
answer. The SDK does not import a model-provider SDK for final answering.

### 5.2 `storage`

`redis_session_context.py` owns:

- Redis messages and summary keys.
- Half-day default TTL and TTL refresh.
- Recent-N-turn reads.
- Summary, fresh Headroom compression messages, and recent-message assembly.
- Recovery of the matching session from journals after Redis expiry.
- Safe trimming after successful background compression.
- Preservation of messages appended after a compression snapshot.

`journal_store.py` owns the existing append-only, per-user, per-day,
per-session JSONL contract.

`vfs_adapter.py` creates only:

```text
{SHORT_TERM_MEMORY_HOME}/{user_id}/journals/
```

It does not create raw, source, Wiki, `_ops`, or index directories. Attachment
placeholders and raw/source references remain opaque message strings; this SDK
does not manage the referenced files.

### 5.3 `compression`

- `policy.py`: the PLAN OR policy for token ratio, message count, and session
  duration.
- `headroom_client.py`: the replaceable `/v1/compress` HTTP adapter and safe
  development/production failure behavior.
- `summary.py`: the five PLAN short-term categories, validation, and injected
  SummaryModel adapter.
- `scope.py`: stable HMAC-based, de-identified Headroom scope headers.
- `telemetry.py`: compression success, failure, fallback, noop, ratio, context
  attachment, and scope-generation metrics without conversation content.

DREAM decides when to call Headroom. The official Headroom service decides how
to compress, which transform to use, whether CCR data is produced, and how a
supported proxied model request retrieves it. The SDK does not vendor Headroom,
parse private Router internals, or implement its own CCR protocol.

### 5.4 `jobs`

`session_compression_job.py` owns:

- Non-blocking execution after a completed Agent turn.
- Calling the injected `CompressionClient`.
- Calling the injected `SummaryModel` after successful compression or an
  allowed development fallback.
- Writing the session summary envelope to Redis.
- Trimming only the processed Redis history while preserving concurrent
  messages.
- Scheduling safe retries after production compression or summary failure.

Production compression failure must not call the SummaryModel, write a Redis
summary, or trim Redis messages. Redis originals and journals remain available
for retry.

## 6. Public package API

The root package exports only stable Agent-facing objects:

```python
from short_term_memory import (
    build_runtime,
    ShortTermMemorySettings,
    PreparedTurn,
    CompletionResult,
)
```

Internal Redis, Headroom, queue, and summary classes are not required imports
for ordinary company-Agent integration. Advanced deployments inject their
implementations through `build_runtime()` ports.

There is no legacy `dream` package after cutover.

## 7. Data compatibility

### 7.1 Redis

Keep the existing keys exactly:

```text
dream:session:{user_id}:{session_id}:messages
dream:session:{user_id}:{session_id}:summary
```

Changing the Python distribution must not invalidate live Redis sessions.

### 7.2 journals

Keep the existing path and JSONL format:

```text
{SHORT_TERM_MEMORY_HOME}/{user_id}/journals/{YYYY-MM-DD}-{session_id}.jsonl
```

The long-term-memory team may read this event log through the documented
journals contract. journals are durable experience records, not Wiki facts and
not the default online read source.

### 7.3 summary envelope

Keep the existing fields and meanings, including:

- `user_id`
- `session_id`
- processed-message coverage
- current goal
- preferences
- confirmed facts
- pending items
- attachment references
- Headroom compression-context messages and token counts
- update timestamp

The Redis summary remains short-term state and never becomes a Wiki write by
itself.

### 7.4 Headroom scope

Keep the existing `dream-v1` HMAC input prefix and scope-header behavior during
the structural migration. Changing it would split Headroom context/cache
identity and should be handled only by a separately versioned protocol change.

## 8. Configuration and dependencies

Replace `DreamSettings` with:

- `ShortTermMemorySettings`
- `RedisSessionSettings`
- `HeadroomServiceSettings`

The new configuration contains only component-owned settings:

```text
SHORT_TERM_MEMORY_HOME
SHORT_TERM_MEMORY_ENV
SHORT_TERM_MEMORY_SCOPE_SECRET
REDIS_URL
REDIS_SESSION_TTL_SECONDS
REDIS_HISTORY_TURNS
CONTEXT_WINDOW_TOKENS
HEADROOM_SERVICE_URL
HEADROOM_SERVICE_TIMEOUT_SECONDS
HEADROOM_COMPRESSION_MODEL
HEADROOM_CCR_TTL_SECONDS
HEADROOM_TRIGGER_RATIO
HEADROOM_MAX_MESSAGES
HEADROOM_MAX_SESSION_SECONDS
```

Legacy DREAM environment names are not retained because the confirmed package
strategy is a clean break without compatibility shims.

Runtime dependencies are limited to:

```text
httpx
pydantic
redis
```

Remove FastAPI, Uvicorn, OpenAI, and Headroom Python packages from runtime
dependencies. The company Agent owns its model SDK. Headroom runs as an
external replaceable service.

## 9. File migration map

| Current file | Target | Treatment |
|---|---|---|
| `dream/api/conversation_handler.py` | `api/conversation_handler.py` | Keep only Agent turn orchestration |
| same file | `storage/redis_session_context.py` | Extract Redis/session/history/recovery |
| same file | `compression/policy.py` | Extract PLAN trigger policy |
| `dream/api/short_term_runtime.py` | `api/runtime.py` | Expose stable runtime methods |
| `dream/api/redis_runtime.py` | `storage/redis_runtime.py` | Preserve behavior |
| `dream/api/optimization_scope.py` | `compression/scope.py` | Preserve scope identity |
| `dream/integrations/headroom_client.py` | `compression/headroom_client.py` | Preserve HTTP boundary |
| `dream/integrations/headroom_telemetry.py` | `compression/telemetry.py` | Preserve content-free metrics |
| `dream/memory/session_compression.py` | `compression/summary.py` | Extract summary models/provider |
| same file | `jobs/session_compression_job.py` | Extract job and executor queue |
| `dream/storage/journal_store.py` | `storage/journal_store.py` | Preserve data format |
| `dream/storage/vfs_adapter.py` | `storage/vfs_adapter.py` | Reduce to journals paths |
| `dream/config.py` | `config.py` | Rewrite only short-term settings; do not copy old imports |
| `dream/__init__.py` | `short_term_memory/__init__.py` | Publish the new SDK API |

Use `git mv` where a file remains conceptually intact. Use an explicit split
when the current file contains multiple responsibilities. Do not copy complete
legacy modules into the new package.

## 10. Removal boundary

After all new-package tests pass, remove the complete old `src/dream/` tree and
all tests/documents unrelated to short-term memory. The removed scope includes:

```text
application/
core/
curators/
extraction/
governance/
integrations/internship/
memory/managers/
memory/storage/
retrieval/
validation/
```

Remove unrelated tests under `tests/core`, `tests/curators`,
`tests/extraction`, `tests/governance`, `tests/memory`, `tests/retrieval`, and
`tests/e2e`. Remove `docs/ai-evolution-and-user-persona.md` and
`docs/dream-mechanism.md`.

Before deletion, generate an exact file list and confirm that every target is
inside the old architecture scope. Do not use `git clean`, `git reset --hard`,
`git add .`, recursive deletion of the worktree root, or an in-repository
archive.

## 11. Migration phases and rollback points

1. **Baseline:** commit the current short-term README/specification and record a
   clean full-test baseline.
2. **Package skeleton:** create `short_term_memory` and change distribution
   metadata without moving behavior.
3. **Contracts/config:** migrate models, ports, and short-term-only settings.
4. **Storage:** migrate Redis, journals, VFS, recovery, and their tests.
5. **Compression/jobs:** migrate policy, Headroom, summary, telemetry, jobs,
   and their tests.
6. **Agent API:** migrate the conversation handler and runtime; publish the
   root API.
7. **Cutover/removal:** update remaining imports, confirm the exact deletion
   list, then remove `src/dream` and unrelated tests/docs.
8. **Distribution acceptance:** build a wheel, install it in a clean temporary
   environment, inspect wheel contents, and run deterministic plus opt-in
   service tests.

Each phase ends in one focused commit. A failed phase is rolled back to the
preceding commit; no archive copy is created.

## 12. Testing strategy

Tests mirror target modules:

```text
tests/api/
tests/storage/
tests/compression/
tests/jobs/
tests/integration/
```

Deterministic tests cover:

- Redis key isolation, ordering, TTL refresh, and deletion.
- Summary plus recent-N-turn history assembly.
- Matching-session journals recovery after Redis expiry.
- Preservation of attachment markers and concurrent messages.
- All three PLAN compression triggers.
- Headroom HTTP request/response validation and environment-aware fallback.
- Five-category SummaryModel validation.
- Production failure safety and retry scheduling.
- Stable de-identified Headroom scope.
- Public runtime preparation/completion lifecycle.

Opt-in tests cover:

- Real Redis operations and expiry recovery.
- Official Headroom automatic routing and actual token reduction.
- Official Proxy/CCR behavior through a supported real model-provider path.

The package migration can be structurally accepted while the external
Headroom transparent-CCR continuation check remains documented as a vendor
acceptance item. The code must not claim that external check passed without
fresh evidence.

Distribution tests cover:

- `import short_term_memory` from a clean wheel install.
- Absence of the `dream` package from the wheel.
- Absence of long-term-memory modules and user runtime data.
- Absence of Headroom source, models, ONNX Runtime, FastAPI, Uvicorn, and model
  provider SDKs from runtime dependencies.

## 13. Completion criteria

The migration is complete only when:

- The distribution is named `short-term-memory`.
- `short_term_memory` is the only project import package.
- No `dream.*` compatibility path remains.
- No medium/long-term-memory source, test, or user-facing documentation remains.
- Redis keys, journals, summary envelope, and Headroom scope identity remain
  compatible.
- The root package exposes `build_runtime`, `ShortTermMemorySettings`,
  `PreparedTurn`, and `CompletionResult`.
- Company Agent integration requires no internal module imports.
- Headroom remains an external, replaceable HTTP component.
- Default tests require no external Redis, Headroom, or model service.
- Deterministic tests, real Redis acceptance, wheel build, clean wheel install,
  and package-content checks pass.
- Official Headroom/CCR status is reported from actual opt-in evidence rather
  than inferred from the adapter implementation.
