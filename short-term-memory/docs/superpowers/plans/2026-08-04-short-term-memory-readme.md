# Short-Term Memory README Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the inherited README with an evidence-based guide for the Redis and Headroom short-term-memory branch.

**Architecture:** The README maps PLAN.md sections 5.1–5.4 to the existing `ConversationHandler`, `RedisSessionContext`, journals, Headroom HTTP adapter, background compression job, and company Agent proxy boundary. It separates deterministic DREAM implementation from official Headroom CCR behavior that still needs a real-provider acceptance test.

**Tech Stack:** Markdown, Mermaid, Python 3.11–3.13, redis-py, Redis, HTTPX, official Headroom Proxy.

## Global Constraints

- Document only the short-term-memory branch scope.
- Redis is the default online read source; journals are the durable event log.
- Headroom owns compression routing and official CCR internals.
- DREAM does not call the final answering model.
- Do not claim transparent CCR continuation is verified unless the real acceptance test passes.

---

### Task 1: Rewrite the root README

**Files:**
- Modify: `README.md`
- Reference: `/Users/fenghao/PycharmProjects/dream/PLAN.md`
- Reference: `docs/short-term-memory.md`

**Interfaces:**
- Consumes: `ConversationHandler.prepare_turn(...) -> PreparedTurn` and `ConversationHandler.complete_turn(...) -> CompletionResult`.
- Produces: A reviewer-facing README with PLAN mapping, runtime flow, setup, and acceptance status.

- [ ] **Step 1: Replace inherited content**

Write a branch-specific README whose first-level sections cover scope, PLAN
mapping, architecture, Redis storage, compression, CCR, Agent integration,
configuration, startup, tests, and limitations.

- [ ] **Step 2: Scan for inherited architecture language**

Run:

```bash
rg -n "用户画像|Decision Card|decision-cards|Curator|Memory Retrieval" README.md
```

Expected: only an explicit out-of-scope statement, with no old-system usage
guide.

- [ ] **Step 3: Verify documented paths exist**

Run:

```bash
test -f src/dream/api/conversation_handler.py \
  && test -f src/dream/integrations/headroom_client.py \
  && test -f src/dream/memory/session_compression.py \
  && test -f src/dream/storage/journal_store.py
```

Expected: exit status 0.

### Task 2: Verify the documented implementation status

**Files:**
- Test: `tests/api/test_conversation_handler.py`
- Test: `tests/api/test_redis_session_context.py`
- Test: `tests/headroom/test_session_compression.py`
- Test: `tests/integrations/test_headroom_client.py`
- Test: `tests/application/test_short_term_runtime.py`

**Interfaces:**
- Consumes: The status claims written in Task 1.
- Produces: Fresh deterministic test and lint evidence for those claims.

- [ ] **Step 1: Run focused tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/api/test_conversation_handler.py \
  tests/api/test_redis_session_context.py \
  tests/headroom/test_session_compression.py \
  tests/integrations/test_headroom_client.py \
  tests/application/test_short_term_runtime.py
```

Expected: all selected deterministic tests pass.

- [ ] **Step 2: Run focused Ruff checks**

Run:

```bash
.venv/bin/python -m ruff check \
  src/dream/api/conversation_handler.py \
  src/dream/api/short_term_runtime.py \
  src/dream/integrations/headroom_client.py \
  src/dream/memory/session_compression.py \
  src/dream/storage/journal_store.py \
  tests/api/test_conversation_handler.py \
  tests/api/test_redis_session_context.py \
  tests/headroom/test_session_compression.py \
  tests/integrations/test_headroom_client.py \
  tests/application/test_short_term_runtime.py
```

Expected: `All checks passed!`.

- [ ] **Step 3: Review the final diff**

Run:

```bash
git diff --check
git diff -- README.md docs/plans/2026-08-04-short-term-memory-readme-design.md docs/superpowers/plans/2026-08-04-short-term-memory-readme.md
```

Expected: no whitespace errors; diff contains documentation changes only.
