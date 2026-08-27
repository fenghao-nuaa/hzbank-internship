# Memory API performance gate

This repository includes a repeatable load-test runner, but **no real-service
performance run has been completed or recorded here yet**. Unit-test results and
synthetic latencies are not measurements of Redis, Headroom, the journal
filesystem, or the memory API. Do not treat the SLO values below as achieved
until the commands have been run against the intended deployment and the
environment and generated JSON artifacts have been retained.

## Release SLOs

| Measured path | Gate |
|---|---:|
| Memory write | p95 <= 150 ms and p99 <= 300 ms |
| Redis warm memory read | p95 <= 100 ms and p99 <= 200 ms |

The runner uses inclusive nearest-rank percentiles. Every request error,
including a non-2xx response, fails the gate. `mixed` applies the write and read
limits to their respective operations. `same-session` and `queue-saturated`
apply the write limits. The JSON report contains status counts, latency
statistics, server-returned phase timings, and sanitized error categories; it
never contains response bodies, message content, or the authentication token.

Journal recovery, Headroom compression, CCR retrieval, and DeepSeek time to
first token are different paths and must be measured separately:

- a read in this gate is warmed by writing its session before the timed window;
- journal recovery is a cold read and has a separate p95 <= 1 second target;
- asynchronous 20K-token Headroom compression has a p95 <= 5 second target;
- Headroom CCR retrieval has a p95 <= 100 ms target;
- DeepSeek TTFT has a p95 <= 3 second target and is not part of either memory
  API request.

## Prerequisites

Start an isolated Redis, Headroom, memory API, and compression worker deployment.
Set `MEMORY_API_AUTH_TOKEN` for the client. The runner reads and writes only the
new load-test user/session IDs that it creates; it does not start, stop, or
delete external services.

Use the project environment from the repository root:

```bash
export MEMORY_API_AUTH_TOKEN='<test token>'

PYTHONPATH=src .venv/bin/python scripts/load_test_memory_api.py \
  --base-url http://127.0.0.1:8080 \
  --scenario write \
  --concurrency 100 \
  --requests 1000 \
  --output /private/tmp/memory-load-write.json
```

The default concurrency is 100 and the default request count is 1000. The
process exits `0` only when all measured and setup requests succeed and all
applicable p95/p99 limits pass. It exits nonzero for request errors, an SLO
violation, invalid queue-test configuration, or an incomplete run. JSON is
printed to stdout and, when `--output` is supplied, written to that path.

Run the other normal scenarios by changing `--scenario`:

```bash
for scenario in read mixed same-session; do
  PYTHONPATH=src .venv/bin/python scripts/load_test_memory_api.py \
    --base-url http://127.0.0.1:8080 \
    --scenario "$scenario" \
    --concurrency 100 \
    --requests 1000 \
    --output "/private/tmp/memory-load-${scenario}.json" || exit 1
done
```

`read` and the read half of `mixed` create warm sessions before starting the
timed read window. `same-session` first sends unique event IDs concurrently,
then runs a separate synchronized replay phase with the identical event IDs and
payloads to exercise idempotency.

## Queue-saturation scenario

Run queue saturation only against an isolated test deployment. Configure a
deliberately small `HEADROOM_QUEUE_CAPACITY` and low
`HEADROOM_COMPRESSION_WORKERS` value before starting the API and worker. Choose
a `--payload-bytes` size that crosses the configured compression policy. The
required confirmation flag prevents accidental use as an ordinary production
benchmark.

```bash
export REDIS_URL='redis://127.0.0.1:6379/0'

PYTHONPATH=src .venv/bin/python scripts/load_test_memory_api.py \
  --base-url http://127.0.0.1:8080 \
  --scenario queue-saturated \
  --concurrency 100 \
  --requests 1000 \
  --payload-bytes 65536 \
  --redis-url "$REDIS_URL" \
  --confirm-test-queue-configuration \
  --output /private/tmp/memory-load-queue-saturated.json
```

This scenario reads Redis queue cardinalities immediately before and after the
timed writes and reports `ready`, `inflight`, `retry`, `pending`, `dead`, and
`corrupt` counts. It does not mutate queue keys. A backlog increase is evidence
that the configured queue was pressured; it is not by itself an SLO pass.

## Recording an actual run

For every real-service result, retain the JSON files and record all of the
following beside them. Until every field is filled from an actual run, label the
result `NOT MEASURED`, not pass or fail.

| Field | Value for this repository |
|---|---|
| Status | **NOT MEASURED** |
| Date/time and time zone | Not recorded |
| Host/VM and operating system | Not recorded |
| CPU model and allocated cores | Not recorded |
| RAM | Not recorded |
| Redis version, host, persistence, and network topology | Not recorded |
| Headroom version, replicas/workers, and network topology | Not recorded |
| Memory API version and worker count | Not recorded |
| Compression worker count and queue capacity | Not recorded |
| Journal filesystem/mount | Not recorded |
| Fixture payload size and request count | Not recorded |
| Exact command and environment overrides | Not recorded |
| JSON result paths | Not recorded |
| Write p95/p99 and pass/fail | Not measured |
| Warm-read p95/p99 and pass/fail | Not measured |

Compare runs only when the topology, fixture size, concurrency, request count,
and warm/cold classification are recorded. A local mock, unit test, or skipped
opt-in integration test must never be presented as a real-service result.
