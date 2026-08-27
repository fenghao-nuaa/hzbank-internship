# Standalone Short-Term Memory Package Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert the existing `short-term-memory` branch from a DREAM monolith snapshot into an independently installable `short-term-memory` Python SDK whose only import package is `short_term_memory`.

**Architecture:** The new package uses PLAN-aligned `api`, `storage`, and `jobs` layers plus a replaceable `compression` layer for official Headroom HTTP integration. Redis Session Context and journals preserve their existing data contracts, while the company Agent integrates only through `build_runtime()`, `prepare_turn()`, and `complete_turn()`.

**Tech Stack:** Python 3.11–3.13, Pydantic 2, redis-py 6.4.0, HTTPX 0.28, pytest 9, Ruff 0.15, official Redis 7.2.15, external Headroom 0.33.0 Proxy.

## Global Constraints

- Work only in the existing `short-term-memory` branch and `/Users/fenghao/PycharmProjects/dream/DREAM/.worktrees/short-term-memory` worktree.
- Distribution name is `short-term-memory`; Python package is `short_term_memory`.
- Do not retain `dream.*` import compatibility shims.
- Expose a Python SDK only; do not add a chat HTTP endpoint or final Answer Generator.
- Redis and Headroom remain external replaceable services.
- Preserve Redis keys `dream:session:{user_id}:{session_id}:messages` and `dream:session:{user_id}:{session_id}:summary`.
- Preserve journals paths and JSONL fields, the Redis summary envelope, and the `dream-v1` HMAC scope identity.
- Keep Headroom outside the project virtual environment and runtime dependencies.
- Do not add Wiki, raw/source processing, indexes, Memory Retrieval Skill, historical-session UI, Persona, Decision Card, Curator, or Daily Memory Job code.
- Do not use `git add .`, `git reset --hard`, `git clean`, broad recursive deletion, or an in-repository archive.
- Before removing old files, print and confirm an exact deletion list.
- Use `apply_patch` for file content changes and explicit file paths for Git staging.

---

## Target file map

| Target file | Responsibility |
|---|---|
| `src/short_term_memory/__init__.py` | Stable root SDK exports |
| `src/short_term_memory/config.py` | Short-term-only dotenv/environment settings |
| `src/short_term_memory/models.py` | Shared session, summary, and compression result models |
| `src/short_term_memory/ports.py` | Replaceable dependency protocols |
| `src/short_term_memory/api/conversation_handler.py` | Agent turn lifecycle |
| `src/short_term_memory/api/runtime.py` | Dependency composition and runtime facade |
| `src/short_term_memory/storage/redis_runtime.py` | redis-py lifecycle |
| `src/short_term_memory/storage/redis_session_context.py` | Redis keys, TTL, history, recovery, trim |
| `src/short_term_memory/storage/journal_store.py` | Append-only session journals |
| `src/short_term_memory/storage/vfs_adapter.py` | User-isolated journals path only |
| `src/short_term_memory/compression/policy.py` | PLAN compression trigger policy |
| `src/short_term_memory/compression/headroom_client.py` | Replaceable Headroom HTTP adapter |
| `src/short_term_memory/compression/summary.py` | Five-category summary generation and validation |
| `src/short_term_memory/compression/scope.py` | Stable de-identified Headroom scope |
| `src/short_term_memory/compression/telemetry.py` | Content-free compression metrics |
| `src/short_term_memory/jobs/session_compression_job.py` | Background compression/summary/store/retry orchestration |

### Task 1: Freeze the migration baseline

**Files:**
- Modify: `README.md`
- Add: `docs/plans/2026-08-04-short-term-memory-readme-design.md`
- Add: `docs/superpowers/plans/2026-08-04-short-term-memory-readme.md`
- Add: `docs/superpowers/plans/2026-08-04-standalone-short-term-memory-package.md`
- Reference: `docs/superpowers/specs/2026-08-04-standalone-short-term-memory-package-design.md`

**Interfaces:**
- Consumes: Current branch state at commit `8d617e9` or its descendant.
- Produces: A clean documentation baseline and a recorded pre-migration test result.

- [ ] **Step 1: Confirm branch and worktree**

Run:

```bash
git branch --show-current
git rev-parse --show-toplevel
git status --short
```

Expected: branch is `short-term-memory`; root is the `.worktrees/short-term-memory` path; only known documentation changes are uncommitted.

- [ ] **Step 2: Run the existing full suite**

Run:

```bash
PYTHONPATH=src /Users/fenghao/PycharmProjects/dream/DREAM/.venv/bin/python -m pytest -q
```

Expected: `410 passed, 9 skipped` or a higher passing count with zero failures. Stop if any deterministic test fails.

- [ ] **Step 3: Commit only the current short-term documentation**

Run:

```bash
git add README.md
git add docs/plans/2026-08-04-short-term-memory-readme-design.md
git add docs/superpowers/plans/2026-08-04-short-term-memory-readme.md
git add docs/superpowers/plans/2026-08-04-standalone-short-term-memory-package.md
git commit -m "docs: document standalone short term memory migration"
```

Expected: commit succeeds and `git status --short` is empty.

### Task 2: Scaffold the independent distribution

**Files:**
- Create: `src/short_term_memory/__init__.py`
- Create: `src/short_term_memory/api/__init__.py`
- Create: `src/short_term_memory/storage/__init__.py`
- Create: `src/short_term_memory/compression/__init__.py`
- Create: `src/short_term_memory/jobs/__init__.py`
- Create: `tests/test_public_package.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: Python packaging through setuptools package discovery under `src`.
- Produces: Installable distribution `short-term-memory` and importable package `short_term_memory`.

- [ ] **Step 1: Write the failing package identity test**

Create `tests/test_public_package.py`:

```python
from importlib.metadata import metadata

import short_term_memory


def test_distribution_and_import_package_have_standalone_identity() -> None:
    assert metadata("short-term-memory")["Name"] == "short-term-memory"
    assert short_term_memory.__version__ == "0.1.0"
```

- [ ] **Step 2: Verify the new package test fails**

Run:

```bash
PYTHONPATH=src /Users/fenghao/PycharmProjects/dream/DREAM/.venv/bin/python -m pytest -q tests/test_public_package.py
```

Expected: collection fails with `ModuleNotFoundError: No module named 'short_term_memory'`.

- [ ] **Step 3: Create the package skeleton**

Create `src/short_term_memory/__init__.py`:

```python
"""Redis and Headroom short-term memory SDK."""

__version__ = "0.1.0"
```

Create each subpackage `__init__.py` with a single module docstring. Do not export implementation classes yet.

- [ ] **Step 4: Change distribution metadata without removing dependencies yet**

Update only these `pyproject.toml` project fields in this task:

```toml
[project]
name = "short-term-memory"
version = "0.1.0"
description = "Redis and Headroom short-term memory SDK for agent applications"
```

Keep current dependencies temporarily so legacy tests remain runnable until cutover.

- [ ] **Step 5: Install editable metadata and run the test**

Run:

```bash
/Users/fenghao/PycharmProjects/dream/DREAM/.venv/bin/python -m pip install --no-deps -e .
PYTHONPATH=src /Users/fenghao/PycharmProjects/dream/DREAM/.venv/bin/python -m pytest -q tests/test_public_package.py
```

Expected: `1 passed`.

- [ ] **Step 6: Commit the scaffold**

Run:

```bash
git add pyproject.toml tests/test_public_package.py
git add src/short_term_memory/__init__.py
git add src/short_term_memory/api/__init__.py
git add src/short_term_memory/storage/__init__.py
git add src/short_term_memory/compression/__init__.py
git add src/short_term_memory/jobs/__init__.py
git commit -m "refactor: scaffold standalone short term memory package"
```

### Task 3: Extract shared models, ports, and short-term configuration

**Files:**
- Create: `src/short_term_memory/models.py`
- Create: `src/short_term_memory/ports.py`
- Create: `src/short_term_memory/config.py`
- Create: `tests/test_config.py`
- Create: `tests/test_models.py`
- Reference: `src/dream/config.py`
- Reference: `src/dream/memory/session_compression.py`
- Reference: `src/dream/api/conversation_handler.py`

**Interfaces:**
- Produces: `ShortTermMemorySettings`, `RedisSessionSettings`, `HeadroomServiceSettings`, `load_settings`, `PreparedTurn`, `CompletionResult`, `SessionSummaryDocument`, `HeadroomCompressionResult`, and replaceable port protocols.
- Consumes later: storage, compression, jobs, and API tasks import only from these new files.

- [ ] **Step 1: Write configuration tests**

Create `tests/test_config.py` with tests that clear the listed environment variables and assert:

```python
from pathlib import Path

import pytest

from short_term_memory.config import load_settings


def test_default_short_term_settings(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "missing.env")
    assert settings.environment == "development"
    assert settings.redis_session.url == "redis://127.0.0.1:6379/0"
    assert settings.redis_session.ttl_seconds == 43_200
    assert settings.redis_session.history_turns == 10
    assert settings.redis_session.trigger_ratio == 0.65
    assert settings.headroom_service.ccr_ttl_seconds == 43_200


def test_production_requires_headroom_and_scope_secret(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".env"
    path.write_text("SHORT_TERM_MEMORY_ENV=production\n", encoding="utf-8")
    with pytest.raises(ValueError, match="HEADROOM_SERVICE_URL"):
        load_settings(path)


@pytest.mark.parametrize("ratio", ["0.59", "0.71"])
def test_trigger_ratio_must_match_plan(tmp_path: Path, ratio: str) -> None:
    path = tmp_path / ".env"
    path.write_text(f"HEADROOM_TRIGGER_RATIO={ratio}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="between 0.60 and 0.70"):
        load_settings(path)
```

Add a test proving process environment values override `.env` values and a test proving production succeeds only when both `HEADROOM_SERVICE_URL` and `SHORT_TERM_MEMORY_SCOPE_SECRET` are non-empty.

- [ ] **Step 2: Write model round-trip tests**

Create `tests/test_models.py` that constructs `SessionSummaryDocument`, serializes with `model_dump_json()`, validates it back, and asserts the existing fields survive unchanged:

```python
from short_term_memory.models import (
    SessionCompressionContext,
    SessionSummaryCoverage,
    SessionSummaryDocument,
)


def test_summary_envelope_round_trips_without_schema_change() -> None:
    document = SessionSummaryDocument(
        user_id="user-1",
        session_id="session-1",
        coverage=SessionSummaryCoverage(processed_message_count=8),
        current_goal=["finish Redis session support"],
        preferences=[],
        confirmed_facts=["journals preserve originals"],
        pending_items=[],
        attachment_references=[],
        compression_context=SessionCompressionContext(
            messages=[], tokens_before=100, tokens_after=60
        ),
        updated_at="2026-08-04T00:00:00+00:00",
    )
    restored = SessionSummaryDocument.model_validate_json(
        document.model_dump_json()
    )
    assert restored == document
```

- [ ] **Step 3: Verify the tests fail**

Run:

```bash
PYTHONPATH=src /Users/fenghao/PycharmProjects/dream/DREAM/.venv/bin/python -m pytest -q tests/test_config.py tests/test_models.py
```

Expected: import errors for the three new modules.

- [ ] **Step 4: Define shared models**

Move these definitions without changing their serialized fields from the current source into `src/short_term_memory/models.py`:

```text
PreparedTurn
CompletionResult
HeadroomCompressionStatus
HeadroomFailureReason
HeadroomCompressionResult
SessionAttachmentReference
SessionSummaryPayload
SessionSummaryCoverage
SessionCompressionMessage
SessionCompressionContext
SessionSummaryDocument
HeadroomJobResult
```

Define `PreparedTurn` with a concrete `headroom_headers: dict[str, str]` field instead of exposing the internal `OptimizationScope` object. `ConversationHandler.prepare_turn()` will call `OptimizationScope.as_headroom_headers()` before constructing the model. This preserves the public `prepared.headroom_headers` contract without creating a models-to-compression dependency. Do not change Redis, journals, or Pydantic field names.

- [ ] **Step 5: Define dependency ports**

Create `src/short_term_memory/ports.py` with the exact protocols consumed by later tasks:

```python
from typing import Any, Callable, Literal, Mapping, Protocol

from short_term_memory.models import (
    HeadroomCompressionResult,
    SessionSummaryPayload,
)


class TokenEstimator(Protocol):
    def estimate(self, messages: tuple[dict[str, Any], ...]) -> int: ...


class CompressionClient(Protocol):
    def compress(
        self,
        messages: tuple[dict[str, Any], ...],
        *,
        model: str,
        correlation_id: str | None = None,
        scope_headers: Mapping[str, str] | None = None,
    ) -> HeadroomCompressionResult: ...


class SummaryModel(Protocol):
    def summarize(
        self, messages: tuple[dict[str, Any], ...]
    ) -> SessionSummaryPayload: ...


class BackgroundExecutor(Protocol):
    def submit(self, function: Callable[..., object], *args: object) -> object: ...


class RetryQueue(Protocol):
    def schedule(
        self,
        user_id: str,
        session_id: str,
        messages: tuple[dict[str, Any], ...],
        processed_message_count: int,
        keep_recent_turns: int,
        failure_stage: Literal["compression", "summary"],
        failure_reason: str,
    ) -> None: ...


class SessionSummaryStore(Protocol):
    def store_compression_result(
        self,
        user_id: str,
        session_id: str,
        summary: str,
        processed_message_count: int,
        keep_recent_turns: int,
    ) -> None: ...


class SessionCompressionQueue(Protocol):
    def enqueue(
        self,
        user_id: str,
        session_id: str,
        messages: tuple[dict[str, Any], ...],
        processed_message_count: int,
        keep_recent_turns: int,
    ) -> None: ...
```

Import `Literal` from `typing`. Do not introduce provider-specific SDK types.

- [ ] **Step 6: Implement short-term-only settings**

Create `src/short_term_memory/config.py` by retaining the existing dotenv parser and validation helpers but defining only:

```python
@dataclass(frozen=True)
class RedisSessionSettings:
    url: str = "redis://127.0.0.1:6379/0"
    ttl_seconds: int = 43_200
    history_turns: int = 10
    context_window_tokens: int = 128_000
    trigger_ratio: float = 0.65
    max_messages: int = 100
    max_session_seconds: int = 14_400


@dataclass(frozen=True)
class HeadroomServiceSettings:
    url: str = ""
    timeout_seconds: float = 300.0
    compression_model: str = "gpt-4o"
    ccr_ttl_seconds: int = 43_200


@dataclass(frozen=True)
class ShortTermMemorySettings:
    environment: str = "development"
    home: str = "~/.dream"
    optimization_scope_secret: str = "development-only-scope-secret"
    redis_session: RedisSessionSettings = field(
        default_factory=RedisSessionSettings
    )
    headroom_service: HeadroomServiceSettings = field(
        default_factory=HeadroomServiceSettings
    )
```

Map only the environment names confirmed in the specification. Do not read old `DREAM_REVIEW_*`, Curator, Internship, Persona, or validation variables.

- [ ] **Step 7: Run model/config tests and Ruff**

Run:

```bash
PYTHONPATH=src /Users/fenghao/PycharmProjects/dream/DREAM/.venv/bin/python -m pytest -q tests/test_config.py tests/test_models.py
/Users/fenghao/PycharmProjects/dream/DREAM/.venv/bin/ruff check src/short_term_memory/models.py src/short_term_memory/ports.py src/short_term_memory/config.py tests/test_config.py tests/test_models.py
```

Expected: all selected tests pass and Ruff reports `All checks passed!`.

- [ ] **Step 8: Commit contracts and settings**

Run:

```bash
git add src/short_term_memory/models.py src/short_term_memory/ports.py src/short_term_memory/config.py
git add tests/test_config.py tests/test_models.py
git commit -m "refactor: extract short term memory contracts and settings"
```

### Task 4: Migrate Redis and journals storage

**Files:**
- Create: `src/short_term_memory/storage/redis_session_context.py`
- Move: `src/dream/api/redis_runtime.py` → `src/short_term_memory/storage/redis_runtime.py`
- Move: `src/dream/storage/journal_store.py` → `src/short_term_memory/storage/journal_store.py`
- Move: `src/dream/storage/vfs_adapter.py` → `src/short_term_memory/storage/vfs_adapter.py`
- Move: `tests/api/fake_redis.py` → `tests/storage/fake_redis.py`
- Move: `tests/api/test_redis_runtime.py` → `tests/storage/test_redis_runtime.py`
- Move: `tests/api/test_redis_session_context.py` → `tests/storage/test_redis_session_context.py`
- Modify: `tests/storage/test_journal_store.py`
- Modify: `tests/storage/test_vfs_adapter.py`
- Move: `tests/integrations/test_redis_session_context.py` → `tests/integration/test_redis_session_context.py`
- Reference: `src/dream/api/conversation_handler.py`

**Interfaces:**
- Consumes: `SessionSummaryDocument` from `models.py` and recovery/store protocols from `ports.py`.
- Produces: `RedisRuntime`, `RedisSessionContext`, `CompressionSnapshot`, `JournalStore`, and journals-only `VFSAdapter`.

- [ ] **Step 1: Move storage files and tests with Git history**

Run explicit `git mv` commands for each move listed above. Create `tests/integration/__init__.py` if it does not exist. Do not move unrelated application or storage tests.

- [ ] **Step 2: Update moved test imports to the new package**

Change imports such as:

```python
from short_term_memory.storage.journal_store import JournalStore
from short_term_memory.storage.redis_runtime import RedisRuntime
from short_term_memory.storage.redis_session_context import RedisSessionContext
from short_term_memory.storage.vfs_adapter import VFSAdapter
from tests.storage.fake_redis import FakeRedis
```

Run the moved tests now. Expected: collection fails because `redis_session_context.py` has not been created and moved source imports still point to `dream`.

- [ ] **Step 3: Extract Redis session context**

Move these current definitions from `dream/api/conversation_handler.py` into `storage/redis_session_context.py` without behavioral changes:

```text
RedisClient
SummarySnapshotReader
RecoveryCompressionQueue
CompressionSnapshot
RedisSessionContext
```

Import shared summary models from `short_term_memory.models`. Preserve exactly:

```python
return f"dream:session:{user}:{session}:messages"
return f"dream:session:{user}:{session}:summary"
```

Preserve the half-day default TTL, summary freshness check, journals recovery, attachment markers, safe LTRIM, and concurrent-message behavior.

- [ ] **Step 4: Reduce VFS to journals only**

Replace `UserPaths` with:

```python
@dataclass(frozen=True)
class UserPaths:
    root: Path
    journals: Path

    def directories(self) -> tuple[Path, ...]:
        return (self.journals,)
```

Update `VFSAdapter.paths()` to create only the user root and `journals` path. Remove raw, source, Wiki, and `_ops` fields and their test assertions.

- [ ] **Step 5: Update moved implementation imports**

Use only new package imports:

```python
from short_term_memory.models import SessionSummaryDocument
from short_term_memory.storage.vfs_adapter import VFSAdapter, safe_component
```

No file under `src/short_term_memory/storage` may import `dream` or Headroom-specific modules.

Update the still-temporary legacy orchestrators to import the migrated storage
classes so the branch remains runnable between commits:

```text
src/dream/api/conversation_handler.py
src/dream/api/short_term_runtime.py
tests/api/test_conversation_handler.py
tests/application/test_short_term_runtime.py
```

Both remaining tests that used `tests.api.fake_redis` must import
`tests.storage.fake_redis` after the move. These temporary `dream` orchestrator
files are removed in later tasks; do not add forwarding modules under the old
storage paths.

- [ ] **Step 6: Run storage tests**

Run:

```bash
PYTHONPATH=src /Users/fenghao/PycharmProjects/dream/DREAM/.venv/bin/python -m pytest -q tests/storage tests/integration/test_redis_session_context.py
```

Expected: all storage tests pass; real Redis tests remain skipped unless their opt-in variable is set.

Then run the current full suite and require zero failures before committing:

```bash
PYTHONPATH=src /Users/fenghao/PycharmProjects/dream/DREAM/.venv/bin/python -m pytest -q
```

- [ ] **Step 7: Verify data-compatibility literals**

Run:

```bash
rg -n "dream:session|journals|raw|source|wiki" src/short_term_memory/storage
```

Expected: Redis key prefix and journals appear; `raw`, `source`, and `wiki` do not appear as managed directories. Attachment rendering may contain the word `attachment` only.

- [ ] **Step 8: Commit storage migration**

Stage every moved/created storage and storage-test path explicitly, then run:

```bash
git commit -m "refactor: migrate redis session and journals storage"
```

### Task 5: Migrate the compression boundary

**Files:**
- Move: `src/dream/api/optimization_scope.py` → `src/short_term_memory/compression/scope.py`
- Move: `src/dream/integrations/headroom_client.py` → `src/short_term_memory/compression/headroom_client.py`
- Move: `src/dream/integrations/headroom_telemetry.py` → `src/short_term_memory/compression/telemetry.py`
- Create: `src/short_term_memory/compression/policy.py`
- Create: `src/short_term_memory/compression/summary.py`
- Move: `tests/api/test_optimization_scope.py` → `tests/compression/test_scope.py`
- Move: `tests/integrations/test_headroom_client.py` → `tests/compression/test_headroom_client.py`
- Move: `tests/integrations/test_headroom_telemetry.py` → `tests/compression/test_telemetry.py`
- Create: `tests/compression/test_policy.py`
- Create: `tests/compression/test_summary.py`
- Move: `tests/headroom/test_headroom_auto_routing.py` → `tests/integration/test_headroom_auto_routing.py`
- Move: `tests/headroom/test_headroom_proxy_ccr_flow.py` → `tests/integration/test_headroom_proxy_ccr_flow.py`
- Move: `tests/headroom/fake_openai_provider.py` → `tests/integration/fake_openai_provider.py`
- Reference: `src/dream/api/conversation_handler.py`
- Reference: `src/dream/memory/session_compression.py`

**Interfaces:**
- Consumes: shared models/ports from Task 3.
- Produces: `HeadroomPolicy`, `HeadroomHttpClient`, `SessionSummaryGenerator`, `OptimizationScopeFactory`, and `InMemoryHeadroomTelemetry`.

- [ ] **Step 1: Move intact Headroom files and their tests**

Use explicit `git mv` for scope, client, telemetry, and the listed tests. Update imports to `short_term_memory.models`, `short_term_memory.ports`, and `short_term_memory.compression.*`.

- [ ] **Step 2: Write the policy test before extraction**

Create `tests/compression/test_policy.py`:

```python
import pytest

from short_term_memory.compression.policy import HeadroomPolicy


def test_any_plan_threshold_triggers_compression() -> None:
    policy = HeadroomPolicy(
        context_window_tokens=1000,
        trigger_ratio=0.65,
        max_messages=100,
        max_session_seconds=3600,
    )
    assert policy.should_compress(
        estimated_tokens=650, message_count=1, session_seconds=1
    )
    assert policy.should_compress(
        estimated_tokens=1, message_count=100, session_seconds=1
    )
    assert policy.should_compress(
        estimated_tokens=1, message_count=1, session_seconds=3600
    )


@pytest.mark.parametrize("ratio", [0.59, 0.71])
def test_policy_rejects_ratio_outside_plan(ratio: float) -> None:
    with pytest.raises(ValueError, match="between 0.60 and 0.70"):
        HeadroomPolicy(1000, ratio, 100, 3600)
```

Run it and expect import failure before creating `policy.py`.

- [ ] **Step 3: Extract HeadroomPolicy**

Move `HeadroomPolicy` unchanged from the current conversation handler into `compression/policy.py`. Keep the inclusive `0.60 <= ratio <= 0.70` validation and OR trigger semantics.

- [ ] **Step 4: Extract summary generation**

Move only these elements from `dream/memory/session_compression.py` into `compression/summary.py`:

```text
SUMMARY_INSTRUCTION
SessionSummaryProviderError
SessionSummaryGenerator
_strip_json_fence
_summary_input
_validate_attachment_references
_message_text
```

Import summary data models from `short_term_memory.models`. Keep the five allowed categories and attachment reference validation unchanged.

- [ ] **Step 5: Split existing summary tests**

Move summary-provider and summary-validation tests from `tests/headroom/test_session_compression.py` into `tests/compression/test_summary.py`. Do not move job/store/retry tests yet.

Update the temporary `src/dream/api/conversation_handler.py`,
`src/dream/api/short_term_runtime.py`, and
`tests/application/test_short_term_runtime.py` imports to use the migrated
scope, telemetry, client, policy, and summary modules. This is an internal
migration bridge only; it is not a published `dream.*` compatibility layer.

- [ ] **Step 6: Run compression tests**

Run:

```bash
PYTHONPATH=src /Users/fenghao/PycharmProjects/dream/DREAM/.venv/bin/python -m pytest -q tests/compression tests/integration/test_headroom_auto_routing.py
```

Expected: deterministic compression tests pass; real auto-routing tests remain skipped unless opted in.

Run the current full suite and require zero failures before committing.

- [ ] **Step 7: Verify the replaceable service boundary**

Run:

```bash
rg -n "import headroom|from headroom|Kompress|ONNX" src/short_term_memory
```

Expected: no Headroom Python import and no model/runtime implementation. Documentation strings may describe external behavior, but runtime code may not select a compressor.

- [ ] **Step 8: Commit compression migration**

Stage the exact compression source/test paths and their old moved paths, then run:

```bash
git commit -m "refactor: isolate headroom compression boundary"
```

### Task 6: Extract the background compression job

**Files:**
- Create: `src/short_term_memory/jobs/session_compression_job.py`
- Create: `tests/jobs/test_session_compression_job.py`
- Delete after split: `tests/headroom/test_session_compression.py`
- Reference: `src/dream/memory/session_compression.py`

**Interfaces:**
- Consumes: `CompressionClient`, `SummaryModel`, `RetryQueue`, and `BackgroundExecutor` ports; summary/compression result models; Redis summary-store port.
- Produces: `SessionCompressionJob` and `ExecutorSessionCompressionQueue`.

- [ ] **Step 1: Move job tests before implementation**

Move the remaining job, fallback, retry, store, trim, and executor tests from `tests/headroom/test_session_compression.py` into `tests/jobs/test_session_compression_job.py`. Rename imports and expected class names:

```text
HeadroomCompressionJob -> SessionCompressionJob
ExecutorHeadroomCompressionQueue -> ExecutorSessionCompressionQueue
```

Run the new job test and expect import failure.

- [ ] **Step 2: Extract and rename the job**

Create `jobs/session_compression_job.py` from the current job logic. Preserve this production failure gate:

```python
if compression.status is HeadroomCompressionStatus.FAILED:
    if not compression.fallback_used:
        schedule_compression_retry(...)
        return HeadroomJobResult(
            compression_status=compression.status,
            fallback_used=False,
            summary_written=False,
            compression_applied=False,
            failure_reason=compression.failure_reason,
        )
```

After success or development fallback, call the injected SummaryModel, validate attachments, build the existing `SessionSummaryDocument`, and invoke the store port. Do not add a direct Redis or model-provider import.

- [ ] **Step 3: Preserve asynchronous execution**

Implement the renamed queue with the existing semantics:

```python
class ExecutorSessionCompressionQueue:
    def __init__(
        self,
        job: SessionCompressionJob,
        executor: BackgroundExecutor,
    ) -> None:
        self.job = job
        self.executor = executor

    def enqueue(
        self,
        user_id: str,
        session_id: str,
        messages: tuple[dict[str, Any], ...],
        processed_message_count: int,
        keep_recent_turns: int,
    ) -> None:
        self.executor.submit(
            self.job.run,
            user_id,
            session_id,
            messages,
            processed_message_count,
            keep_recent_turns,
        )
```

Update the temporary `src/dream/api/short_term_runtime.py` and its test to
import `SessionCompressionJob`, `ExecutorSessionCompressionQueue`, and the
summary types from their new modules. After all definitions and tests have
been moved, delete `src/dream/memory/session_compression.py` with
`apply_patch`; do not leave a forwarding module.

- [ ] **Step 4: Run job tests and storage concurrency regression tests**

Run:

```bash
PYTHONPATH=src /Users/fenghao/PycharmProjects/dream/DREAM/.venv/bin/python -m pytest -q tests/jobs/test_session_compression_job.py tests/storage/test_redis_session_context.py
```

Expected: all selected tests pass, including production no-write/no-trim and concurrent-message preservation.

Run the current full suite and require zero failures before committing.

- [ ] **Step 5: Commit job extraction**

Stage `src/short_term_memory/jobs/session_compression_job.py`,
`tests/jobs/test_session_compression_job.py`, and the exact removed old source
and test paths, then run:

```bash
git commit -m "refactor: extract session compression background job"
```

### Task 7: Migrate and publish the Agent-facing runtime

**Files:**
- Move and reduce: `src/dream/api/conversation_handler.py` → `src/short_term_memory/api/conversation_handler.py`
- Move and update: `src/dream/api/short_term_runtime.py` → `src/short_term_memory/api/runtime.py`
- Modify: `tests/api/test_conversation_handler.py`
- Move: `tests/application/test_short_term_runtime.py` → `tests/api/test_runtime.py`
- Modify: `src/short_term_memory/__init__.py`
- Modify: `tests/test_public_package.py`

**Interfaces:**
- Consumes: all prior storage, compression, job, model, port, and config interfaces.
- Produces: root exports `build_runtime`, `ShortTermMemorySettings`, `PreparedTurn`, and `CompletionResult`, plus runtime facade methods `prepare_turn()` and `complete_turn()`.

- [ ] **Step 1: Change public API tests first**

Extend `tests/test_public_package.py`:

```python
from short_term_memory import (
    CompletionResult,
    PreparedTurn,
    ShortTermMemorySettings,
    build_runtime,
)


def test_root_package_exports_only_agent_facing_contract() -> None:
    assert callable(build_runtime)
    assert ShortTermMemorySettings.__name__ == "ShortTermMemorySettings"
    assert PreparedTurn.__name__ == "PreparedTurn"
    assert CompletionResult.__name__ == "CompletionResult"
```

Run the test and expect import failure for the not-yet-exported names.

- [ ] **Step 2: Move and reduce ConversationHandler**

Move the current file with Git history, then remove the definitions already migrated to storage, policy, models, and ports. The resulting file contains only `ConversationHandler` and imports:

```python
from short_term_memory.compression.policy import HeadroomPolicy
from short_term_memory.models import CompletionResult, PreparedTurn
from short_term_memory.ports import TokenEstimator
from short_term_memory.storage.journal_store import JournalStore
from short_term_memory.storage.redis_session_context import RedisSessionContext
```

Keep the `prepare_turn()` and `complete_turn()` behavior unchanged.

When constructing `PreparedTurn`, convert the internal scope immediately:

```python
scope = self.optimization_scope_factory.for_session(user_id, session_id)
return PreparedTurn(
    user_id=user_id,
    session_id=session_id,
    history=(*prior_history, user_message),
    timestamp=timestamp,
    session_seconds=session_seconds,
    headroom_headers=scope.as_headroom_headers(),
    headroom_proxy_url=self.headroom_proxy_url,
)
```

- [ ] **Step 3: Move runtime composition**

Rename `build_short_term_runtime` to `build_runtime` and update all imports to the new package. Define the runtime facade:

```python
@dataclass(frozen=True)
class ShortTermMemoryRuntime:
    session_context: RedisSessionContext
    conversation_handler: ConversationHandler
    compression_client: CompressionClient
    compression_job: SessionCompressionJob
    compression_queue: ExecutorSessionCompressionQueue
    telemetry: HeadroomTelemetry

    def prepare_turn(
        self,
        user_id: str,
        session_id: str,
        content: str,
        *,
        timestamp: datetime | None = None,
        session_seconds: int = 0,
    ) -> PreparedTurn:
        return self.conversation_handler.prepare_turn(
            user_id,
            session_id,
            content,
            timestamp=timestamp,
            session_seconds=session_seconds,
        )

    def complete_turn(
        self,
        prepared: PreparedTurn,
        *,
        assistant_content: str,
    ) -> CompletionResult:
        return self.conversation_handler.complete_turn(
            prepared,
            assistant_content=assistant_content,
        )
```

`build_runtime()` accepts the existing injected Redis client, TokenEstimator, SummaryModel, executor, retry queue, optional CompressionClient, telemetry, and clock.

- [ ] **Step 4: Publish the stable root API**

Replace `src/short_term_memory/__init__.py` with:

```python
"""Redis and Headroom short-term memory SDK."""

from short_term_memory.api.runtime import build_runtime
from short_term_memory.config import ShortTermMemorySettings
from short_term_memory.models import CompletionResult, PreparedTurn

__all__ = [
    "build_runtime",
    "ShortTermMemorySettings",
    "PreparedTurn",
    "CompletionResult",
]
__version__ = "0.1.0"
```

- [ ] **Step 5: Update and run API/runtime tests**

Update all moved tests to import only `short_term_memory`. Add assertions that `runtime.prepare_turn()` and `runtime.complete_turn()` delegate correctly and that `PreparedTurn` exposes the OpenAI-compatible Proxy URL and de-identified headers.

Run:

```bash
PYTHONPATH=src /Users/fenghao/PycharmProjects/dream/DREAM/.venv/bin/python -m pytest -q tests/api tests/test_public_package.py
```

Expected: all API/runtime/public-package tests pass.

- [ ] **Step 6: Run the complete new-package deterministic suite**

Run:

```bash
PYTHONPATH=src /Users/fenghao/PycharmProjects/dream/DREAM/.venv/bin/python -m pytest -q tests/api tests/storage tests/compression tests/jobs tests/test_config.py tests/test_models.py tests/test_public_package.py
```

Expected: all selected tests pass with zero external service requirements.

- [ ] **Step 7: Commit the Agent API**

Stage the exact API/runtime/root package files and moved tests, then run:

```bash
git commit -m "refactor: expose standalone short term memory sdk"
```

### Task 8: Cut over tests and remove the inherited DREAM architecture

**Files:**
- Delete after explicit confirmation: `src/dream/**`
- Delete after explicit confirmation: unrelated tests under `tests/application`, `tests/core`, `tests/curators`, `tests/e2e`, `tests/extraction`, `tests/governance`, `tests/headroom`, `tests/memory`, `tests/retrieval`, and old `tests/integrations`
- Delete: `docs/ai-evolution-and-user-persona.md`
- Delete: `docs/dream-mechanism.md`
- Modify: `pyproject.toml`
- Modify: `.env.example`

**Interfaces:**
- Consumes: the fully passing `short_term_memory` implementation and migrated tests.
- Produces: no legacy `dream` package, no unrelated tests/docs, and minimal runtime dependencies.

- [ ] **Step 1: Print the exact deletion manifest and stop for confirmation**

Run read-only commands:

```bash
rg --files src/dream | sort
rg --files tests/application tests/core tests/curators tests/e2e tests/extraction tests/governance tests/headroom tests/memory tests/retrieval tests/integrations | sort
printf '%s\n' docs/ai-evolution-and-user-persona.md docs/dream-mechanism.md
```

Present the complete output to the user. Do not delete anything until the user explicitly confirms that manifest.

- [ ] **Step 2: Remove only confirmed files with apply_patch**

Use `apply_patch` `Delete File` entries for the exact confirmed files. Do not delete directories recursively. Empty directories disappear from Git automatically.

- [ ] **Step 3: Rewrite runtime dependencies**

Set `pyproject.toml` runtime dependencies to:

```toml
dependencies = [
  "httpx>=0.28,<1",
  "pydantic>=2.13,<3",
  "redis==6.4.0",
]
```

Keep pytest and Ruff in `dev`. Add `build>=1,<2` to `dev` for the wheel acceptance task. Remove FastAPI, Uvicorn, OpenAI, and any Headroom dependency.

- [ ] **Step 4: Rewrite `.env.example` to short-term settings only**

The final file contains exactly the confirmed component variables and explanatory comments. It must not include Review, Curator, Persona, Internship, Wiki, indexing, or validation configuration.

- [ ] **Step 5: Verify no legacy import or module remains**

Run:

```bash
rg -n "from dream|import dream" src tests
test ! -d src/dream
test ! -e src/dream/__init__.py
```

Expected: `rg` returns no matches and both `test` commands succeed.

- [ ] **Step 6: Run the full remaining suite**

Run:

```bash
PYTHONPATH=src /Users/fenghao/PycharmProjects/dream/DREAM/.venv/bin/python -m pytest -q
/Users/fenghao/PycharmProjects/dream/DREAM/.venv/bin/ruff check src tests
```

Expected: every remaining deterministic test passes; opt-in Redis/Headroom tests are skipped unless explicitly enabled; Ruff reports `All checks passed!`.

- [ ] **Step 7: Commit the cutover**

Stage the new configuration/dependency files explicitly, then stage deletions only under each confirmed old path with `git add -u <exact-path>`. Review `git diff --cached --name-status` before committing.

Run:

```bash
git commit -m "refactor: remove inherited dream architecture"
```

### Task 9: Update documentation and verify the wheel

**Files:**
- Modify: `README.md`
- Modify: `docs/short-term-memory.md`
- Modify: `docs/third_party/redis.md`
- Modify: `docs/third_party/headroom.md`
- Create: `tests/test_distribution_contents.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: the final standalone source tree and public API.
- Produces: accurate installation/integration docs and clean-install evidence.

- [ ] **Step 1: Write a distribution-content test**

Create `tests/test_distribution_contents.py`:

```python
from pathlib import Path
from zipfile import ZipFile


def test_built_wheel_contains_only_short_term_package() -> None:
    wheels = sorted(Path("dist").glob("short_term_memory-*.whl"))
    assert len(wheels) == 1
    with ZipFile(wheels[0]) as archive:
        names = archive.namelist()
    assert any(name.startswith("short_term_memory/") for name in names)
    assert not any(name.startswith("dream/") for name in names)
    forbidden = ("curator", "persona", "decision_card", "retrieval", "wiki")
    assert not any(
        term in name.casefold() for term in forbidden for name in names
    )
```

- [ ] **Step 2: Update documentation imports and scope**

Replace every public example with:

```python
from short_term_memory import build_runtime
```

Document only Redis, journals, Headroom compression/CCR boundary, summary, and Agent pre/post-turn integration. Remove remaining old `dream.*` imports and references to old project capabilities.

- [ ] **Step 3: Update third-party descriptions**

Change third-party documents from “DREAM uses” to “short-term-memory uses” while preserving verified Redis version/tag/license and Headroom version/license/deployment information. Continue to state that no Redis or Headroom source/model is vendored.

- [ ] **Step 4: Build a clean wheel**

Run:

```bash
/Users/fenghao/PycharmProjects/dream/DREAM/.venv/bin/python -m build
```

Expected: one source distribution and one `short_term_memory-0.1.0-*.whl` are created under `dist/`.

- [ ] **Step 5: Run the wheel-content test**

Run:

```bash
PYTHONPATH=src /Users/fenghao/PycharmProjects/dream/DREAM/.venv/bin/python -m pytest -q tests/test_distribution_contents.py
```

Expected: `1 passed`.

- [ ] **Step 6: Install and import in a temporary environment**

Create a temporary directory with `mktemp -d`, create a virtual environment under that exact directory, install the local wheel, and run:

```python
import short_term_memory
from short_term_memory import (
    CompletionResult,
    PreparedTurn,
    ShortTermMemorySettings,
    build_runtime,
)

assert short_term_memory.__version__ == "0.1.0"
assert callable(build_runtime)
```

Expected: the script exits 0 and `import dream` fails in that clean environment.

- [ ] **Step 7: Run opt-in service acceptance when services are available**

Real Redis:

```bash
SHORT_TERM_MEMORY_RUN_REDIS_INTEGRATION=1 \
REDIS_URL=redis://127.0.0.1:6379/15 \
PYTHONPATH=src /Users/fenghao/PycharmProjects/dream/DREAM/.venv/bin/python -m pytest -q -s tests/integration/test_redis_session_context.py
```

Real Headroom routing:

```bash
SHORT_TERM_MEMORY_RUN_HEADROOM_AUTO_ROUTING=1 \
HEADROOM_SERVICE_URL=http://127.0.0.1:8787 \
PYTHONPATH=src /Users/fenghao/PycharmProjects/dream/DREAM/.venv/bin/python -m pytest -q -s tests/integration/test_headroom_auto_routing.py
```

Official Proxy/CCR acceptance:

```bash
SHORT_TERM_MEMORY_RUN_HEADROOM_PROXY_CCR=1 \
SHORT_TERM_MEMORY_HEADROOM_BINARY="$HOME/.local/bin/headroom" \
PYTHONPATH=src /Users/fenghao/PycharmProjects/dream/DREAM/.venv/bin/python -m pytest -q -s tests/integration/test_headroom_proxy_ccr_flow.py
```

Record actual results. Do not convert a skipped or vendor-blocked CCR test into a pass claim.

- [ ] **Step 8: Run final verification**

Run:

```bash
PYTHONPATH=src /Users/fenghao/PycharmProjects/dream/DREAM/.venv/bin/python -m pytest -q
/Users/fenghao/PycharmProjects/dream/DREAM/.venv/bin/ruff check src tests
git diff --check
git status --short
```

Expected: all deterministic tests pass, Ruff and diff checks pass, and only intended documentation/test changes remain unstaged.

- [ ] **Step 9: Commit documentation and distribution acceptance**

Run:

```bash
git add README.md docs/short-term-memory.md docs/third_party/redis.md docs/third_party/headroom.md
git add pyproject.toml tests/test_distribution_contents.py
git commit -m "chore: validate standalone short term memory distribution"
```

After commit, run `git status --short` and expect no output.
