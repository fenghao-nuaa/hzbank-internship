# Headroom Memory HTTP and DeepSeek Integration Design

**Date:** 2026-08-06

**Status:** Approved design

## 1. Goal

Extend `short-term-memory` into an HTTP memory service that:

- exposes exactly two business APIs, one for writing memory and one for reading memory;
- accepts conversation, code, document, and skill content;
- preserves immutable original events while delegating compression, CCR cache management, hash generation, retrieval, and automatic LLM continuation to Headroom;
- prevents Headroom output from being compressed again on later turns;
- supports an independent DeepSeek chat call through the official OpenAI-compatible SDK, with Headroom Proxy as the SDK base URL and DeepSeek's official service as Headroom's upstream;
- meets the agreed 100-concurrency latency targets;
- provides deterministic unit/integration tests, opt-in real Headroom and DeepSeek tests, and four content-type examples.

The service remains short-term memory infrastructure. It does not add user profiles, long-term semantic retrieval, Wiki generation, or a chat endpoint.

## 2. Confirmed Boundaries

There are three independent request paths:

```text
Memory write client -> POST /v1/memories/write -> Redis/journals -> background Headroom compression
Memory read client  -> POST /v1/memories/read  -> assembled short-term context
DeepSeek client     -> official OpenAI SDK -> Headroom Proxy -> DeepSeek official API
                                                  -> transparent Headroom CCR
```

The memory service never calls DeepSeek from either business API. The DeepSeek API key stays in the chat caller and is not stored by the memory service.

Headroom owns:

- content routing and all compression algorithms;
- CCR original-content cache storage and eviction behavior;
- CCR TTL enforcement;
- marker and hash creation;
- `headroom_retrieve` tool injection;
- retrieval of the original content;
- automatic continuation of OpenAI-compatible model requests.

The memory service treats Headroom responses as opaque protocol data. It does not import Headroom, inspect or modify Headroom's memory/SQLite backend, implement a hash index, or return original content for a CCR hash.

Headroom's documentation calls the compression store an LRU cache. In the installed Headroom 0.33.0 Python Proxy, the cache has a pluggable backend and the default global backend is SQLite; `HEADROOM_CCR_BACKEND=memory` opts into process memory. This storage-medium detail remains entirely Headroom-owned and does not change the boundary above.

## 3. Data Ownership and Retention

| Data | Storage | Default retention | Owner |
|---|---|---:|---|
| Online originals and recent messages | Redis | 43,200-second sliding TTL | memory service |
| Opaque compressed messages and five-category summary | Redis summary envelope | 43,200-second sliding TTL | memory service stores Headroom output without interpreting it |
| Complete immutable original events | journals JSONL | 30 days | memory service |
| CCR original, compressed representation, hash, retrieval state | Headroom compression store | 43,200 seconds | Headroom |
| DeepSeek chat request/response | caller; written only through the memory write API when desired | caller-defined | chat caller |

Redis and Headroom CCR are short-lived online stores. Journals are the 30-day recoverable source of original events. A daily retention job removes journal files whose latest event is older than `JOURNAL_RETENTION_DAYS`. There is no original-content recovery guarantee after both the journal retention period and Headroom CCR TTL have elapsed.

The memory service must never advertise journals as CCR retrieval. Journals are used only to recover the memory service's state and to resubmit original content to Headroom when an opaque compression segment needs rebuilding.

## 4. Original Event Model

Every accepted event has this logical shape:

```json
{
  "sequence": 101,
  "event_id": "client-generated-id",
  "role": "user",
  "content_type": "code",
  "content": "def hello():\n    return 'world'",
  "metadata": {
    "filename": "example.py",
    "language": "python"
  },
  "created_at": "2026-08-06T08:00:00Z"
}
```

`content_type` is one of:

```text
conversation | code | document | skill
```

`event_id` is required and provides idempotency. `sequence` is monotonically increasing within one `user_id + session_id`. A Redis script atomically reserves the sequence and a short-lived `pending` idempotency record before the journal write. A second atomic script commits the Redis message, changes the idempotency record to `committed`, and refreshes TTL only after the journal fsync succeeds. An abandoned reservation creates a harmless sequence gap and expires; sequence values are ordered identifiers, not a promise of contiguity.

Original content is preserved byte-for-byte as UTF-8 text. Metadata may describe a filename, language, MIME type, document title, or skill name, but metadata cannot replace `content` as the original fact source. A SHA-256 digest is recorded for exact-recovery tests and corruption detection.

## 5. Compression Generations and No-Recompression Invariant

The Redis summary envelope contains semantic summary fields plus zero or more opaque Headroom compression segments. Each segment records:

```json
{
  "generation": 2,
  "from_sequence": 101,
  "through_sequence": 180,
  "messages": [],
  "tokens_before": 20000,
  "tokens_after": 5000,
  "created_at": "2026-08-06T08:00:00Z",
  "ccr_expires_at": "2026-08-06T20:00:00Z"
}
```

`messages` is the exact message list returned by Headroom. The memory service may validate that it is a message-shaped JSON list, but it must not parse markers or rewrite content.

The central invariant is:

> Every call to Headroom `/v1/compress` contains only original events loaded from Redis or journals. No message previously returned by Headroom may be submitted to `/v1/compress`.

After generation 1 covers sequences 1-100, generation 2 may only compress original sequences 101-180. The current high-water mark is the greatest successfully stored `through_sequence`. Failed, timed-out, or stale compression jobs do not advance it.

Read context may combine opaque historical segments with recent original turns for conversational quality. That read-time assembly is not reused as compression input.

When the segment count reaches `HEADROOM_MAX_COMPRESSION_SEGMENTS`, the service loads the covered originals from journals and asks Headroom to create one replacement generation. It does not merge or recompress the old Headroom output. When a segment reaches its CCR refresh window, the same original-only rebuild occurs before the segment is returned to a chat caller. The memory service tracks only the creation and configured expiry timestamps; it does not inspect Headroom's store.

## 6. Business API Contracts

### 6.1 Write memory

`POST /v1/memories/write`

Request:

```json
{
  "user_id": "user-001",
  "session_id": "session-001",
  "events": [
    {
      "event_id": "event-001",
      "role": "user",
      "content_type": "code",
      "content": "def hello():\n    return 'world'",
      "metadata": {
        "filename": "example.py",
        "language": "python"
      }
    }
  ],
  "session_seconds": 120
}
```

Response:

```json
{
  "request_id": "req-001",
  "accepted": true,
  "sequence_from": 101,
  "sequence_through": 101,
  "duplicate_event_ids": [],
  "compression_queued": false,
  "policy_version": "v1",
  "timing_ms": {
    "total": 42.6,
    "redis": 8.1,
    "journal": 28.4,
    "queue": 1.2
  }
}
```

The API accepts one to `MEMORY_WRITE_MAX_BATCH_EVENTS` events. It returns success only after the originals are durable in journals and represented in Redis. Headroom compression remains asynchronous.

The write order uses a short reservation followed by a write-ahead original and an online commit:

```text
validate and authenticate
-> atomically reserve sequence and pending event-ID digest in Redis
-> append the idempotent journal record with that sequence and fsync
-> atomically commit the Redis message/event ID and refresh TTL
-> enqueue compression if policy matches
-> respond
```

If the journal succeeds and the Redis commit fails, the API returns a retryable `503`. Retrying the same `event_id` and digest reuses the reservation, detects the existing journal record, repairs Redis, and does not append another journal record. Reusing an event ID with different content returns `409`. If the process stops after reservation but before journal fsync, no message becomes visible and the pending reservation expires.

### 6.2 Read memory

`POST /v1/memories/read`

Request:

```json
{
  "user_id": "user-001",
  "session_id": "session-001",
  "history_turns": 10,
  "include_effective_config": true
}
```

Response:

```json
{
  "request_id": "req-002",
  "messages": [
    {
      "role": "system",
      "content": "opaque Headroom-compressed context"
    },
    {
      "role": "user",
      "content": "recent original message"
    }
  ],
  "memory": {
    "compressed_through_sequence": 100,
    "latest_sequence": 101,
    "source": "redis",
    "compression_segments": 1
  },
  "headroom": {
    "proxy_url": "http://headroom:8787/v1",
    "scope_headers": {
      "x-headroom-user-id": "opaque-value",
      "x-headroom-session-id": "opaque-value",
      "x-headroom-project-id": "opaque-value"
    }
  },
  "effective_config": {
    "history_turns": 10,
    "redis_ttl_seconds": 43200,
    "ccr_ttl_seconds": 43200,
    "journal_retention_days": 30,
    "trigger_ratio": 0.65
  },
  "timing_ms": {
    "total": 31.5,
    "redis": 12.2,
    "recovery": 0.0,
    "assembly": 3.1
  }
}
```

The read API does not call DeepSeek and does not accept a CCR hash. It returns fresh opaque segments and recent originals. If Redis has expired, it performs bounded journal recovery. Segments inside the refresh window are rebuilt proactively in the background. If a segment is already expired when read, the service rebuilds it from original journal events through Headroom before returning it; that request is classified and measured as a cold rebuild rather than a Redis warm read.

## 7. Independent DeepSeek Call

The chat caller uses the official OpenAI-compatible SDK. The SDK base URL is the Headroom Proxy URL returned by the read API; Headroom is configured with DeepSeek's official API as its upstream.

```python
from openai import OpenAI

memory = memory_client.read(...)

client = OpenAI(
    api_key=deepseek_api_key,
    base_url=memory["headroom"]["proxy_url"],
    default_headers=memory["headroom"]["scope_headers"],
)

response = client.chat.completions.create(
    model="deepseek-v4-flash",
    messages=memory["messages"],
)
```

The default model is `deepseek-v4-flash`. `DEEPSEEK_MODEL` remains configurable for examples and acceptance tests. The memory service does not receive or persist `DEEPSEEK_API_KEY`.

The caller writes the user's event before reading context, calls DeepSeek independently, and writes the assistant response through the same write API after completion.

## 8. Configuration Contract

Safe effective values are documented in OpenAPI, README, `.env.example`, and optionally returned by the read API:

- API host, port, worker count, concurrency limit, body-size limit, and request timeout;
- Redis URL without credentials, pool size, session TTL, history turns, lock TTL, and idempotency TTL;
- journal root and 30-day retention;
- Headroom service URL, request timeout, CCR TTL, refresh window, compression worker count, retry queue capacity, and maximum compression segments;
- token-window, trigger ratio, message-count, and session-duration thresholds;
- DeepSeek public API URL and default model name;
- latency SLO values and policy version.

Secrets are accepted only through environment variables or a secret manager and are never returned:

- DeepSeek API key;
- Redis password;
- memory API authentication key;
- Headroom scope HMAC secret.

The service has only two business endpoints. The following operational endpoints do not read or write business memory:

```text
GET /health
GET /ready
GET /metrics
GET /openapi.json
```

## 9. Concurrency and Deployment

The HTTP layer uses asynchronous FastAPI/Uvicorn. Redis access uses `redis.asyncio` with a default pool size of 200. Headroom requests reuse one bounded `httpx.AsyncClient`. Journal writes run outside the event loop and use per-session locks rather than the current process-wide journal lock.

The memory API defaults to four Uvicorn workers. Cross-process correctness uses Redis transactions or Lua scripts for:

- atomic sequence allocation;
- event-ID idempotency;
- summary generation/version comparison;
- one active compression job per session;
- stale-job rejection.

Background compression uses a bounded persistent queue. The default maximum active compression jobs is eight. Queue saturation never rejects an already durable memory write; the job remains in the Redis retry queue. A successful Headroom call and successful summary validation are required before advancing the compression high-water mark or trimming Redis originals.

Production Headroom deployment uses multiple single-worker replicas behind a load balancer that hashes the anonymized session header. Requests for one session remain on one Headroom replica, avoiding per-process context fragmentation. Headroom's CCR backend remains a Headroom deployment choice. A single-machine acceptance environment may use multiple Headroom workers with a shared SQLite backend, but production favors single-worker replicas with session affinity.

## 10. Performance Requirements

The release gate is:

| Path | Target |
|---|---:|
| 100-concurrent memory writes | p95 <= 150 ms; p99 <= 300 ms |
| Redis warm memory reads | p95 <= 100 ms; p99 <= 200 ms |
| Journal cold recovery | p95 <= 1 second |
| Headroom CCR retrieval | p95 <= 100 ms |
| 20K-token asynchronous compression | p95 <= 5 seconds |
| DeepSeek time to first token | p95 <= 3 seconds, measured separately from memory APIs |

An expired-segment synchronous rebuild is a cold path and must meet the 20K-token compression target; it is excluded from the Redis warm-read percentile. Proactive refresh is the normal path and is required to keep user reads within the warm-read SLO.

API timing output and Prometheus metrics never include message content. They include request count, errors, current concurrency, phase latency, queue depth/wait, compression ratio, CCR hit/miss/expiry, DeepSeek time to first token, and full response latency.

## 11. Error Handling

- **Redis unavailable:** read performs bounded journal fallback. A write that cannot reserve a sequence returns `503` before journaling; if Redis fails only after the journal fsync, the durable journal record is retained and the API returns a retryable `503` until Redis commit repair succeeds.
- **Journal write failure:** write returns an error and does not claim acceptance.
- **Headroom compression unavailable:** originals remain intact and the job enters the persistent retry queue.
- **Headroom Proxy unavailable:** the service reports an explicit degraded/error state. It does not silently direct callers to DeepSeek without CCR.
- **CCR expired:** the service rebuilds a fresh opaque segment from journal originals through Headroom before exposing the segment to the chat caller and records the request as a cold rebuild.
- **Summary generation invalid:** no summary, sequence watermark, or Redis trim is committed.
- **Stale compression job:** generation/version comparison rejects the result.
- **Journal retention cleanup failure:** metrics and logs record a retryable maintenance failure without blocking online requests.
- **DeepSeek error:** the independent caller handles it; user memory written before the call remains durable.

Error logs contain request IDs, stages, exception categories, and timing, but no conversation content, API keys, Redis credentials, or HMAC secrets.

## 12. Test and Acceptance Design

### 12.1 Unit and contract tests

Tests cover:

- all four content types;
- event-ID idempotency and atomic sequence allocation;
- write-ahead recovery after Redis failure;
- Redis and CCR TTL validation;
- 30-day journal cleanup;
- compression locks, generation comparison, retry backpressure, and stale-result rejection;
- API schemas, status codes, authentication, body limits, and secret redaction;
- no-recompression invariant across three generations.

The no-recompression test asserts:

```text
generation 1 input = original sequences 1-100
generation 2 input = original sequences 101-180
generation 3 input = original sequences 181-240
```

Generation 2 and 3 inputs must contain no prior Headroom message or marker.

### 12.2 Real Headroom integration cases

Conversation, code, document, and skill fixtures each contain a unique anchor:

```text
CONVERSATION_ORIGINAL_ANCHOR_7391
CODE_ORIGINAL_ANCHOR_7391
DOCUMENT_ORIGINAL_ANCHOR_7391
SKILL_ORIGINAL_ANCHOR_7391
```

For every fixture, the acceptance test requires token reduction, a CCR marker/hash, retrieval through Headroom's supported API, byte-identical original content, and matching SHA-256. It also verifies that the project does not access Headroom's backend and that an expired segment is rebuilt from journals into a fresh Headroom marker.

### 12.3 DeepSeek end-to-end case

The example writes all four fixture types, waits for asynchronous compression, reads memory, and sends the returned messages through the official SDK to Headroom Proxy with `deepseek-v4-flash`. It asks for facts that exist only in compressed-away original content. Passing requires observable Headroom CCR retrieval, automatic continuation, an answer containing the expected anchors, correct assistant-memory writeback, and successful second/third turns without recompressing older Headroom output.

Real DeepSeek tests are opt-in and require an explicit API key and run flag. Missing credentials must produce `skipped`, never a pass. Default CI uses a deterministic OpenAI-compatible fake upstream to validate tool calls and automatic continuation without cost.

### 12.4 Load tests

The asynchronous load runner executes:

- 100 users writing to 100 sessions;
- 100 users reading warm sessions;
- a 70% read / 30% write mixed workload;
- 100 idempotent concurrent writes to one session;
- online read/write while the compression queue is saturated.

It reports requests, success/error count, throughput, p50/p95/p99/max latency, Redis/journal/queue phase latency, memory delta, and CPU use. The command exits nonzero when an agreed SLO is missed.

## 13. Deliverables

- FastAPI application with the two business endpoints and operational endpoints;
- request/response models and generated OpenAPI schema;
- sequence-aware Redis and journal storage with event idempotency;
- opaque compression generations and the no-recompression invariant;
- bounded persistent compression/retry queue;
- 30-day journal retention job;
- independent official-SDK DeepSeek example through Headroom Proxy;
- conversation, code, document, and skill fixtures;
- unit, integration, opt-in real-service, end-to-end, and load tests;
- deployment/environment examples for Redis, memory API, Headroom, and DeepSeek upstream configuration;
- README storage, retention, concurrency, failure-boundary, and teacher-alignment sections.

## 14. References

- [Headroom CCR documentation](https://headroom-docs.vercel.app/docs/ccr)
- [Headroom Proxy repository](https://github.com/headroomlabs-ai/headroom)
- [DeepSeek official API documentation](https://api-docs.deepseek.com/)
