# Headroom external service

short-term-memory integrates Headroom only as a replaceable HTTP service. It does not copy,
package, import, or execute Headroom source code, Kompress models, ONNX Runtime or
Headroom Python dependencies.

## Upstream and deployment identity

- Repository: <https://github.com/headroomlabs-ai/headroom>
- Documentation: <https://headroom-docs.vercel.app/docs/proxy>
- Isolated tool distribution: `headroom-ai[all]==0.33.0`
- Upstream license: Apache License 2.0
- Health endpoint: `GET /health`
- Background compression endpoint: `POST /v1/compress`
- Agent model paths: OpenAI/Anthropic-compatible Headroom Proxy endpoints

For local diagnosis, install Headroom outside the short-term-memory `.venv`:

```bash
uv tool install --python 3.13 "headroom-ai[all]==0.33.0"
```

Start the official service without forcing a compressor or ratio:

```bash
HEADROOM_CCR_TTL_SECONDS=43200 headroom proxy \
  --host 127.0.0.1 \
  --port 8787 \
  --mode token
```

short-term-memory decides only when its three configured thresholds require background compression.
Headroom owns ContentRouter selection, Kompress and other compressors, CCR cache,
markers, `headroom_retrieve`, relevance decisions and supported model continuation.
short-term-memory preserves Headroom messages as opaque protocol objects.

For production and the 100-concurrency target, use the independent HTTP service in
`compose.memory.yml` instead of a developer `uv tool` process. The Compose boundary exposes
Headroom Proxy with an explicit concurrency limit; memory-api and compression-worker communicate
with it only over HTTP.

## CCR storage boundary

Headroom owns the CCR cache, original recovery payload, marker format, expiry, and
`headroom_retrieve`. Its selected backend may vary by Headroom version and deployment. This project
therefore does not claim that the backend is always an in-memory LRU or always SQLite, and never
reads or writes `~/.headroom/ccr_store.db`.

The project stores two separate forms of state for its own responsibilities:

- exact input events in Journal JSONL, plus a TTL-limited online Redis copy;
- Headroom's returned compressed messages as opaque Redis generation envelopes.

Only exact original events selected by monotonic sequence are sent to `/v1/compress`. Existing
generation messages, semantic summaries, and CCR markers are never used as compression source, so
subsequent turns cannot recursively compress a previous compressed result.

Because no Headroom code or model artifact is redistributed, short-term-memory has no vendored
Headroom license/source tree or ML dependency. Another service can replace Headroom
by implementing the same `CompressionClient` HTTP contract and Agent proxy boundary.
